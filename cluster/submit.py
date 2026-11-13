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

--after J starts the job once job J has ended (a chain needs no slot until
then). Each Mynewt job runs a frozen copy of the code
(<run>/jobs/source-<time>/, with its sha256 manifest), so editing the
checkout never changes a job that is queued or running.

A NuttX run runs each phase as its own job, chained with --after: an
apps-PR run triage and validate, a run of existing instances (prepare.py
--instances) statement and validate:

    python cluster/submit.py triage   --run R                         # GPU nodes, the model
    python cluster/submit.py statement --run R                        # GPU nodes, the model
    python cluster/submit.py validate --run R --arch aarch64 --after J
    python cluster/submit.py validate --run R --arch x86_64  --after J  # slurm.build_partition

--arch takes the instances that validate on that architecture (manifest
"arch"); an apps PR's pairs go to either (node_worker.py records an ARM64
pair that needs x86 as needs_x86). Each phase job runs a frozen copy of the
code (<run>/jobs/source-<time>/) and checks its sha256 list before it starts,
so editing the checkout never changes a queued job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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


def phase_todo(run: Path, mode: str, arch: str, cfg: dict) -> list[int]:
    """The phase's work on `arch`: entries without an "arch" (apps PRs to
    triage or validate) go to either."""
    todo = {"triage": status.triage_todo, "statement": status.statement_todo,
            "validate": status.validate_todo}[mode](run)
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
        sys.exit("Mynewt runs submit each phase as its own job: triage, then validate, "
                 "each with --after the one before")
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
    todo = {"triage": status.triage_todo, "validate": status.validate_todo}[mode](run)
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
    per = {"triage": par["triage_per_node"], "validate": par["validate_per_node"]}[mode]
    idle = args.idle if args.idle is not None else idle_nodes(partition)
    lowest, wanted, note = plan_nodes(cfg, math.ceil(n / per), idle)

    stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}"   # two submissions may come within a second
    jobs = run / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    source = jobs / f"source-{stamp}"
    frozen_src = freeze_source(REPO, source)
    deps = [f"afterany:{args.after}"] if args.after else []
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
        limit_min=minutes(s["time_limit"]), report_min=REPORT_MIN, stamp=stamp))
    print(f"run {run.name}: {len(todo)} PRs to {mode}")
    print(f"  nodes: {lowest}-{wanted} on {partition} ({idle} idle; max {s['nodes_max']})"
          + (f"\n  note: {note}" if note else ""))
    print(f"  job script: {script}\n  source: {source} (sha256 {frozen_src['sha256'][:16]})")
    if deps:
        print(f"  dependency: {','.join(deps)}")
    if args.dry_run:
        print("  --dry-run: not submitted")
        return f"<{name}>"          # what a later job of a chain would follow
    r = subprocess.run(["sbatch", "--parsable",
                        *([f"--dependency={','.join(deps)}"] if deps else []),
                        str(script)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"sbatch failed: {r.stderr.strip()}")
    job = r.stdout.strip().split(";")[0]
    with open(jobs / "submitted.jsonl", "a") as fh:
        fh.write(json.dumps({"job": job, "script": script.name, "submitted": stamp, "mode": mode,
                             "nodes": f"{lowest}-{wanted}", "after": args.after,
                             "todo": len(todo),
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
    per = {"triage": cfg["parallel"]["triage_per_node"], "statement": cfg["parallel"]["triage_per_node"], "validate": cfg["parallel"]["validate_per_node"]}[mode]
    idle = args.idle if args.idle is not None else idle_nodes(partition)
    lowest, wanted, note = plan_nodes(cfg, math.ceil(n / per), idle)

    stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}"   # two submissions may come within a second
    jobs = run / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    source = jobs / f"source-{stamp}"
    frozen_src = freeze_source(REPO, source)
    deps = [f"afterany:{args.after}"] if args.after else []
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
        stamp=stamp))
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
    r = subprocess.run(["sbatch", "--parsable",
                        *([f"--dependency={','.join(deps)}"] if deps else []),
                        str(script)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"sbatch failed: {r.stderr.strip()}")
    job = r.stdout.strip().split(";")[0]
    with open(jobs / "submitted.jsonl", "a") as fh:
        fh.write(json.dumps({"job": job, "script": script.name, "submitted": stamp, "mode": mode,
                             "arch": arch, "nodes": f"{lowest}-{wanted}", "after": args.after,
                             "todo": len(todo),
                             "source_sha256": frozen_src["sha256"]}) + "\n")
    print(f"  submitted: job {job}; log {jobs}/job-{job}.out")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["pipeline", "triage", "observe", "statement", "validate"])
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
                   help="NuttX validate: the instances that validate on this architecture")
    args = p.parse_args()

    cfg = cluster_config.load_or_exit(args.config)
    run = Path(args.run) if "/" in args.run else Path(cfg["derived"]["runs"]) / args.run
    if not (run / "manifest.json").exists():
        sys.exit(f"{run} is not a prepared run (no manifest.json); run cluster/prepare.py first")
    if status.project(run) == "contiki-ng":
        return submit(args)
    if args.mode == "observe":
        sys.exit("observe jobs are for Contiki-NG runs")
    if status.project(run) == "mynewt":
        if args.mode == "statement":
            sys.exit("statement jobs are for NuttX and Contiki-NG runs")
        return submit_phase(args, cfg, run)
    if status.project(run) == "nuttx" and args.mode != "pipeline":
        if args.mode == "statement" and \
                not any(e.get("instance") for e in status.manifest(run)["prs"]):
            sys.exit(f"{args.mode} jobs are for runs of instances (prepare.py --instances)")
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


