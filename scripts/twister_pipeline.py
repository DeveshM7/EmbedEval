#!/usr/bin/env python3
"""
Run Zephyr PRs through the twister pipeline end to end, after triage.

    python scripts/twister_pipeline.py --pr 65697 85079 --verdicts triage/ideal_verdicts
    python scripts/twister_pipeline.py --pr 65697 --verdicts triage/ideal_verdicts --keep

Per PR, in order, stopping at the first stage that rejects it:

    1. enrich-twister  twister facts from the PR's commits (local git only)
    2. dry run         which board/scenario pairs twister selects; auto-reject
                       when none is
    3. verdict         triage's verdict (from --verdicts/<pr>.json); a reject
                       stops here
    4. generate        the instance recipe, into generated/
    5. build           the Apptainer image
    6. validate        run_tests fails as built, passes with the upstream fix
    7. clean up        the image and other per-instance files, once validated
                       (unless --keep); recipe, report and logs stay

Must run where Apptainer can build (on Scholar: inside a spark-batch job).
PRs run in parallel (--parallel). Writes a summary to
generated/pipeline-summary.json.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
OUT = REPO_ROOT / "generated"


def step(pr: int, name: str, cmd: list[str], logdir: Path, env: dict | None = None) -> int:
    log = logdir / f"{pr}-{name}.log"
    with open(log, "w") as fh:
        return subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=REPO_ROOT,
                              env={**os.environ, **(env or {})}).returncode


def run_pr(pr: int, verdicts: Path, keep: bool, logdir: Path) -> dict:
    t0 = time.time()
    iid = f"zephyr__zephyr-{pr}"
    res = {"pr": pr, "stage": None, "outcome": None}
    env = {"EMBEDEVAL_INSTANCES_DIR": str(OUT)}

    def done(stage: str, outcome: str) -> dict:
        res.update(stage=stage, outcome=outcome, minutes=round((time.time() - t0) / 60, 1))
        print(f"  {pr}: {outcome} at {stage} ({res['minutes']} min)", flush=True)
        return res

    s = "scripts/"
    if step(pr, "enrich", [PY, s + "filter_candidates.py", "enrich-twister", "--pr", str(pr)],
            logdir):
        return done("enrich", "error")
    if step(pr, "dryrun", [PY, s + "dry_run.py", str(pr)], logdir):
        return done("dry run", "error")
    dr = json.loads((REPO_ROOT / "candidates" / "dryrun" / f"{pr}.json").read_text())
    if dr["auto_reject"]:
        return done("dry run", "rejected: no board/scenario pair can run")
    vpath = verdicts / f"{pr}.json"
    if not vpath.exists():
        return done("triage", "no verdict")
    if json.loads(vpath.read_text()).get("verdict") == "reject":
        return done("triage", "rejected by triage")
    if step(pr, "generate", [PY, s + "generate_instance.py", "--pr", str(pr),
                             "--triage", str(vpath)], logdir):
        return done("generate", "rejected: verdict's pair not runnable (see log)")
    if step(pr, "build", [PY, s + "build_instances.py", "--instance", iid], logdir, env):
        return done("build", "error")
    rc = step(pr, "validate", [PY, s + "validate_instance.py", iid], logdir, env)
    report = json.loads((OUT / iid / "validation.json").read_text())
    res["validation"] = report.get("reason")
    if not keep:
        step(pr, "cleanup", [PY, s + "cleanup_instance.py", iid], logdir)
    return done("validate", "VALID" if rc == 0 else f"rejected: {report.get('reason')}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pr", type=int, nargs="+", required=True)
    p.add_argument("--verdicts", required=True, help="directory of <pr>.json triage verdicts")
    p.add_argument("--parallel", type=int, default=3)
    p.add_argument("--keep", action="store_true", help="keep images after validation")
    args = p.parse_args()

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import apptainer_util as au
    logdir = au.path("logs", "pipeline", time.strftime("%Y%m%d-%H%M%S"), "x").parent
    logdir.mkdir(parents=True, exist_ok=True)
    print(f"{len(args.pr)} PR(s), {args.parallel} at a time; step logs in {logdir}", flush=True)
    with ThreadPoolExecutor(args.parallel) as ex:
        results = list(ex.map(lambda pr: run_pr(pr, Path(args.verdicts), args.keep, logdir),
                              args.pr))
    OUT.mkdir(exist_ok=True)
    (OUT / "pipeline-summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print("\nsummary")
    for r in results:
        print(f"  {r['pr']:>7}  {r['outcome']}  [{r['stage']}, {r.get('minutes')} min]")


if __name__ == "__main__":
    main()
