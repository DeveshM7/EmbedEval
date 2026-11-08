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

A NuttX run runs each phase as its own job, chained with --after: an
apps-PR run triage and validate, a run of existing instances (prepare.py
--instances) statement, validate and evaluate:

    python cluster/submit.py triage   --run R                         # GPU nodes, the model
    python cluster/submit.py statement --run R                        # GPU nodes, the model
    python cluster/submit.py validate --run R --arch aarch64 --after J
    python cluster/submit.py validate --run R --arch x86_64  --after J  # slurm.build_partition
    python cluster/submit.py evaluate --run R --arch aarch64 --model qwen38-nvfp4
    python cluster/submit.py evaluate --run R --arch x86_64  --model qwen38-nvfp4 --after J

--arch takes the instances that validate on that architecture (manifest
"arch"); an apps PR's pairs go to either (node_worker.py records an ARM64
pair that needs x86 as needs_x86). --after J starts the job once job J has ended. evaluate takes the
model per run (--model, default evaluate.model): a profile of
triage/models.json is served by each GPU node itself, or, for x86 nodes, by a
model-server job submitted on the GPU partition (--server-job: one already
running), which the evaluate job ends when done. Each phase job runs a frozen
copy of the code (<run>/jobs/source-<time>/) and checks its sha256 list
before it starts, so editing the checkout never changes a queued job.
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


# What a phase job runs: the code, frozen per job (freeze_source).
SOURCE_DIRS = ("cluster", "scripts", "harness", "templates", "docs")
SOURCE_FILES = ("triage/models.json", "triage/nuttx_apps_gold.json", "requirements.txt")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".venv", "runs", "generated", "outputs", "tests"}
REPORT_MIN = 5          # minutes a phase job keeps for its report
SERVER_GRACE_MIN = 30   # minutes an x86 evaluate job's server waits for it, once ready


def freeze_source(source: Path, destination: Path) -> dict:
    """Copy the code a job runs and record the sha256 of every file
    (source-manifest.json, and source-sha256.txt, which the job checks before
    it starts)."""
    destination.mkdir(parents=True, exist_ok=False)
    files = {}
    paths = [p for root in SOURCE_DIRS for p in (source / root).rglob("*")]
    paths += [source / name for name in SOURCE_FILES if (source / name).exists()]
    for src in sorted(paths):
        rel = src.relative_to(source)
        if (not src.is_file() or src.is_symlink() or any(part in SKIP_DIRS for part in rel.parts)
                # the user's settings (config.yaml, config-*.yaml): the job reads its frozen copy
                or (rel.parent == Path("cluster") and rel.suffix == ".yaml" and rel.name != "config.example.yaml")
                or src.suffix in (".pyc", ".pyo")):
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
    (destination / "source-sha256.txt").write_text("".join(f"{h}  {f}\n" for f, h in sorted(files.items())))
    return manifest


