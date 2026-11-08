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
import re
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
          ready=lambda: True, stop=lambda: False) -> None:
    """Run one(pr) on PRs taken from the queue, `slots` at a time, until none
    is left or `until` (a time) has passed. While ready() is false, no PR is
    taken. A slot with nothing to take waits while other nodes still hold
    PRs, since a node that dies frees its PRs for the others."""
    global _queue
    _queue = q

    def slot() -> None:
        while not _stopping.is_set() and time.time() < until and not stop():
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
            "EMBEDEVAL_GPU_MEMORY_UTILIZATION": str(cfg["model"]["gpu_memory_utilization"]),
            "EMBEDEVAL_MAX_MODEL_LEN": str(cfg["model"]["max_model_len"]),
            "EMBEDEVAL_MAX_NUM_SEQS": str(cfg["model"]["max_num_seqs"]),
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


def triage(cfg: dict, run: Path, env: dict, node_dir: Path, minutes: float) -> None:
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
        # verdicts are copied out per PR, so stop starting PRs 5 min before the phase ends
        deadline = t0 + (minutes - 5) * 60

        def one(pr: int) -> None:
            left = (deadline - time.time()) / 60
            if left < 2 or _stopping.is_set():
                return                                    # stays to do: a resubmit runs it
            cap = min(caps["triage_pr"], left)
            start = time.time()
            rc = step([PY, "scripts/triage_prs.py", "--run-dir", str(run), "run", "--project",
                       cfg["project"], "--profile",
                       profile, "--pr", str(pr), "--parallel", "1"],
                      LOGS / f"triage-{pr}.log", cap, env)
            if rc == 143:
                return
            minutes = round((time.time() - start) / 60, 1)
            f = run / "triage" / f"{pr}.json"
            if f.exists() and f.stat().st_mtime >= start:
                v = json.loads(f.read_text())
                pair = (f"{v['platform']} / {v['scenario']}" if v.get("platform") else
                        v.get("profile") or v.get("test_target"))
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


def statements(cfg: dict, run: Path, env: dict, node_dir: Path, minutes: float) -> None:
    """Problem statements, written by the served model, for VALID instances
    without one (observation-only instances have no triage verdict to take
    one from); with statement_recheck, existing statements are checked under
    the current rules and replaced when they fail."""
    recheck = bool(cfg.get("statement_recheck"))
    todo = status.statement_todo(run, recheck)
    log(f"statements: {len(todo)} instances to " + ("write or recheck" if recheck else "write"))
    if not todo:
        return
    caps, profile = cfg["caps"], cfg["model"]["profile"]
    t0 = time.time()
    server = start_server(cfg, env, node_dir)
    try:
        if not server_ready(server, caps["server_start"]):
            log("model server did not come up; see server.log. Taking no instances")
            return
        log(f"model server ready after {(time.time() - t0) / 60:.1f} min")
        deadline = t0 + (minutes - 5) * 60

        tried: set[int] = set()     # a statement that failed is not retried in this job

        def one(pr: int) -> None:
            left = (deadline - time.time()) / 60
            if left < 2 or _stopping.is_set():
                return
            tried.add(pr)
            start = time.time()
            folder = run / status.instance_folder(run, pr) / f"contiki-ng__contiki-ng-{pr}"
            rc = step([PY, "scripts/triage_prs.py", "--run-dir", str(run), "statement", "--project",
                       cfg["project"], "--profile", profile, "--instance", str(folder),
                       *(["--recheck"] if recheck else [])],
                      LOGS / f"statement-{pr}.log", min(caps["triage_pr"], left), env)
            if rc == 143:
                return
            info = {"state": "done" if rc == 0 else "failed", "node": N,
                    "minutes": round((time.time() - start) / 60, 1)}
            if record(run, pr, "statement", info):
                log(f"statement {pr}: {info['state']} ({info['minutes']} min)")

        q = claims.Queue(run, "statement", ME, lambda pr: pr not in tried and pr in status.statement_todo(run, recheck))
        drain(q, todo, one, cfg["parallel"]["triage_per_node"], deadline - 2 * 60)
    finally:
        kill_group(server.pid)
        with _lock:
            _children.discard(server.pid)
        log("model server stopped")


# ---------------------------------------------------------------- validate


