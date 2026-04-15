"""
Evaluate agent-generated patches by applying them in fresh containers and running tests.

For each instance, this script:
  1. Starts a fresh container from the instance's Docker image
  2. Copies the agent's .patch file into the container
  3. Applies the patch with `git apply`
  4. Rebuilds and runs `run_tests`
  5. Records pass/fail based on the exit code

Usage:
    python harness/evaluate_patches.py --model anthropic/claude-sonnet-4-6
    python harness/evaluate_patches.py --model anthropic/claude-sonnet-4-6 --instances zephyr__zephyr-65697
    python harness/evaluate_patches.py --patch-dir outputs/my_run/
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
INSTANCES_DIR = REPO_ROOT / "docker" / "instances"


def load_metadata(instance_id: str) -> dict:
    path = INSTANCES_DIR / instance_id / "metadata.json"
    with open(path) as f:
        return json.load(f)


def evaluate_patch(instance_id: str, patch_path: Path, timeout: int = 300) -> dict:
    """
    Spin up a fresh container, apply the patch, build, run tests, return result.
    """
    meta = load_metadata(instance_id)
    image = meta["docker_image"]
    build_command = meta["build_command"]

    result = {
        "instance_id": instance_id,
        "patch_file": str(patch_path),
        "patch_empty": False,
        "patch_applied": False,
        "build_ok": False,
        "tests_passed": False,
        "grade": "fail",
        "error": None,
        "elapsed_seconds": 0,
    }

    # Check if patch file exists and is non-empty
    if not patch_path.exists():
        result["error"] = "patch file not found"
        return result

    patch_content = patch_path.read_text().strip()
    if not patch_content:
        result["patch_empty"] = True
        result["error"] = "empty patch — agent made no changes"
        return result

    container_id = None
    start = time.time()

    try:
        # Start a fresh container from the instance image.
        print(f"  Starting container from {image}...")
        proc = subprocess.run(
            ["docker", "run", "-d", "--rm",
             image, "sleep", "600"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            result["error"] = f"docker run failed: {proc.stderr.strip()}"
            return result
        container_id = proc.stdout.strip()

        # Copy patch into container
        print(f"  Applying patch...")
        copy_proc = subprocess.run(
            ["docker", "cp", str(patch_path), f"{container_id}:/tmp/agent.patch"],
            capture_output=True, text=True, timeout=15,
        )
        if copy_proc.returncode != 0:
            result["error"] = f"docker cp failed: {copy_proc.stderr.strip()}"
            return result

        # Apply the patch
        apply_proc = subprocess.run(
            ["docker", "exec", container_id, "bash", "-c",
             "cd /testbed && git apply /tmp/agent.patch"],
            capture_output=True, text=True, timeout=30,
        )
        if apply_proc.returncode != 0:
            result["error"] = f"git apply failed: {apply_proc.stderr.strip()}"
            return result
        result["patch_applied"] = True

        # Clean the build directory so CMake re-configures from scratch.
        # This is necessary when the agent adds new Kconfig options or
        # CMakeLists changes that a stale cache wouldn't pick up.
        subprocess.run(
            ["docker", "exec", container_id, "bash", "-c",
             "rm -rf /testbed/build"],
            capture_output=True, text=True, timeout=10,
        )

        # Build using the instance's specific build command
        print(f"  Building ({build_command})...")
        build_proc = subprocess.run(
            ["docker", "exec", container_id, "bash", "-c",
             f"cd /testbed && {build_command}"],
            capture_output=True, text=True, timeout=180,
        )
        if build_proc.returncode != 0:
            result["error"] = f"build failed (rc={build_proc.returncode})"
            # Still show output for debugging
            output = build_proc.stdout + build_proc.stderr
            if output:
                result["build_output_tail"] = output[-500:]
            return result
        result["build_ok"] = True

        # Run tests
        print(f"  Running tests...")
        test_proc = subprocess.run(
            ["docker", "exec", container_id, "bash", "-c",
             "cd /testbed && run_tests"],
            capture_output=True, text=True, timeout=timeout,
        )

        output = test_proc.stdout + test_proc.stderr
        result["test_output_tail"] = output[-1000:]

        if test_proc.returncode == 0:
            result["tests_passed"] = True
            result["grade"] = "pass"
            print(f"  PASS")
        elif test_proc.returncode == 1:
            result["error"] = "tests failed"
            print(f"  FAIL — tests did not pass")
        elif test_proc.returncode == 2:
            result["error"] = "tests timed out (possible infinite loop)"
            print(f"  FAIL — timeout")
        else:
            result["error"] = f"run_tests exited with rc={test_proc.returncode}"
            print(f"  FAIL — unexpected exit code {test_proc.returncode}")

    except subprocess.TimeoutExpired:
        result["error"] = "evaluation timed out"
        print(f"  FAIL — overall timeout")
    except Exception as e:
        result["error"] = str(e)
        print(f"  FAIL — exception: {e}")
    finally:
        result["elapsed_seconds"] = round(time.time() - start, 1)
        # Clean up container
        if container_id:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True, timeout=15,
            )

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate agent patches by applying them in fresh containers."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--model",
        help="Model slug — looks for patches in outputs/<model_slug>/",
    )
    group.add_argument(
        "--patch-dir",
        help="Direct path to directory containing .patch files",
    )
    parser.add_argument(
        "--instances",
        nargs="+",
        default=None,
        help="Specific instance IDs to evaluate (default: all patches found)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds for test execution (default: 300)",
    )
    args = parser.parse_args()

    # Locate patch directory
    if args.model:
        slug = args.model.replace("/", "_").replace(":", "_")
        patch_dir = REPO_ROOT / "outputs" / slug
    else:
        patch_dir = Path(args.patch_dir)

    if not patch_dir.exists():
        sys.exit(f"ERROR: Patch directory not found: {patch_dir}")

    # Find all .patch files
    patch_files = sorted(patch_dir.glob("*.patch"))
    if not patch_files:
        sys.exit(f"ERROR: No .patch files found in {patch_dir}")

    # Map instance_id -> patch_path
    patches = {}
    for pf in patch_files:
        instance_id = pf.stem  # e.g. zephyr__zephyr-65697.patch -> zephyr__zephyr-65697
        patches[instance_id] = pf

    # Filter to requested instances
    if args.instances:
        for inst in args.instances:
            if inst not in patches:
                sys.exit(f"ERROR: No patch found for '{inst}'. Available: {list(patches.keys())}")
        patches = {k: v for k, v in patches.items() if k in args.instances}

    print(f"Patch dir  : {patch_dir}")
    print(f"Patches    : {len(patches)} — {', '.join(patches.keys())}")
    print(f"Timeout    : {args.timeout}s per instance")
    print()

    # Evaluate each patch
    results = []
    for i, (instance_id, patch_path) in enumerate(patches.items(), 1):
        print(f"[{i}/{len(patches)}] Evaluating {instance_id}")
        result = evaluate_patch(instance_id, patch_path, timeout=args.timeout)
        results.append(result)
        print(f"  Grade: {result['grade']}  ({result['elapsed_seconds']}s)")
        if result["error"]:
            print(f"  Error: {result['error']}")
        print()

    # Summary
    print("=" * 60)
    print("EVALUATION RESULTS")
    print("=" * 60)

    for r in results:
        icon = "PASS" if r["grade"] == "pass" else "FAIL"
        err = f"  ({r['error']})" if r["error"] else ""
        print(f"  [{icon}] {r['instance_id']:40s} {r['elapsed_seconds']:>6.1f}s{err}")

    passed = sum(1 for r in results if r["grade"] == "pass")
    total = len(results)
    print(f"\n  {passed}/{total} patches produce passing tests")

    # Save evaluation results
    eval_path = patch_dir / "evaluation.json"
    eval_data = {
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total": total,
        "passed": passed,
        "results": results,
    }
    with open(eval_path, "w") as f:
        json.dump(eval_data, f, indent=2)
    print(f"  Results saved to: {eval_path}")


if __name__ == "__main__":
    main()