def submit(args) -> None:
    """Submit one job of a Contiki-NG run for `args`."""
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
    if project == "contiki-ng" and args.mode in ("observe", "validate") and not cfg["slurm"]["build_partition"]:
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
    job_name = (f"embedeval-{run.name}" if project == "zephyr" else
                f"embedeval-contiki-{run.name}-{args.mode}")
    if project == "zephyr":
        q = subprocess.run(["squeue", "-h", "--me", "-n", job_name, "-o", "%i %T"],
                           capture_output=True, text=True)
        live = [l for l in q.stdout.splitlines() if l.strip() and not l.endswith("COMPLETING")]
    else:
        q = subprocess.run(["squeue", "-h", "--me", "-o", "%i %T %.200j"],
                           capture_output=True, text=True)
        live = [l for l in q.stdout.splitlines()
                if (l.split()[-1] in {f"embedeval-contiki-{run.name}-{m}"
                                      for m in ("triage", "observe", "validate", "statement")})
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
    counts = {"triage": len(triage_left), "observe": len(observe_left),
              "validate": len(validate_left), "statement": len(statement_left)}
    if (not triage_left and not validate_left) if args.mode == "pipeline" else not counts[args.mode]:
        if not args.after:
            print(f"nothing left to {args.mode} in {run.name}. "
                  f"Report: python cluster/report.py --run {run.name}")
            return
        # Chained behind the job that creates this work: size it by the run.
        counts[args.mode] = sum(1 for e in status.manifest(run)["prs"] if e["stage"] in ("to triage", "to validate"))
        print(f"  --after {args.after}: no {args.mode} work yet; sized for the run's "
              f"{counts[args.mode]} PRs")
    work = (max(math.ceil(len(triage_left) / par["triage_per_node"]),
                math.ceil(len(validate_left) / par["validate_per_node"]))
            if args.mode == "pipeline" else
            math.ceil(counts[args.mode] / par["triage_per_node" if args.mode in ("triage", "statement")
                                           else "validate_per_node"]))
    model_phase = args.mode in ("pipeline", "triage", "statement")
    partition = s["partition"] if model_phase else s["build_partition"]
    cpu_phase = project == "contiki-ng" and args.mode in ("observe", "validate")
    cpus = s["build_cpus_per_node"] if cpu_phase else s["cpus_per_node"]
    mem = s["build_mem"] if cpu_phase else s["mem"]
    python = (sys.executable if project == "zephyr" else
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
    script = jobs / f"{args.mode}-{stamp}.sbatch"
    template = "pipeline.sbatch" if args.mode == "pipeline" else "phase.sbatch"
    script.write_text((CLUSTER / "templates" / template).read_text().format(
        partition=partition, nodes=f"{lowest}-{wanted}" if lowest < wanted else wanted,
        cpus=cpus, gres=s["gres"] if model_phase else "",
        gres_line=f"#SBATCH --gres={s['gres']}" if model_phase else "",
        mem=mem, time_limit=s["time_limit"], phase=args.mode,
        job_name=job_name, name=job_name, run=run, repo=source_root, config=frozen, python=python,
        limit_min=minutes(s["time_limit"]), report_min=REPORT_MIN, validate_min=VALIDATE_MIN,
        stamp=stamp))

    print(f"run {run.name}: " + (f"{len(triage_left)} PRs to triage, {len(validate_left)} to validate"
          if args.mode == "pipeline" else f"{counts[args.mode]} PRs to {args.mode}"))
    print(f"  nodes: {lowest}-{wanted} (work needs {work}; {idle} idle; max {schedule_cfg['slurm']['nodes_max']})"
          + (f"\n  note: {note}" if note else ""))
    print(f"  limit: {s['time_limit']}; {args.mode} runs until {REPORT_MIN} min remain for report")
    print(f"  job script: {script}")
    if source_manifest:
        print(f"  source: {source_root} (sha256 {source_manifest['sha256']})")
    forecast = subprocess.run(["sbatch", "--test-only", str(script)], capture_output=True, text=True)
    lines = (forecast.stderr or forecast.stdout).strip().splitlines()
    print(f"  forecast: {lines[-1] if lines else '?'}")
    if args.dry_run:
        print("  --dry-run: not submitted" + (f"; would start after {args.after}" if args.after else ""))
        return
    r = subprocess.run(["sbatch", "--parsable",
                        *([f"--dependency={','.join(deps)}"] if deps else []), str(script)],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"sbatch failed: {r.stderr.strip()}")
    job = r.stdout.strip().split(";")[0]
    with open(jobs / "submitted.jsonl", "a") as fh:
        fh.write(json.dumps({"job": job, "script": script.name, "submitted": stamp,
                             "nodes": f"{lowest}-{wanted}", "mode": args.mode,
                             "source": str(source_root) if source_manifest else None,
                             "source_sha256": source_manifest["sha256"] if source_manifest else None,
                             "after": args.after,
                             "triage": len(triage_left),
                             "validate": len(validate_left)}) + "\n")
    print(f"  submitted: job {job}; log {jobs}/job-{job}.out")


if __name__ == "__main__":
    main()