def finalize(cfg: dict, run: Path, pr: int, env: dict, left) -> None:
    """An observed-valid PR as a runtime instance: generated from its observation
    into <run>/finalized/, built, and validated there (named before failures,
    after passes, three before repeats). The observation folder stays as it is."""
    iid = f"contiki-ng__contiki-ng-{pr}"
    final = run / "finalized"
    fenv = {**env, "EMBEDEVAL_INSTANCES_DIR": str(final)}
    lg = LOGS / f"finalize-{pr}.log"
    start = time.time()
    outcome = None
    try:
        step([PY, "scripts/cleanup_instance.py", iid], lg, 10, fenv)
        for cmd, cap in (([PY, "scripts/generate_instance.py", "--project", "contiki-ng", "--pr", str(pr),
                           "--from-observed", str(run / "instances" / iid), "--out", str(final)], 5),
                         ([PY, "scripts/build_instances.py", "--instance", iid], 2 * cfg["caps"]["build"] + 5),
                         ([PY, "scripts/validate_instance.py", iid], left())):
            rc = step(cmd, lg, min(cap, left()), fenv)
            if rc == 143:
                return
            if rc and cmd[1] != "scripts/validate_instance.py":
                outcome = f"failed: {Path(cmd[1]).stem} rc={rc}"
                break
        if outcome is None:
            receipt = final / iid / "validation.json"
            r = json.loads(receipt.read_text()) if receipt.exists() else {}
            outcome = ("VALID" if r.get("valid") else
                       "HOLD" if r.get("status") == "hold" else f"INVALID: {r.get('reason', 'no receipt')}")
        if record(run, pr, "finalize", {"state": "done" if not outcome.startswith("failed") else "failed",
                                        "outcome": outcome, "node": N,
                                        "minutes": round((time.time() - start) / 60, 1)}):
            log(f"finalize {pr}: {outcome}")
    finally:
        step([PY, "scripts/cleanup_instance.py", iid], lg, 10, fenv)


def observe(cfg: dict, run: Path, env: dict, minutes: float) -> None:
    """Run Contiki's upstream test inventory without model-selected expectations,
    and finalize what observes valid."""
    todo = list(dict.fromkeys(status.observe_todo(run) + status.finalize_todo(run)))
    log(f"observation: {len(todo)} PRs left in the run")
    if not todo:
        return
    deadline = time.time() + (minutes - 3) * 60
    sweep(Path(env["EMBEDEVAL_WORK"]))
    attempted = set()
    # A PR whose affected units span profiles is observed under the one its
    # manifest entry names; without one, generation refuses to choose.
    profiles = {e["pr"]: e["observe_profile"] for e in status.manifest(run)["prs"]
                if e.get("observe_profile")}

    def one(pr: int) -> None:
        attempted.add(pr)
        start = time.time()
        iid = f"contiki-ng__contiki-ng-{pr}"
        lg = LOGS / f"observe-{pr}.log"

        def left() -> float:
            return min(cfg["caps"]["validate_pr"] - (time.time() - start) / 60,
                       (deadline - time.time()) / 60)

        if left() < 10 or _stopping.is_set():
            return
        if pr in status.finalize_todo(run):               # observed in an earlier job
            finalize(cfg, run, pr, env, left)
            return
        try:
            for cmd, cap in (([PY, "scripts/generate_instance.py", "--project", "contiki-ng",
                               "--pr", str(pr), "--observe",
                               *(["--profile", profiles[pr]] if pr in profiles else [])], 5),
                             ([PY, "scripts/build_instances.py", "--instance", iid],
                              2 * cfg["caps"]["build"] + 5),
                             ([PY, "scripts/validate_instance.py", iid], left())):
                rc = step(cmd, lg, min(cap, left()), env)
                if rc == 143:
                    return
                observed = run / "instances" / iid / "observed.json"
                if rc == 3 and cmd[1] == "scripts/generate_instance.py":
                    state = "done"                       # nothing to observe: final, not retried
                    break
                if rc and not (cmd[1] == "scripts/validate_instance.py" and observed.exists()):
                    state = "cap" if rc == 124 else "failed"
                    break
            else:
                state = "done"
            note = (str(observed) if observed.exists() else
                    "not observable under this profile (see the log)" if rc == 3 else "")
            if record(run, pr, "observe", {"state": state, "minutes": round((time.time() - start) / 60, 1),
                                            "node": N, "note": note}):
                log(f"observe {pr}: {state}")
        finally:
            step([PY, "scripts/cleanup_instance.py", iid], lg, 10, env)
        if pr in status.finalize_todo(run) and left() >= 10 and not _stopping.is_set():
            finalize(cfg, run, pr, env, left)

    # Failed/capped observations may resume in a later job, but must not spin
    # on the same deterministic failure for this job's entire wall time.
    q = claims.Queue(run, "observe", ME,
                     lambda pr: pr not in attempted and (pr in status.observe_todo(run)
                                                         or pr in status.finalize_todo(run)))
    drain(q, todo, one, cfg["parallel"]["validate_per_node"], deadline - 10 * 60)


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


