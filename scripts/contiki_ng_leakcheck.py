"""What an agent could find in a Contiki-NG instance image that it must not:
the upstream fix, or anything from after the base commit.

contiki_ng_pipeline.validate runs this on the unpacked image before any test
runs and records the result in validation.json ("leak_check"); an instance
with a finding is invalid, and promote_instance.py refuses one without a
clean record. Checked from the host, on the image's files:

    history   every commit object in /testbed/.git is the base commit or one
              the recipe made (author EmbedEval); the base is a shallow boundary
    refs      /testbed and each submodule repository has no remote, branch,
              tag, reflog, FETCH_HEAD or ORIG_HEAD: only a detached HEAD
    fix       neither the fix commit nor a blob of a fixed file (as fixed) is
              in /testbed's object store, unless that blob is in the starting
              tree
    lines     no distinctive line the fix adds (absent from the base tree, and
              not part of a line the tests add) appears in any file of the
              image; a match inside a pinned submodule is recorded, not a finding
    copies    no git repository other than /testbed and its submodules, and no
              other Contiki source tree (os/contiki.h outside /testbed)
    statement the problem statement names no identifier only the fix
              introduces (in neither the base tree nor the test patch)

The network is probed inside the container, under the isolation every run
uses (apptainer_util.exec_args(network=False)), by contiki_ng_pipeline.
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
                          errors="replace", timeout=600)


def added_lines(patch: str) -> list[str]:
    """The lines a unified diff adds, stripped, that say something on their
    own: long enough, not comments, preprocessor lines or punctuation."""
    out = []
    for line in patch.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        text = line[1:].strip()
        if (len(text) < MIN_LINE or text.startswith(("*", "/*", "//", "#", "- "))
                or not re.search(r"[A-Za-z_]\w{3,}", text)):
            continue
        out.append(text)
    return list(dict.fromkeys(out))


def identifiers(text: str) -> set[str]:
    """C-like names: with an underscore or camel case, four characters or more
    (not plain English words)."""
    return {t for t in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text)
            if len(t) > 3 and ("_" in t or re.search(r"[a-z][A-Z]", t))}


def fix_facts(clone: Path, meta: dict, fix_patch: str, test_patch: str) -> dict:
    """What the fix is, from the host clone: its commit, the blobs of the
    files it changes as fixed, its distinctive added lines, and the
    identifiers of the problem statement only the fix introduces."""
    base, fix = meta["base_commit"], meta["fix_commit"]
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", fix_patch, re.M)
    blobs = {}
    if files:
        for row in _git(clone, "ls-tree", fix, "--", *files, bare=True).stdout.splitlines():
            mode_type_sha, path = row.split("\t", 1)
            kind, sha = mode_type_sha.split()[1:3]
            if kind == "blob":
                blobs[sha] = path
    base_blobs = set(_git(clone, "ls-tree", "-r", "--object-only", base, bare=True).stdout.split())
    blobs = {sha: p for sha, p in blobs.items() if sha not in base_blobs}
    # The scan matches a line anywhere in a file, so a fix line that is part
    # of a line the tests add is the tests' (shown by design), not a clue.
    tests = [l[1:] for l in test_patch.splitlines() if l.startswith("+") and not l.startswith("+++")]
    candidates = [l for l in added_lines(fix_patch) if not any(l in t for t in tests)]
    present = set()
    if candidates:
        hay = _git(clone, "grep", "-h", "-F", "-f", "/dev/stdin", base, "--",
                   stdin="\n".join(candidates) + "\n", bare=True).stdout
        present = {c for c in candidates if c in hay}
    lines = sorted((c for c in candidates if c not in present), key=len, reverse=True)[:MAX_LINES]
    named = statement_fix_identifiers(clone, base, meta.get("problem_statement") or "", fix_patch, test_patch)
    return {"fix_commit": fix, "base_commit": base, "fix_blobs": blobs, "fix_lines": lines,
            "statement_fix_identifiers": named}


def statement_fix_identifiers(clone: Path, base: str, statement: str, fix_patch: str, test_patch: str) -> list[str]:
    """Identifiers the statement names that only the fix introduces: in the
    fix's added lines, not in the test patch, absent from the base tree."""
    said = identifiers(statement)
    added = identifiers("\n".join(l[1:] for l in fix_patch.splitlines()
                                  if l.startswith("+") and not l.startswith("+++")))
    return sorted(i for i in (said & added) - identifiers(test_patch)
                  if _git(clone, "grep", "-q", "-w", "-F", "-e", i, base, "--", bare=True).returncode)


def _repositories(root: Path) -> tuple[list[str], list[str]]:
    """(git directories, .git pointer files) in the image, as image paths."""
    prune = [x for d in SKIP_DIRS for x in ("-path", str(root / d), "-o")][:-1]
    r = subprocess.run(["find", str(root), "(", *prune, ")", "-prune", "-o",
                        "(", "-name", ".git", "-o", "-name", "*.git", ")", "-print", "-prune"],
                       capture_output=True, text=True, errors="replace", timeout=900)
    dirs, files = [], []
    for p in r.stdout.splitlines():
        if p:
            (dirs if Path(p).is_dir() else files).append("/" + str(Path(p).relative_to(root)))
    return sorted(dirs), sorted(files)


