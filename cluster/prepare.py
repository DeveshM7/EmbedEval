#!/usr/bin/env python3
"""
Prepare a run: everything before the cluster job, on the node you run it from.

    python cluster/prepare.py --name 2025-09 --since 2025-09-01 --until 2025-09-30
    python cluster/prepare.py --name gold --pr 65697 85079 ...

Creates <scratch_root>/runs/<name>/ and, in it:

    1. the PRs        --since/--until: fetch merged PRs from GitHub and apply the
                      hard filters (scripts/filter_candidates.py fetch, filter).
                      --pr: these PRs, through the same hard filters
                      (--no-filter: all of them, unfiltered).
                      Either way, PRs an earlier run settled are skipped
                      (cluster/ledger.py; --redo takes them again), and so
                      are PRs another run has taken and not finished.
    2. enriched/      full GitHub context, several PRs at once
    3. dryrun/        twister's own selection on our boards, several at once
    4. shared SDK     any SDK image the PRs that reach triage need and that
       images         does not exist yet, built once here so that no two workers
                      ever build the same image
    5. commits        every base, head and merge commit fetched into the clone
    6. manifest.json  the plan: each PR and where preparation left it, the code
                      commit, a copy of the config

Then `python cluster/submit.py pipeline --run <name>`. Run this from an
interactive session on a Spark node: the SDK images are built here.

--project openthread (or `project: openthread` in the config) prepares an
OpenThread run instead (plan_openthread): the PRs come from the OpenThread
clone's history (scripts/openthread_candidates.py filter and enrich, its
hard filters and its test selection), there is no dry run and no SDK image
(the one shared toolchain image is built here), a PR whose changed tests map
to no runnable executable is settled here, and every base and fix commit
must be in the clone, which workers never fetch into.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

CLUSTER = Path(__file__).resolve().parent
REPO = CLUSTER.parent
sys.path.insert(0, str(CLUSTER))
import config as cluster_config  # noqa: E402
import ledger  # noqa: E402
import report  # noqa: E402
import status  # noqa: E402

PY = sys.executable


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def script(name: str, *args: str, env: dict, logfile: Path, timeout: int | None = 7200) -> int:
    """One of the pipeline's scripts, with the run's environment, logged as
    it goes. GitHub steps take timeout=None: they wait out the rate limit
    (filter_candidates.gh), which for thousands of PRs is hours."""
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "a") as fh:
        fh.write(f"\n$ {name} {' '.join(args)}\n")
        fh.flush()
        try:
            return subprocess.run([PY, str(REPO / "scripts" / name), *args], cwd=REPO,
                                  env={**os.environ, **env, "PYTHONUNBUFFERED": "1"}, stdout=fh,
                                  stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\n(timed out after {timeout} s)\n")
            return 124


def git_absent(clone: Path, shas) -> list[str]:
    """The commits among `shas` that the clone lacks."""
    return [x for x in shas if subprocess.run(["git", "-C", str(clone), "cat-file", "-e", f"{x}^{{commit}}"],
                                              capture_output=True).returncode]


def plan_openthread(args, cfg: dict, run: Path, env: dict, logs: Path, skip_known, stage: dict,
                    notes: dict, timings: dict) -> tuple[list[int], dict, list[str]]:
    """Steps 1-5 for OpenThread: (the PRs, the manifest's source, the shared
    images that could not be built). Every PR ends in `stage`: "to triage", or
    why not. Its own scripts do the work (openthread_candidates.py filter /
    enrich); PRs whose enrichment selects no runnable test are settled here."""
    sys.path.insert(0, str(REPO / "scripts"))
    import apptainer_util as au
    import layout
    import openthread_candidates as oc
    clone = layout.openthread_clone()
    if not clone.exists():
        sys.exit(f"no OpenThread clone at {clone}; clone https://github.com/openthread/openthread.git "
                 "there (bare, full) or set paths.openthread_clone")
    t = time.time()
    if args.pr:
        prs = list(dict.fromkeys(args.pr))
        source = {"pr": prs, "hard_filter": True, "redo": args.redo}
        skip_known(prs)
        log("hard filters")
        for pr in (p for p in prs if p not in stage):
            try:
                why = oc.classify(dict(oc.find_pr(pr)))
            except SystemExit as e:      # not in the clone's history: not an outcome, so not settled
                stage[pr], notes[pr] = "fetch failed", str(e)
                continue
            if why:
                stage[pr], notes[pr] = "hard filter", why
    else:
        source = {"since": args.since, "until": args.until, "redo": args.redo}
        log(f"candidates merged {args.since} .. {args.until}")
        if script("openthread_candidates.py", "filter", "--since", args.since, env=env,
                  logfile=logs / "filter.log"):
            sys.exit(f"filter failed; see {logs / 'filter.log'}")
        found = [json.loads(l) for l in open(run / "candidates" / "openthread" / "candidates.jsonl")]
        prs = [c["number"] for c in found if c["merged_at"][:10] <= args.until]
        skip_known(prs)
    timings["select"] = time.time() - t
    log(f"{len(prs)} PRs")

    t = time.time()
    par = cfg["parallel"]["enrich"]
    log(f"enriching, {par} at a time")
    kept = [pr for pr in prs if pr not in stage]
    with ThreadPoolExecutor(par) as pool:
        list(pool.map(lambda pr: script("openthread_candidates.py", "enrich", "--pr", str(pr), env=env,
                                        logfile=logs / "enrich" / f"{pr}.log", timeout=None), kept))
    timings["enrich"] = time.time() - t
    for pr in kept:
        f = oc.enriched_dir() / f"{pr}.json"
        if not f.exists():
            stage[pr], notes[pr] = "enrich failed", f"see {logs / 'enrich' / f'{pr}.log'}"
            continue
        rec = json.loads(f.read_text())
        sel = oc.select_tests(rec)
        if not sel["unit"] and not sel["gtest"]:
            gated = oc.gated_targets(rec)
            stage[pr], notes[pr] = "enrich: no runnable test", (
                "profile-gated: its tests are only built with " + ", ".join(sorted({o for _, m in gated for o in m}))
                if gated else f"its changed test files {rec['test_files']} are compiled into no unit or gtest executable")
        else:
            stage[pr] = "to triage"
    log(f"enriched {sum(1 for pr in kept if stage[pr] != 'enrich failed')}/{len(kept)} in {timings['enrich']:.0f} s")

    # every commit a worker reads must be in the clone already
    t = time.time()
    recs = {pr: json.loads((oc.enriched_dir() / f"{pr}.json").read_text()) for pr in kept if stage[pr] == "to triage"}
    need = lambda r: {r["base_commit"], r["fix_commit"], *r.get("commits", [])}
    lacking = set(git_absent(clone, sorted(set().union(*map(need, recs.values())))))
    if lacking:
        subprocess.run(["git", "-C", str(clone), "fetch", "-q", "origin", *sorted(lacking)], capture_output=True)
        lacking = set(git_absent(clone, sorted(lacking)))
    for pr, r in recs.items():
        if lacking & need(r):
            stage[pr], notes[pr] = "enrich failed", f"commits not in the clone: {sorted(lacking & need(r))}"
    timings["commits"] = time.time() - t
    to_triage = [pr for pr in prs if stage[pr] == "to triage"]
    log(f"commits: {'all present' if not lacking else f'{len(lacking)} missing'}; {len(to_triage)} to triage")

    # the one shared toolchain image, built once here so no two workers build it
    t = time.time()
    missing = []
    if to_triage:
        try:
            au.ensure_openthread_base(Path(cfg["derived"]["shared"]) / "logs")
        except RuntimeError as e:
            log(f"  FAILED: {e}")
            missing = [au.openthread_image().name]
    timings["base_image"] = time.time() - t
    return prs, source, missing


def finish(args, cfg: dict, project: str, run: Path, env: dict, source: dict, prs: list[int],
           stage: dict, notes: dict, timings: dict, t0: float, missing_images: list[str]) -> None:
    """Step 6: the manifest, the ledger's settled outcomes, and the summary."""
    git = lambda *a: subprocess.run(["git", "-C", str(REPO), *a], capture_output=True,
                                    text=True).stdout.strip()
    manifest = {
        "name": args.name, "project": project, "prepared": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "code": {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))},
        "config": {k: v for k, v in cfg.items() if k != "derived"},
        "environment": env,
        "prs": [{"pr": pr, "stage": stage[pr]} | ({"note": notes[pr]} if pr in notes else {})
                for pr in prs],
        "counts": {s: sum(1 for v in stage.values() if v == s) for s in sorted(set(stage.values()))},
        "missing_sdk_images": missing_images,
        "seconds": {k: round(v) for k, v in timings.items()},
    }
    tmp = run / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    tmp.replace(run / "manifest.json")
    report.to_ledger(run)          # the hard filters' and dry runs' settled outcomes
    log(f"done in {time.time() - t0:.0f} s -> {run / 'manifest.json'}")
    for s, n in manifest["counts"].items():
        print(f"    {n:>5}  {s}")
    if manifest["missing_sdk_images"]:
        print(f"  WARNING: {'SDK' if project == 'zephyr' else 'shared'} images could not be built: "
              f"{manifest['missing_sdk_images']}")
    print(f"\nnext: python cluster/submit.py pipeline --run {args.name}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True, help="the run's name, its folder under runs/")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--since", help="fetch PRs merged on or after this date (YYYY-MM-DD)")
    src.add_argument("--pr", type=int, nargs="+", help="these PRs, hard filters applied")
    p.add_argument("--no-filter", action="store_true", help="with --pr: skip the hard filters")
    p.add_argument("--redo", action="store_true",
                   help="also take PRs the ledger has already settled in an earlier run")
    p.add_argument("--until", help="with --since: merged on or before this date")
    p.add_argument("--config", help="default: cluster/config.yaml")
    p.add_argument("--project", choices=cluster_config.PROJECTS,
                   help="default: the config's `project` (zephyr)")
    args = p.parse_args()
    if args.since and not args.until:
        p.error("--since needs --until")
    if args.no_filter and not args.pr:
        p.error("--no-filter goes with --pr")

    cfg = cluster_config.load_or_exit(args.config)
    project = args.project or cfg["project"]
    if args.no_filter and project != "zephyr":
        p.error("--no-filter is Zephyr only: openthread_candidates enriches no PR the hard filters drop")
    run = Path(cfg["derived"]["runs"]) / args.name
    if (run / "manifest.json").exists():
        sys.exit(f"{run} is already prepared; pick another --name "
                 "(or delete that run folder yourself)")
    run.mkdir(parents=True, exist_ok=True)
    env = cluster_config.env_for(cfg, run, project)
    os.environ.update(env)              # for the modules imported below
    logs = run / "logs" / "prepare"
    par = cfg["parallel"]
    t0 = time.time()
    timings = {}
    log(f"preparing {run}")

    # 1. the PRs; skipped: those an earlier run settled (ledger.py), and those
    # another run has taken and not finished (never two runs on one PR)
    stage, notes = {}, {}
    done = {} if args.redo else ledger.latest(Path(cfg["derived"]["ledger"]), project)
    taken = status.unfinished(Path(cfg["derived"]["runs"]), but=run, of=project)

    def skip_known(prs):
        for pr in prs:
            if pr in taken:
                stage[pr], notes[pr] = "in another run", (
                    f"run {taken[pr]} has it and has not finished; finish or delete that run")
            elif pr in done:
                e = done[pr]
                stage[pr], notes[pr] = "in ledger", f"{e['outcome']} in run {e['run']} ({e['date'][:10]})"
        n = sum(1 for pr in prs if pr in done and pr not in taken)
        if n:
            log(f"{n} already settled in earlier runs (ledger); --redo takes them again")
        n = sum(1 for pr in prs if pr in taken)
        if n:
            log(f"{n} taken by unfinished runs ({', '.join(sorted({taken[p] for p in prs if p in taken}))})")

    if project == "openthread":
        prs, source, missing_images = plan_openthread(args, cfg, run, env, logs, skip_known, stage,
                                                      notes, timings)
        finish(args, cfg, project, run, env, source, prs, stage, notes, timings, t0, missing_images)
        return
    if args.pr:
        prs = list(dict.fromkeys(args.pr))
        source = {"pr": prs, "hard_filter": not args.no_filter, "redo": args.redo}
        skip_known(prs)
        fresh = [pr for pr in prs if pr not in stage]
        if not args.no_filter and fresh:
            log("hard filters")
            script("filter_candidates.py", "fetch", "--pr", *map(str, fresh), env=env,
                   logfile=logs / "fetch.log", timeout=None)
            sys.path.insert(0, str(REPO / "scripts"))
            import filter_candidates as fc
            for pr in fresh:
                f = fc.CACHE / f"{pr}.json"
                if not f.exists():     # GitHub did not answer: not an outcome, so not settled
                    stage[pr], notes[pr] = "fetch failed", f"see {logs / 'fetch.log'}"
                    continue
                ok, why = fc.classify(json.loads(f.read_text()))
                if not ok:
                    stage[pr], notes[pr] = "hard filter", why
    else:
        source = {"since": args.since, "until": args.until, "redo": args.redo}
        log(f"fetching PRs merged {args.since} .. {args.until}")
        if script("filter_candidates.py", "fetch", "--since", args.since, "--until", args.until,
                  env=env, logfile=logs / "fetch.log", timeout=None):
            sys.exit(f"fetch failed; see {logs / 'fetch.log'}")
        if script("filter_candidates.py", "filter", env=env, logfile=logs / "filter.log"):
            sys.exit(f"filter failed; see {logs / 'filter.log'}")
        prs = [json.loads(l)["number"] for l in open(run / "candidates" / "candidates.jsonl")]
        skip_known(prs)
    timings["select"] = time.time() - t0
    log(f"{len(prs)} PRs")

    # 2. enrichment, in parallel (filter_candidates.py enrich --parallel)
    t = time.time()
    log(f"enriching, {par['enrich']} at a time")
    kept = [pr for pr in prs if pr not in stage]
    if kept:
        script("filter_candidates.py", "enrich", "--parallel", str(par["enrich"]),
               "--pr", *map(str, kept), env=env, logfile=logs / "enrich.log", timeout=None)
    enriched = [pr for pr in kept if (run / "enriched" / f"{pr}.json").exists()]
    timings["enrich"] = time.time() - t
    log(f"enriched {len(enriched)}/{len(kept)} in {timings['enrich']:.0f} s")

    # 3. dry runs, in parallel: one process per PR
    t = time.time()
    log(f"dry runs, {par['dry_run']} at a time")
    with ThreadPoolExecutor(par["dry_run"]) as pool:
        list(pool.map(lambda pr: script("dry_run.py", str(pr), env=env,
                                        logfile=logs / "dryrun" / f"{pr}.log", timeout=1800),
                      enriched))
    timings["dry_run"] = time.time() - t

    for pr in kept:
        f = run / "dryrun" / f"{pr}.json"
        if pr not in enriched:
            stage[pr] = "enrich failed"
        elif not f.exists():
            stage[pr] = "dry run error"
        elif json.loads(f.read_text())["auto_reject"]:
            stage[pr] = "dry run: no runnable pair"
        else:
            stage[pr] = "to triage"
    to_triage = [pr for pr in prs if stage[pr] == "to triage"]
    log(f"dry runs done in {timings['dry_run']:.0f} s: {len(to_triage)} to triage")

    # 4. shared SDK images the triaged PRs may need, each built once
    sys.path.insert(0, str(REPO / "scripts"))
    import apptainer_util as au
    t = time.time()
    need = {}
    for pr in to_triage:
        tw = json.loads((run / "enriched" / f"{pr}.json").read_text())["twister"]
        need[(tw["python"], tw["sdk"], tw["sdk_archive"])] = pr
    missing = [k for k in need if not au.sdk_image(k[1], k[0]).exists()]
    for py, sdk, archive in missing:          # one at a time: two may share a Python image
        log(f"building SDK image {sdk} (Python {py})")
        try:
            au.ensure_base_images(py, sdk, archive, Path(cfg["derived"]["shared"]) / "logs")
        except RuntimeError as e:
            log(f"  FAILED: {e}")
    timings["sdk_images"] = time.time() - t

    # 5. every commit a worker reads, fetched now: workers never fetch
    t = time.time()
    import zephyr_env
    shas = set()
    for pr in to_triage:
        rec = json.loads((run / "enriched" / f"{pr}.json").read_text())
        shas |= {s for s in (rec.get("base_commit"), rec.get("head_commit"),
                             rec.get("merge_commit_sha")) if s}
    try:
        zephyr_env.ensure_commits(*sorted(shas))
        fetched = "ok"
    except RuntimeError as e:
        fetched = str(e)[:300]
    timings["commits"] = time.time() - t
    log(f"commits: {len(shas)} present ({fetched})")

    # 6. the manifest
    finish(args, cfg, project, run, env, source, prs, stage, notes, timings, t0,
           [f"{sdk} (Python {py})" for py, sdk, _ in missing if not au.sdk_image(sdk, py).exists()])


if __name__ == "__main__":
    main()
