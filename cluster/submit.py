#!/usr/bin/env python3
"""
Submit a prepared run to Slurm.

    python cluster/submit.py pipeline --run 2025-09            # triage + validation
    python cluster/submit.py pipeline --run 2025-09 --dry-run  # show the plan, submit nothing
    python cluster/submit.py evaluate --run R --model qwen38-nvfp4   # Contiki-NG: agents on R's VALID instances
    python cluster/submit.py evaluate --run R --model A B C --queue  # one evaluation per model, in a chain

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
also submits the model's servers on the GPU partition (a job of as many nodes
as the evaluate job has: one server per evaluate node, which reaches it over
an SSH tunnel), unless --server-job names one already running. The servers
end when the evaluate job is done, or 30 minutes after the model answers if
that job still has no slot. Each Mynewt job runs a frozen copy of the code
(<run>/jobs/source-<time>/, with its sha256 manifest), so editing the
checkout never changes a job that is queued or running.

A NuttX run runs each phase as its own job, chained with --after: an
apps-PR run triage and validate, a run of existing instances (prepare.py
--instances) statement, validate and evaluate:

    python cluster/submit.py triage   --run R                         # GPU nodes, the model
    python cluster/submit.py statement --run R                        # GPU nodes, the model
    python cluster/submit.py validate --run R --arch aarch64 --after J
    python cluster/submit.py validate --run R --arch x86_64  --after J  # slurm.build_partition

--arch takes the instances that validate on that architecture (manifest
"arch"); an apps PR's pairs go to either (node_worker.py records an ARM64
pair that needs x86 as needs_x86). Each phase job runs a frozen copy of the
code (<run>/jobs/source-<time>/) and checks its sha256 list before it starts,
so editing the checkout never changes a queued job.

Contiki-NG evaluate runs an agent (harness/run.py) on each VALID instance of
the run and grades its patch (harness/evaluate_patches.py), on the x86 build
partition. The model is the job's parameter (--model, a profile of
triage/models.json); the limits are the config's evaluate section, the same
for every model. --model may name several: one evaluation (an agents job and
its server) is submitted per model, each after the previous pair has ended,
so only one pair runs at a time (the account runs two jobs). Unless --server-job
names a running model server, the model is served by a job of its own on the
GPU partition (scripts/serve_triage_model.sh, with a clean environment), which starts only once the evaluate job runs (so it
never holds a job slot while the agents wait for one); the evaluate job waits
caps.server_start for it, reaches it through an SSH tunnel, and cancels it
when its workers are done.
"""

from __future__ import annotations

import argparse
import copy
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
SERVER_GRACE_MIN = 30   # minutes an evaluate job's server waits for it, once ready
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
    r = subprocess.run(["sinfo", "-p", partition, "-h", "-o", "%t %D"],
                       capture_output=True, text=True)
    return sum(int(parts[1]) for line in r.stdout.splitlines()
               if len(parts := line.split()) == 2 and parts[0].lower() == "idle"
               and parts[1].isdigit())


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
SOURCE_FILES = ("triage/models.json", "triage/mynewt_gold.json", "triage/nuttx_apps_gold.json",
                "triage/gold-contiki-ng.json", "requirements.txt", ".embedeval-revision")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".venv", "runs", "generated", "outputs"}


