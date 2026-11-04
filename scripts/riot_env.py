"""
Facts about RIOT at one commit, from the local clone: a PR's commits, which
native board the commit has, and which build image of RIOT's its era needs.

The clone is <shared>/riot/riot.git (cluster/setup_riot.py). RIOT merges with
merge commits, so a PR is base..head of its merge commit:

    head = the merge's second parent
    base = the merge base of its parents

Not merge^1..merge: on a merge whose tree already had the change, that diff is
empty (PRs 20347, 20830).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import apptainer_util as au

URL = "https://github.com/RIOT-OS/RIOT.git"


def in_shard(pr: int, shard: str | None) -> bool:
    """--shard I/N: this node's share of the PRs, of N nodes. A rerun with another N
    is safe: PRs that have a record are skipped wherever they were done."""
    if not shard:
        return True
    i, n = shard.split("/")
    return pr % int(n) == int(i)


def clone() -> Path:
    return au.shared_path("riot", "riot.git")


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(clone()), *args], capture_output=True, text=True,
                       timeout=600)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args[:3])}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def has_commit(sha: str) -> bool:
    return bool(git("cat-file", "-t", sha, check=False))


def ensure_commits(*shas: str) -> None:
    missing = [s for s in shas if s and not has_commit(s)]
    if missing:
        git("fetch", "-q", "origin", *missing)


def pr_commits(merge: str) -> tuple[str, str]:
    """(base, head) of a merged PR. A merge with one parent is its own head."""
    parents = git("rev-parse", f"{merge}^@").split()
    if len(parents) == 1:
        return parents[0], merge
    return git("merge-base", parents[0], parents[1]), parents[1]


def has_path(commit: str, path: str) -> bool:
    return bool(git("ls-tree", commit, "--", path, check=False))


def show(commit: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(clone()), "show", f"{commit}:{path}"],
                       capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else None


def board(commit: str) -> str:
    """native64 from when it exists (2024-02); before, native (32-bit)."""
    return "native64" if has_path(commit, "boards/native64") else "native"


def image_tags() -> list[str]:
    """The releases of RIOT's build image we have, oldest first."""
    return sorted(p.name[len("riotbuild-"):-len(".sif")]
                  for p in au.shared_path("images", "x").parent.glob("riotbuild-*.sif"))


def image_tag(commit: str) -> str:
    """The newest release not newer than the commit, else the oldest we have:
    code does not build with a much newer compiler (RIOT uses -Werror)."""
    tags = image_tags()
    if not tags:
        raise RuntimeError("no riotbuild images; run cluster/setup_riot.py")
    month = git("log", "-1", "--format=%cd", "--date=format:%Y.%m", commit)
    return next((t for t in reversed(tags) if t <= month), tags[0])
