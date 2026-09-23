#!/usr/bin/env python3
"""
Build the shared base images.

    python scripts/build_bases.py               # all projects
    python scripts/build_bases.py --repo nuttx

One base image per project, shared by every instance of that project. Build
these once; they only change when the base Dockerfile does.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_config
import instances


def build_base(project: str, dry_run: bool = False) -> bool:
    cfg = build_config.config(project)
    dockerfile = instances.REPO_ROOT / cfg["base_dockerfile"]
    if not dockerfile.exists():
        print(f"  ERROR: {dockerfile} not found")
        return False

    cmd = ["docker", "build", "-f", str(dockerfile), "-t", cfg["base_image"]]
    if cfg["platform"]:
        cmd += ["--platform", cfg["platform"]]
    cmd.append(str(instances.BASES_DIR))

    print(f"\n=== {project}: {cfg['base_image']} ===")
    print("  " + " ".join(cmd))
    if dry_run:
        return True

    start = time.time()
    rc = subprocess.run(cmd).returncode
    mins = (time.time() - start) / 60
    print(f"  {'built' if rc == 0 else 'FAILED'} in {mins:.1f} min")
    return rc == 0


def main() -> None:
    p = argparse.ArgumentParser(description="Build EmbedEval base images.")
    p.add_argument("--repo", choices=instances.PROJECTS, help="only this project")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    targets = [args.repo] if args.repo else list(instances.PROJECTS)
    results = {t: build_base(t, args.dry_run) for t in targets}

    print("\n=== summary ===")
    for t, ok in results.items():
        print(f"  [{'ok  ' if ok else 'FAIL'}] {t}")
    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