def server_export(cfg: dict, run: Path, stamp: str) -> str:
    """sbatch --export for a model-server job: only what the server script
    reads, as the triage phase gives it (phase.sbatch's `env -i`). Everything
    else from the submitting shell stays out: the login node's PATH finds an
    x86 python3 that cannot run on the ARM GPU node."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ["HOME"],
           "USER": os.environ["USER"], "LANG": "C.UTF-8", "PORT": "8000",
           "EMBEDEVAL_GPU_MEMORY_UTILIZATION": str(cfg["model"]["gpu_memory_utilization"]),
           "EMBEDEVAL_MAX_MODEL_LEN": str(cfg["model"]["max_model_len"]),
           "EMBEDEVAL_MAX_NUM_SEQS": str(cfg["model"]["max_num_seqs"]),
           # its own compile cache: never one another server is writing into
           "EMBEDEVAL_VLLM_CACHE": str(run / "jobs" / f"vllm-cache-{stamp}"),
           "EMBEDEVAL_SCRATCH": str(run / "jobs" / f"server-scr-{stamp}"),
           **cluster_config.env_for(cfg)}
    if any("," in v for v in env.values()):
        sys.exit(f"a server environment value holds a comma (sbatch --export splits on it): {env}")
    return ",".join(f"{k}={v}" for k, v in env.items())


def local_models() -> list[str]:
    return [k for k in json.loads((REPO / "triage" / "models.json").read_text()) if not k.startswith("_")]


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
    required = {"cluster/node_worker.py", "cluster/report.py", "scripts/triage_prs.py",
                "scripts/contiki_ng_pipeline.py", "harness/projects.py",
                "templates/contiki-ng/apptainer/instance.def.tmpl", "docs/contiki_ng_triage.md",
                "triage/models.json", "triage/gold-contiki-ng.json", "requirements.txt"}
    missing = required - files.keys()
    if missing:
        raise RuntimeError(f"source freeze missing runtime files: {', '.join(sorted(missing))}")
    git = lambda *a: subprocess.run(["git", "-C", str(source), *a], capture_output=True,
                                    text=True).stdout.strip()
    # An archive (no git) names its revision in .embedeval-revision; a checkout
    # gets one written into the copy, so every job records what it ran.
    stamp = source / ".embedeval-revision"
    revision = stamp.read_text().strip() if stamp.exists() else git("rev-parse", "HEAD")
    if revision and ".embedeval-revision" not in files:
        data = (revision + "\n").encode()
        (destination / ".embedeval-revision").write_bytes(data)
        files[".embedeval-revision"] = hashlib.sha256(data).hexdigest()
    manifest = {"revision": revision or None, "dirty": bool(git("status", "--porcelain")),
                "files": files, "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
    (destination / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (destination / "source-sha256.txt").write_text("".join(f"{h}  {f}\n" for f, h in sorted(files.items())))
    return manifest


def submit_phase(args, cfg: dict, run: Path) -> str | None:
    """One Mynewt phase as its own job (see the module docstring); returns
    its job id (None: nothing to submit; a dry run: the job's name in <>)."""
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
    if mode == "evaluate":
        # one job name per evaluation, so the guard below sees only that
        # evaluation's own job, not another model's
        name += "-" + status.eval_label(cfg["evaluate"])
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
            return None
        n = sum(1 for e in status.manifest(run)["prs"] if e["stage"] in ("to triage", "to validate"))
        print(f"  --after {args.after}: no {mode} work yet; sized for the run's {n} PRs")
    per = {"triage": par["triage_per_node"], "validate": par["validate_per_node"],
           "evaluate": cfg["evaluate"]["per_node"]}[mode]
    idle = args.idle if args.idle is not None else idle_nodes(partition)
    lowest, wanted, note = plan_nodes(cfg, math.ceil(n / per), idle)
    # a local model is served by a job of its own on the GPU partition
    # (server.sbatch), unless --server-job names one
    own_server = (mode == "evaluate" and not args.server_job
                  and cfg["evaluate"]["model"] in harness_paths.LOCAL_PROFILES)
    if own_server:
        lowest = wanted        # every node gets a server of its own

    stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}"   # two submissions may come within a second
    jobs = run / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    source = jobs / f"source-{stamp}"
    frozen_src = freeze_source(REPO, source)
    deps = [f"afterany:{args.after}"] if args.after else []
    if args.server_job:
        cfg["evaluate_server_job"] = args.server_job
        deps.append(f"after:{args.server_job}")
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
        # the servers this job started serve it alone (a later job starts its
        # own): they end with the workers, not at their own time limit
        end_server=(f"scancel $(cat {jobs}/server-of-$SLURM_JOB_ID 2>/dev/null)\n" if own_server else "")))
    print(f"run {run.name}: {len(todo)} PRs to {mode}")
    print(f"  nodes: {lowest}-{wanted} on {partition} ({idle} idle; max {s['nodes_max']})"
          + (f"\n  note: {note}" if note else ""))
    print(f"  job script: {script}\n  source: {source} (sha256 {frozen_src['sha256'][:16]})")
    if deps:
        print(f"  dependency: {','.join(deps)}")
    if own_server:
        print(f"  model server: {wanted} node(s) of {s['partition']} for {cfg['evaluate']['model']}, "
              "submitted with this job")
    if args.dry_run:
        print("  --dry-run: not submitted")
        return f"<{name}>"          # what a later job of a chain would follow
    # With its own server, the evaluate job is submitted held, so the server
    # job can name it; it is released once its config names the server.
    r = subprocess.run(["sbatch", "--parsable", *(["--hold"] if own_server else []),
                        *([f"--dependency={','.join(deps)}"] if deps else []),
                        str(script)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"sbatch failed: {r.stderr.strip()}")
    job = r.stdout.strip().split(";")[0]
    if own_server:
        server_script = jobs / f"server-{stamp}.sbatch"
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
        print(f"  model server: job {server} ({cfg['evaluate']['model']}, {wanted} node(s) of {s['partition']}; "
              f"ends {SERVER_GRACE_MIN} min after the model answers if job {job} cannot start)")
    with open(jobs / "submitted.jsonl", "a") as fh:
        fh.write(json.dumps({"job": job, "script": script.name, "submitted": stamp, "mode": mode,
                             "nodes": f"{lowest}-{wanted}", "after": args.after,
                             "server": cfg.get("evaluate_server_job"), "todo": len(todo),
                             "model": cfg["evaluate"]["model"] if mode == "evaluate" else None,
                             "source_sha256": frozen_src["sha256"]}) + "\n")
    print(f"  submitted: job {job}; log {jobs}/job-{job}.out")
    return job


def submit_nuttx_phase(args, cfg: dict, run: Path) -> None:
    """One NuttX phase as its own job (see the module docstring). Its phase
    cap (caps.triage_phase or caps.validate_phase) is the workers' time."""
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
    text = ((CLUSTER / "templates" / "phase.sbatch").read_text().format(
        partition=partition, nodes=f"{lowest}-{wanted}" if lowest < wanted else wanted,
        cpus=s["build_cpus_per_node"] if x86 else s["cpus_per_node"],
        # Scholar's GPU partitions take only jobs that request a GPU
        gres_line="" if x86 or not s["gres"] else f"#SBATCH --gres={s['gres']}",
        mem=s["build_mem"] if x86 else s["mem"], time_limit=s["time_limit"],
        name=name, run=run, repo=source, config=frozen, python=python, phase=mode,
        limit_min=phase_min + REPORT_MIN, report_min=REPORT_MIN,
        stamp=stamp,
        # the server this job started serves it alone; it ends with the
        # workers rather than hold a job slot until its own time limit
        end_server=(f"scancel $(cat {jobs}/server-of-$SLURM_JOB_ID 2>/dev/null)\n" if own_server else "")))
    # The shared template's workers' PATH, with this kind of job's tools first
    # (slurm.build_path / triage_path): not a template field, as Contiki-NG's
    # submit fills the template without one.
    if extra_path:
        text = text.replace('env -i PATH="', f'env -i PATH="{extra_path}:', 1)
    script.write_text(text)
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


def submit_evaluations(args, cfg: dict, run: Path) -> None:
    """One evaluate job (with its model servers) per model of --model, each
    after the one before: the account runs two jobs at once and an x86
    evaluation is a pair (agents and servers) that must run together, so
    pairs never interleave. Models not known are refused before anything is
    submitted."""
    models = args.model or [cfg["evaluate"]["model"]]
    if len(set(models)) < len(models):
        sys.exit(f"--model names a model twice: {' '.join(models)}")
    for m in models:
        try:
            harness_paths.resolve_model(m)
        except harness_paths.UnknownModelError as e:
            sys.exit(str(e))
    previous, chain = args.after, []
    for m in models:
        label = status.eval_label({**cfg["evaluate"], "model": m})
        if not args.after and not status.evaluate_todo(run, label):
            print(f"nothing left to evaluate as {label} in {run.name}. "
                  f"Report: python cluster/report.py --run {run.name}")
            continue                  # (the work of one label never depends on the jobs before it)
        job = submit_phase(argparse.Namespace(**{**vars(args), "model": m, "after": previous}),
                           copy.deepcopy(cfg), run)
        chain.append((m, job))
        previous = job
    if chain:
        print("evaluations, in order: " + " -> ".join(f"{m} (job {j})" for m, j in chain))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["pipeline", "triage", "observe", "statement", "validate", "evaluate"])
    p.add_argument("--run", required=True, help="run name (folder under runs/) or path")
    p.add_argument("--config", help="default: cluster/config.yaml")
    p.add_argument("--dry-run", action="store_true", help="write the job script, do not submit")
    p.add_argument("--idle", type=int, help="assume this many idle nodes (testing)")
    p.add_argument("--after", metavar="JOB", help="phase jobs: start only once this Slurm job has "
                   "ended (a chain keeps at most two jobs running)")
    p.add_argument("--recheck", action="store_true", help="Contiki-NG statement mode: also check "
                   "existing statements under the current rules, replacing those that fail")
    p.add_argument("--queue", action="store_true", help="Contiki-NG: submit even when both job "
                   "slots are busy: the job waits in the queue for one")
    p.add_argument("--arch", choices=("aarch64", "x86_64"),
                   help="NuttX validate / evaluate: the instances that validate on this architecture")
    p.add_argument("--model", nargs="+", metavar="MODEL",
                   help="Mynewt evaluate: the models (triage/models.json profiles, or hosted "
                   "models harness/paths.py knows), one evaluation each, one after the other; "
                   "default evaluate.model")
    p.add_argument("--server-job", metavar="JOB", help="Mynewt evaluate: a running model-server "
                   "job (scripts/serve_triage_model.sh) to use instead of submitting one")
    args = p.parse_args()

    cfg = cluster_config.load_or_exit(args.config)
    run = Path(args.run) if "/" in args.run else Path(cfg["derived"]["runs"]) / args.run
    if not (run / "manifest.json").exists():
        sys.exit(f"{run} is not a prepared run (no manifest.json); run cluster/prepare.py first")
    if status.project(run) == "contiki-ng":
        return submit_contiki(args)
    if args.mode == "observe":
        sys.exit("observe jobs are for Contiki-NG runs")
    if status.project(run) == "mynewt":
        if args.mode == "statement":
            sys.exit("statement jobs are for NuttX and Contiki-NG runs")
        if args.model and args.mode != "evaluate":
            sys.exit("--model is for evaluate jobs")
        return submit_evaluations(args, cfg, run) if args.mode == "evaluate" else submit_phase(args, cfg, run)
    if status.project(run) == "nuttx" and args.mode != "pipeline":
        if args.mode in ("statement", "evaluate") and \
                not any(e.get("instance") for e in status.manifest(run)["prs"]):
            sys.exit(f"{args.mode} jobs are for runs of instances (prepare.py --instances)")
        if args.model and len(args.model) > 1:
            sys.exit("NuttX evaluate takes one --model")
        args.model = args.model[0] if args.model else None
        return submit_nuttx_phase(args, cfg, run)
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


