#!/usr/bin/env python3
"""
Build per-instance Docker images.

    python scripts/build_instances.py --instance nuttx__nuttx-11889
    python scripts/build_instances.py --repo riot
    python scripts/build_instances.py --all

Replaces build_instance_images.sh and build_riot_instance_images.sh, and adds
NuttX, which previously had no general build path -- only 16 hand-written
per-PR scripts, now under archive/nuttx-validation/.

Requires the project's base image; see scripts/build_bases.py.
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

REQUIRED_FILES = ("Dockerfile", "metadata.json", "test_patch.diff", "run_tests.sh")


def build_instance(instance_id: str, dry_run: bool = False, no_cache: bool = False) -> bool:
    d = instances.instance_dir(instance_id)
    missing = [f for f in REQUIRED_FILES if not (d / f).exists()]
    if missing:
        print(f"  ERROR: {instance_id} missing {', '.join(missing)}")
        return False

    meta = instances.load_metadata(instance_id)
    cfg = build_config.config(meta["project"])
    try:
        args = build_config.build_args(meta)
    except KeyError as e:
        print(f"  ERROR: {instance_id}: {e}")
        return False

    cmd = ["docker", "build"]
    if cfg["platform"]:
        cmd += ["--platform", cfg["platform"]]
    if no_cache:
        cmd.append("--no-cache")
    for k, v in args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    cmd += ["-t", meta["docker_image"], str(d)]

    print(f"\n=== {instance_id} -> {meta['docker_image']} ===")
    print("  " + " ".join(cmd))
    if dry_run:
        return True

    start = time.time()
    rc = subprocess.run(cmd).returncode
    mins = (time.time() - start) / 60
    print(f"  {'built' if rc == 0 else 'FAILED'} in {mins:.1f} min")
    return rc == 0


def select(args) -> list[str]:
    if args.instance:
        known = set(instances.discover_instances())
        for i in args.instance:
            if i not in known:
                sys.exit(f"ERROR: unknown instance {i!r}")
        return list(args.instance)
    if args.repo:
        return instances.discover_instances(args.repo)
    return instances.discover_instances()


def main() -> None:
    p = argparse.ArgumentParser(description="Build EmbedEval instance images.")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--instance", nargs="+")
    sel.add_argument("--repo", choices=instances.PROJECTS)
    sel.add_argument("--all", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    targets = select(args)
    print(f"Instances to build: {len(targets)}")

    results = {t: build_instance(t, args.dry_run, args.no_cache) for t in targets}

    print("\n=== summary ===")
    for t, ok in results.items():
        print(f"  [{'ok  ' if ok else 'FAIL'}] {t}")
    ok = sum(results.values())
    print(f"\n  {ok}/{len(results)} built")
    if ok != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