def server_export(cfg: dict, run: Path, stamp: str) -> str:
    """sbatch --export for a model-server job: only what the server script
    reads. Everything else from the submitting shell stays out: the login
    node's PATH finds an x86 python3 that cannot run on the ARM GPU node."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ["HOME"],
           "USER": os.environ["USER"], "LANG": "C.UTF-8", "PORT": "8000",
           # its own compile cache: never one another server is writing into
           "EMBEDEVAL_VLLM_CACHE": str(run / "jobs" / f"vllm-cache-{stamp}"),
           "EMBEDEVAL_SCRATCH": str(run / "jobs" / f"server-scr-{stamp}"),
           **cluster_config.env_for(cfg)}
    if any("," in v for v in env.values()):
        sys.exit(f"a server environment value holds a comma (sbatch --export splits on it): {env}")
    # only these variables (and Slurm's own): no ALL
    return ",".join(f"{k}={v}" for k, v in env.items())


def phase_todo(run: Path, mode: str, arch: str, cfg: dict) -> list[int]:
    """The phase's work on `arch`: entries without an "arch" (apps PRs to
    triage or validate) go to either."""
    todo = (status.evaluate_todo(run, status.eval_label(cfg["evaluate"])) if mode == "evaluate" else
            {"triage": status.triage_todo, "statement": status.statement_todo,
             "validate": status.validate_todo}[mode](run))
    if mode in ("triage", "statement"):          # the model's work: any instance
        return todo
    entries = {e["pr"]: e for e in status.manifest(run)["prs"]}
    return [pr for pr in todo if entries[pr].get("arch", arch) == arch]


def submit_phase(args, cfg: dict, run: Path) -> None:
    """One NuttX phase as its own job (see the module docstring)."""
    s, caps, mode = cfg["slurm"], cfg["caps"], args.mode
    arch = "aarch64" if mode in ("triage", "statement") else args.arch
    if not arch:
        sys.exit(f"{mode} needs --arch aarch64 or --arch x86_64 (instances validate on one)")
    x86 = arch == "x86_64"
    if x86 and not s["build_partition"]:
        sys.exit("slurm.build_partition must name an x86 CPU partition (e.g. cpu)")
    partition = s["build_partition"] if x86 else s["partition"]
    if minutes(s["time_limit"]) > 240:
        sys.exit("Scholar jobs may not exceed four hours")
    phase_min = caps["triage_phase"] if mode in ("triage", "statement") else caps["validate_phase"]
    if phase_min + REPORT_MIN > minutes(s["time_limit"]):
        sys.exit(f"the {mode} phase cap ({phase_min} min) leaves no time for the report "
                 f"within slurm.time_limit {s['time_limit']}")
    if mode == "evaluate":
        if args.model:
            cfg["evaluate"]["model"] = args.model
        model = cfg["evaluate"]["model"]
        sys.path.insert(0, str(REPO / "harness"))
        import paths as harness_paths
        try:
            harness_paths.resolve_model(model)
        except harness_paths.UnknownModelError as e:
            sys.exit(str(e))
    name = f"embedeval-{run.name}-{mode}-{arch}"
    q = subprocess.run(["squeue", "-h", "--me", "-n", name, "-o", "%i %T"], capture_output=True, text=True)
    live = [l for l in q.stdout.splitlines() if l.strip() and not l.endswith("COMPLETING")]
    if live and not args.after:
        sys.exit(f"run {run.name} already has a {mode} job on {arch} ({'; '.join(live)}); wait for it, "
                 "scancel it, or chain this one with --after")
    todo = phase_todo(run, mode, arch, cfg)
    n = len(todo)
    if not n:
        if not args.after:
            print(f"nothing left to {mode} on {arch} in {run.name}. "
                  f"Report: python cluster/report.py --run {run.name}")
            return
        n = sum(1 for e in status.manifest(run)["prs"] if e.get("arch", arch) == arch)
        print(f"  --after {args.after}: no {mode} work yet; sized for the run's {n} instances on {arch}")
    per = {"triage": cfg["parallel"]["triage_per_node"], "statement": cfg["parallel"]["triage_per_node"], "validate": cfg["parallel"]["validate_per_node"],
           "evaluate": cfg["evaluate"]["per_node"]}[mode]
    idle = args.idle if args.idle is not None else idle_nodes(partition)
    lowest, wanted, note = plan_nodes(cfg, math.ceil(n / per), idle)
    if mode == "evaluate" and x86 and not args.server_job and \
            cfg["evaluate"]["model"] in harness_paths.LOCAL_PROFILES:
        lowest = wanted        # every node gets a server of its own (server.sbatch)

    stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}"   # two submissions may come within a second
    jobs = run / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    source = jobs / f"source-{stamp}"
    frozen_src = freeze_source(REPO, source)
    deps = [f"afterany:{args.after}"] if args.after else []
    # x86 nodes have no GPU: a local model is served by a job of its own on the
    # GPU partition (server.sbatch), which this evaluate job cancels when done.
    own_server = (mode == "evaluate" and x86 and not args.server_job
                  and cfg["evaluate"]["model"] in harness_paths.LOCAL_PROFILES)
    if args.server_job:
        cfg["evaluate_server_job"] = args.server_job
        deps.append(f"after:{args.server_job}")
    frozen = jobs / f"config-{stamp}.json"
    frozen.write_text(json.dumps(cfg, indent=2) + "\n")
    python = (s["build_python"] if x86 else s["triage_python"]) or sys.executable
    extra_path = s["build_path"] if x86 else s["triage_path"]
    script = jobs / f"{mode}-{arch}-{stamp}.sbatch"
    script.write_text((CLUSTER / "templates" / "phase.sbatch").read_text().format(
        partition=partition, nodes=f"{lowest}-{wanted}" if lowest < wanted else wanted,
        cpus=s["build_cpus_per_node"] if x86 else s["cpus_per_node"],
        # Scholar's GPU partitions take only jobs that request a GPU
        gres_line="" if x86 or not s["gres"] else f"#SBATCH --gres={s['gres']}\n",
        mem=s["build_mem"] if x86 else s["mem"], time_limit=s["time_limit"],
        name=name, run=run, repo=source, config=frozen, python=python, phase=mode,
        phase_min=phase_min, stamp=stamp, path_prefix=f"{extra_path}:" if extra_path else "",
        # the server this job started serves it alone; it ends with the
        # workers rather than hold a job slot until its own time limit
        end_server=(f"scancel $(cat {jobs}/server-of-$SLURM_JOB_ID 2>/dev/null)\n" if own_server else "")))
    print(f"run {run.name}: {len(todo)} instances to {mode} on {arch}")
    print(f"  nodes: {lowest}-{wanted} on {partition} ({idle} idle; max {s['nodes_max']})"
          + (f"\n  note: {note}" if note else ""))
    print(f"  job script: {script}\n  source: {source} (sha256 {frozen_src['sha256'][:16]})")
    if args.dry_run:
        print("  --dry-run: not submitted")
        return
    # With its own server, the evaluate job is submitted held, so the server
    # job can name it; it is released once its config names the server.
    r = subprocess.run(["sbatch", "--parsable", *(["--hold"] if own_server else []),
                        *([f"--dependency={','.join(deps)}"] if deps else []),
                        str(script)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"sbatch failed: {r.stderr.strip()}")
    job = r.stdout.strip().split(";")[0]
    if own_server:
        server_script = jobs / f"server-{arch}-{stamp}.sbatch"
        server_script.write_text((CLUSTER / "templates" / "server.sbatch").read_text().format(
            partition=s["partition"], cpus=s["cpus_per_node"], gres=s["gres"], mem=s["mem"],
            time_limit=s["time_limit"], name=f"{name}-server", jobs=jobs, model=cfg["evaluate"]["model"],
            eval_job=job, stamp=stamp, source=source, grace=SERVER_GRACE_MIN, server_nodes=wanted,
            warm_cache=cfg["derived"]["vllm_cache"]))
        r = subprocess.run(["sbatch", "--parsable", "--export=" + server_export(cfg, run, stamp),
                            *([f"--dependency={deps[0]}"] if args.after else []), str(server_script)],
                           capture_output=True, text=True, cwd=source)
        if r.returncode:
            subprocess.run(["scancel", job])
            sys.exit(f"sbatch of the model server failed: {r.stderr.strip()}; evaluate job {job} cancelled")
        server = r.stdout.strip().split(";")[0]
        cfg["evaluate_server_job"] = server
        frozen.write_text(json.dumps(cfg, indent=2) + "\n")       # the workers read it at start
        (jobs / f"server-of-{job}").write_text(server + "\n")
        deps.append(f"after:{server}")
        u = subprocess.run(["scontrol", "update", f"JobId={job}", f"Dependency={','.join(deps)}"],
                           capture_output=True, text=True)
        rel = subprocess.run(["scontrol", "release", job], capture_output=True, text=True)
        if u.returncode or rel.returncode:
            subprocess.run(["scancel", job, server])
            sys.exit(f"could not chain evaluate job {job} to server job {server}: "
                     f"{(u.stderr + rel.stderr).strip()}; both cancelled")
        print(f"  model server: job {server} ({cfg['evaluate']['model']}, {s['partition']}; "
              f"ends {SERVER_GRACE_MIN} min after the model answers if job {job} cannot start)")
    with open(jobs / "submitted.jsonl", "a") as fh:
        fh.write(json.dumps({"job": job, "script": script.name, "submitted": stamp, "mode": mode,
                             "arch": arch, "nodes": f"{lowest}-{wanted}", "after": args.after,
                             "server": cfg.get("evaluate_server_job"), "todo": len(todo),
                             "model": cfg["evaluate"]["model"] if mode == "evaluate" else None,
                             "source_sha256": frozen_src["sha256"]}) + "\n")
    print(f"  submitted: job {job}; log {jobs}/job-{job}.out")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["pipeline", "triage", "statement", "validate", "evaluate"])
    p.add_argument("--run", required=True, help="run name (folder under runs/) or path")
    p.add_argument("--config", help="default: cluster/config.yaml")
    p.add_argument("--dry-run", action="store_true", help="write the job script, do not submit")
    p.add_argument("--idle", type=int, help="assume this many idle nodes (testing)")
    p.add_argument("--after", metavar="JOB", help="phase jobs: start once this job has ended")
    p.add_argument("--arch", choices=("aarch64", "x86_64"),
                   help="validate / evaluate: the instances that validate on this architecture")
    p.add_argument("--model", help="evaluate: the model (a triage/models.json profile, or a "
                   "hosted model harness/paths.py knows); default evaluate.model")
    p.add_argument("--server-job", metavar="JOB", help="evaluate on x86_64: a running model-server "
                   "job (scripts/serve_triage_model.sh) to use instead of submitting one")
    args = p.parse_args()

    cfg = cluster_config.load_or_exit(args.config)
    run = Path(args.run) if "/" in args.run else Path(cfg["derived"]["runs"]) / args.run
    if not (run / "manifest.json").exists():
        sys.exit(f"{run} is not a prepared run (no manifest.json); run cluster/prepare.py first")
    if args.mode != "pipeline":
        if cfg["project"] != "nuttx":
            sys.exit(f"{args.mode} jobs are for NuttX runs; Zephyr runs use pipeline")
        if args.mode in ("statement", "evaluate") and \
                not any(e.get("instance") for e in status.manifest(run)["prs"]):
            sys.exit(f"{args.mode} jobs are for runs of instances (prepare.py --instances)")
        return submit_phase(args, cfg, run)

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
        cpus=s["cpus_per_node"], gres_line=f"#SBATCH --gres={s['gres']}\n" if s["gres"] else "", mem=s["mem"], time_limit=s["time_limit"],
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
