#!/usr/bin/env python3
"""
One evaluation worker: takes the next PR no one has graded, runs the agent on
it and grades it (harness/run.py, run_and_grade), and repeats.

    python harness/eval_worker.py --project zephyr --model qwen38-nvfp4 \
        --name <job>-node<N>-w<K> --stop-at <epoch seconds>

Started by scripts/eval_zephyr_node.sh, several per node, all talking to the
node's model server. Workers on every node of a job, and of later jobs, share
one queue (cluster/claims.py) in the model's eval folder:

    <eval dir>/<model>/claims/<project>.json

A PR is finished when its grade.json exists; a worker never takes a finished
PR, and a PR held by a worker that has gone silent (its node died) is taken
over. A worker takes no new PR once less than --margin minutes are left before
--stop-at (the job's end): a run is capped at an hour and then graded, and a
run cut off by the job's end only counts as an infrastructure error, retried
by the next job.

--instances FILE limits the work to the instance ids listed in it (a trial).
--delete-images removes each instance image once its PR is graded, so the
images of a whole project need not fit on scratch at once.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cluster"))

import paths  # noqa: E402
import run  # noqa: E402
import claims  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True, choices=paths.PROJECTS)
    p.add_argument("--model", required=True)
    p.add_argument("--name", required=True, help="<job>-node<N>-w<K>, unique per worker")
    p.add_argument("--stop-at", type=float, required=True,
                   help="epoch seconds: the job's end")
    p.add_argument("--margin", type=float, default=75,
                   help="minutes before --stop-at after which no new PR is taken (default 75)")
    p.add_argument("--instances", help="file of instance ids to work on (default: all)")
    p.add_argument("--delete-images", action="store_true")
    p.add_argument("--step-limit", type=int, default=250)
    p.add_argument("--cost-limit", type=float, default=3.0)
    p.add_argument("--time-limit", type=float, default=60)
    a = p.parse_args()

    signal.signal(signal.SIGTERM, run._stop_on_sigterm)
    model = paths.resolve_model(a.model)
    ids = (Path(a.instances).read_text().split() if a.instances
           else paths.discover_instances(a.project))
    by_pr = {int(paths.split_instance(i)[1]): i for i in ids
             if paths.split_instance(i)[0] == a.project}
    todo = sorted(by_pr)
    args = argparse.Namespace(step_limit=a.step_limit, cost_limit=a.cost_limit,
                              time_limit=a.time_limit, force=False, verbose=False)
    folder = paths.OUTPUTS_DIR / paths.model_dir(model)
    graded = lambda pr: paths.grade_path(by_pr[pr], model).exists()   # noqa: E731
    claims.keep_alive(folder, a.name)
    queue = claims.Queue(folder, a.project, a.name, needed=lambda pr: not graded(pr))
    log = lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {a.name} {msg}", flush=True)  # noqa: E731

    done = 0
    log(f"{len(todo)} PRs, {sum(graded(pr) for pr in todo)} graded already")
    while True:
        left = (a.stop_at - time.time()) / 60
        if left < a.margin:
            log(f"{left:.0f} min left: taking no new PR")
            break
        pr = queue.take(todo)
        if pr is None:
            if queue.busy_elsewhere(todo):
                time.sleep(60)          # it may come free, if its worker dies
                continue
            log("nothing left to take")
            break
        iid = by_pr[pr]
        log(f"took {iid} ({left:.0f} min left)")
        try:
            g = run.run_and_grade(iid, model, args)
            log(f"{iid}: {g['grade']}" + (f" ({g['error'][:120]})" if g.get("error") else ""))
            done += 1
        except Exception as e:          # this PR only; the worker goes on
            log(f"{iid}: worker error {type(e).__name__}: {e}")
        finally:
            queue.release(pr, done=graded(pr))
        if a.delete_images and graded(pr):
            import apptainer_util as au   # on the path through harness/paths -> scripts/
            image = au.instance_image(iid)
            if image.exists():
                au.remove(image)
    log(f"stopping: {done} PRs this run")


if __name__ == "__main__":
    main()
