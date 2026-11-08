#!/usr/bin/env python3
"""
Submit a prepared run to Slurm.

    python cluster/submit.py pipeline --run 2025-09            # triage + validation
    python cluster/submit.py pipeline --run 2025-09 --dry-run  # show the plan, submit nothing
    python cluster/submit.py evaluate --run R --model qwen38-nvfp4   # Contiki-NG: agents on R's VALID instances

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

Contiki-NG evaluate runs an agent (harness/run.py) on each VALID instance of
the run and grades its patch (harness/evaluate_patches.py), on the x86 build
partition. The model is the job's parameter (--model, a profile of
triage/models.json); the limits are the config's evaluate section, the same
for every model. Unless --server-job names a running model server, the model
is served by a job of its own on the GPU partition (scripts/serve_triage_model.sh,
with a clean environment); the evaluate job starts once that one runs, reaches
it through an SSH tunnel, and cancels it when its workers are done.
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
SOURCE_DIRS = ("cluster", "scripts", "harness", "templates", "docs")
SOURCE_FILES = ("triage/models.json", "triage/gold-contiki-ng.json", "requirements.txt", ".embedeval-revision")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".venv", "runs", "generated", "outputs"}
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


def server_export(cfg: dict, run: Path, stamp: str) -> str:
    """sbatch --export for a model-server job: only what the server script
    reads, as the triage phase gives it (phase.sbatch's `env -i`). Nothing
    else of the submitting shell: a login node's PATH can put an interpreter
    of another architecture first."""
    m = cfg["model"]
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ["HOME"],
           "USER": os.environ["USER"], "LANG": "C.UTF-8", "PORT": "8000",
           "EMBEDEVAL_GPU_MEMORY_UTILIZATION": str(m["gpu_memory_utilization"]),
           "EMBEDEVAL_MAX_MODEL_LEN": str(m["max_model_len"]),
           "EMBEDEVAL_MAX_NUM_SEQS": str(m["max_num_seqs"]),
           # its own compile cache: never one another server is writing into
           "EMBEDEVAL_VLLM_CACHE": str(run / "jobs" / f"vllm-cache-{stamp}"),
           **cluster_config.env_for(cfg)}
    if any("," in v for v in env.values()):
        sys.exit(f"a server environment value holds a comma (sbatch --export splits on it): {env}")
    return ",".join(f"{k}={v}" for k, v in env.items())


def local_models() -> list[str]:
    return [k for k in json.loads((REPO / "triage" / "models.json").read_text()) if not k.startswith("_")]


def freeze_source(source: Path, destination: Path) -> dict:
    """Copy one job's Contiki runtime code and record the bytes it will run."""
    destination.mkdir(parents=True, exist_ok=False)
    files = {}
    paths = [p for root in SOURCE_DIRS for p in (source / root).rglob("*")]
    paths += [source / name for name in SOURCE_FILES if (source / name).exists()]
    for src in sorted(paths):
        rel = src.relative_to(source)
        if (not src.is_file() or src.is_symlink() or any(part in SKIP_DIRS for part in rel.parts)
                or rel == Path("cluster/config.yaml") or src.name.endswith((".pyc", ".pyo"))):
            continue
        data = src.read_bytes()
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        shutil.copymode(src, target)
        files[rel.as_posix()] = hashlib.sha256(data).hexdigest()
    required = {"cluster/node_worker.py", "cluster/report.py", "scripts/triage_prs.py",
                "scripts/contiki_ng_pipeline.py", "harness/projects.py",
                "templates/contiki-ng/apptainer/instance.def.tmpl", "docs/contiki_ng_triage.md",
                "triage/models.json", "triage/gold-contiki-ng.json", "requirements.txt"}
    missing = required - files.keys()
    if missing:
        raise RuntimeError(f"source freeze missing runtime files: {', '.join(sorted(missing))}")
    stamp = source / ".embedeval-revision"
    revision = stamp.read_text().strip() if stamp.exists() else subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True,
        text=True).stdout.strip()
    if revision and ".embedeval-revision" not in files:
        data = (revision + "\n").encode()
        (destination / ".embedeval-revision").write_bytes(data)
        files[".embedeval-revision"] = hashlib.sha256(data).hexdigest()
    manifest = {"revision": revision or None, "files": files,
                "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
    (destination / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["pipeline", "triage", "observe", "validate", "statement", "evaluate"])
    p.add_argument("--run", required=True, help="run name (folder under runs/) or path")
    p.add_argument("--config", help="default: cluster/config.yaml")
    p.add_argument("--dry-run", action="store_true", help="write the job script, do not submit")
    p.add_argument("--after", metavar="JOB", help="start only once this Slurm job has ended (a chain "
                   "keeps at most two jobs running)")
    p.add_argument("--recheck", action="store_true", help="statement mode: also check existing "
                   "statements under the current rules, replacing those that fail")
    p.add_argument("--queue", action="store_true", help="submit even when both job slots are busy: "
                   "the job waits in the queue for one")
    p.add_argument("--model", help="evaluate: the model, a profile of triage/models.json")
    p.add_argument("--server-job", metavar="JOB", help="evaluate: a running model-server job "
                   "(scripts/serve_triage_model.sh, serving --model) to use instead of submitting one")
    p.add_argument("--idle", type=int, help="assume this many idle nodes (testing)")
    args = p.parse_args()

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
    if args.mode == "evaluate" and args.model not in local_models():
        # hosted models are paid APIs: not from a cluster job
        sys.exit(f"--model must be a profile of triage/models.json: {', '.join(local_models())}")
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
                if l.split()[-1] in {f"embedeval-contiki-{run.name}-{m}"
                                     for m in ("triage", "observe", "validate", "statement", "evaluate")}
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
    evaluate_left = status.evaluate_todo(run, args.model) if args.mode == "evaluate" else []
    counts = {"triage": len(triage_left), "observe": len(observe_left),
              "validate": len(validate_left), "statement": len(statement_left),
              "evaluate": len(evaluate_left)}
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
    if args.mode == "evaluate":
        cfg["evaluate_model"] = args.model
        server = args.server_job
        if not server and not args.dry_run:
            r = subprocess.run(["sbatch", "--parsable", "-p", s["partition"],
                                "-J", f"embedeval-contiki-{run.name}-server", f"--gres={s['gres']}",
                                f"--mem={s['mem']}", f"--cpus-per-task={s['cpus_per_node']}",
                                "-t", s["time_limit"], "--chdir", str(source_root),
                                # chained: the server starts only when the evaluate job could
                                *([f"--dependency=afterany:{args.after}"] if args.after else []),
                                "--export=" + server_export(cfg, run, stamp),
                                "-o", str(jobs / "server-%j.log"),
                                str(source_root / "scripts" / "serve_triage_model.sh"), args.model],
                               capture_output=True, text=True)
            if r.returncode:
                sys.exit(f"sbatch of the model server failed: {r.stderr.strip()}")
            server = r.stdout.strip().split(";")[0]
            # the evaluate job ends the server it was given here when its workers are done
            end_server = f"/usr/bin/scancel {server}\n"
            print(f"  model server: job {server} ({args.model}, {s['partition']})")
        if server:
            cfg["evaluate_server_job"] = server
            deps.append(f"after:{server}")
        frozen.write_text(json.dumps(cfg, indent=2) + "\n")
    script = jobs / f"{args.mode}-{stamp}.sbatch"
    template = "pipeline.sbatch" if args.mode == "pipeline" else "phase.sbatch"
    script.write_text((CLUSTER / "templates" / template).read_text().format(
        partition=partition, nodes=f"{lowest}-{wanted}" if lowest < wanted else wanted,
        cpus=cpus, gres=s["gres"] if model_phase else "",
        gres_line=f"#SBATCH --gres={s['gres']}" if model_phase else "",
        mem=mem, time_limit=s["time_limit"], phase=args.mode,
        job_name=job_name, name=run.name, run=run, repo=source_root, config=frozen, python=python,
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
    forecast = subprocess.run(["sbatch", "--test-only", str(script)], capture_output=True, text=True)
    lines = (forecast.stderr or forecast.stdout).strip().splitlines()
    print(f"  forecast: {lines[-1] if lines else '?'}")
    if args.dry_run:
        print("  --dry-run: not submitted")
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
                             **({"model": args.model, "server": cfg.get("evaluate_server_job"),
                                 "evaluate": len(evaluate_left)} if args.mode == "evaluate" else {}),
                             "triage": len(triage_left),
                             "validate": len(validate_left)}) + "\n")
    print(f"  submitted: job {job}; log {jobs}/job-{job}.out")


if __name__ == "__main__":
    main()
