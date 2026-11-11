"""
Grade agent patches by applying them in fresh containers.

    python harness/evaluate_patches.py --all --model gpt-5.4
    python harness/evaluate_patches.py --repo riot --model gpt-5.4 gemini-2.5-pro
    python harness/evaluate_patches.py --instance zephyr__zephyr-65697 --model gpt-5.4

Reads each run's folder, <eval dir>/<model>/<project>/<PR>/ (harness/paths.py),
written by run.py, and writes its grade.json there. run.py grades each run as
soon as it ends; this command grades runs again or late. Only a run the agent
submitted is tested; any other ending is a fail (grade_run). Selection flags
mirror run.py so the same invocation shape grades what you just ran.

Evaluation is deliberately decoupled from the agent run: each patch is applied
in a container started fresh from the instance image, so nothing the agent left
behind -- stale build artefacts, a modified test file, a running process -- can
influence the result. The patch text is the only thing that crosses over.

Twister-pipeline Zephyr instances are graded in their Apptainer image instead
(evaluate_apptainer): the patch on a fresh overlay, then the instance's
`run_tests`, which must pass every test of the scenario.

    EMBEDEVAL_INSTANCES_DIR=generated python harness/evaluate_patches.py \
        --instance zephyr__zephyr-65697 --model qwen38-nvfp4
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


RUN_TESTS_ERRORS = {1: "tests failed (a test, the build or the configuration)",
                    2: "the test runner did not start", 3: "nothing ran"}


def evaluate_apptainer(instance_id: str, model_name: str, meta: dict, timeout: int) -> dict:
    """Grade a patch on an instance run in Apptainer (twister-pipeline Zephyr, RIOT).

    A fresh writable overlay on the image as built (base commit + the PR's
    tests), the agent's patch applied on top, then `run_tests`: twister builds
    the instance's scenario and runs it. Pass means exit 0 -- every test case
    of the scenario passed, not just the listed ones. The patch never contains
    tests/ (the runner excludes protected paths when capturing it), so the
    tests graded are the PR's own.
    """
    sys.path.insert(0, str(paths.REPO_ROOT / "scripts"))
    import apptainer_util as au

    patch_path = paths.patch_path(instance_id, model_name)
    result = {"instance_id": instance_id, "model": paths.resolve_model(model_name),
              "patch_file": str(paths.show(patch_path)),
              "patch_empty": False, "patch_applied": False, "build_ok": False,
              "tests_passed": False, "grade": "fail", "error": None, "elapsed_seconds": 0}
    if not patch_path.exists():
        result.update(grade="missing", error="no patch file (run not found)")
        return result
    if not patch_path.read_text().strip():
        result.update(patch_empty=True, error="empty patch — agent made no changes")
        return result
    import registry       # the validated image: pulled from the registry if published
    try:
        image = registry.image_for(instance_id)
    except (FileNotFoundError, RuntimeError) as e:
        # Our side, not the patch's: the image could not be had.
        result.update(error=str(e), infra_error=f"image: {e}")
        return result

    start = time.time()
    run_dir = au.path("runs", f"eval-{instance_id}-{time.strftime('%Y%m%d-%H%M%S')}", "x").parent
    overlay = run_dir / "overlay"
    overlay.mkdir(parents=True)
    (run_dir / "agent.patch").write_text(patch_path.read_text())
    base, layer = au.instance_container(meta, image)
    # No network, as in the agent's run: the tests build and run offline.
    container = au.exec_args(base, binds={run_dir: "/embedeval-run"}, overlay=overlay,
                             layer=layer, network=False)
    try:
        print("  applying patch ...")
        ap = subprocess.run(container + ["git", "-C", "/testbed", "apply",
                                         "/embedeval-run/agent.patch"],
                            capture_output=True, text=True, timeout=120)
        if ap.returncode != 0:
            text = (ap.stdout + ap.stderr).strip()
            result["error"] = f"git apply failed: {text[:300]}"
            if "FATAL:" in text:          # Apptainer itself did not start the container
                result["infra_error"] = f"container: {text[:300]}"
            return result
        result["patch_applied"] = True
        print("  run_tests (twister build + run) ...")
        tp = subprocess.run(container + ["run_tests"], capture_output=True, text=True,
                            timeout=max(timeout, 1800))
        out = (tp.stdout or "") + (tp.stderr or "")
        result["test_output_tail"] = out[-2000:]
        result["build_ok"] = tp.returncode in (0, 3) or "Build failure" not in out
        if tp.returncode == 0:
            result.update(tests_passed=True, grade="pass")
            print("  PASS")
        else:
            result["error"] = RUN_TESTS_ERRORS.get(tp.returncode, f"run_tests exited {tp.returncode}")
            print(f"  FAIL — {result['error']}")
    except subprocess.TimeoutExpired:
        result["error"] = "evaluation timed out"
        print("  FAIL — overall timeout")
    finally:
        result["elapsed_seconds"] = round(time.time() - start, 1)
        au.remove(run_dir)
    return result


def evaluate_patch(instance_id: str, model_name: str, timeout: int = 300) -> dict:
    meta = paths.load_metadata(instance_id)
    if meta.get("pipeline") in ("twister", "riot-layer"):
        return evaluate_apptainer(instance_id, model_name, meta, timeout)
    if meta["project"] == "zephyr":
        return {"instance_id": instance_id, "model": paths.resolve_model(model_name),
                "grade": "missing", "error": "Docker Zephyr instance (reference metadata only); "
                "grade Zephyr from generated/ (EMBEDEVAL_INSTANCES_DIR=generated)",
                "elapsed_seconds": 0}
    cfg = projects.config(meta["project"])
    image = meta["docker_image"]
    build_command = projects.setting(meta, "build_command")
    patch_path = paths.patch_path(instance_id, model_name)

    result = {
        "instance_id": instance_id,
        "model": paths.resolve_model(model_name),
        "patch_file": str(paths.show(patch_path)),
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


def grade_run(instance_id: str, model_name: str, timeout: int = 1800) -> dict | None:
    """Grade one finished agent run and write its grade.json; return the grade.

    As on SWE-bench (mini-swe-agent), only an explicit submission is graded: a run
    that ended any other way -- the step limit, the time cap, the model's context
    overflowing, repeated format errors -- is a fail without running the tests.
    Its patch.diff is still kept, for reading.

    Returns None, and writes nothing, when there is nothing to grade yet: no
    run.json, or a run that ended in an infrastructure error (the runner retries
    those). An infrastructure error while grading (the image could not be had,
    the container did not start) is retried once; if it happens again the run is
    recorded as "not_run", not as a fail.
    """
    rec_path = paths.run_record_path(instance_id, model_name)
    if not rec_path.exists():
        return None
    run = json.loads(rec_path.read_text())
    if run.get("infra_error"):
        return None
    grade = {"instance_id": instance_id, "model": paths.resolve_model(model_name),
             "graded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "exit_status": run.get("exit_status")}
    if run.get("exit_status") != "Submitted":
        grade.update(grade="fail", tests_run=False,
                     error=f"not submitted (run ended: {run.get('exit_status')})")
        print(f"  FAIL — {grade['error']}")
    else:
        for attempt in (1, 2):
            result = evaluate_patch(instance_id, model_name, timeout=timeout)
            if not result.get("infra_error"):
                break
            print(f"  grading attempt {attempt}: infrastructure error: {result['infra_error']}")
        result.pop("model", None)
        grade.update(result, tests_run=result.get("patch_applied", False))
        if result.get("infra_error"):
            grade["grade"] = "not_run"
    paths.write_json(paths.grade_path(instance_id, model_name), grade)
    return grade


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
    p.add_argument("--timeout", type=int, default=1800,
                   help="seconds for the test run (default: 1800)")
    p.add_argument("--regrade", action="store_true",
                   help="grade again runs that already have a grade.json")
    p.add_argument("--output",
                   help="also write a summary of this grading here (default: "
                        "<eval dir>/results-<time>.json)")
    p.add_argument("--dry-run", action="store_true",
                   help="list the runs that would be graded and exit")
    args = p.parse_args()

    try:
        models = [paths.resolve_model(m) for m in args.model]
    except paths.UnknownModelError as e:
        sys.exit(f"ERROR: {e}")
    instances = select_instances(args)

    pairs = [(i, m) for m in models for i in instances]
    todo = [(i, m) for i, m in pairs if paths.run_record_path(i, m).exists()
            and (args.regrade or not paths.grade_path(i, m).exists())]
    print(f"Instances : {len(instances)}")
    print(f"Models    : {', '.join(models)}")
    print(f"Pairs     : {len(pairs)} ({len(todo)} to grade)\n")

    if args.dry_run:
        for i, m in todo:
            print(f"  {paths.show(paths.run_dir(i, m))}")
        return

    results = []
    for n, (instance_id, model_name) in enumerate(todo, 1):
        print(f"[{n}/{len(todo)}] {instance_id}  x  {model_name}")
        g = grade_run(instance_id, model_name, timeout=args.timeout)
        if g is None:
            print("  not graded: the run ended in an infrastructure error")
        else:
            results.append(g)
        print()

    print("=" * 74)
    print("RESULTS")
    print("=" * 74)
    for r in results:
        icon = {"pass": "PASS", "not_run": "----"}.get(r["grade"], "FAIL")
        err = f"  ({r['error']})" if r.get("error") else ""
        print(f"  [{icon}] {r['model']:34s} {r['instance_id']:24s} "
              f"{r.get('elapsed_seconds', 0):>6.1f}s{err}")

    graded = [r for r in results if r["grade"] != "not_run"]
    passed = sum(1 for r in graded if r["grade"] == "pass")
    print(f"\n  {passed}/{len(graded)} graded runs pass"
          + (f"   ({len(results) - len(graded)} not run)" if len(graded) < len(results) else ""))

    out = (paths.layout.base() / args.output if args.output else
           paths.OUTPUTS_DIR / f"results-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models": models,
        "total": len(results),
        "graded": len(graded),
        "passed": passed,
        "results": results,
    }, indent=2))
    print(f"  written to {paths.show(out)}")


if __name__ == "__main__":
    main()