def validate(cfg: dict, run: Path, env: dict, minutes: float) -> None:
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
    deadline = t0 + (minutes - 3) * 60
    venv = {**env, "EMBEDEVAL_BUILD_TIMEOUT_MIN": str(caps["build"])}
    sweep(Path(env["EMBEDEVAL_WORK"]))

    def one(pr: int) -> None:
        iid = (f"contiki-ng__contiki-ng-{pr}" if cfg["project"] == "contiki-ng"
               else f"zephyr__zephyr-{pr}")
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
        source = status.entry(run, pr).get("instance")
        if source:                  # a revalidation: render the existing instance again
            res = attempt(run, pr, iid, None, lg, left, venv, render=run / "sources" / source)
            if res is None:
                return
            state, what, published = res
            return done(state, published, outcome=what) if state == "done" else done("failed", what)
        verdict = run / "triage" / f"{pr}.json"
        tried = []
        while True:
            tried.append(json.loads(verdict.read_text()).get("platform", "contiki-ng"))
            note = f"retried on {tried[-1]}: nothing ran on {tried[0]}" if len(tried) > 1 else ""
            res = attempt(run, pr, iid, verdict, lg, left, venv)
            if res is None:
                return                                     # stopping: stays to do
            state, what, published = res
            nothing_ran = what.startswith("INVALID: nothing ran")
            if cfg["project"] == "zephyr" and nothing_ran and len(tried) == 1 and left() >= 10:
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


def attempt(run: Path, pr: int, iid: str, verdict: Path | None, lg: Path, left, venv: dict,
            render: Path | None = None):
    """One generate (or, with render, a re-render of an existing instance),
    build, validate of the verdict's pair: ("done", outcome, note), ("failed",
    why, ""), or None if the worker is stopping. A VALID image is pushed to
    the registry, if there is one, before it is removed; a push that fails
    leaves the PR VALID, with a note."""
    step([PY, "scripts/cleanup_instance.py", iid], lg, 10, venv)   # leftovers of a cut-short try
    try:
        project = "contiki-ng" if iid.startswith("contiki-ng__") else "zephyr"
        if render:
            shutil.rmtree(run / "instances" / iid, ignore_errors=True)
            generate = [PY, "scripts/generate_instance.py", "--project", project, "--render", str(render),
                        "--out", str(run / "instances")]
        else:
            generate = [PY, "scripts/generate_instance.py", "--project", project,
                        "--pr", str(pr), "--triage", str(verdict)]
        rc = step(generate, lg, min(10, left()), venv)
        if rc == 143:
            return None
        if rc:
            return "failed", ("render failed; see the log" if render
                              else "generate failed: the verdict's pair is not runnable"), ""
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
        receipt = json.loads(report.read_text()) if report.exists() else {}
        reason = receipt.get("reason")
        if receipt.get("status") == "hold":
            return "done", "HOLD", reason or "infrastructure/profile hold"
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


# ---------------------------------------------------------------- evaluate
#
# Per VALID instance of the run: build its image, run the agent on it
# (harness/run.py: no network, the problem statement and the repository
# only), grade the patch in a fresh container (harness/evaluate_patches.py),
# remove the image. The model is the job's (submit.py evaluate --model); it
# is reached through an SSH tunnel to the node of its server job. Outputs:
# <run>/outputs/contiki-ng/<model>/<PR>/ (patch, trajectory) and
# <run>/outputs/grades/<model>/<instance>.json.


def model_tunnel(job: str, minutes: float, port: int = 8000):
    """(the tunnel process, api base) to the server of job `job`, once it answers."""
    deadline = time.time() + minutes * 60
    node = ""
    while time.time() < deadline and not _stopping.is_set():
        r = subprocess.run(["squeue", "-h", "-j", job, "-o", "%T %N"], capture_output=True, text=True)
        state, _, node = r.stdout.strip().partition(" ")
        if state == "RUNNING" and node:
            break
        if not state:
            return None, f"server job {job} is not in the queue"
        time.sleep(30)
    else:
        return None, f"server job {job} did not start"
    local = port + N             # one tunnel per node, and never the server's own port
    tunnel = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                               "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=60",
                               "-N", "-L", f"{local}:127.0.0.1:8000", node], start_new_session=True)
    with _lock:
        _children.add(tunnel.pid)
    base = f"http://127.0.0.1:{local}/v1"
    while time.time() < deadline and not _stopping.is_set() and tunnel.poll() is None:
        try:
            urllib.request.urlopen(base + "/models", timeout=5)
            return tunnel, base
        except Exception:
            time.sleep(20)
    kill_group(tunnel.pid)
    return None, f"server of job {job} on {node} did not answer"


