#!/usr/bin/env python3
"""A worker of the NuttX triage GA, one per node of a server pool.

Started on every node of a pool job as an `--overlap` step (see ga_pool.sh):

    srun --jobid=<pool> --overlap --ntasks-per-node=1 -c 2 \
        python3 scripts/ga_worker.py

It waits for its node's model server, then repeatedly claims a task from the
shared queue, runs one triage run (normal or blind, one individual, one
repeat) against that server and writes the run and its scorecard under the
task's output directory. Tasks and their state live on the shared
filesystem under $GA (default /scratch/scholar/zhan5136/ga):

    queue/<task>.json       waiting (claimed by renaming it: atomic)
    running/<task>.<node>   claimed; the file's mtime is touched while it runs
    done/<task>.json        finished, with the exit status and timings
    nodes/<node>.json       heartbeat: server health and the current task

Create `$GA/stop` to make the workers exit after their current task.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

GA = Path(os.environ.get("GA", "/scratch/scholar/zhan5136/ga"))
NODE = socket.gethostname().split(".")[0]
PYTHON = os.environ.get("GA_PYTHON", str(Path.home() / "EmbedEval/.venv/bin/python"))
LOCAL = Path(os.environ.get("GA_LOCAL", "/tmp/ga-local"))   # node-local scratch
SHARED_DATA = GA / "shared" / "nuttx-data"
TASK_LIMIT = 100 * 60   # seconds; a run takes about 30 minutes


def server_up() -> bool:
    try:
        with urlopen("http://127.0.0.1:8000/v1/models", timeout=5) as response:
            return b"qwen38-nvfp4" in response.read()
    except OSError:
        return False


def heartbeat(task: str | None) -> None:
    (GA / "nodes").mkdir(parents=True, exist_ok=True)
    (GA / "nodes" / f"{NODE}.json").write_text(json.dumps({
        "node": NODE, "job": os.environ.get("SLURM_JOB_ID"), "task": task,
        "server": server_up(), "time": datetime.now().isoformat(timespec="seconds")}))


def node_data() -> Path:
    """The shared NuttX data (PR records, bare clones, caches) copied once to
    this node: clones are fetched into and caches written, so each node gets
    its own, and the pre-warmed shared copy stays read-only."""
    data = LOCAL / "nuttx-data"
    if not (data / ".complete").exists():
        shutil.rmtree(data, ignore_errors=True)
        data.mkdir(parents=True)
        subprocess.run(["cp", "-a", f"{SHARED_DATA}/.", str(data)], check=True)
        (data / ".complete").touch()
    return data


def claim() -> tuple[str, dict] | None:
    for path in sorted((GA / "queue").glob("*.json")):
        target = GA / "running" / f"{path.stem}.{NODE}"
        try:
            os.rename(path, target)
        except OSError:
            continue    # another node was first
        return path.stem, json.loads(target.read_text())
    return None


def run_task(name: str, task: dict) -> dict:
    snap = Path(task["snap"])
    out = Path(task["out"])
    work = LOCAL / "runs" / name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    spec = json.loads((snap / "ga_individual.json").read_text())
    knobs = spec["knobs"]
    command = [PYTHON, "scripts/triage_nuttx_prs.py", "apps", "--profile", "qwen38-nvfp4",
               "--gold", "--max-tool-calls", str(knobs.get("max_tool_calls", 40)),
               "--max-minutes", str(knobs.get("max_minutes", 30)),
               "--parallel", str(knobs.get("parallel", 4))]
    if task["mode"] == "blind":
        command.append("--blind")
    if knobs.get("followup_review"):
        command.append("--followup-review")
    env = {**os.environ,
           "EMBEDEVAL_NUTTX_DATA": str(node_data()),
           "EMBEDEVAL_NUTTX_DATA_RO": str(SHARED_DATA),
           "EMBEDEVAL_NUTTX_RUNS": str(work),
           "EMBEDEVAL_CONFIG_WORK": str(LOCAL / "config-work"),
           "PATH": ":".join(p for p in os.environ.get("PATH", "").split(":")
                            if not p.startswith("/apps/cent7"))}
    (LOCAL / "config-work").mkdir(parents=True, exist_ok=True)
    started = time.time()
    status = {"task": name, "node": NODE, "started": datetime.now().isoformat(timespec="seconds")}
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "client.log", "w") as log:
        try:
            done = subprocess.run(command, cwd=snap, env=env, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=TASK_LIMIT)
            status["exit"] = done.returncode
        except subprocess.TimeoutExpired:
            status["exit"] = "timeout"
        runs = sorted(p for p in work.iterdir() if p.is_dir())
        if runs:
            status["eval_exit"] = subprocess.run(
                [PYTHON, "scripts/triage_nuttx_prs.py", "eval", str(runs[-1])], cwd=snap, env=env,
                stdout=open(out / "eval.log", "w"), stderr=subprocess.STDOUT).returncode
            shutil.copytree(runs[-1], out, dirs_exist_ok=True)
        else:
            status["exit"] = status.get("exit", "no run directory")
    status["seconds"] = round(time.time() - started)
    shutil.rmtree(work, ignore_errors=True)
    return status


def main() -> None:
    for sub in ("queue", "running", "done", "nodes"):
        (GA / sub).mkdir(parents=True, exist_ok=True)
    while not (GA / "stop").exists():
        heartbeat(None)
        if not server_up():
            time.sleep(20)
            continue
        claimed = claim()
        if claimed is None:
            time.sleep(15)
            continue
        name, task = claimed
        running = GA / "running" / f"{name}.{NODE}"
        finished = threading.Event()

        def keep_alive():
            while not finished.wait(30):
                heartbeat(name)
                running.touch()
        threading.Thread(target=keep_alive, daemon=True).start()
        heartbeat(name)
        try:
            status = run_task(name, task)
        finally:
            finished.set()
        (GA / "done" / f"{name}.json").write_text(json.dumps({**task, **status}, indent=1))
        (GA / "running" / f"{name}.{NODE}").unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
