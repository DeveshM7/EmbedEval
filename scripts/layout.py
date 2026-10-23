"""
Where the pipeline reads and writes: the repository, or a run folder.

Every script takes `--run-dir DIR` (or EMBEDEVAL_RUN_DIR=DIR). Without it,
everything is where it always was -- candidates/, generated/, triage/runs/,
outputs/ in the repository. With it, everything a run produces goes into that
one folder, so one code checkout can serve many nodes at once and the
repository is only written when an instance is promoted:

    DIR/candidates/         fetched PRs (cache/) and the hard filter's candidates.jsonl
    DIR/enriched/           enriched records
    DIR/dryrun/             dry runs
    DIR/triage/             verdicts and traces, one file per PR
    DIR/instances/          generated recipes and their validation.json
    DIR/outputs/            agent runs and grading results
    DIR/lookup/             CI images and SDK releases found during the run
                            (the repository's tables are read as well)
    DIR/work/               instance images, logs, overlays, temp files

Shared between runs, set by environment variable:

    EMBEDEVAL_SHARED        Python and SDK images, dry-run venvs, the uv cache
                            (scripts/apptainer_util.py)
    EMBEDEVAL_ZEPHYR_CLONE  the full Zephyr clone; default candidates/zephyr.git
    EMBEDEVAL_NUTTX_CLONE, EMBEDEVAL_NUTTX_APPS_CLONE
                            full NuttX kernel and apps clones, if any: NuttX
                            Apptainer images check out from them instead of
                            GitHub, and the validator diffs the fix there

Imported before any module computes a path, so it reads `--run-dir` from the
command line itself and removes it, before argparse sees the arguments.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _take_run_dir_from_argv() -> None:
    argv = sys.argv
    for i, a in enumerate(argv[1:], 1):
        if a == "--run-dir" and i + 1 < len(argv):
            value = argv[i + 1]
            del argv[i:i + 2]
        elif a.startswith("--run-dir="):
            value = a.split("=", 1)[1]
            del argv[i]
        else:
            continue
        os.environ["EMBEDEVAL_RUN_DIR"] = str(Path(value).expanduser().resolve())
        return


_take_run_dir_from_argv()


def run_dir() -> Path | None:
    value = os.environ.get("EMBEDEVAL_RUN_DIR")
    return Path(value) if value else None


def _here(run_part: str, *repo_parts: str) -> Path:
    r = run_dir()
    return r / run_part if r else REPO_ROOT.joinpath(*repo_parts)


def base() -> Path:
    """Where relative output paths given on the command line are resolved."""
    return run_dir() or REPO_ROOT


def candidates() -> Path:
    return _here("candidates", "candidates")


def enriched() -> Path:
    return _here("enriched", "candidates", "enriched")


def dryrun() -> Path:
    return _here("dryrun", "candidates", "dryrun")


def triage_runs() -> Path:
    """Run mode: the verdicts themselves, one file per PR. Otherwise the folder
    of timestamped triage runs."""
    return _here("triage", "triage", "runs")


def generated() -> Path:
    return _here("instances", "generated")


def outputs() -> Path:
    return _here("outputs", "outputs")


def lookup_overlay() -> Path | None:
    """Where lookup tables learned during a run are written; None means the
    repository's own tables are updated, as before."""
    r = run_dir()
    return r / "lookup" if r else None


def zephyr_clone() -> Path:
    value = os.environ.get("EMBEDEVAL_ZEPHYR_CLONE")
    return Path(value) if value else REPO_ROOT / "candidates" / "zephyr.git"


def nuttx_clones() -> tuple[Path | None, Path | None]:
    """The local NuttX kernel and apps clones; None for one not given."""
    return tuple(Path(os.environ[v]) if os.environ.get(v) else None
                 for v in ("EMBEDEVAL_NUTTX_CLONE", "EMBEDEVAL_NUTTX_APPS_CLONE"))


def show(p: Path) -> str:
    """A path for printing: relative to the repository or the run folder when
    inside one, else absolute."""
    p = Path(p)
    for root in (run_dir(), REPO_ROOT):
        if root is not None and (p == root or root in p.parents):
            return str(p.relative_to(root))
    return str(p)
