"""What an agent could find in a NuttX instance image that it must not: the
upstream kernel fix, or anything of either repository from after its base.

validate_instance.py runs this on the unpacked image (a sandbox directory)
before any test runs and records the result in validation.json
("leak_check"); an instance with a finding is invalid, and
promote_instance.py refuses an instance without a clean record. Checked from
the host, on the image's files:

    history   every commit object in /testbed/.git (the kernel) and
              /testbed/apps/.git (the apps) is that repository's base commit
              or one the recipe made (author EmbedEval); each base is a
              shallow boundary
    refs      no remote, tag, branch, FETCH_HEAD, ORIG_HEAD or reflog
    fix       neither a fix commit (the kernel PR's merge commit, the head of
              each follow-up PR the fix applies with) nor the apps PR's merge
              commit is in the object store, nor a blob of a fixed file as
              fixed, unless that blob is already in the starting tree
    lines     no distinctive line the fix adds (absent from the starting tree
              and from no line the test patch adds) appears in any file of
              the image
    copies    no git repository other than /testbed and /testbed/apps; the
              empty mount points of build_instances.py's local clones under
              /embedeval-src are allowed
    statement the problem statement names no identifier that only the fix
              introduces (one in neither the starting tree nor the test patch);
              recorded apart ("statement_ok"), as it is fixed by rewriting
              the statement (scripts/nuttx_statement.py), not the image:
              validation goes on, promotion refuses it

The network is checked inside the container by validate_instance.py, under
the same isolation as every other container command (apptainer_util.exec_args).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

RECIPE_AUTHOR = "EmbedEval"
MAX_LINES = 20                 # distinctive fix lines searched for
MIN_LINE = 24                  # characters, stripped
SKIP_DIRS = ("proc", "sys", "dev", "run")
REPOS = {"kernel": "testbed", "apps": "testbed/apps"}
MOUNT_POINTS = ("/embedeval-src/nuttx.git", "/embedeval-src/nuttx-apps.git")


def _git(repo: Path, *args: str, stdin: str | None = None, bare: bool = False) -> subprocess.CompletedProcess:
    where = ["--git-dir", str(repo)] if bare else ["-C", str(repo)]
    return subprocess.run(["git", *where, *args], input=stdin, capture_output=True, text=True,
                          timeout=900)


def added_lines(patch: str) -> list[str]:
    """The lines a unified diff adds, stripped, that say something on their
    own: long enough, not comments or preprocessor lines, not punctuation."""
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


def patch_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/\S+ b/(\S+)$", patch, re.M)


def fix_facts(clone: Path, meta: dict, fixes: list[tuple[str, str, str]], test_patch: str,
              base_patch: str = "") -> dict:
    """What the fix is, from the host kernel clone. `fixes`: (label, commit
    the patch ends at, patch) for the primary fix and each follow-up; the
    blobs of the files each patch changes, as of its commit, are the fixed
    blobs. base_patch: what the recipe commits onto the base (prerequisite
    kernel PRs), part of the starting tree."""
    base = meta["kernel_base_commit"]
    base_blobs = set(_git(clone, "ls-tree", "-r", "--object-only", base).stdout.split())
    blobs: dict[str, str] = {}
    for _, commit, patch in fixes:
        files = patch_files(patch)
        if not files:
            continue
        for row in _git(clone, "ls-tree", commit, "--", *files).stdout.splitlines():
            mode_type_sha, path = row.split("\t", 1)
            kind, sha = mode_type_sha.split()[1:3]
            if kind == "blob" and sha not in base_blobs:
                blobs[sha] = path
    fix_text = "\n".join(p for _, _, p in fixes)
    # A fix line inside a line the tests add is the tests' (shown by design);
    # one the prerequisites add is part of the starting tree.
    shown = [l[1:] for patch in (test_patch, base_patch) for l in patch.splitlines() if l.startswith("+")]
    candidates = [l for l in added_lines(fix_text) if not any(l in t for t in shown)]
    present = set()
    if candidates:          # already in the base tree as a whole: no clue
        hay = _git(clone, "grep", "-h", "-F", "-f", "/dev/stdin", base,
                   stdin="\n".join(candidates) + "\n").stdout
        present = {c for c in candidates if c in hay}
    lines = sorted((c for c in candidates if c not in present), key=len, reverse=True)[:MAX_LINES]
    said = identifiers(meta.get("problem_statement") or "")
    added = identifiers("\n".join(l[1:] for l in fix_text.splitlines()
                                  if l.startswith("+") and not l.startswith("+++")))
    named = sorted(i for i in (said & added) - identifiers(test_patch) - identifiers(base_patch)
                   if _git(clone, "grep", "-q", "-w", "-F", i, base).returncode)
    return {"base_commits": {"kernel": base, "apps": meta["apps_base_commit"]},
            "fix_commits": {"kernel": sorted({c for _, c, _ in fixes}),
                            "apps": [meta["apps_merge_commit"]] if meta.get("apps_merge_commit") else []},
            "fix_blobs": blobs, "fix_lines": lines, "statement_fix_identifiers": named}


def _refs(gitdir: Path) -> list[str]:
    """What a repository keeps besides its commits: refs, remotes, reflogs."""
    out = []
    refs = _git(gitdir, "for-each-ref", "--format=%(refname)", bare=True).stdout.split()
    out += [f"ref {r}" for r in refs]
    remotes = _git(gitdir, "config", "--get-regexp", r"^remote\.", bare=True).stdout.split("\n")
    out += [f"remote config {r.split()[0]}" for r in remotes if r.strip()]
    for name in ("FETCH_HEAD", "ORIG_HEAD", "packed-refs"):
        f = gitdir / name
        # gc leaves packed-refs with only its "# pack-refs with:" header
        if f.exists() and any(l.strip() and not l.startswith("#") for l in f.read_text().splitlines()):
            out.append(name)
    logs = gitdir / "logs"
    if logs.is_dir() and any(p.is_file() and p.stat().st_size for p in logs.rglob("*")):
        out.append("reflog")
    return out


def scan(root: Path, facts: dict) -> dict:
    """The findings in the unpacked image at `root` (a sandbox directory)."""
    root = Path(root)
    findings: list[str] = []
    record: dict = {"commits": {}}

    for repo, rel in REPOS.items():
        gitdir = root / rel / ".git"
        base = facts["base_commits"][repo]
        if not gitdir.is_dir():
            findings.append(f"history: no /{rel}/.git")
            continue
        objects = _git(gitdir, "cat-file", "--batch-all-objects",
                       "--batch-check=%(objectname) %(objecttype)", bare=True).stdout.split("\n")
        commits = [row.split()[0] for row in objects if row.endswith(" commit")]
        foreign = [sha for sha in commits if sha != base and
                   _git(gitdir, "log", "-1", "--format=%an", sha, bare=True).stdout.strip() != RECIPE_AUTHOR]
        shallow = (gitdir / "shallow").read_text().split() if (gitdir / "shallow").exists() else []
        record["commits"][repo] = len(commits)
        if base not in commits:
            findings.append(f"history: the {repo} base commit is not in /{rel}")
        if foreign:
            findings.append(f"history: {repo} commits other than the base and the recipe's: {foreign[:5]}")
        if base not in shallow:
            findings.append(f"history: the {repo} base commit is not a shallow boundary (its history is present)")
        kept = _refs(gitdir)
        if kept:
            findings.append(f"refs: /{rel} keeps {kept[:5]}")
        for sha in facts["fix_commits"][repo]:
            if _git(gitdir, "cat-file", "-e", sha, bare=True).returncode == 0:
                findings.append(f"fix: the {repo} commit {sha[:12]} is in /{rel}")
        if repo == "kernel" and facts["fix_blobs"]:
            rows = _git(gitdir, "cat-file", "--batch-check", bare=True,
                        stdin="\n".join(facts["fix_blobs"]) + "\n").stdout.splitlines()
            have = {row.split()[0] for row in rows if not row.endswith(" missing")}
            tree = set(_git(gitdir, "ls-tree", "-r", "--object-only", "HEAD", bare=True).stdout.split())
            leaked = sorted(facts["fix_blobs"][s] for s in have if s not in tree)
            if leaked:
                findings.append(f"fix: fixed versions of {leaked[:5]} are in /{rel}'s object store")
    record["fix_blobs_checked"] = len(facts["fix_blobs"])

    hits = []
    if facts["fix_lines"]:
        cmd = ["grep", "-rIlF", "-f", "/dev/stdin", str(root)] + [f"--exclude-dir={d}" for d in SKIP_DIRS]
        r = subprocess.run(cmd, input="\n".join(facts["fix_lines"]) + "\n", capture_output=True,
                           text=True, timeout=3600)
        hits = sorted("/" + str(Path(p).relative_to(root)) for p in r.stdout.splitlines())
    record["fix_lines_checked"] = len(facts["fix_lines"])
    if hits:
        findings.append(f"lines: files hold lines the fix adds: {hits[:5]}")

    prune = [x for d in SKIP_DIRS for x in ("-path", str(root / d), "-o")][:-1]
    r = subprocess.run(["find", str(root), "(", *prune, ")", "-prune", "-o",
                        "(", "-name", ".git", "-o", "-name", "*.git", ")", "-print", "-prune"],
                       capture_output=True, text=True, timeout=900)
    found = {"/" + str(Path(p).relative_to(root)) for p in r.stdout.splitlines() if p}
    allowed = {f"/{rel}/.git" for rel in REPOS.values()}
    other = sorted(p for p in found - allowed
                   if not (p in MOUNT_POINTS and (root / p[1:]).is_dir() and not any((root / p[1:]).iterdir())))
    record["repositories"] = sorted(found)
    if other:
        findings.append(f"copies: other repositories: {other[:5]}")

    named = facts.get("statement_fix_identifiers") or []
    return {"ok": not findings, "findings": findings, **record,
            "statement_ok": not named, "statement_fix_identifiers": named}