def _module_dirs(gitdir: Path) -> list[Path]:
    """The submodule repositories under /testbed/.git/modules, nested ones too."""
    return sorted(p.parent for p in (gitdir / "modules").rglob("HEAD")
                  if p.is_file() and (p.parent / "objects").is_dir()) if (gitdir / "modules").is_dir() else []


def _ref_findings(gitdir: Path, label: str) -> list[str]:
    out = []
    refs = _git(gitdir, "for-each-ref", "--format=%(refname)", bare=True).stdout.split()
    if refs:
        out.append(f"refs: {label} has refs {refs[:5]}")
    remotes = _git(gitdir, "remote", bare=True).stdout.split()
    if remotes:
        out.append(f"refs: {label} has remotes {remotes}")
    for name in ("FETCH_HEAD", "ORIG_HEAD"):        # packed refs are for-each-ref's
        if (gitdir / name).exists():
            out.append(f"refs: {label} has {name}")
    if (gitdir / "logs").exists() and any(p.is_file() for p in (gitdir / "logs").rglob("*")):
        out.append(f"refs: {label} has a reflog")
    return out


def scan(root: Path, facts: dict) -> dict:
    """The findings in the unpacked image at `root` (a sandbox directory)."""
    root = Path(root)
    gitdir = root / "testbed" / ".git"
    findings: list[str] = []
    record: dict = {}

    # history: every commit object of /testbed itself
    objects = _git(gitdir, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)",
                   bare=True).stdout.split("\n")
    commits = [row.split()[0] for row in objects if row.endswith(" commit")]
    foreign = [sha for sha in commits if sha != facts["base_commit"]
               and _git(gitdir, "log", "-1", "--format=%an", sha, bare=True).stdout.strip() != RECIPE_AUTHOR]
    shallow = (gitdir / "shallow").read_text().split() if (gitdir / "shallow").exists() else []
    record["commits"] = len(commits)
    if facts["base_commit"] not in commits:
        findings.append("history: the base commit is not in /testbed")
    if foreign:
        findings.append(f"history: commits other than the base and the recipe's: {foreign[:5]}")
    if facts["base_commit"] not in shallow:
        findings.append("history: the base commit is not a shallow boundary (its history is present)")
    head = _git(gitdir, "rev-parse", "HEAD", bare=True).stdout.strip()
    if head != facts["base_commit"]:
        findings.append(f"history: HEAD is {head[:12] or 'unset'}, not the base commit")

    # refs: /testbed and each submodule
    modules = _module_dirs(gitdir)
    findings += _ref_findings(gitdir, "/testbed")
    for m in modules:
        findings += _ref_findings(m, "/" + str(m.relative_to(root)))
    record["submodule_repositories"] = ["/" + str(m.relative_to(root)) for m in modules]

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

    # lines: anywhere in the image's files; a submodule's work tree is where
    # its .git pointer file is
    dirs, pointers = _repositories(root)
    submodule_paths = [str(Path(f).parent) + "/" for f in pointers if f.startswith("/testbed/")]
    hits_dep, hits = [], []
    if facts["fix_lines"]:
        cmd = ["grep", "-rIlF", "-f", "/dev/stdin", str(root)] + [f"--exclude-dir={d}" for d in SKIP_DIRS]
        r = subprocess.run(cmd, input="\n".join(facts["fix_lines"]) + "\n", capture_output=True,
                           text=True, errors="replace", timeout=3600)
        for path in r.stdout.splitlines():
            rel = "/" + str(Path(path).relative_to(root))
            (hits_dep if any(rel.startswith(s) for s in submodule_paths)
             or rel.startswith("/testbed/.git/modules/") else hits).append(rel)
    record["fix_lines_checked"] = len(facts["fix_lines"])
    if hits:
        findings.append(f"lines: files hold lines the fix adds: {sorted(hits)[:5]}")
    if hits_dep:
        record["fix_lines_in_pinned_submodules"] = sorted(hits_dep)[:20]

    # copies: other repositories and Contiki source trees
    record["repositories"] = dirs
    other = [d for d in dirs if d != "/testbed/.git" and not d.startswith("/testbed/.git/modules/")]
    other += [f for f in pointers if not f.startswith("/testbed/")]
    trees = subprocess.run(["find", str(root), "(", *[x for d in (*SKIP_DIRS, "testbed")
                                                     for x in ("-path", str(root / d), "-o")][:-1], ")",
                            "-prune", "-o", "-path", "*/os/contiki.h", "-print"],
                           capture_output=True, text=True, errors="replace", timeout=900).stdout.split()
    other += ["/" + str(Path(t).relative_to(root).parent.parent) + " (Contiki tree)" for t in trees]
    if other:
        findings.append(f"copies: other repositories or Contiki trees: {sorted(other)[:5]}")

    if facts.get("statement_fix_identifiers"):
        findings.append(f"statement: names what only the fix introduces: {facts['statement_fix_identifiers']}")

    return {"ok": not findings, "findings": findings, **record}
