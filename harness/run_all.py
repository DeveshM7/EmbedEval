"""
Run the agent harness across all instances for a given model.

Discovers all instance directories under docker/instances/, runs each one
sequentially, and organizes outputs into outputs/<model_slug>/.

Usage:
    python harness/run_all.py --model anthropic/claude-sonnet-4-6
    python harness/run_all.py --model openai/gpt-5.4 --instances zephyr__zephyr-65697 zephyr__zephyr-43405
    python harness/run_all.py --model anthropic/claude-sonnet-4-6 --step-limit 30 --cost-limit 2.0
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
INSTANCES_DIR = REPO_ROOT / "docker" / "instances"


def discover_instances() -> list[str]:
    """Find all valid instance directories (must have metadata.json)."""
    instances = []
    for d in sorted(INSTANCES_DIR.iterdir()):
        if d.is_dir() and (d / "metadata.json").exists():
            instances.append(d.name)
    return instances


def model_slug(model: str) -> str:
    """Convert a model name like 'anthropic/claude-sonnet-4-6' to a filesystem-safe slug."""
    return model.replace("/", "_").replace(":", "_")


def run_instance(instance_id: str, model: str, output_dir: Path, extra_args: list[str]) -> dict:
    """Run run_instance.py for a single instance. Returns a result dict."""
    cmd = [
        sys.executable,
        str(REPO_ROOT / "harness" / "run_instance.py"),
        "--instance", instance_id,
        "--model", model,
        "--output-dir", str(output_dir),
        *extra_args,
    ]

    print(f"\n{'=' * 60}")
    print(f"Running instance: {instance_id}")
    print(f"Model: {model}")
    print(f"Output: {output_dir}")
    print(f"{'=' * 60}\n")

    start = time.time()
    try:
        result = subprocess.run(cmd, timeout=1800)  # 30 min max per instance
        elapsed = time.time() - start
        return {
            "instance_id": instance_id,
            "returncode": result.returncode,
            "elapsed_seconds": round(elapsed, 1),
            "status": "success" if result.returncode == 0 else "error",
        }
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return {
            "instance_id": instance_id,
            "returncode": -1,
            "elapsed_seconds": round(elapsed, 1),
            "status": "timeout",
        }
    except Exception as e:
        elapsed = time.time() - start
        return {
            "instance_id": instance_id,
            "returncode": -1,
            "elapsed_seconds": round(elapsed, 1),
            "status": f"exception: {e}",
        }


def main():
    parser = argparse.ArgumentParser(
        description="Run the agent harness across all instances for a given model."
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Model in LiteLLM format (e.g. anthropic/claude-sonnet-4-6, openai/gpt-5.4)",
    )
    parser.add_argument(
        "--instances",
        nargs="+",
        default=None,
        help="Specific instance IDs to run (default: all discovered instances)",
    )
    parser.add_argument(
        "--step-limit",
        type=int,
        default=50,
        help="Max agent steps per instance (default: 50)",
    )
    parser.add_argument(
        "--cost-limit",
        type=float,
        default=3.0,
        help="Max spend in USD per instance (default: 3.0)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Pass --verbose to each run_instance invocation",
    )
    args = parser.parse_args()

    # Discover or filter instances
    all_instances = discover_instances()
    if not all_instances:
        sys.exit(f"ERROR: No instances found under {INSTANCES_DIR}")

    if args.instances:
        # Validate requested instances exist
        for inst in args.instances:
            if inst not in all_instances:
                sys.exit(f"ERROR: Instance '{inst}' not found. Available: {all_instances}")
        instances = args.instances
    else:
        instances = all_instances

    # Output dir: outputs/<model_slug>/
    slug = model_slug(args.model)
    output_dir = REPO_ROOT / "outputs" / slug
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Model      : {args.model}")
    print(f"Instances  : {len(instances)} — {', '.join(instances)}")
    print(f"Output dir : {output_dir}")
    print(f"Step limit : {args.step_limit}  Cost limit: ${args.cost_limit}")
    print()

    # Build extra args to forward
    extra_args = [
        "--step-limit", str(args.step_limit),
        "--cost-limit", str(args.cost_limit),
    ]
    if args.verbose:
        extra_args.append("--verbose")

    # Run each instance
    results = []
    for i, instance_id in enumerate(instances, 1):
        print(f"\n[{i}/{len(instances)}] Starting {instance_id}")
        result = run_instance(instance_id, args.model, output_dir, extra_args)
        results.append(result)
        print(f"[{i}/{len(instances)}] {instance_id}: {result['status']} ({result['elapsed_seconds']}s)")

    # Save agent run summary
    summary = {
        "model": args.model,
        "step_limit": args.step_limit,
        "cost_limit": args.cost_limit,
        "results": results,
    }
    summary_path = output_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    # Print agent run summary
    print(f"\n{'=' * 60}")
    print(f"AGENT RUN SUMMARY — {args.model}")
    print(f"{'=' * 60}")
    for r in results:
        status_icon = "OK" if r["status"] == "success" else "FAIL"
        print(f"  [{status_icon}] {r['instance_id']:40s} {r['elapsed_seconds']:>8.1f}s  {r['status']}")

    completed = sum(1 for r in results if r["status"] == "success")
    print(f"\n  {completed}/{len(results)} instances completed successfully")
    print(f"  Summary saved to: {summary_path}")

    # --- Evaluate patches in fresh containers ---
    print(f"\n{'=' * 60}")
    print(f"EVALUATING PATCHES")
    print(f"{'=' * 60}\n")

    sys.path.insert(0, str(Path(__file__).parent))
    from evaluate_patches import evaluate_patch

    eval_results = []
    for i, r in enumerate(results, 1):
        instance_id = r["instance_id"]
        patch_path = output_dir / f"{instance_id}.patch"
        print(f"[{i}/{len(results)}] Evaluating {instance_id}")
        eval_result = evaluate_patch(instance_id, patch_path)
        eval_results.append(eval_result)
        print()

    # Merge grades into summary
    grade_map = {er["instance_id"]: er for er in eval_results}
    for r in summary["results"]:
        er = grade_map.get(r["instance_id"], {})
        r["grade"] = er.get("grade", "unknown")
        r["tests_passed"] = er.get("tests_passed", False)
        r["eval_error"] = er.get("error")

    # Save updated summary with grades
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    # Save detailed evaluation
    eval_path = output_dir / "evaluation.json"
    eval_data = {
        "model": args.model,
        "total": len(eval_results),
        "passed": sum(1 for er in eval_results if er["grade"] == "pass"),
        "results": eval_results,
    }
    with open(eval_path, "w") as f:
        json.dump(eval_data, f, indent=2)

    # Final summary
    passed = sum(1 for er in eval_results if er["grade"] == "pass")
    print(f"{'=' * 60}")
    print(f"FINAL RESULTS — {args.model}")
    print(f"{'=' * 60}")
    for er in eval_results:
        icon = "PASS" if er["grade"] == "pass" else "FAIL"
        err = f"  ({er['error']})" if er["error"] else ""
        print(f"  [{icon}] {er['instance_id']:40s} {er['elapsed_seconds']:>6.1f}s{err}")
    print(f"\n  {passed}/{len(eval_results)} patches produce passing tests")
    print(f"  Evaluation saved to: {eval_path}")


if __name__ == "__main__":
    main()
