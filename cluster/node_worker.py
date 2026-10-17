#!/usr/bin/env python3
"""
One node's share of a run's work. pipeline.sbatch starts one per node, per
phase, with srun:

    node_worker.py --run RUN --config FROZEN.json --phase triage
    node_worker.py --run RUN --config FROZEN.json --phase validate

Its share (fixed split): the PRs still to do in this phase, sorted, every
NN-th one starting at its index N (SLURM_PROCID of SLURM_NTASKS).

triage    start the node's own model server (caches in nodes/node<N>-job<ID>/),
          triage its PRs parallel.triage_per_node at a time, each capped at
          caps.triage_pr, then stop the server. If the server never comes
          up, its PRs are marked "server failed" and retried on a resubmit.
validate  generate, build and validate its accepted PRs
          parallel.validate_per_node at a time, each capped; a PR's image and
          overlays are always removed afterwards. No model server runs.

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
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CLUSTER = Path(__file__).resolve().parent
REPO = CLUSTER.parent
sys.path[:0] = [str(CLUSTER), str(REPO / "scripts")]
import config as cluster_config  # noqa: E402
import status  # noqa: E402

PY = sys.executable
WARM = ".warm"                       # in the compile cache once a server filled it
_children: set[int] = set()          # process groups of running steps
_lock = threading.Lock()
_nuttx_data_lock = threading.Lock()
_stopping = threading.Event()
_lock_data = threading.Lock()


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


def step(cmd: list[str], logfile: Path, minutes: float, env: dict, cwd: Path = REPO) -> int:
    """Run one step in its own process group; 124 if it hit `minutes`."""
    if _stopping.is_set():
        return 143
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "a") as fh:
        fh.write(f"\n$ {' '.join(cmd)}   (cap {minutes:.0f} min)\n")
        fh.flush()
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT,
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
    return True


def my_share(prs: list[int]) -> list[int]:
    return [pr for i, pr in enumerate(sorted(prs)) if i % NN == N]


def assigned_work(run: Path, phase: str) -> list[int]:
    """Keep node ownership fixed as other nodes write status during a phase."""
    planned = [e["pr"] for e in status.manifest(run)["prs"] if e["stage"] == "to triage"]
    todo = set(status.triage_todo(run) if phase == "triage" else status.validate_todo(run))
    return [pr for pr in my_share(planned) if pr in todo]


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


def nuttx_data(cfg: dict, node_dir: Path) -> Path:
    """This node's copy of the NuttX data (PR records, bare clones): fetches
    and caches are written here, never into the shared, pre-warmed copy."""
    own = node_dir / "work" / "nuttx-data"
    with _nuttx_data_lock:
        if not (own / ".complete").exists():
            shutil.rmtree(own, ignore_errors=True)
            own.mkdir(parents=True)
            subprocess.run(["cp", "-a", f"{cfg['nuttx']['data']}/.", str(own)], check=True)
            (own / ".complete").touch()
    return own


def nuttx_triage_step(cfg: dict, run: Path, pr: int, env: dict, node_dir: Path, cap: float) -> int:
    """Triage one apps PR with the chosen version; its verdict is copied to
    <run>/triage/<pr>.json."""
    n = cfg["nuttx"]
    code = Path(n["individual"] or REPO)
    out = run / "triage_runs" / str(pr)
    shutil.rmtree(out, ignore_errors=True)
    nenv = {**env, "EMBEDEVAL_NUTTX_DATA": str(nuttx_data(cfg, node_dir)),
            "EMBEDEVAL_NUTTX_DATA_RO": n["data"], "EMBEDEVAL_NUTTX_RUNS": str(out),
            "EMBEDEVAL_CONFIG_WORK": str(node_dir / "work" / "config")}
    (node_dir / "work" / "config").mkdir(parents=True, exist_ok=True)
    cmd = [PY, "scripts/triage_nuttx_prs.py", "apps", "--profile", cfg["model"]["profile"],
           "--apps-pr", str(pr), "--parallel", "1", "--max-tool-calls", str(n["max_tool_calls"]),
           "--max-minutes", str(min(n["max_minutes"], cap))]
    if n["followup_review"]:
        cmd.append("--followup-review")
    rc = step(cmd, LOGS / f"triage-{pr}.log", cap, nenv, cwd=code)
    verdicts = list(out.glob(f"*/apps-{pr}.json"))
    if verdicts:
        (run / "triage").mkdir(exist_ok=True)
        shutil.copy(verdicts[0], run / "triage" / f"{pr}.json")
        trace = verdicts[0].with_name(f"apps-{pr}.trace.json")
        if trace.exists():
            shutil.copy(trace, run / "triage" / f"{pr}.trace.json")
    return rc


def nuttx_verdict_info(v: dict) -> dict:
    """status.json fields of an apps-PR verdict: accept when a pair is high
    or medium (it goes on to validation), reject otherwise."""
    pairs = [p for p in v.get("pairs", []) if isinstance(p, dict)]
    sent = [p for p in pairs if p.get("confidence") in ("high", "medium")]
    verdict = ("error" if v.get("kind") in ("error", "budget_exhausted")
               else "accept" if sent else "reject")
    pair = "; ".join(f"#{p['kernel_pr']} {p['confidence']}"
                     + (f" +{[e.get('kernel_pr') for e in p.get('also_needs') or []]}"
                        if p.get("also_needs") else "") for p in sent) or None
    return {"verdict": verdict, "pair": f"{v.get('kind')}: {pair}" if pair else v.get("kind"),
            "confidence": sent[0]["confidence"] if sent else None}


def triage(cfg: dict, run: Path, env: dict, node_dir: Path) -> None:
    mine = assigned_work(run, "triage")
    log(f"triage: {len(mine)} PRs {mine}")
    if not mine:
        return
    caps, profile = cfg["caps"], cfg["model"]["profile"]
    if cfg["project"] == "nuttx":
        time.sleep(N * 6)   # about ten model starts per minute across Spark nodes
    t0 = time.time()
    server = start_server(cfg, env, node_dir)
    try:
        if not server_ready(server, caps["server_start"]):
            log("model server did not come up; see server.log")
            for pr in mine:
                record(run, pr, "triage", {"state": "server failed", "node": N,
                       "note": f"server not ready (log: {LOGS / 'server.log'})"})
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
            if cfg["project"] == "nuttx":
                rc = nuttx_triage_step(cfg, run, pr, env, node_dir, cap)
            else:
                rc = step([PY, "scripts/triage_prs.py", "--run-dir", str(run), "run", "--profile",
                           profile, "--pr", str(pr), "--parallel", "1"],
                          LOGS / f"triage-{pr}.log", cap, env)
            if rc == 143:
                return
            minutes = round((time.time() - start) / 60, 1)
            f = run / "triage" / f"{pr}.json"
            if f.exists() and f.stat().st_mtime >= start:
                v = json.loads(f.read_text())
                if cfg["project"] == "nuttx":
                    info = {"state": "done", **nuttx_verdict_info(v), "minutes": minutes, "node": N}
                else:
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

        with ThreadPoolExecutor(cfg["parallel"]["triage_per_node"]) as pool:
            list(pool.map(one, mine))
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
    if cfg["project"] == "nuttx" and not cfg["nuttx"]["validate"]:
        log("validation: off for this run (nuttx.validate)")
        return
    mine = assigned_work(run, "validate")
    log(f"validation: {len(mine)} PRs {mine}")
    if not mine:
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
        used = images_gb(run)
        if used > cfg["disk_guard_gb"]:
            done("skipped", f"disk guard: instance images at {used:.0f} GB "
                            f"(limit {cfg['disk_guard_gb']} GB)")
            return
        if cfg["project"] == "nuttx":
            validate_nuttx(cfg, run, pr, venv, Path(env["EMBEDEVAL_WORK"]).parent, left, done)
            return
        lg = LOGS / f"validate-{pr}.log"
        verdict = run / "triage" / f"{pr}.json"
        tried = []
        while True:
            tried.append(json.loads(verdict.read_text())["platform"])
            note = f"retried on {tried[-1]}: nothing ran on {tried[0]}" if len(tried) > 1 else ""
            res = attempt(run, pr, iid, verdict, lg, left, venv)
            if res is None:
                return                                     # stopping: stays to do
            state, what = res
            nothing_ran = what.startswith("INVALID: nothing ran")
            if nothing_ran and len(tried) == 1 and left() >= 10:
                verdict = other_board(run, pr, verdict, tried)
                if verdict:
                    log(f"validate {pr}: nothing ran on {tried[0]}; retrying on another board")
                    continue
            if state == "done":
                done("done", note, outcome=what)
            else:
                done("failed", f"{what}; {note}" if note else what)
            return

    with ThreadPoolExecutor(cfg["parallel"]["validate_per_node"]) as pool:
        list(pool.map(one, mine))


ARM64_SIM_FROM = "2022-02-02"      # sim on an ARM64 host needs a kernel from this date on
VFORK_FIXED = "2023-07-17"         # before the fork rework, ARM64 sim vfork can SIGBUS


def validate_nuttx(cfg: dict, run: Path, pr: int, env: dict, node_dir: Path, left, done) -> None:
    """Generate, build and validate each high/medium pair of an apps PR's
    verdict (its also_needs applied with the primary PR), on sim:nsh in
    Apptainer. Era limits are recorded, not hidden: a sim pair merged before
    ARM64 sim existed is needs_x86; a pre-fork-rework sim pair that fails is
    arm64_env_failure. qemu-armv8a pairs use the QEMU runner."""
    os.environ["EMBEDEVAL_NUTTX_DATA"] = str(nuttx_data(cfg, node_dir))   # before the import
    import nuttx_candidates as nc
    verdict = json.loads((run / "triage" / f"{pr}.json").read_text())
    pairs = [p for p in verdict.get("pairs", []) if isinstance(p, dict)
             and p.get("confidence") in ("high", "medium")]
    venv = {**env, "EMBEDEVAL_NUTTX_DATA": str(nuttx_data(cfg, node_dir)),
            "EMBEDEVAL_INSTANCES_DIR": str(node_dir / "work" /
                                          f"instances-{env.get('SLURM_JOB_ID', os.getpid())}-{pr}")}
    clones = Path(venv["EMBEDEVAL_NUTTX_DATA"])
    venv.update(EMBEDEVAL_NUTTX_CLONE=str(clones / "kernel.git"),
                EMBEDEVAL_NUTTX_APPS_CLONE=str(clones / "apps.git"))
    results = []
    for pair in pairs:
        kernel, lg = pair["kernel_pr"], LOGS / f"validate-{pr}-{pair['kernel_pr']}.log"
        iid = f"nuttx__nuttx-{kernel}"
        try:
            merged = (nc.kernel_record(kernel).get("merged_at") or "")[:10]
        except Exception as e:
            results.append((kernel, "failed", f"kernel record: {str(e)[:80]}"))
            continue
        platform = pair.get("platform", "sim:nsh")
        qemu = platform in ("qemu-armv8a:nsh", "qemu-armv8a:nsh_smp")
        if str(platform).startswith("qemu") and not qemu:
            results.append((kernel, "qemu_pending", f"{pair['platform']}: QEMU runner not wired in"))
            continue
        if not qemu and merged and merged < ARM64_SIM_FROM:
            results.append((kernel, "needs_x86", f"kernel #{kernel} merged {merged}: no ARM64 sim"))
            continue
        if left() < 8:
            return
        pv = run / "triage" / f"{pr}-kernel-{kernel}.json"
        pv.write_text(json.dumps(verdict))
        step([PY, "scripts/cleanup_instance.py", iid], lg, 10, venv)
        try:
            if step([PY, "scripts/generate_nuttx_instance.py", "--triage", str(pv), "--kernel-pr",
                     str(kernel), "--runtime", "apptainer", "--out", venv["EMBEDEVAL_INSTANCES_DIR"]],
                    lg, min(8, left()), venv):
                results.append((kernel, "failed", "generate failed: the pair is not runnable"))
                continue
            rc = step([PY, "scripts/build_instances.py", "--instance", iid], lg,
                      min(2 * float(venv["EMBEDEVAL_BUILD_TIMEOUT_MIN"]) + 5, left()), venv)
            if rc == 143:
                return
            if rc:
                results.append((kernel, "failed", "build failed" if rc != 124 else "build hit the cap"))
                continue
            rc = step([PY, "scripts/validate_instance.py", iid], lg, max(5, left()), venv)
            if rc == 143:
                return
            rep = Path(venv["EMBEDEVAL_INSTANCES_DIR"]) / iid / "validation.json"
            reason = json.loads(rep.read_text()).get("reason") if rep.exists() else None
            if rep.exists():
                pair_report = run / "instances" / f"apps-{pr}-kernel-{kernel}"
                pair_report.mkdir(parents=True, exist_ok=True)
                shutil.copy(rep, pair_report / "validation.json")
            if rc == 0:
                results.append((kernel, "VALID", ""))
            elif rc == 124:
                results.append((kernel, "failed", "validation hit the cap"))
            elif not qemu and merged and merged < VFORK_FIXED:
                results.append((kernel, "arm64_env_failure",
                                f"INVALID on ARM64 with a pre-{VFORK_FIXED} kernel (vfork SIGBUS "
                                f"risk): {reason or rc}"))
            elif reason in ("tests PASSED before the fix -- the instance does not isolate the bug",
                            "tests still fail with the fix"):
                results.append((kernel, "INVALID", reason))
            else:
                results.append((kernel, "failed", f"validation inconclusive: {reason or f'rc={rc}'}"))
        finally:
            step([PY, "scripts/cleanup_instance.py", iid], lg, 10, venv)
    order = ["VALID", "INVALID", "arm64_env_failure", "needs_x86", "qemu_pending", "failed"]
    results.sort(key=lambda r: order.index(r[1]))
    note = "; ".join(f"#{k} {o}{': ' + n if n else ''}" for k, o, n in results)
    if not results:
        done("done", "no high/medium pair", outcome="INVALID: no high/medium pair")
    elif any(r[1] == "failed" for r in results):
        done("failed", note)
    elif any(r[1] in ("arm64_env_failure", "needs_x86", "qemu_pending") for r in results):
        done("done", note, outcome=next(r[1] for r in results if r[1] in
                                   ("arm64_env_failure", "needs_x86", "qemu_pending")))
    else:
        done("done", note, outcome=results[0][1] if results[0][1] != "INVALID" else
             f"INVALID: {results[0][2]}")


def attempt(run: Path, pr: int, iid: str, verdict: Path, lg: Path, left, venv: dict):
    """One generate, build, validate of the verdict's pair: ("done", outcome),
    ("failed", why), or None if the worker is stopping."""
    step([PY, "scripts/cleanup_instance.py", iid], lg, 10, venv)   # leftovers of a cut-short try
    try:
        rc = step([PY, "scripts/generate_instance.py", "--pr", str(pr), "--triage", str(verdict)],
                  lg, min(5, left()), venv)
        if rc == 143:
            return None
        if rc:
            return "failed", "generate failed: the verdict's pair is not runnable"
        rc = step([PY, "scripts/build_instances.py", "--instance", iid], lg,
                  min(2 * float(venv["EMBEDEVAL_BUILD_TIMEOUT_MIN"]) + 5, left()), venv)
        if rc == 143:
            return None
        if rc:
            return "failed", "build failed" if rc != 124 else "build hit the cap"
        if left() < 3:
            return "failed", "no time left to validate"
        rc = step([PY, "scripts/validate_instance.py", iid], lg, left(), venv)
        if rc == 143:
            return None
        report = run / "instances" / iid / "validation.json"
        reason = json.loads(report.read_text()).get("reason") if report.exists() else None
        if rc == 0:
            return "done", "VALID"
        if rc == 124:
            return "failed", "validation hit the cap"
        return "done", f"INVALID: {reason or f'rc={rc}'}"
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


    with ThreadPoolExecutor(cfg["parallel"]["validate_per_node"]) as pool:
        list(pool.map(one, mine))


# ---------------------------------------------------------------- main


def on_sigterm(signum, frame) -> None:
    _stopping.set()
    log(f"signal {signum}: stopping running steps")
    with _lock:
        groups = list(_children)
    for g in groups:
        kill_group(g, grace=10)
    os._exit(128 + signum)


def main() -> None:
    global N, NN, LOGS
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", required=True)
    p.add_argument("--config", required=True, help="the frozen settings submit.py wrote")
    p.add_argument("--phase", required=True, choices=["triage", "validate"])
    args = p.parse_args()
    N = int(os.environ.get("SLURM_PROCID", "0"))
    NN = int(os.environ.get("SLURM_NTASKS", "1"))
    run = Path(args.run)
    cfg = json.loads(Path(args.config).read_text())
    name = f"node{N}-job{os.environ['SLURM_JOB_ID']}" if os.environ.get("SLURM_JOB_ID") else f"node{N}"
    node_dir = run / "nodes" / name
    LOGS = run / "logs" / name
    LOGS.mkdir(parents=True, exist_ok=True)
    sys.stdout = open(LOGS / f"worker-{args.phase}.log", "a", buffering=1)
    sys.stderr = sys.stdout
    signal.signal(signal.SIGTERM, on_sigterm)
    env = {**os.environ, **cluster_config.env_for(cfg, run),
           # this node's own images, overlays and temp files: its cleanup can
           # never touch another node's work
           "EMBEDEVAL_WORK": str(node_dir / "work")}
    log(f"phase {args.phase}, node {N} of {NN}, run {run}")
    if args.phase == "triage":
        triage(cfg, run, env, node_dir)
    else:
        validate(cfg, run, env)
    log(f"phase {args.phase} done")


N, NN, LOGS = 0, 1, Path(".")

if __name__ == "__main__":
    main()
