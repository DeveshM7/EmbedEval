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

Mynewt runs submit one phase per job instead, each on the nodes it needs:

    python cluster/submit.py triage   --run R             # GPU partition, the model
    python cluster/submit.py validate --run R --after J   # slurm.build_partition (x86)
    python cluster/submit.py evaluate --run R --after J   # an agent on each VALID instance
    python cluster/submit.py evaluate --run R --model M   # M: a triage/models.json profile

--after J starts the job once job J has ended (a chain needs no slot until
then). evaluate with a local model (evaluate.model in triage/models.json)
also submits the model's server on the GPU partition, unless --server-job
names one already running; the evaluate job reaches it over an SSH tunnel and
ends it when done. Each Mynewt job runs a frozen copy of the code
(<run>/jobs/source-<time>/, with its sha256 manifest), so editing the
checkout never changes a job that is queued or running.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CLUSTER = Path(__file__).resolve().parent
REPO = CLUSTER.parent
# The job's phases share its time limit: triage may take all of it,
# validation what triage leaves, if at least VALIDATE_MIN (less is not worth
# starting: a PR takes up to caps.validate_pr). REPORT_MIN is kept for the
# report and for srun's kill grace.
REPORT_MIN = 8
VALIDATE_MIN = 30
sys.path.insert(0, str(CLUSTER))
import config as cluster_config  # noqa: E402
import status  # noqa: E402
sys.path.insert(0, str(REPO / "harness"))
import paths as harness_paths  # noqa: E402


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


# What a Mynewt job runs: the code, frozen per job (freeze_source).
SOURCE_DIRS = ("cluster", "scripts", "harness", "templates", "docs")
SOURCE_FILES = ("triage/models.json", "triage/mynewt_gold.json", "requirements.txt")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".venv", "runs", "generated", "outputs"}