def server_alive(base: str, tunnel) -> bool:
    if tunnel.poll() is not None:
        return False
    try:
        urllib.request.urlopen(base + "/models", timeout=10)
        return True
    except Exception:
        return False


def server_capacity(job: str) -> float | None:
    """How many requests of the full context length the server's KV cache
    holds at once (vLLM's "Maximum concurrency" line in the server job's
    log), or None when the log cannot be read."""
    r = subprocess.run(["scontrol", "show", "job", job], capture_output=True, text=True)
    m = re.search(r"StdOut=(\S+)", r.stdout)
    try:
        text = Path(m.group(1).replace("%j", job)).read_text(errors="replace") if m else ""
    except OSError:
        return None
    found = re.findall(r"Maximum concurrency for [\d,]+ tokens per request: ([\d.]+)x", text)
    return float(found[-1]) if found else None


def watch_queue(base: str, stop: threading.Event, samples: list, every: float = 30) -> None:
    """Append the server's waiting requests (vLLM /metrics) every `every`
    seconds until `stop` is set."""
    url = base.removesuffix("/v1") + "/metrics"
    while not stop.wait(every):
        try:
            text = urllib.request.urlopen(url, timeout=10).read().decode(errors="replace")
        except Exception:
            continue
        waiting = re.findall(r"^vllm:num_requests_waiting\{[^}]*\} ([\d.]+)", text, re.M)
        if waiting:
            samples.append(sum(float(w) for w in waiting))


def agent_record(run: Path, model: str, iid: str) -> dict:
    """What the trajectory says of an agent run: exit status, model calls."""
    sys.path.insert(0, str(REPO / "harness"))
    import paths
    f = Path(run) / "outputs" / "contiki-ng" / paths.model_dir(model) / iid.rsplit("-", 1)[1] / "trajectory.json"
    try:
        info = json.loads(f.read_text())["info"]
        return {"exit_status": info.get("exit_status"), "steps": info["model_stats"]["api_calls"]}
    except (OSError, ValueError, KeyError):
        return {"exit_status": None, "steps": None}


