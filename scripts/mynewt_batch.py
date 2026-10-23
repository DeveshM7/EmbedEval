#!/usr/bin/env python3
"""Generate, render, build and validate many Mynewt instances in one allocation.

    # reviewed triage verdicts (triage/mynewt_reviewed/<pr>.json) -> instances
    python scripts/mynewt_batch.py --run-dir $SCRATCH/mynewt-run --prs 2809 3299 3680
    python scripts/mynewt_batch.py --run-dir RUN --reviewed triage/mynewt_reviewed   # all of them

    # existing Docker instance folders, re-rendered for Apptainer
    python scripts/mynewt_batch.py --run-dir RUN --from-docker mynewt__mynewt-2809

    # several nodes of one job: one worker per node, each takes its share
    srun --ntasks-per-node=1 python scripts/mynewt_batch.py --run-dir RUN --reviewed ...

Per instance: generate_instance.py --repo mynewt --runtime apptainer (or
render_mynewt_instance.py), build_instances.py, validate_instance.py, then
cleanup_instance.py. Everything is under RUN (scripts/layout.py): instances in
RUN/instances, images and logs in RUN/work. RUN/results/<id>.json says what
happened:

    status   valid | invalid | generate_error | build_env | timeout
    stage    the last stage reached: generate | build | validate
    reason   why (the validator's reason, or the failing step's last lines)
    before / after   the validator's test inventories (passed, failed, ...)
    minutes, host, finished_at

A finished instance (a results file) is skipped on a rerun; --force redoes it.
Set EMBEDEVAL_MYNEWT_CLONE to a full mynewt-core clone: generation, the ARM64
port patch, the image build and the fix diff all read from it.
Instances are built and validated on the host's own architecture (x86_64
primary; aarch64 works through the per-era sim_arm64.patch).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run_step(cmd: list[str], logfile: Path, minutes: float, env: dict) -> int:
    """Run one step in its own process group; 124 on timeout (group killed)."""
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "a") as fh:
        fh.write(f"\n$ {' '.join(cmd)}\n")
        fh.flush()
        p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=REPO_ROOT,
                             env=env, start_new_session=True)
        try:
            return p.wait(timeout=max(minutes, 1) * 60)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    pass
                time.sleep(5)
            p.wait()
            return 124


def tail(logfile: Path, n: int = 12) -> str:
    try:
        return "\n".join(logfile.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def work_one(source: str, kind: str, run: Path, args, env: dict) -> dict:
    """kind: "pr" (source is a reviewed verdict path) or "docker" (an instance id)."""
    t0 = time.time()
    if kind == "pr":
        pr = int(Path(source).stem)
        iid = f"mynewt__mynewt-{pr}"
    else:
        iid = source
    lg = run / "logs" / f"{iid}.log"
    res = {"instance_id": iid, "host": platform.machine(), "status": "generate_error",
           "stage": "generate", "reason": "", "log": str(lg)}

    def done(status: str, stage: str, reason: str) -> dict:
        run_step([PY, "scripts/cleanup_instance.py", iid], lg, 10, env)   # frees the image
        res.update(status=status, stage=stage, reason=reason,
                   minutes=round((time.time() - t0) / 60, 1),
                   finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        (run / "results").mkdir(parents=True, exist_ok=True)
        (run / "results" / f"{iid}.json").write_text(json.dumps(res, indent=2) + "\n")
        return res

    inst = run / "instances" / iid
    run_step([PY, "scripts/cleanup_instance.py", iid], lg, 10, env)    # leftovers of a cut-short try
    if inst.exists():
        subprocess.run(["rm", "-rf", str(inst)])
    if kind == "pr":
        cmd = [PY, "scripts/generate_instance.py", "--repo", "mynewt", "--runtime", "apptainer",
               "--pr", str(pr), "--triage", source]
    else:
        d = REPO_ROOT / "docker" / "instances" / iid
        cmd = [PY, "scripts/render_mynewt_instance.py", "--metadata", str(d / "metadata.json"),
               "--test-patch", str(d / "test_patch.diff"), "--out", str(run / "instances")]
    rc = run_step(cmd, lg, 10, env)
    if rc:
        return done("timeout" if rc == 124 else "generate_error", "generate",
                    tail(lg) if rc != 124 else "generate hit the cap")
    res["stage"] = "build"
    rc = run_step([PY, "scripts/build_instances.py", "--instance", iid], lg, args.build_cap + 5,
                  {**env, "EMBEDEVAL_BUILD_TIMEOUT_MIN": str(args.build_cap)})
    if rc:
        return done("timeout" if rc == 124 else "build_env", "build",
                    "build hit the cap" if rc == 124 else tail(lg))
    res["stage"] = "validate"
    rc = run_step([PY, "scripts/validate_instance.py", iid, "--timeout", str(args.test_cap)],
                  lg, args.validate_cap, env)
    report = {}
    if (inst / "validation.json").exists():
        report = json.loads((inst / "validation.json").read_text())
    for key in ("before", "after", "baseline"):
        if key in report:
            res[key] = report[key]
    reason = report.get("reason") or tail(lg)
    if rc == 124:
        return done("timeout", "validate", "validation hit the cap")
    if rc == 0 and report.get("valid"):
        return done("valid", "validate", reason)
    # An image that was not there, or a crash of the validator itself, is the
    # environment's fault; everything else is the instance's.
    if not report or reason.startswith(("image not built", "error:")):
        return done("build_env", "validate", reason)
    return done("invalid", "validate", reason)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--reviewed", type=Path, help="folder of reviewed verdicts <pr>.json")
    src.add_argument("--from-docker", nargs="+", metavar="ID",
                     help="existing docker/instances ids to re-render for Apptainer")
    p.add_argument("--prs", nargs="+", type=int, help="with --reviewed: only these PRs")
    p.add_argument("--parallel", type=int, default=2, help="instances at once on this node")
    p.add_argument("--build-cap", type=int, default=60, help="minutes per image build")
    p.add_argument("--validate-cap", type=int, default=30, help="minutes per validation")
    p.add_argument("--test-cap", type=int, default=600, help="seconds per run_tests step")
    p.add_argument("--force", action="store_true", help="redo instances that have a result")
    args = p.parse_args()

    run = layout.run_dir()
    if run is None:
        p.error("--run-dir is required")
    if args.reviewed:
        files = sorted(args.reviewed.glob("*.json"), key=lambda f: int(f.stem))
        work = [(str(f), "pr") for f in files if not args.prs or int(f.stem) in args.prs]
    else:
        work = [(i, "docker") for i in args.from_docker]
    # Several workers (one per node of an srun) split the list by their rank.
    rank, size = int(os.environ.get("SLURM_PROCID", "0")), int(os.environ.get("SLURM_NTASKS", "1"))
    work = work[rank::size]
    todo = [(s, k) for s, k in work if args.force or not (
        run / "results" / f"mynewt__mynewt-{Path(s).stem if k == 'pr' else s.rsplit('-', 1)[1]}.json"
    ).exists()]
    env = {**os.environ, "EMBEDEVAL_RUN_DIR": str(run)}
    print(f"[{rank}/{size}] {len(todo)} of {len(work)} instances to do, host {platform.machine()}",
          flush=True)
    with ThreadPoolExecutor(args.parallel) as pool:
        for r in pool.map(lambda w: work_one(*w, run, args, env), todo):
            print(f"  {r['instance_id']}: {r['status']} ({r['stage']}, {r.get('minutes')} min)"
                  f" {r['reason'].splitlines()[0] if r['reason'] else ''}", flush=True)
    # Summary of every result in the run so far.
    counts: dict[str, int] = {}
    for f in (run / "results").glob("*.json"):
        s = json.loads(f.read_text())["status"]
        counts[s] = counts.get(s, 0) + 1
    print("results so far:", counts or "none", flush=True)


if __name__ == "__main__":
    main()
