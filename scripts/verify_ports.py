"""
What scripts/verify_statements.py needs to know about the ports (Mynewt, NuttX on
Apptainer, Contiki-NG), beyond the Zephyr and RIOT instances it was written for:
where their instances live, which clone holds the fix and what the fix is.

The hidden tests are each instance's own test_patch.diff, as for Zephyr and RIOT. The
fix is what the port's own pipeline calls the gold patch: the diff of the files in
metadata "files_changed_by_fix" (never a test file) between the base and the fix:

    mynewt      mynewt-core  gold_base_commit (else base_commit) .. fix_commit
    contiki-ng  contiki-ng   base_commit .. fix_commit
    nuttx       the kernel   kernel_base_commit .. kernel_merge_commit, then each follow-up
                PR of "extra_fix_prs" (base_commit .. head_commit, its own files), in merge
                order. The tests are in the apps repository; they are not part of the fix.

The clones are read, never fetched into (a blobless clone fetches the few blobs a diff
needs). Where they are, first match:

    mynewt      EMBEDEVAL_MYNEWT_CLONE, candidates/mynewt.git, <shared>/mynewt-core.git
    contiki-ng  EMBEDEVAL_CONTIKI_NG_CLONE, <shared>/contiki-ng.git
    nuttx       EMBEDEVAL_NUTTX_CLONE, $EMBEDEVAL_NUTTX_DATA/kernel.git
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTANCES = REPO / "apptainer" / "instances"
PORTS = ("mynewt", "nuttx", "contiki-ng")
# GitHub lists at most this many files of a PR: a longer list is cut, so the whole diff
# is taken (scripts/validate_instance.py does the same).
FILES_CAP = 3000


def instance_roots() -> dict[str, Path]:
    return {p: INSTANCES / p for p in PORTS}


def clone_for(project: str, shared: Path) -> Path:
    """The clone holding the fix's commits, or a RuntimeError saying what to set."""
    env = {"mynewt": "EMBEDEVAL_MYNEWT_CLONE", "contiki-ng": "EMBEDEVAL_CONTIKI_NG_CLONE",
           "nuttx": "EMBEDEVAL_NUTTX_CLONE"}[project]
    data = os.environ.get("EMBEDEVAL_NUTTX_DATA")
    candidates = {"mynewt": [os.environ.get(env), REPO / "candidates" / "mynewt.git",
                             shared / "mynewt-core.git"],
                  "contiki-ng": [os.environ.get(env), shared / "contiki-ng.git"],
                  "nuttx": [os.environ.get(env), Path(data) / "kernel.git" if data else None]}[project]
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    raise RuntimeError(f"no {project} clone: set {env}"
                       + (" or EMBEDEVAL_NUTTX_DATA" if project == "nuttx" else "")
                       + f" (looked in: {', '.join(str(c) for c in candidates if c)})")


def _git(clone: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "--literal-pathspecs", "-C", str(clone), *args],
                          capture_output=True, text=True, errors="replace")


def _diff(clone: Path, base: str, fix: str, files: list[str]) -> str:
    for sha in (base, fix):
        if _git(clone, "cat-file", "-e", f"{sha}^{{commit}}").returncode:
            raise RuntimeError(f"{sha[:12]} is not in {clone} (git -C {clone} fetch origin {sha})")
    r = _git(clone, "diff", base, fix, "--", *(files if 0 < len(files) < FILES_CAP else ["."]))
    if r.returncode:
        raise RuntimeError(f"git diff {base[:12]}: {r.stderr[:300]}")
    return r.stdout


def fix_diff(meta: dict, shared: Path) -> str:
    """The fix of a port instance as one diff, without tests."""
    project = meta["project"]
    clone = clone_for(project, shared)
    if project == "mynewt":
        return _diff(clone, meta.get("gold_base_commit", meta["base_commit"]), meta["fix_commit"],
                     meta.get("files_changed_by_fix", []))
    if project == "contiki-ng":
        return _diff(clone, meta["base_commit"], meta["fix_commit"], meta.get("files_changed_by_fix", []))
    patches = [(meta.get("kernel_merged_at", ""), "",
                _diff(clone, meta["kernel_base_commit"], meta["kernel_merge_commit"],
                      meta.get("files_changed_by_fix", [])))]
    for e in meta.get("extra_fix_prs") or []:
        patches.append((e.get("merged_at", ""), f"# follow-up PR #{e['kernel_pr']}\n",
                        _diff(clone, e["base_commit"], e["head_commit"], e["files_changed"])))
    if meta.get("kernel_merged_at"):        # merge order, as validate_instance.nuttx_fix_patches
        patches.sort(key=lambda p: p[0])
    return "".join(head + body for _, head, body in patches)
