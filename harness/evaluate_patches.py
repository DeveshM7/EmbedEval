"""
Grade agent patches by applying them in fresh containers.

    python harness/evaluate_patches.py --all --model gpt-5.4
    python harness/evaluate_patches.py --repo riot --model gpt-5.4 gemini-2.5-pro
    python harness/evaluate_patches.py --instance zephyr__zephyr-65697 --model gpt-5.4

Reads outputs/<repo>/<model>/<PR>/patch.diff, written by run.py. Selection
flags mirror run.py so the same invocation shape grades what you just ran.

Evaluation is deliberately decoupled from the agent run: each patch is applied
in a container started fresh from the instance image, so nothing the agent left
behind -- stale build artefacts, a modified test file, a running process -- can
influence the result. The patch text is the only thing that crosses over.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths
import projects


def evaluate_patch(instance_id: str, model_name: str, timeout: int = 300) -> dict:
    meta = paths.load_metadata(instance_id)
    cfg = projects.config(meta["project"])
    image = meta["docker_image"]
    build_command = projects.setting(meta, "build_command")
    patch_path = paths.patch_path(instance_id, model_name)

    result = {
        "instance_id": instance_id,
        "model": paths.resolve_model(model_name),
        "patch_file": str(patch_path.relative_to(paths.REPO_ROOT)),
        "patch_empty": False,
        "patch_applied": False,
        "build_ok": False,
        "tests_passed": False,
        "grade": "fail",
        "error": None,
        "elapsed_seconds": 0,
    }

    if not patch_path.exists():
        # Distinct from a failing patch: the run never happened, or wrote
        # somewhere else. Not graded, so it cannot silently count as a failure.
        result["grade"] = "missing"
        result["error"] = "no patch file (run not found)"
        return result

    if not patch_path.read_text().strip():
        result["patch_empty"] = True
        result["error"] = "empty patch — agent made no changes"
        return result

    if build_command is None:
        result["error"] = "no build_command in metadata or project config"
        return result

    run_args = ["docker", "run", "-d", "--rm"]
    platform = projects.setting(meta, "docker_platform")
    if platform:
        run_args += ["--platform", platform]
    run_args += [image, "sleep", "900"]

    container_id = None
    start = time.time()
    try:
        print(f"  starting container from {image} ...")
        proc = subprocess.run(run_args, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            result["error"] = f"docker run failed: {proc.stderr.strip()[:200]}"
            return result
        container_id = proc.stdout.strip()

        def dexec(cmd: str, t: int):
            return subprocess.run(
                ["docker", "exec", container_id, "bash", "-c", cmd],
                capture_output=True, text=True, timeout=t,
            )

        print("  applying patch ...")
        cp = subprocess.run(
            ["docker", "cp", str(patch_path), f"{container_id}:/tmp/agent.patch"],
            capture_output=True, text=True, timeout=30,
        )
        if cp.returncode != 0:
            result["error"] = f"docker cp failed: {cp.stderr.strip()[:200]}"
            return result

        ap = dexec("cd /testbed && git apply /tmp/agent.patch", 60)
        if ap.returncode != 0:
            result["error"] = f"git apply failed: {ap.stderr.strip()[:300]}"
            return result
        result["patch_applied"] = True

        # Clean before rebuilding so a stale cache cannot mask a Kconfig or
        # CMakeLists change. Paths are per project: zephyr builds out-of-tree
        # into /testbed/build, while nuttx builds in-tree and riot under
        # tests/unittests -- for those the previous hardcoded
        # `rm -rf /testbed/build` was a silent no-op.
        for p in cfg["clean_paths"]:
            dexec(f"rm -rf {p}", 30)

        print(f"  building ({build_command}) ...")
        bp = dexec(f"cd /testbed && {build_command}", 600)
        if bp.returncode != 0:
            result["error"] = f"build failed (rc={bp.returncode})"
            out = (bp.stdout or "") + (bp.stderr or "")
            if out:
                result["build_output_tail"] = out[-600:]
            return result
        result["build_ok"] = True

        print("  running tests ...")
        tp = dexec("cd /testbed && run_tests", timeout)
        out = (tp.stdout or "") + (tp.stderr or "")
        result["test_output_tail"] = out[-1200:]

        if tp.returncode == 0:
            result["tests_passed"] = True
            result["grade"] = "pass"
            print("  PASS")
        elif tp.returncode == 1:
            result["error"] = "tests failed"
            print("  FAIL — tests did not pass")
        elif tp.returncode == 2:
            result["error"] = "tests timed out (possible infinite loop)"
            print("  FAIL — timeout")
        else:
            result["error"] = f"run_tests exited with rc={tp.returncode}"
            print(f"  FAIL — unexpected exit code {tp.returncode}")

    except subprocess.TimeoutExpired:
        result["error"] = "evaluation timed out"
        print("  FAIL — overall timeout")
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        print(f"  FAIL — {result['error']}")
    finally:
        result["elapsed_seconds"] = round(time.time() - start, 1)
        if container_id:
            subprocess.run(["docker", "rm", "-f", container_id],
                           capture_output=True, timeout=30)
    return result


def select_instances(args) -> list[str]:
    if args.instance:
        known = set(paths.discover_instances())
        for i in args.instance:
            if i not in known:
                sys.exit(f"ERROR: unknown instance {i!r}")
        return list(args.instance)
    if args.repo:
        return paths.discover_instances(args.repo)
    return paths.discover_instances()


def main() -> None:
    p = argparse.ArgumentParser(description="Grade agent patches in fresh containers.")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--instance", nargs="+")
    sel.add_argument("--repo", choices=paths.PROJECTS)
    sel.add_argument("--all", action="store_true")
    p.add_argument("--model", nargs="+", required=True)
    p.add_argument("--timeout", type=int, default=300,
                   help="seconds for the test run (default: 300)")
    p.add_argument("--output", default="results.json",
                   help="where to write the results file (default: results.json)")
    p.add_argument("--dry-run", action="store_true",
                   help="list the patches that would be graded and exit")
    args = p.parse_args()

    try:
        models = [paths.resolve_model(m) for m in args.model]
    except paths.UnknownModelError as e:
        sys.exit(f"ERROR: {e}")
    instances = select_instances(args)

    pairs = [(i, m) for m in models for i in instances]
    present = [(i, m) for i, m in pairs if paths.patch_path(i, m).exists()]
    print(f"Instances : {len(instances)}")
    print(f"Models    : {', '.join(models)}")
    print(f"Pairs     : {len(pairs)} ({len(present)} with a patch on disk)\n")

    if args.dry_run:
        for i, m in pairs:
            pp = paths.patch_path(i, m)
            mark = "ok     " if pp.exists() else "MISSING"
            print(f"  [{mark}] {pp.relative_to(paths.REPO_ROOT)}")
        return

    results = []
    for n, (instance_id, model_name) in enumerate(pairs, 1):
        print(f"[{n}/{len(pairs)}] {instance_id}  x  {model_name}")
        results.append(evaluate_patch(instance_id, model_name, timeout=args.timeout))
        print()

    print("=" * 74)
    print("RESULTS")
    print("=" * 74)
    for r in results:
        icon = {"pass": "PASS", "missing": "----"}.get(r["grade"], "FAIL")
        err = f"  ({r['error']})" if r["error"] else ""
        print(f"  [{icon}] {r['model']:34s} {r['instance_id']:24s} "
              f"{r['elapsed_seconds']:>6.1f}s{err}")

    graded = [r for r in results if r["grade"] != "missing"]
    passed = sum(1 for r in graded if r["grade"] == "pass")
    missing = len(results) - len(graded)
    print(f"\n  {passed}/{len(graded)} graded patches pass"
          + (f"   ({missing} not run)" if missing else ""))

    out = paths.REPO_ROOT / args.output
    out.write_text(json.dumps({
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models": models,
        "total": len(results),
        "graded": len(graded),
        "passed": passed,
        "results": results,
    }, indent=2))
    print(f"  written to {out.relative_to(paths.REPO_ROOT)}")


if __name__ == "__main__":
    main()
