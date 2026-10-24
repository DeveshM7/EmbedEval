#!/usr/bin/env python3
"""
One node's part of a run's work. pipeline.sbatch starts one per node, per
phase, with srun:

    node_worker.py --run RUN --config FROZEN.json --phase triage
    node_worker.py --run RUN --config FROZEN.json --phase validate

The PRs come from a queue (claims.py): each of the node's slots takes the
next PR no one has, so a fast node does more and a slow one less. A node
whose queue is empty waits while other nodes still hold PRs, and takes over
those of a node that died; then it exits.

triage    start the node's own model server (caches in nodes/node<N>/),
          then take PRs parallel.triage_per_node at a time, each capped at
          caps.triage_pr, then stop the server. If the server never comes
          up, the node takes no PRs: the other nodes do them.
validate  only once the run's triage is complete: take accepted PRs
          parallel.validate_per_node at a time, generate, build and validate
          each, capped; a PR's image and overlays are always removed
          afterwards. While the run's instance images exceed disk_guard_gb,
          no new PR is taken. No model server runs.

Every PR gets status/<pr>.json once a phase is through with it (status.py).
Each step runs in its own process group, killed whole on a timeout. On
SIGTERM (scancel, the job's time limit) it stops its steps and the server and
exits; what was unfinished has no status, so a resubmit redoes it.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CLUSTER = Path(__file__).resolve().parent
REPO = CLUSTER.parent
sys.path[:0] = [str(CLUSTER), str(REPO / "scripts")]
import claims  # noqa: E402
import config as cluster_config  # noqa: E402
import report  # noqa: E402
import status  # noqa: E402

PY = sys.executable
WARM = ".warm"                       # in the compile cache once a server filled it
_children: set[int] = set()          # process groups of running steps
_lock = threading.Lock()
_stopping = threading.Event()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] node{N} {os.uname().nodename.split('.')[0]}: {msg}",
          flush=True)


def kill_group(pgid: int, grace: int = 20) -> None:
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(1)


def step(cmd: list[str], logfile: Path, minutes: float, env: dict) -> int:
    """Run one step in its own process group; 124 if it hit `minutes`."""
    if _stopping.is_set():
        return 143
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "a") as fh:
        fh.write(f"\n$ {' '.join(cmd)}   (cap {minutes:.0f} min)\n")
        fh.flush()
        p = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT,
                             start_new_session=True)
        with _lock:
            _children.add(p.pid)
        try:
            rc = p.wait(timeout=max(60, minutes * 60))
        except subprocess.TimeoutExpired:
            kill_group(p.pid)
            p.wait()
            rc = 124
        finally:
            with _lock:
                _children.discard(p.pid)
    # Ended because this worker is being stopped (SIGTERM kills its steps,
    # which then return -15): not an outcome of the step at all.
    return 143 if _stopping.is_set() else rc


def record(run: Path, pr: int, stage: str, info: dict) -> bool:
    """Write a PR's outcome -- unless the worker is stopping: then whatever
    was cut short stays without an outcome, so a resubmit redoes it."""
    if _stopping.is_set():
        return False
    status.write_stage(run, pr, stage, info)
    try:                        # settled now, so in the ledger now (report.py would catch up)
        report.to_ledger(run, {pr})
    except Exception as e:
        log(f"ledger: could not record {pr}: {type(e).__name__}: {e}")
    return True


_queue: claims.Queue | None = None     # this phase's queue, for on_sigterm


def drain(q: claims.Queue, todo: list[int], one, slots: int, until: float,
          ready=lambda: True) -> None:
    """Run one(pr) on PRs taken from the queue, `slots` at a time, until none
    is left or `until` (a time) has passed. While ready() is false, no PR is
    taken. A slot with nothing to take waits while other nodes still hold
    PRs, since a node that dies frees its PRs for the others."""
    global _queue
    _queue = q

    def slot() -> None:
        while not _stopping.is_set() and time.time() < until:
            if not ready():
                time.sleep(claims.BEAT)
                continue
            pr = q.take(todo)
            if pr is None:
                if q.busy_elsewhere(todo):
                    time.sleep(claims.BEAT)
                    continue
                return
            try:
                one(pr)
            finally:            # done if it now has its outcome (written by this node)
                q.release(pr, done=not q.needed(pr))

    with ThreadPoolExecutor(slots) as pool:
        list(pool.map(lambda _: slot(), range(slots)))


# ---------------------------------------------------------------- triage


_filling = None    # the open lock file while this node fills the shared compile cache


def compile_cache(cfg: dict, node_dir: Path) -> Path:
    """The vLLM compile cache this node's server uses. The server sees it at
    one fixed path, /vllm-cache (serve_triage_model.sh), so the paths vLLM
    writes into it hold for any copy. The first server to start fills the
    shared cache in place, holding a lock; servers starting meanwhile compile
    into an empty cache of their node. Once the shared cache is warm, each
    server gets a private copy of it: a warm cache can still lack a piece
    (one model part compiled only later), and servers compiling that piece
    into one shared cache at once break each other on NFS."""
    global _filling
    cache = Path(cfg["derived"]["vllm_cache"])
    if (cache / WARM).exists():
        own = node_dir / "work" / "vllm-cache"
        os.environ["EMBEDEVAL_WORK"] = str(node_dir / "work")
        import apptainer_util as au
        au.remove(own)
        shutil.copytree(cache, own, ignore=shutil.ignore_patterns(".fill.lock"))
        log(f"compile cache copied from {cache}")
        return own
    cache.mkdir(parents=True, exist_ok=True)
    f = open(cache / ".fill.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        log("compile cache being filled by another node; compiling privately")
        return node_dir / "scr" / "cache" / "vllm"
    _filling = f
    log(f"filling the compile cache {cache}")
    return cache


def cache_filled(cfg: dict) -> None:
    global _filling
    if _filling:
        (Path(cfg["derived"]["vllm_cache"]) / WARM).touch()
        _filling.close()
        _filling = None


def start_server(cfg: dict, env: dict, node_dir: Path) -> subprocess.Popen:
    scr = node_dir / "scr"
    senv = {**env, "EMBEDEVAL_SCRATCH": str(scr), "PORT": "8000",
            "EMBEDEVAL_VLLM_CACHE": str(compile_cache(cfg, node_dir))}
    senv.pop("SLURM_SUBMIT_DIR", None)    # the script then reads models.json from the repo
    out = open(LOGS / "server.log", "a")
    p = subprocess.Popen(["bash", str(REPO / "scripts" / "serve_triage_model.sh"),
                          cfg["model"]["profile"]], cwd=REPO, env=senv, stdout=out,
                         stderr=subprocess.STDOUT, start_new_session=True)
    with _lock:
        _children.add(p.pid)
    return p


def server_ready(p: subprocess.Popen, minutes: float) -> bool:
    deadline = time.time() + minutes * 60
    while time.time() < deadline and not _stopping.is_set():
        if p.poll() is not None:
            return False
        try:
            urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=5)
            return True
        except Exception:
            time.sleep(10)
    return False


def triage(cfg: dict, run: Path, env: dict, node_dir: Path) -> None:
    todo = status.triage_todo(run)
    log(f"triage: {len(todo)} PRs left in the run")
    if not todo:
        return
    caps, profile = cfg["caps"], cfg["model"]["profile"]
    t0 = time.time()
    server = start_server(cfg, env, node_dir)
    try:
        if not server_ready(server, caps["server_start"]):
            log("model server did not come up; see server.log. Taking no PRs: "
                "the other nodes do them")
            return
        log(f"model server ready after {(time.time() - t0) / 60:.1f} min")
        cache_filled(cfg)
        # verdicts are copied out per PR, so stop starting PRs 5 min before the phase cap
        deadline = t0 + (caps["triage_phase"] - 5) * 60

        def one(pr: int) -> None:
            left = (deadline - time.time()) / 60
            if left < 2 or _stopping.is_set():
                return                                    # stays to do: a resubmit runs it
            cap = min(caps["triage_pr"], left)
            start = time.time()
            rc = step([PY, "scripts/triage_prs.py", "--run-dir", str(run), "run", "--profile",
                       profile, "--pr", str(pr), "--parallel", "1"],
                      LOGS / f"triage-{pr}.log", cap, env)
            if rc == 143:
                return
            minutes = round((time.time() - start) / 60, 1)
            f = run / "triage" / f"{pr}.json"
            if f.exists() and f.stat().st_mtime >= start:
                v = json.loads(f.read_text())
                pair = f"{v['platform']} / {v['scenario']}" if v.get("platform") else None
                info = {"state": "done", "verdict": v.get("verdict"), "pair": pair,
                        "confidence": v.get("confidence"), "minutes": minutes, "node": N}
            elif rc == 124:
                info = {"state": "cap", "minutes": minutes, "node": N,
                        "note": f"stopped at the {cap:.0f}-min cap"}
            else:
                info = {"state": "failed", "minutes": minutes, "node": N,
                        "note": f"no verdict (rc={rc}); log: {LOGS / f'triage-{pr}.log'}"}
            if record(run, pr, "triage", info):
                log(f"triage {pr}: {info.get('verdict') or info['state']} ({minutes} min)")

        q = claims.Queue(run, "triage", ME, lambda pr: status.triage_needed(run, pr))
        drain(q, todo, one, cfg["parallel"]["triage_per_node"], deadline - 2 * 60)
    finally:
        kill_group(server.pid)
        with _lock:
            _children.discard(server.pid)
        log("model server stopped")


# ---------------------------------------------------------------- validate


def images_gb(run: Path) -> float:
    total = 0
    for f in (run / "nodes").glob("node*/work/images/instances/*"):
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total / 2**30


def sweep(work: Path) -> None:
    """Remove what a job cut short left in this node's work folder: build
    temp folders and partly written images. Nothing of this node is building
    yet, so none of it is in use."""
    os.environ["EMBEDEVAL_WORK"] = str(work)
    import apptainer_util as au
    leftovers = list((work / "tmp").glob("*")) + list((work / "images" / "instances").glob(".*.partial-*"))
    for p in leftovers:
        try:
            au._stop(au._holders(p) if p.is_dir() else [])
            au.remove(p)
            log(f"removed leftover {p.name}")
        except OSError as e:
            log(f"could not remove leftover {p}: {e}")


def validate(cfg: dict, run: Path, env: dict) -> None:
    left = status.triage_todo(run)
    if left:
        log(f"triage is not complete ({len(left)} PRs left): no validation until a "
            "job finishes it")
        return
    todo = status.validate_todo(run)
    log(f"validation: {len(todo)} PRs left in the run")
    if not todo:
        return
    caps = cfg["caps"]
    t0 = time.time()
    deadline = t0 + (caps["validate_phase"] - 3) * 60
    venv = {**env, "EMBEDEVAL_BUILD_TIMEOUT_MIN": str(caps["build"])}
    sweep(Path(env["EMBEDEVAL_WORK"]))

    def one(pr: int) -> None:
        iid = f"zephyr__zephyr-{pr}"
        start = time.time()

        def left() -> float:
            return min(caps["validate_pr"] - (time.time() - start) / 60,
                       (deadline - time.time()) / 60)

        def done(state: str, note: str = "", outcome: str | None = None) -> None:
            info = {"state": state, "minutes": round((time.time() - start) / 60, 1), "node": N}
            if outcome:
                info["outcome"] = outcome
            if note:
                info["note"] = note
            if record(run, pr, "validate", info):
                log(f"validate {pr}: {outcome or state} {note}")

        if left() < 10 or _stopping.is_set():
            return                                         # stays to do
        lg = LOGS / f"validate-{pr}.log"
        verdict = run / "triage" / f"{pr}.json"
        tried = []
        while True:
            tried.append(json.loads(verdict.read_text())["platform"])
            note = f"retried on {tried[-1]}: nothing ran on {tried[0]}" if len(tried) > 1 else ""
            res = attempt(run, pr, iid, verdict, lg, left, venv)
            if res is None:
                return                                     # stopping: stays to do
            state, what, published = res
            nothing_ran = what.startswith("INVALID: nothing ran")
            if nothing_ran and len(tried) == 1 and left() >= 10:
                verdict = other_board(run, pr, verdict, tried)
                if verdict:
                    log(f"validate {pr}: nothing ran on {tried[0]}; retrying on another board")
                    continue
            if state == "done":
                done("done", "; ".join(x for x in (note, published) if x), outcome=what)
            else:
                done("failed", f"{what}; {note}" if note else what)
            return

    warned = threading.Event()

    def room() -> bool:
        used = images_gb(run)
        if used <= cfg["disk_guard_gb"]:
            return True
        if not warned.is_set():
            warned.set()
            log(f"disk guard: instance images at {used:.0f} GB (limit "
                f"{cfg['disk_guard_gb']} GB); taking no new PR until there is room")
        return False

    q = claims.Queue(run, "validate", ME, lambda pr: status.validate_needed(run, pr))
    drain(q, todo, one, cfg["parallel"]["validate_per_node"], deadline - 10 * 60, room)


def attempt(run: Path, pr: int, iid: str, verdict: Path, lg: Path, left, venv: dict):
    """One generate, build, validate of the verdict's pair: ("done", outcome,
    note), ("failed", why, ""), or None if the worker is stopping. A VALID
    image is pushed to the registry, if there is one, before it is removed;
    a push that fails leaves the PR VALID, with a note."""
    step([PY, "scripts/cleanup_instance.py", iid], lg, 10, venv)   # leftovers of a cut-short try
    try:
        rc = step([PY, "scripts/generate_instance.py", "--pr", str(pr), "--triage", str(verdict)],
                  lg, min(5, left()), venv)
        if rc == 143:
            return None
        if rc:
            return "failed", "generate failed: the verdict's pair is not runnable", ""
        rc = step([PY, "scripts/build_instances.py", "--instance", iid], lg,
                  min(2 * float(venv["EMBEDEVAL_BUILD_TIMEOUT_MIN"]) + 5, left()), venv)
        if rc == 143:
            return None
        if rc:
            return "failed", "build failed" if rc != 124 else "build hit the cap", ""
        if left() < 3:
            return "failed", "no time left to validate", ""
        rc = step([PY, "scripts/validate_instance.py", iid], lg, left(), venv)
        if rc == 143:
            return None
        report = run / "instances" / iid / "validation.json"
        reason = json.loads(report.read_text()).get("reason") if report.exists() else None
        if rc == 0:
            if not venv.get("EMBEDEVAL_REGISTRY"):
                return "done", "VALID", ""
            rc = step([PY, "scripts/registry.py", "push", iid], lg, max(left(), 10), venv)
            return "done", "VALID", "" if rc == 0 else (
                "not published (push cut short)" if rc == 143 else
                f"not published (push failed, rc={rc}; see the log); scripts/registry.py publish")
        if rc == 124:
            return "failed", "validation hit the cap", ""
        return "done", f"INVALID: {reason or f'rc={rc}'}", ""
    finally:
        step([PY, "scripts/cleanup_instance.py", iid], lg, 10, venv)


def other_board(run: Path, pr: int, verdict: Path, tried: list[str]) -> Path | None:
    """The verdict moved to the next board twister selects the same scenario
    on, QEMU boards first: the dry run cannot see every filter (a Kconfig
    dependency, say), so a selected pair can still skip at run time."""
    v = json.loads(verdict.read_text())
    dry = json.loads((run / "dryrun" / f"{pr}.json").read_text())
    boards = [p["board"] for p in dry["pairs"] if p["scenario"] == v["scenario"]
              and p["status"] == "selected" and p["board"] not in tried]
    boards.sort(key=lambda b: not b.startswith("qemu"))      # stable: dry-run order otherwise
    if not boards:
        return None
    out = run / "validate" / f"{pr}-{boards[0].replace('/', '_')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(v | {"platform": boards[0], "retry_of": tried[0]}, indent=2) + "\n")
    return out


# ---------------------------------------------------------------- main


def on_sigterm(signum, frame) -> None:
    _stopping.set()
    log(f"signal {signum}: stopping running steps")
    with _lock:
        groups = list(_children)
    for g in groups:
        kill_group(g, grace=10)
    if _queue:
        _queue.release_all()                # what was cut short is free at once
    os._exit(128 + signum)


def main() -> None:
    global N, NN, LOGS, ME
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", required=True)
    p.add_argument("--config", required=True, help="the frozen settings submit.py wrote")
    p.add_argument("--phase", required=True, choices=["triage", "validate"])
    args = p.parse_args()
    N = int(os.environ.get("SLURM_PROCID", "0"))
    NN = int(os.environ.get("SLURM_NTASKS", "1"))
    ME = f"{os.environ.get('SLURM_JOB_ID', 'local')}-node{N}"
    run = Path(args.run)
    cfg = json.loads(Path(args.config).read_text())
    node_dir = run / "nodes" / f"node{N}"
    LOGS = run / "logs" / f"node{N}"
    LOGS.mkdir(parents=True, exist_ok=True)
    sys.stdout = open(LOGS / f"worker-{args.phase}.log", "a", buffering=1)
    sys.stderr = sys.stdout
    signal.signal(signal.SIGTERM, on_sigterm)
    env = {**os.environ, **cluster_config.env_for(cfg, run),
           # this node's own images, overlays and temp files: its cleanup can
           # never touch another node's work
           "EMBEDEVAL_WORK": str(node_dir / "work")}
    log(f"phase {args.phase}, node {N} of {NN}, run {run}")
    claims.keep_alive(run, ME)
    if args.phase == "triage":
        triage(cfg, run, env, node_dir)
    else:
        validate(cfg, run, env)
    log(f"phase {args.phase} done")


N, NN, LOGS, ME = 0, 1, Path("."), "local-node0"

if __name__ == "__main__":
    main()