def submit_contiki(args) -> None:
    """A Contiki-NG run's job (submit), or, for several --model, one evaluation per
    model, chained."""
    models = args.model or [None]
    if len(set(models)) != len(models):
        sys.exit("--model names a model twice")
    if args.mode == "evaluate":
        bad = [m for m in models if m not in local_models()]
        if bad:      # hosted models are paid APIs: not from a cluster job
            sys.exit(f"--model must be a profile of triage/models.json, not {', '.join(map(str, bad))}: "
                     f"{', '.join(local_models())}")
    if args.model and args.mode != "evaluate":
        sys.exit("--model and --server-job go with evaluate")
    if len(models) > 1 and args.server_job:
        sys.exit("--server-job serves one model")
    if len(models) == 1:
        args.model = models[0]
        submit(args)
        return
    # One evaluation per model, each after the previous pair (agents job and
    # its server) so pairs never interleave. The first model's job is
    # submitted as given; a model with nothing left to do is skipped.
    done, after = [], args.after
    for model in models:
        one = argparse.Namespace(**{**vars(args), "model": model, "after": after})
        try:
            job = submit(one, chain=True, skip_empty=not args.after)
        except SystemExit:
            for m, j in done:
                print(f"already submitted: {m}: evaluate job {j['job']}, model server job {j['server'] or '-'}")
            raise
        if job:
            done.append((model, job))
            after = f"{job['job']}:{job['server']}" if job["server"] else job["job"]
    for model, job in done:
        print(f"{model}: evaluate job {job['job']}, model server job {job['server'] or '-'}")


