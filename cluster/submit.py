#!/usr/bin/env python3
"""
Submit a prepared run to Slurm.

    python cluster/submit.py pipeline --run 2025-09            # triage + validation
    python cluster/submit.py pipeline --run 2025-09 --dry-run  # show the plan, submit nothing

Reads the run's manifest and status, works out what is still to do (so the
same command resumes a run that was cut short), and chooses the node count:

    wanted   enough nodes for the work: triage runs parallel.triage_per_node
             PRs per node; never more than slurm.nodes_max
    minimum  slurm.nodes_min, lowered to the nodes idle right now rather
             than waiting for more (sinfo)

It writes <run>/jobs/pipeline-<time>.sbatch from cluster/templates/, with a
frozen copy of the settings beside it, and submits it with `-N min-wanted`:
Slurm starts the job on as many idle nodes as it can, up to the wanted
number. One job, a hard time limit, nothing resubmitted automatically.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CLUSTER = Path(__file__).resolve().parent
REPO = CLUSTER.parent
sys.path.insert(0, str(CLUSTER))
import config as cluster_config  # noqa: E402
import status  # noqa: E402


def minutes(hhmmss: str) -> int:
    parts = [int(x) for x in hhmmss.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 60 + m + (s > 0)


def idle_nodes(partition: str) -> int:
    r = subprocess.run(["sinfo", "-p", partition, "-h", "-t", "idle", "-o", "%D"],
                       capture_output=True, text=True)
    return sum(int(x) for x in r.stdout.split() if x.isdigit())


def plan_nodes(cfg: dict, work_nodes: int, idle: int) -> tuple[int, int, str]:
    s = cfg["slurm"]
    wanted = max(1, min(s["nodes_max"], work_nodes))
    lowest = min(s["nodes_min"], wanted)
    note = ""
    if idle < lowest:
        if max(1, idle) < lowest:
            note = f"only {idle} node(s) idle: minimum lowered from {lowest} to {max(1, idle)}"
            lowest = max(1, idle)
        if idle == 0:
            note = (note + "; " if note else "") + "no node idle: the job will queue until one is free"
    return lowest, wanted, note


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["pipeline"])
    p.add_argument("--run", required=True, help="run name (folder under runs/) or path")
    p.add_argument("--config", help="default: cluster/config.yaml")
    p.add_argument("--dry-run", action="store_true", help="write the job script, do not submit")
    p.add_argument("--idle", type=int, help="assume this many idle nodes (testing)")
    args = p.parse_args()

    cfg = cluster_config.load_or_exit(args.config)
    run = Path(args.run) if "/" in args.run else Path(cfg["derived"]["runs"]) / args.run
    if not (run / "manifest.json").exists():
        sys.exit(f"{run} is not a prepared run (no manifest.json); run cluster/prepare.py first")

    s, caps, par = cfg["slurm"], cfg["caps"], cfg["parallel"]
    if caps["triage_phase"] + caps["validate_phase"] + 5 > minutes(s["time_limit"]):
        sys.exit(f"caps.triage_phase + caps.validate_phase ({caps['triage_phase']} + "
                 f"{caps['validate_phase']} min) leave no time for the report within "
                 f"slurm.time_limit {s['time_limit']}")

    triage_left, validate_left = status.triage_todo(run), status.validate_todo(run)
    if cfg["project"] == "nuttx" and not cfg["nuttx"]["validate"]:
        validate_left = []
    if not triage_left and not validate_left:
        print(f"nothing left to do in {run.name}: every PR has an outcome. "
              f"Report: python cluster/report.py --run {run.name}")
        return
    work = max(math.ceil(len(triage_left) / par["triage_per_node"]),
               math.ceil(len(validate_left) / par["validate_per_node"]))
    idle = args.idle if args.idle is not None else idle_nodes(s["partition"])
    lowest, wanted, note = plan_nodes(cfg, work, idle)

    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    jobs = run / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    frozen = jobs / f"config-{stamp}.json"
    frozen.write_text(json.dumps(cfg, indent=2) + "\n")
    script = jobs / f"pipeline-{stamp}.sbatch"
    script.write_text((CLUSTER / "templates" / "pipeline.sbatch").read_text().format(
        partition=s["partition"], nodes=f"{lowest}-{wanted}" if lowest < wanted else wanted,
        cpus=s["cpus_per_node"], gres=s["gres"], mem=s["mem"], time_limit=s["time_limit"],
        name=run.name, run=run, repo=REPO, config=frozen, python=sys.executable,
        triage_phase=caps["triage_phase"], validate_phase=caps["validate_phase"], stamp=stamp))

    print(f"run {run.name}: {len(triage_left)} PRs to triage, {len(validate_left)} to validate")
    print(f"  nodes: {lowest}-{wanted} (work needs {work}; {idle} idle; max {s['nodes_max']})"
          + (f"\n  note: {note}" if note else ""))
    print(f"  limit: {s['time_limit']}; phase caps: triage {caps['triage_phase']} min, "
          f"validation {caps['validate_phase']} min")
    print(f"  job script: {script}")
    forecast = subprocess.run(["sbatch", "--test-only", str(script)], capture_output=True, text=True)
    lines = (forecast.stderr or forecast.stdout).strip().splitlines()
    print(f"  forecast: {lines[-1] if lines else '?'}")
    if args.dry_run:
        print("  --dry-run: not submitted")
        return
    r = subprocess.run(["sbatch", "--parsable", str(script)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"sbatch failed: {r.stderr.strip()}")
    job = r.stdout.strip().split(";")[0]
    with open(jobs / "submitted.jsonl", "a") as fh:
        fh.write(json.dumps({"job": job, "script": script.name, "submitted": stamp,
                             "nodes": f"{lowest}-{wanted}", "triage": len(triage_left),
                             "validate": len(validate_left)}) + "\n")
    print(f"  submitted: job {job}; log {jobs}/job-{job}.out")


if __name__ == "__main__":
    main()
