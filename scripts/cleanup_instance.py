#!/usr/bin/env python3
"""
Free the space one twister-pipeline instance uses, once it has been validated.

    python scripts/cleanup_instance.py zephyr__zephyr-65697
    python scripts/cleanup_instance.py zephyr__zephyr-65697 --dry-run

Removes only what belongs to that instance, all under the work root:

    images/instances/<id>.sif   the instance image (2-5 GB)
    dryrun/<pr>/                the dry run's exported tree and outputs
    runs/<id>-*/                any validation overlay left by an interrupted run
    runs/agent-<id>-*/          any agent overlay left by an interrupted run

Keeps everything small or shared: the recipe and validation.json in the
instance directory, logs/<id>/, candidates/dryrun/<pr>.json, and the shared
Python and SDK images and venvs other instances reuse.

Every deletion goes through apptainer_util.remove, which refuses any path
outside the work root.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apptainer_util as au  # noqa: E402
import instances  # noqa: E402


def targets(instance_id: str) -> list[Path]:
    _, pr = instances.split_instance(instance_id)
    root = au.work_root()
    found = [au.instance_image(instance_id), root / "dryrun" / pr]
    found += sorted((root / "runs").glob(f"{instance_id}-[0-9]*"))
    found += sorted((root / "runs").glob(f"agent-{instance_id}-[0-9]*"))   # agent overlays
    return [t for t in found if t.exists()]


def size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("instance", nargs="+")
    p.add_argument("--dry-run", action="store_true", help="list what would be removed")
    args = p.parse_args()
    for iid in args.instance:
        found = targets(iid)
        if not found:
            print(f"{iid}: nothing to remove")
            continue
        for t in found:
            gb = size(t) / 2**30
            if args.dry_run:
                print(f"{iid}: would remove {t} ({gb:.2f} GB)")
            else:
                au.remove(t)
                print(f"{iid}: removed {t} ({gb:.2f} GB)")


if __name__ == "__main__":
    main()