def submit(args, chain: bool = False, skip_empty: bool = False) -> dict | None:
    """Submit one job for `args`; returns its job ids ({"job", "server"}; names
    in angle brackets under --dry-run), or None when nothing was submitted.
    chain: one of several evaluations: no forecast (sbatch is not called under
    --dry-run), and `skip_empty` skips it when its model has nothing left."""
    cfg = cluster_config.load_or_exit(args.config)
    run = Path(args.run) if "/" in args.run else Path(cfg["derived"]["runs"]) / args.run
    if not (run / "manifest.json").exists():
        sys.exit(f"{run} is not a prepared run (no manifest.json); run cluster/prepare.py first")
    project = status.manifest(run).get("project", "zephyr")
    if cfg["project"] != project:
        sys.exit(f"config project {cfg['project']} does not match run project {project}")
    if project == "zephyr" and args.mode != "pipeline":
        sys.exit("Zephyr runs use pipeline mode")
    if project == "contiki-ng" and args.mode == "pipeline":
        sys.exit("Contiki-NG requires separate triage, observe, validate or statement jobs")
    if args.mode == "evaluate" and project != "contiki-ng":
        sys.exit("evaluate jobs are Contiki-NG's")
    if (args.model or args.server_job) and args.mode != "evaluate":
        sys.exit("--model and --server-job go with evaluate")
    if project == "contiki-ng" and args.mode in ("observe", "validate", "evaluate") and not cfg["slurm"]["build_partition"]:
        sys.exit("slurm.build_partition must name an x86 CPU partition")

    s, par = cfg["slurm"], cfg["parallel"]
    if minutes(s["time_limit"]) > 240:
        sys.exit("Scholar jobs may not exceed four hours")
    if minutes(s["time_limit"]) < REPORT_MIN + VALIDATE_MIN:
        sys.exit(f"slurm.time_limit {s['time_limit']} is too short for a phase")

    # One job per run at a time: the nodes of a job are numbered, and two jobs
    # would share nodes/node<N>/. (Within a job, the queue balances the work.)
    # A job left COMPLETING has no workers (a dead node can hold it there for
    # hours), so it does not count.
    label = (status.eval_label(args.model, cfg["evaluate"]["step_limit"], cfg["evaluate"]["time_limit"])
             if args.mode == "evaluate" else None)
    job_name = (f"embedeval-{run.name}" if project == "zephyr" else
                f"embedeval-contiki-{run.name}-{args.mode}" + (f"-{label}" if label else ""))
    if project == "zephyr":
        q = subprocess.run(["squeue", "-h", "--me", "-n", job_name, "-o", "%i %T"],
                           capture_output=True, text=True)
        live = [l for l in q.stdout.splitlines() if l.strip() and not l.endswith("COMPLETING")]
    else:
        q = subprocess.run(["squeue", "-h", "--me", "-o", "%i %T %.200j"],
                           capture_output=True, text=True)
        live = [l for l in q.stdout.splitlines()
                if (l.split()[-1] in {f"embedeval-contiki-{run.name}-{m}"
                                      for m in ("triage", "observe", "validate", "statement", "evaluate")}
                    or l.split()[-1].startswith(f"embedeval-contiki-{run.name}-evaluate-"))
                and " COMPLETING " not in f" {l} "]
        running = [l for l in q.stdout.splitlines() if len(l.split()) >= 2 and l.split()[1] in ("RUNNING", "CONFIGURING")]
        if len(running) >= 2 and not (args.after or args.queue):   # a chained job waits for its predecessor
            sys.exit("Both shared Scholar job slots are occupied; wait for a running job to finish")
    if live and not args.after:          # a chained job starts only after the one it follows
        sys.exit(f"run {run.name} already has a job ({'; '.join(live)}); wait for it to "
                 "end, or scancel it, then submit again")

    triage_left, validate_left = status.triage_todo(run), status.validate_todo(run)
    # Observation jobs also finalize what observed valid; statements are model work.
    observe_left = (list(dict.fromkeys(status.observe_todo(run) + status.finalize_todo(run)))
                    if project == "contiki-ng" else [])
    if args.recheck and args.mode != "statement":
        sys.exit("--recheck applies to statement jobs")
    cfg["statement_recheck"] = args.recheck
    statement_left = status.statement_todo(run, args.recheck) if project == "contiki-ng" else []
    evaluate_left = status.evaluate_todo(run, label) if args.mode == "evaluate" else []
    counts = {"triage": len(triage_left), "observe": len(observe_left),
              "validate": len(validate_left), "statement": len(statement_left),
              "evaluate": len(evaluate_left)}
    if (not triage_left and not validate_left) if args.mode == "pipeline" else not counts[args.mode]:
        if not args.after or skip_empty:
            print(f"nothing left to {args.mode}{f' for {label}' if label else ''} in {run.name}. "
                  f"Report: python cluster/report.py --run {run.name}")
            return None
        # Chained behind the job that creates this work: size it by the run.
        counts[args.mode] = sum(1 for e in status.manifest(run)["prs"] if e["stage"] in ("to triage", "to validate"))
        print(f"  --after {args.after}: no {args.mode} work yet; sized for the run's "
              f"{counts[args.mode]} PRs")
    work = (max(math.ceil(len(triage_left) / par["triage_per_node"]),
                math.ceil(len(validate_left) / par["validate_per_node"]))
            if args.mode == "pipeline" else
            math.ceil(counts[args.mode] / (cfg["evaluate"]["per_node"] if args.mode == "evaluate" else
                                           par["triage_per_node" if args.mode in ("triage", "statement")
                                               else "validate_per_node"])))
    model_phase = args.mode in ("pipeline", "triage", "statement")
    partition = s["partition"] if model_phase else s["build_partition"]
    cpu_phase = project == "contiki-ng" and args.mode in ("observe", "validate", "evaluate")
    cpus = s["build_cpus_per_node"] if cpu_phase else s["cpus_per_node"]
    mem = s["build_mem"] if cpu_phase else s["mem"]
    python = (sys.executable if project == "zephyr" else
              (cfg["evaluate"]["python"] or s["build_python"]) if args.mode == "evaluate" else
              s["build_python"] if cpu_phase else s["triage_python"])
    idle = args.idle if args.idle is not None else idle_nodes(partition)
    schedule_cfg = cfg
    lowest, wanted, note = plan_nodes(schedule_cfg, work, idle)

    stamp = (f"{datetime.now():%Y%m%d-%H%M%S%f}" if project == "contiki-ng"
             else f"{datetime.now():%Y%m%d-%H%M%S}")
    jobs = run / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    frozen = jobs / f"config-{stamp}.json"
    frozen.write_text(json.dumps(cfg, indent=2) + "\n")
    source_root = REPO
    source_manifest = None
    if project == "contiki-ng":
        source_root = jobs / f"source-{stamp}"
        source_manifest = freeze_source(REPO, source_root)
    deps = [f"afterany:{args.after}"] if args.after else []
    end_server = ""
    own_server = args.mode == "evaluate" and not args.server_job
    server_id = jobs / f"server-{stamp}.job"      # written once the server is submitted
    if args.mode == "evaluate":
        cfg["evaluate_model"] = args.model
        if args.server_job:
            cfg["evaluate_server_job"] = args.server_job
            deps.append(f"after:{args.server_job}")
        else:
            # The server is submitted after this job and starts only once it
            # runs, so it never holds a job slot while its agents wait for
            # one; this job ends it (or the still-pending job) when done.
            end_server = f"S=$(cat {server_id} 2>/dev/null) && /usr/bin/scancel \"$S\"\n"
        frozen.write_text(json.dumps(cfg, indent=2) + "\n")
    script = jobs / f"{args.mode}-{stamp}.sbatch"
    template = "pipeline.sbatch" if args.mode == "pipeline" else "phase.sbatch"
    script.write_text((CLUSTER / "templates" / template).read_text().format(
        partition=partition, nodes=f"{lowest}-{wanted}" if lowest < wanted else wanted,
        cpus=cpus, gres=s["gres"] if model_phase else "",
        gres_line=f"#SBATCH --gres={s['gres']}" if model_phase else "",
        mem=mem, time_limit=s["time_limit"], phase=args.mode,
        job_name=job_name, name=job_name, run=run, repo=source_root, config=frozen, python=python,
        limit_min=minutes(s["time_limit"]), report_min=REPORT_MIN, validate_min=VALIDATE_MIN,
        stamp=stamp, end_server=end_server))

    print(f"run {run.name}: " + (f"{len(triage_left)} PRs to triage, {len(validate_left)} to validate"
          if args.mode == "pipeline" else f"{counts[args.mode]} PRs to {args.mode}"))
    print(f"  nodes: {lowest}-{wanted} (work needs {work}; {idle} idle; max {schedule_cfg['slurm']['nodes_max']})"
          + (f"\n  note: {note}" if note else ""))
    print(f"  limit: {s['time_limit']}; {args.mode} runs until {REPORT_MIN} min remain for report")
    print(f"  job script: {script}")
    if source_manifest:
        print(f"  source: {source_root} (sha256 {source_manifest['sha256']})")
    if not chain:
        forecast = subprocess.run(["sbatch", "--test-only", str(script)], capture_output=True, text=True)
        lines = (forecast.stderr or forecast.stdout).strip().splitlines()
        print(f"  forecast: {lines[-1] if lines else '?'}")
    if args.dry_run:
        print("  --dry-run: not submitted" + (f"; would start after {args.after}" if args.after else ""))
        return {"job": f"<{label} job>", "server": f"<{label} server>" if own_server else None}
    r = subprocess.run(["sbatch", "--parsable", *(["--hold"] if own_server else []),
                        *([f"--dependency={','.join(deps)}"] if deps else []), str(script)],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"sbatch failed: {r.stderr.strip()}")
    job = r.stdout.strip().split(";")[0]
    if own_server:
        r = subprocess.run(["sbatch", "--parsable", "-p", s["partition"],
                            "-J", f"embedeval-contiki-{run.name}-server-{label}", f"--gres={s['gres']}",
                            f"--mem={s['mem']}", f"--cpus-per-task={s['cpus_per_node']}",
                            "-t", s["time_limit"], "--chdir", str(source_root),
                            f"--dependency=after:{job}",       # once the evaluate job runs
                            "--export=" + server_export(cfg, run, stamp),
                            "-o", str(jobs / "server-%j.log"),
                            str(source_root / "scripts" / "serve_triage_model.sh"), args.model],
                           capture_output=True, text=True)
        if r.returncode:
            subprocess.run(["scancel", job])
            sys.exit(f"sbatch of the model server failed (evaluate job {job} cancelled): {r.stderr.strip()}")
        server = r.stdout.strip().split(";")[0]
        cfg["evaluate_server_job"] = server
        frozen.write_text(json.dumps(cfg, indent=2) + "\n")
        server_id.write_text(server + "\n")
        subprocess.run(["scontrol", "release", job], check=True)
        print(f"  model server: job {server} ({args.model}, {s['partition']}), starts after job {job}")
    with open(jobs / "submitted.jsonl", "a") as fh:
        fh.write(json.dumps({"job": job, "script": script.name, "submitted": stamp,
                             "nodes": f"{lowest}-{wanted}", "mode": args.mode,
                             "source": str(source_root) if source_manifest else None,
                             "source_sha256": source_manifest["sha256"] if source_manifest else None,
                             "after": args.after,
                             **({"model": args.model, "label": label, "server": cfg.get("evaluate_server_job"),
                                 "evaluate": len(evaluate_left)} if args.mode == "evaluate" else {}),
                             "triage": len(triage_left),
                             "validate": len(validate_left)}) + "\n")
    print(f"  submitted: job {job}; log {jobs}/job-{job}.out")
    return {"job": job, "server": cfg.get("evaluate_server_job") if own_server else None}


if __name__ == "__main__":
    main()
