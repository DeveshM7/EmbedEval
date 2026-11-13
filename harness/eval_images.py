"""
Instance images for an evaluation of Mynewt, NuttX or Contiki-NG, which are
built where the evaluation runs, not published (scripts/registry.py holds
Zephyr's and RIOT's, which an evaluation pulls).

harness/eval_worker.py calls these around each PR of those projects:

    ensure(iid)          the instance's image is there: pulled if it was
                         published, else built (scripts/build_instances.py)
                         unless it exists already. Returns None, or why not.
    record_failure(...)  a build that failed is an infrastructure error, as
                         harness/run.py counts one: run.json says so, the next
                         attempt (the next job's, or the worker's own next
                         take) builds again, and after run.MAX_ATTEMPTS
                         attempts in all the PR is graded "not_run". It is
                         never a fail of the model.
    cleanup(iid)         what the PR left behind: image, overlays, node-local
                         sandboxes (Contiki-NG's 32-bit eras) and the
                         sandboxes Mynewt unpacks (EMBEDEVAL_SANDBOX_DIR).

A worker that is stopped while a build runs (SIGTERM at the job's end) stops
the build with it.

    python harness/eval_images.py base nuttx     # print the project's shared base image,
                                                 # built first if need be (the agents
                                                 # script's checks use it)
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
REPO = HARNESS.parent
sys.path.insert(0, str(HARNESS))

import paths  # noqa: E402  (puts scripts/ on the path)

import apptainer_util as au  # noqa: E402

BUILD_CAP_MIN = {"mynewt": 60, "nuttx": 60, "contiki-ng": 120}    # as scripts/build_instances.py


def base_image(project: str, meta: dict | None = None, instance_dir: Path | None = None) -> Path:
    """The shared base image an instance of `project` is built FROM (the
    project's default one without `meta`), built if it is not there yet:
    build_instances.py would too; here it is done once, under a lock, so that
    no two workers, on one node or several, build the same base."""
    logdir = au.path("logs", "bases", "x").parent
    with open(au.shared_path("images", ".eval-base.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if project == "mynewt":
            return au.ensure_mynewt_base_image(logdir)
        if project == "nuttx":
            return au.ensure_nuttx_base_image(logdir, (meta or {}).get("sim_config", "sim:nsh"))
        ci = json.loads((instance_dir / "instance.json").read_text()).get("ci_image") if instance_dir else None
        return au.ensure_contiki_ng_base(logdir, ci)


def _build(instance_id: str, minutes: float) -> int:
    """scripts/build_instances.py for one instance, in a process group of its
    own that a SIGTERM to this worker takes down too. Returns its exit code,
    124 on the time cap."""
    cmd = [sys.executable, str(REPO / "scripts" / "build_instances.py"), "--instance", instance_id]
    proc = subprocess.Popen(cmd, start_new_session=True)
    previous = signal.getsignal(signal.SIGTERM)

    def kill(grace: float = 20) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(grace if sig == signal.SIGTERM else 5)
                return
            except subprocess.TimeoutExpired:
                pass

    def on_term(signum, frame):
        kill(grace=10)
        if callable(previous):
            previous(signum, frame)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, on_term)
    try:
        return proc.wait(timeout=minutes * 60)
    except subprocess.TimeoutExpired:
        kill()
        return 124
    finally:
        signal.signal(signal.SIGTERM, previous)


def ensure(instance_id: str) -> str | None:
    """Make the instance's image available; None if it is, else the reason."""
    import registry
    try:
        if registry.published(instance_id):
            registry.pull(instance_id)           # checked against the published sha256
            return None
        image = au.instance_image(instance_id)
        if image.exists():
            return None
        meta = paths.load_metadata(instance_id)
        au.require_host_arch(meta)
        project = paths.split_instance(instance_id)[0]
        base_image(project, meta, paths.instance_dir(instance_id))
        minutes = int(os.environ.get("EMBEDEVAL_BUILD_TIMEOUT_MIN", BUILD_CAP_MIN[project]))
        print(f"  building {instance_id} -> {image}", flush=True)
        rc = _build(instance_id, 2 * minutes + 5)   # build_instances.py stops itself at `minutes`
    except Exception as e:                          # RuntimeError, OSError, ...: ours, not the model's
        return f"{type(e).__name__}: {str(e)[:300]}"
    if rc == 0 and image.exists():
        return None
    log = au.path("logs", instance_id, "x").parent / "build.log"
    tail = log.read_text(errors="replace").strip().splitlines()[-1:] if log.exists() else []
    return f"image build failed (exit {rc}){': ' + tail[0][:200] if tail else ''}"


def record_failure(instance_id: str, model_name: str, args, error: str) -> dict | None:
    """Count a failed image build as harness/run.py counts an infrastructure
    error. Returns the grade written once the attempts are used up
    (run.MAX_ATTEMPTS, kept in run.json across jobs), else None: the PR is
    taken again."""
    import run
    model = paths.resolve_model(model_name)
    record_file = paths.run_record_path(instance_id, model)
    try:
        previous = json.loads(record_file.read_text())
    except (OSError, ValueError):
        previous = None
    attempt = (previous["attempt"] if previous else 0) + 1
    paths.write_json(record_file, {
        "instance_id": instance_id, "model": model, "attempt": attempt,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "node": socket.gethostname(),
        "limits": {"steps": args.step_limit, "minutes": args.time_limit},
        "exit_status": None, "steps": None, "elapsed_seconds": 0.0,
        "hit_time_cap": False, "hit_step_limit": False, "infra_error": error, "error": error,
        "patch_empty": True, "protected_touched": [], "tests_hidden": False})
    if attempt < run.MAX_ATTEMPTS:
        return None
    grade = {"instance_id": instance_id, "model": model,
             "graded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "grade": "not_run", "tests_run": False,
             "error": f"infrastructure error in {attempt} attempts: {error}"}
    paths.write_json(paths.grade_path(instance_id, model), grade)
    return grade


def sweep_sandboxes() -> None:
    """Remove the sandboxes Mynewt unpacked for this worker (its
    EMBEDEVAL_SANDBOX_DIR) and did not remove: a run killed outright."""
    folder = os.environ.get("EMBEDEVAL_SANDBOX_DIR")
    for d in Path(folder).glob("embedeval-sbx-*") if folder and Path(folder).is_dir() else []:
        au.remove_sandbox(d / "root")


def cleanup(instance_id: str) -> None:
    """Remove what the PR's build, run and grading left: the instance's image,
    overlays and node-local sandbox (scripts/cleanup_instance.py), and any
    sandbox of this worker's."""
    import cleanup_instance
    au.remove_local_sandbox(au.instance_image(instance_id))
    for target in cleanup_instance.targets(instance_id):
        au.remove(target)
    sweep_sandboxes()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "base" and sys.argv[2] in BUILD_CAP_MIN:
        import contextlib
        with contextlib.redirect_stdout(sys.stderr):       # a build's messages; stdout is the path
            image = base_image(sys.argv[2])
        print(image)
    else:
        sys.exit(__doc__)