def server_export(cfg: dict, run: Path, stamp: str) -> str:
    """sbatch --export for a model-server job: only what the server script
    reads, as the triage phase gives it (phase.sbatch's `env -i`). Everything
    else from the submitting shell stays out: the login node's PATH finds an
    x86 python3 that cannot run on the ARM GPU node."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ["HOME"],
           "USER": os.environ["USER"], "LANG": "C.UTF-8", "PORT": "8000",
           # its own compile cache: never one another server is writing into
           "EMBEDEVAL_VLLM_CACHE": str(run / "jobs" / f"vllm-cache-{stamp}"),
           **cluster_config.env_for(cfg)}
    if any("," in v for v in env.values()):
        sys.exit(f"a server environment value holds a comma (sbatch --export splits on it): {env}")
    return ",".join(f"{k}={v}" for k, v in env.items())


def freeze_source(source: Path, destination: Path) -> dict:
    """Copy the code a job runs and record the sha256 of every file."""
    destination.mkdir(parents=True, exist_ok=False)
    files = {}
    paths = [p for root in SOURCE_DIRS for p in (source / root).rglob("*")]
    paths += [source / name for name in SOURCE_FILES if (source / name).exists()]
    for src in sorted(paths):
        rel = src.relative_to(source)
        if (not src.is_file() or src.is_symlink() or any(part in SKIP_DIRS for part in rel.parts)
                or rel == Path("cluster/config.yaml") or src.suffix in (".pyc", ".pyo")):
            continue
        data = src.read_bytes()
        (destination / rel).parent.mkdir(parents=True, exist_ok=True)
        (destination / rel).write_bytes(data)
        shutil.copymode(src, destination / rel)
        files[rel.as_posix()] = hashlib.sha256(data).hexdigest()
    git = lambda *a: subprocess.run(["git", "-C", str(source), *a], capture_output=True,
                                    text=True).stdout.strip()
    manifest = {"revision": git("rev-parse", "HEAD") or None, "dirty": bool(git("status", "--porcelain")),
                "files": files, "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
    (destination / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def submit_phase(args, cfg: dict, run: Path) -> None:
    """One Mynewt phase as its own job (see the module docstring)."""
    s, par, mode = cfg["slurm"], cfg["parallel"], args.mode
    if mode == "pipeline":
        sys.exit("Mynewt runs submit each phase as its own job: triage, then validate "
                 "(and evaluate), each with --after the one before")
    if mode == "evaluate":
        if args.model:
            cfg["evaluate"]["model"] = args.model
        try:
            harness_paths.resolve_model(cfg["evaluate"]["model"])
        except harness_paths.UnknownModelError as e:
            sys.exit(str(e))
    model_phase = mode == "triage"
    partition = s["partition"] if model_phase else s["build_partition"]
    if not partition:
        sys.exit("slurm.build_partition must name an x86 CPU partition (e.g. cpu)")
    if minutes(s["time_limit"]) > 240:
        sys.exit("Scholar jobs may not exceed four hours")
    name = f"embedeval-{run.name}-{mode}"
    q = subprocess.run(["squeue", "-h", "--me", "-n", name, "-o", "%i %T"], capture_output=True, text=True)
    live = [l for l in q.stdout.splitlines() if l.strip() and not l.endswith("COMPLETING")]
    if live and not args.after:
        sys.exit(f"run {run.name} already has a {mode} job ({'; '.join(live)}); wait for it, "
                 "scancel it, or chain this one with --after")
    todo = (status.evaluate_todo(run, status.eval_label(cfg["evaluate"])) if mode == "evaluate" else
            {"triage": status.triage_todo, "validate": status.validate_todo}[mode](run))
    if mode == "validate" and status.triage_todo(run) and not args.after:
        sys.exit(f"triage is not complete ({len(status.triage_todo(run))} PRs left): submit "
                 "validate --after the triage job")
    n = len(todo)
    if not n:
        if not args.after:
            print(f"nothing left to {mode} in {run.name}. Report: python cluster/report.py --run {run.name}")
            return
        n = sum(1 for e in status.manifest(run)["prs"] if e["stage"] in ("to triage", "to validate"))
        print(f"  --after {args.after}: no {mode} work yet; sized for the run's {n} PRs")
    per = {"triage": par["triage_per_node"], "validate": par["validate_per_node"],
           "evaluate": cfg["evaluate"]["per_node"]}[mode]
    idle = args.idle if args.idle is not None else idle_nodes(partition)
    lowest, wanted, note = plan_nodes(cfg, math.ceil(n / per), idle)

    stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}"   # two submissions may come within a second
    jobs = run / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    source = jobs / f"source-{stamp}"
    frozen_src = freeze_source(REPO, source)
    deps = [f"afterany:{args.after}"] if args.after else []
    if mode == "evaluate":
        local = cfg["evaluate"]["model"] in harness_paths.LOCAL_PROFILES
        server = args.server_job
        if local and not server and not args.dry_run:
            r = subprocess.run(["sbatch", "--parsable", "-p", s["partition"], "-J", f"{name}-server",
                                f"--mem={s['mem']}", f"--cpus-per-task={s['cpus_per_node']}",
                                f"--gres={s['gres']}", "--export=" + server_export(cfg, run, stamp),
                                "-o", str(jobs / "server-%j.log"), "-t", s["time_limit"],
                                *([f"--dependency={deps[0]}"] if deps else []),
                                str(source / "scripts" / "serve_triage_model.sh"), cfg["evaluate"]["model"]],
                               capture_output=True, text=True, cwd=source)
            if r.returncode:
                sys.exit(f"sbatch of the model server failed: {r.stderr.strip()}")
            server = r.stdout.strip().split(";")[0]
            cfg["evaluate_server_owned"] = True       # the evaluate job ends it when done
            print(f"  model server: job {server} ({cfg['evaluate']['model']}, {s['partition']})")
        if server:
            cfg["evaluate_server_job"] = server
            deps.append(f"after:{server}")
    frozen = jobs / f"config-{stamp}.json"
    frozen.write_text(json.dumps(cfg, indent=2) + "\n")
    python = (s["triage_python"] if model_phase else s["build_python"]) or sys.executable
    script = jobs / f"{mode}-{stamp}.sbatch"
    script.write_text((CLUSTER / "templates" / "phase.sbatch").read_text().format(
        partition=partition, nodes=f"{lowest}-{wanted}" if lowest < wanted else wanted,
        cpus=s["cpus_per_node"] if model_phase else s["build_cpus_per_node"],
        gres_line=f"#SBATCH --gres={s['gres']}" if model_phase else "",
        mem=s["mem"] if model_phase else s["build_mem"], time_limit=s["time_limit"],
        name=name, run=run, repo=source, config=frozen, python=python, phase=mode,
        limit_min=minutes(s["time_limit"]), report_min=REPORT_MIN, stamp=stamp,
        # the model server this job started serves it alone (a later job starts its own):
        # it ends with the workers, not at its own time limit, holding a job slot
        end_server=(f"scancel {cfg['evaluate_server_job']}\n"
                    if cfg.get("evaluate_server_owned") else "")))
    print(f"run {run.name}: {len(todo)} PRs to {mode}")
    print(f"  nodes: {lowest}-{wanted} on {partition} ({idle} idle; max {s['nodes_max']})"
          + (f"\n  note: {note}" if note else ""))
    print(f"  job script: {script}\n  source: {source} (sha256 {frozen_src['sha256'][:16]})")
    if args.dry_run:
        print("  --dry-run: not submitted")
        return
    r = subprocess.run(["sbatch", "--parsable", *([f"--dependency={','.join(deps)}"] if deps else []),
                        str(script)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"sbatch failed: {r.stderr.strip()}")
    job = r.stdout.strip().split(";")[0]
    with open(jobs / "submitted.jsonl", "a") as fh:
        fh.write(json.dumps({"job": job, "script": script.name, "submitted": stamp, "mode": mode,
                             "nodes": f"{lowest}-{wanted}", "after": args.after,
                             "server": cfg.get("evaluate_server_job"), "todo": len(todo),
                             "model": cfg["evaluate"]["model"] if mode == "evaluate" else None,
                             "source_sha256": frozen_src["sha256"]}) + "\n")
    print(f"  submitted: job {job}; log {jobs}/job-{job}.out")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["pipeline", "triage", "validate", "evaluate"])
    p.add_argument("--run", required=True, help="run name (folder under runs/) or path")
    p.add_argument("--config", help="default: cluster/config.yaml")
    p.add_argument("--dry-run", action="store_true", help="write the job script, do not submit")
    p.add_argument("--idle", type=int, help="assume this many idle nodes (testing)")
    p.add_argument("--after", metavar="JOB", help="Mynewt: start once this job has ended")
    p.add_argument("--model", help="Mynewt evaluate: the model (a triage/models.json profile, or "
                   "a hosted model harness/paths.py knows); default evaluate.model")
    p.add_argument("--server-job", metavar="JOB", help="Mynewt evaluate: a running model-server "
                   "job (scripts/serve_triage_model.sh) to use instead of submitting one")
    args = p.parse_args()

    cfg = cluster_config.load_or_exit(args.config)
    run = Path(args.run) if "/" in args.run else Path(cfg["derived"]["runs"]) / args.run
    if not (run / "manifest.json").exists():
        sys.exit(f"{run} is not a prepared run (no manifest.json); run cluster/prepare.py first")
    if status.project(run) == "mynewt":
        return submit_phase(args, cfg, run)
    if args.mode != "pipeline":
        sys.exit("Zephyr runs use pipeline mode")

    s, par = cfg["slurm"], cfg["parallel"]
    if minutes(s["time_limit"]) < REPORT_MIN + VALIDATE_MIN:
        sys.exit(f"slurm.time_limit {s['time_limit']} is too short for a phase")

    # One job per run at a time: the nodes of a job are numbered, and two jobs
    # would share nodes/node<N>/. (Within a job, the queue balances the work.)
    # A job left COMPLETING has no workers (a dead node can hold it there for
    # hours), so it does not count.
    q = subprocess.run(["squeue", "-h", "--me", "-n", f"embedeval-{run.name}", "-o", "%i %T"],
                       capture_output=True, text=True)
    live = [l for l in q.stdout.splitlines() if l.strip() and not l.endswith("COMPLETING")]
    if live:
        sys.exit(f"run {run.name} already has a job ({'; '.join(live)}); wait for it to "
                 "end, or scancel it, then submit again")

    triage_left, validate_left = status.triage_todo(run), status.validate_todo(run)
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
        limit_min=minutes(s["time_limit"]), report_min=REPORT_MIN, validate_min=VALIDATE_MIN,
        stamp=stamp))

    print(f"run {run.name}: {len(triage_left)} PRs to triage, {len(validate_left)} to validate")
    print(f"  nodes: {lowest}-{wanted} (work needs {work}; {idle} idle; max {s['nodes_max']})"
          + (f"\n  note: {note}" if note else ""))
    print(f"  limit: {s['time_limit']}; triage may use all of it, validation what is left "
          f"(if {VALIDATE_MIN}+ min), less {REPORT_MIN} min for the report")
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