def evaluate(cfg: dict, run: Path, env: dict, minutes: float) -> None:
    model, ev, caps = cfg["evaluate_model"], cfg["evaluate"], cfg["caps"]
    todo = status.evaluate_todo(run, model)
    log(f"evaluate {model}: {len(todo)} instances left in the run")
    if not todo:
        return
    t0 = time.time()
    deadline = t0 + (minutes - 3) * 60
    venv = {**env, "EMBEDEVAL_BUILD_TIMEOUT_MIN": str(caps["build"])}
    tunnel, base = model_tunnel(cfg["evaluate_server_job"], caps["server_start"])
    if tunnel is None:
        log(f"no model server: {base}. Taking no instances")
        return
    venv["EMBEDEVAL_LOCAL_API_BASE"] = base
    log(f"model server of job {cfg['evaluate_server_job']} answers at {base}")
    # one agent per full-length request the server holds: more would wait on
    # each other, and the time limit would measure the server, not the model
    held = server_capacity(cfg["evaluate_server_job"])
    lanes = ev["per_node"] if held is None else min(ev["per_node"], int(held))
    log(f"model server holds {held if held is not None else '?'} full-length requests: {lanes} agents")
    if lanes < 1:
        log("the model server holds no full-length request; size its memory "
            "(model.gpu_memory_utilization, slurm.mem). Taking no instances")
        kill_group(tunnel.pid)
        return
    sweep(Path(env["EMBEDEVAL_WORK"]))
    grades = run / "outputs" / "grades" / model
    grades.mkdir(parents=True, exist_ok=True)
    key = status.evaluate_key(model)
    lost = threading.Event()        # the model server is gone: take no more instances
    later: set[int] = set()         # given back for the next job, not again on this server
    # an instance is started only with time for its build, the agent and the grading
    enough = lambda: (deadline - time.time()) / 60 >= ev["time_limit"] + caps["build"] + 25

    def one(pr: int) -> None:
        if lost.is_set() or not server_alive(base, tunnel):
            lost.set()
            return                                         # stays to do: the next job runs it
        iid = f"contiki-ng__contiki-ng-{pr}"
        lg, start = LOGS / f"evaluate-{pr}.log", time.time()
        if not enough() or _stopping.is_set():
            return                                         # stays to do
        info = {"model": model, "instance": iid, "node": N,
                "limits": {"step_limit": ev["step_limit"], "time_limit": ev["time_limit"]}}

        def done(state: str, **more) -> None:
            info.update(state=state, minutes=round((time.time() - start) / 60, 1), **more)
            if record(run, pr, key, info):
                log(f"evaluate {pr}: {info.get('grade') or info.get('note')}")

        step([PY, "scripts/cleanup_instance.py", iid], lg, 10, venv)
        try:
            rc = step([PY, "scripts/build_instances.py", "--instance", iid], lg, 2 * caps["build"] + 5, venv)
            if rc == 143:
                return
            if rc:
                return done("failed", note="build failed" if rc != 124 else "build hit the cap")
            agent_start, waiting, watched = time.time(), [], threading.Event()
            watcher = threading.Thread(target=watch_queue, args=(base, watched, waiting), daemon=True)
            watcher.start()
            try:
                rc = step([PY, "harness/run.py", "--instance", iid, "--model", model,
                           "--step-limit", str(ev["step_limit"]), "--time-limit", str(ev["time_limit"])],
                          lg, ev["time_limit"] + 15, venv)
            finally:
                watched.set()
            if rc == 143:
                return
            agent = {**agent_record(run, model, iid), "agent_minutes": round((time.time() - agent_start) / 60, 1),
                     # share of the run's samples in which the server had requests waiting
                     "server_queued": round(sum(w > 0 for w in waiting) / len(waiting), 2) if waiting else None}
            if not server_alive(base, tunnel):
                # the agent lost its model: not the agent's outcome, so it is run again
                lost.set()
                return done("failed", retry=True, note="model server gone during the run", **agent)
            out = grades / f"{iid}.json"
            rc = step([PY, "harness/evaluate_patches.py", "--instance", iid, "--model", model,
                       "--output", str(out)], lg, 45, venv)
            if rc == 143:
                return
            if not out.exists():
                return done("failed", note=f"no grade (rc={rc}); see the log", **agent)
            g = json.loads(out.read_text())["results"][0]
            timed_out = agent["exit_status"] == "TimeExceeded"
            if timed_out and agent["server_queued"]:
                # its requests waited for the server: the limit measured the server
                later.add(pr)
                return done("failed", retry=True, note="timed out while the model server queued requests",
                            grade=g.get("grade"), error=g.get("error"), timed_out=True, **agent)
            done("done", grade=g.get("grade"), error=g.get("error"), timed_out=timed_out,
                 step_limit_hit=agent["exit_status"] == "LimitsExceeded", **agent)
        finally:
            step([PY, "scripts/cleanup_instance.py", iid], lg, 10, venv)

    q = claims.Queue(run, key, ME, lambda pr: pr not in later and status.evaluate_needed(run, pr, model))
    try:
        drain(q, todo, one, lanes, deadline - 5 * 60, stop=lambda: lost.is_set() or not enough())
        if lost.is_set():
            log("the model server is gone: the rest stays to do for the next evaluate job")
    finally:
        kill_group(tunnel.pid)


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
    p.add_argument("--phase", required=True, choices=["triage", "observe", "validate", "statement", "evaluate"])
    p.add_argument("--minutes", type=float, required=True,
                   help="the phase's time: what is left of the job, less the report's")
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
    log(f"phase {args.phase}, node {N} of {NN}, run {run}, {args.minutes:.0f} min")
    claims.keep_alive(run, ME)
    if args.phase == "triage":
        if cfg["project"] == "contiki-ng" and os.uname().machine != "aarch64":
            sys.exit("Contiki-NG model triage requires Spark ARM64")
        triage(cfg, run, env, node_dir, args.minutes)
    elif args.phase == "statement":
        if os.uname().machine != "aarch64":
            sys.exit("model statements run on Spark ARM64")
        statements(cfg, run, env, node_dir, args.minutes)
    elif args.phase == "evaluate":
        if cfg["project"] != "contiki-ng" or os.uname().machine != "x86_64":
            sys.exit("Contiki-NG evaluation requires x86_64")
        evaluate(cfg, run, env, args.minutes)
    elif args.phase == "observe":
        if cfg["project"] != "contiki-ng" or os.uname().machine != "x86_64":
            sys.exit("Contiki-NG observation requires x86_64")
        observe(cfg, run, env, args.minutes)
    else:
        if cfg["project"] == "contiki-ng" and os.uname().machine != "x86_64":
            sys.exit("Contiki-NG validation requires x86_64")
        validate(cfg, run, env, args.minutes)
    log(f"phase {args.phase} done")


N, NN, LOGS, ME = 0, 1, Path("."), "local-node0"

if __name__ == "__main__":
    main()
