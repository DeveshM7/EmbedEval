#!/usr/bin/env python3
"""
Verify an instance is well formed: its tests must FAIL at the base commit and
PASS once the upstream fix is applied.

    python scripts/validate_instance.py nuttx__nuttx-11889
    python scripts/validate_instance.py --repo riot
    python scripts/validate_instance.py zephyr__zephyr-65697 --patch outputs/.../patch.diff

No API key and no LLM involved, so this is the cheapest way to confirm a built
image actually works. Replaces validate_instance.sh and
validate_riot_instance.sh, and gives NuttX a validator for the first time --
previously it had only 16 hand-written per-PR scripts.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_config
import instances


def sh(cmd: list[str], timeout: int = 120, quiet: bool = True):
    return subprocess.run(cmd, capture_output=quiet, text=True, timeout=timeout)


def gold_patch(meta: dict, workdir: Path) -> str:
    """
    Fetch the upstream fix as a diff, restricted to the files the fix should
    touch so test changes never leak in.

    Zephyr and RIOT are single-repo (base_commit..fix_commit). NuttX is two
    repos and the fix lives in the kernel one, between kernel_base_commit and
    kernel_merge_commit.
    """
    project = meta["project"]
    if project == "nuttx":
        repo, base, fix = meta["kernel_repo"], meta["kernel_base_commit"], meta["kernel_merge_commit"]
    else:
        repo, base, fix = meta["repo"], meta["base_commit"], meta["fix_commit"]

    clone = workdir / "repo"
    print(f"  cloning {repo} (blobless) ...")
    sh(["git", "clone", "--filter=blob:none", "--no-checkout", repo, str(clone)], timeout=900)
    sh(["git", "-C", str(clone), "fetch", "origin", base, fix], timeout=900)

    files = meta.get("files_changed_by_fix", [])
    cmd = ["git", "-C", str(clone), "diff", f"{base}..{fix}", "--"]
    cmd += files if files else [".", ":(exclude)tests/"]
    r = sh(cmd, timeout=300)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"could not produce a gold patch: {r.stderr.strip()[:200]}")
    return r.stdout


def validate(instance_id: str, patch_override: Path | None = None, timeout: int = 600,
             verbose: bool = False) -> bool:
    meta = instances.load_metadata(instance_id)
    cfg = build_config.config(meta["project"])
    image = meta["docker_image"]
    build_cmd = meta.get("build_command")
    if not build_cmd:
        print(f"  ERROR: {instance_id} has no build_command")
        return False

    print(f"\n{'=' * 70}\n{instance_id}  ({image})\n{'=' * 70}")

    run_args = ["docker", "run", "-d", "--rm"]
    if cfg["platform"]:
        run_args += ["--platform", cfg["platform"]]
    run_args += [image, "sleep", "1800"]

    r = sh(run_args, timeout=120)
    if r.returncode != 0:
        print(f"  ERROR: docker run failed: {r.stderr.strip()[:200]}")
        return False
    cid = r.stdout.strip()
    workdir = Path(tempfile.mkdtemp())

    def dexec(cmd: str, t: int):
        # With --verbose the container output streams live; otherwise it is
        # captured and only shown when a step fails.
        return sh(["docker", "exec", cid, "bash", "-c", cmd], timeout=t, quiet=not verbose)

    try:
        # --- Step 1: tests must FAIL on the unfixed code -------------------
        print("\n  Step 1: expecting tests to FAIL at the base commit")
        dexec(f"cd /testbed && {build_cmd}", 900)
        r1 = dexec("cd /testbed && run_tests", timeout)
        if r1.returncode == 0:
            print("  FAIL: tests PASSED before the fix -- the instance does not "
                  "isolate the bug (test patch may not apply, or the bug is absent)")
            return False
        print(f"  ok: run_tests exited {r1.returncode} as expected")

        # --- Step 2: tests must PASS with the fix applied ------------------
        print("\n  Step 2: applying the fix, expecting tests to PASS")
        if patch_override:
            patch_text = Path(patch_override).read_text()
            print(f"  using supplied patch {patch_override}")
        else:
            patch_text = gold_patch(meta, workdir)
        pf = workdir / "fix.diff"
        pf.write_text(patch_text)

        sh(["docker", "cp", str(pf), f"{cid}:/tmp/fix.diff"], timeout=60)
        ra = dexec("cd /testbed && git apply /tmp/fix.diff", 120)
        if ra.returncode != 0:
            print(f"  FAIL: git apply failed: {ra.stderr.strip()[:300]}")
            return False

        for p in cfg.get("clean_paths", []):
            dexec(f"rm -rf {p}", 60)
        rb = dexec(f"cd /testbed && {build_cmd}", 900)
        if rb.returncode != 0:
            print(f"  FAIL: build failed after applying the fix (rc={rb.returncode})")
            print("  " + ((rb.stdout or "") + (rb.stderr or ""))[-400:].replace("\n", "\n  "))
            return False

        r2 = dexec("cd /testbed && run_tests", timeout)
        if r2.returncode != 0:
            print(f"  FAIL: tests still failing after the fix (rc={r2.returncode})")
            print("  " + ((r2.stdout or "") + (r2.stderr or ""))[-400:].replace("\n", "\n  "))
            return False

        print("\n  VALID: fails before the fix, passes after")
        return True

    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return False
    finally:
        sh(["docker", "rm", "-f", cid], timeout=60)
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Validate instance fail-then-pass behaviour.")
    p.add_argument("instance", nargs="*", help="instance id(s)")
    p.add_argument("--repo", choices=instances.PROJECTS, help="every instance for one project")
    p.add_argument("--all", action="store_true")
    p.add_argument("--patch", help="validate this patch instead of the upstream fix")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--verbose", "-v", action="store_true",
                   help="stream build and test output instead of capturing it")
    args = p.parse_args()

    if args.all:
        targets = instances.discover_instances()
    elif args.repo:
        targets = instances.discover_instances(args.repo)
    elif args.instance:
        targets = args.instance
    else:
        p.error("give an instance id, or --repo, or --all")

    if args.patch and len(targets) != 1:
        p.error("--patch applies to exactly one instance")

    start = time.time()
    results = {t: validate(t, Path(args.patch) if args.patch else None, args.timeout,
                           args.verbose)
               for t in targets}

    print(f"\n{'=' * 70}\nSUMMARY  ({(time.time() - start) / 60:.1f} min)\n{'=' * 70}")
    for t, ok in results.items():
        print(f"  [{'VALID' if ok else 'FAIL '}] {t}")
    ok = sum(results.values())
    print(f"\n  {ok}/{len(results)} valid")
    if ok != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
