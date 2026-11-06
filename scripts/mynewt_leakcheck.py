"""What an agent could find in a Mynewt instance image that it must not: the
upstream fix, or anything from after the base commit.

validate_instance.py runs this on the unpacked image (its sandbox directory)
before any test runs, records the result in validation.json ("leak_check"),
and an instance with a finding is invalid; promote_instance.py refuses an
instance without a clean record. Checked from the host, on the image's files:

    history   every commit object in /testbed/.git is the base commit or one the
              recipe made (author EmbedEval); the base is a shallow boundary
    fix       neither the fix commit nor a blob of a fixed file (as fixed) is
              in the object store, unless that blob is already in the starting
              snapshot's tree
    lines     no distinctive line the fix adds (absent from the base tree and
              from the test patch) appears in any file of the image; a match in
              a pinned dependency under /project/repos is recorded, not a finding
    copies    no git repository other than /testbed and the pinned dependencies
              under /project/repos, and no newt source tree (it carries a later
              mynewt-core snapshot)

    statement the problem statement names no identifier that only the fix
              introduces (one in neither the base tree nor the test patch)

The network is checked inside the container by validate_instance.py (no route
out under the agent's isolation: scripts/mynewt_network.py).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

RECIPE_AUTHOR = "EmbedEval"
MAX_LINES = 20                 # distinctive fix lines searched for
MIN_LINE = 24                  # characters, stripped
SKIP_DIRS = ("proc", "sys", "dev", "run")


def _git(repo: Path, *args: str, stdin: str | None = None, bare: bool = False) -> subprocess.CompletedProcess:
    where = ["--git-dir", str(repo)] if bare else ["-C", str(repo)]
    return subprocess.run(["git", *where, *args], input=stdin, capture_output=True, text=True,
                          timeout=600)


def added_lines(patch: str) -> list[str]:
    """The lines a unified diff adds, stripped, that say something on their
    own: long enough, not comments, not punctuation."""
    out = []
    for line in patch.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        text = line[1:].strip()
        if (len(text) < MIN_LINE or text.startswith(("*", "/*", "//", "#include", "- ", "#"))
                or not re.search(r"[A-Za-z_]\w{3,}", text)):
            continue
        out.append(text)
    return list(dict.fromkeys(out))


def fix_facts(clone: Path, meta: dict, fix_patch: str, test_patch: str) -> dict:
    """What the fix is, from the host clone: its commit, the blobs of the
    files it changes as fixed, and its distinctive added lines."""
    base, fix = meta.get("gold_base_commit", meta["base_commit"]), meta["fix_commit"]
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", fix_patch, re.M)
    blobs = {}
    if files:
        r = _git(clone, "ls-tree", fix, "--", *files)
        for row in r.stdout.splitlines():
            mode_type_sha, path = row.split("\t", 1)
            kind, sha = mode_type_sha.split()[1:3]
            if kind == "blob":
                blobs[sha] = path
    base_blobs = set(_git(clone, "ls-tree", "-r", "--object-only", meta["base_commit"]).stdout.split())
    blobs = {sha: p for sha, p in blobs.items() if sha not in base_blobs}
    tests = {l[1:].strip() for l in test_patch.splitlines() if l.startswith("+")}
    candidates = [l for l in added_lines(fix_patch) if l not in tests]
    # absent from the base tree as a whole (a line that is already there is no clue)
    present = set()
    if candidates:
        r = _git(clone, "grep", "-h", "-F", "-f", "/dev/stdin", meta["base_commit"], stdin="\n".join(candidates) + "\n")
        hay = r.stdout
        present = {c for c in candidates if c in hay}
    lines = sorted((c for c in candidates if c not in present), key=len, reverse=True)[:MAX_LINES]
    # identifiers of the statement that only the fix has
    said = identifiers(meta.get("problem_statement") or "")
    added = identifiers("\n".join(l[1:] for l in fix_patch.splitlines()
                                  if l.startswith("+") and not l.startswith("+++")))
    named = sorted(i for i in (said & added) - identifiers(test_patch)
                   if _git(clone, "grep", "-q", "-w", "-F", i, meta["base_commit"]).returncode)
    return {"fix_commit": fix, "base_commit": meta["base_commit"], "fix_blobs": blobs, "fix_lines": lines,
            "statement_fix_identifiers": named}


def identifiers(text: str) -> set[str]:
    """C-like names: with an underscore or camel case, four characters or more
    (not plain English words)."""
    return {t for t in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text)
            if len(t) > 3 and ("_" in t or re.search(r"[a-z][A-Z]", t))}


def scan(root: Path, facts: dict) -> dict:
    """The findings in the unpacked image at `root` (a sandbox directory)."""
    root = Path(root)
    gitdir = root / "testbed" / ".git"
    findings: list[str] = []
    record: dict = {}

    # history: every commit object
    objects = _git(gitdir, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)",
                   bare=True).stdout.split("\n")
    commits = [row.split()[0] for row in objects if row.endswith(" commit")]
    foreign = []
    for sha in commits:
        if sha == facts["base_commit"]:
            continue
        author = _git(gitdir, "log", "-1", "--format=%an", sha, bare=True).stdout.strip()
        if author != RECIPE_AUTHOR:
            foreign.append(sha)
    shallow = (gitdir / "shallow").read_text().split() if (gitdir / "shallow").exists() else []
    record["commits"] = len(commits)
    if facts["base_commit"] not in commits:
        findings.append("history: the base commit is not in /testbed")
    if foreign:
        findings.append(f"history: commits other than the base and the recipe's: {foreign[:5]}")
    if facts["base_commit"] not in shallow:
        findings.append("history: the base commit is not a shallow boundary (its history is present)")

    # fix: commit and fixed blobs
    if _git(gitdir, "cat-file", "-e", facts["fix_commit"], bare=True).returncode == 0:
        findings.append(f"fix: the fix commit {facts['fix_commit'][:12]} is in /testbed")
    if facts["fix_blobs"]:
        rows = _git(gitdir, "cat-file", "--batch-check", bare=True,
                    stdin="\n".join(facts["fix_blobs"]) + "\n").stdout.splitlines()
        have = {row.split()[0] for row in rows if not row.endswith(" missing")}
        tree = set(_git(gitdir, "ls-tree", "-r", "--object-only", "HEAD", bare=True).stdout.split())
        leaked = sorted(facts["fix_blobs"][s] for s in have if s not in tree)
        if leaked:
            findings.append(f"fix: fixed versions of {leaked[:5]} are in /testbed's object store")
    record["fix_blobs_checked"] = len(facts["fix_blobs"])

    # lines: anywhere in the image's files
    hits_dep, hits = [], []
    if facts["fix_lines"]:
        cmd = ["grep", "-rIlF", "-f", "/dev/stdin", str(root)] + [f"--exclude-dir={d}" for d in SKIP_DIRS]
        r = subprocess.run(cmd, input="\n".join(facts["fix_lines"]) + "\n", capture_output=True,
                           text=True, timeout=1800)
        for path in r.stdout.splitlines():
            rel = "/" + str(Path(path).relative_to(root))
            (hits_dep if rel.startswith("/project/repos/") else hits).append(rel)
    record["fix_lines_checked"] = len(facts["fix_lines"])
    if hits:
        findings.append(f"lines: files hold lines the fix adds: {sorted(hits)[:5]}")
    if hits_dep:
        record["fix_lines_in_pinned_dependencies"] = sorted(hits_dep)[:20]

    # copies: other repositories, newt sources (find does not follow the
    # /project/repos/apache-mynewt-core -> /testbed link)
    prune = [x for d in SKIP_DIRS for x in ("-path", str(root / d), "-o")][:-1]
    r = subprocess.run(["find", str(root), "(", *prune, ")", "-prune", "-o",
                        "(", "-name", ".git", "-o", "-name", "*.git", "-o", "-name", "newt-src*", ")",
                        "-print", "-prune"], capture_output=True, text=True, timeout=600)
    found = {"/" + str(Path(p).relative_to(root)) for p in r.stdout.splitlines() if p}
    allowed = re.compile(r"^/(testbed|project/repos/[^/]+)/\.git$")
    other = sorted(p for p in found if not allowed.match(p))
    src = root / "embedeval-src" / "mynewt-core.git"
    if "/embedeval-src/mynewt-core.git" in other and src.is_dir() and not any(src.iterdir()):
        other.remove("/embedeval-src/mynewt-core.git")      # the empty mount point of the build's clone
    record["repositories"] = sorted(found)
    if other:
        findings.append(f"copies: other repositories or newt sources: {other[:5]}")

    if facts.get("statement_fix_identifiers"):
        findings.append(f"statement: names what only the fix introduces: {facts['statement_fix_identifiers']}")

    return {"ok": not findings, "findings": findings, **record}
