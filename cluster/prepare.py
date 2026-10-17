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
                      (cluster/ledger.py; --redo takes them again).
    2. enriched/      full GitHub context, several PRs at once
    3. dryrun/        twister's own selection on our boards, several at once
    4. shared SDK     any SDK image the PRs that reach triage need and that
       images         does not exist yet, built once here so that no two workers
                      ever build the same image
    5. commits        every base, head and merge commit fetched into the clone
    6. manifest.json  the plan: each PR and where preparation left it, the code
                      commit, a copy of the config

    --verdicts-from RUN (project nuttx): the PRs' triage verdicts are taken from
    an earlier run, so the job only validates them.

Then `python cluster/submit.py pipeline --run <name>`. Run this from an
interactive session on a Spark node: the SDK images are built here.
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

PY = sys.executable


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def script(name: str, *args: str, env: dict, logfile: Path, timeout: int = 7200) -> int:
    """One of the pipeline's scripts, with the run's environment, logged."""
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "a") as fh:
        fh.write(f"\n$ {name} {' '.join(args)}\n")
        fh.flush()
        try:
            return subprocess.run([PY, str(REPO / "scripts" / name), *args], cwd=REPO,
                                  env={**os.environ, **env}, stdout=fh,
                                  stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\n(timed out after {timeout} s)\n")
            return 124


def write_manifest(run: Path, name: str, cfg: dict, env: dict, source: dict, prs: list,
                   stage: dict, notes: dict, timings: dict, missing_sdk: list, t0: float) -> None:
    git = lambda *a: subprocess.run(["git", "-C", str(REPO), *a], capture_output=True,
                                    text=True).stdout.strip()
    manifest = {
        "name": name, "prepared": datetime.now().isoformat(timespec="seconds"),
        "project": cfg["project"], "source": source,
        "code": {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))},
        "config": {k: v for k, v in cfg.items() if k != "derived"},
        "environment": env, "ledger": cfg["derived"]["ledger"],
        "prs": [{"pr": pr, "stage": stage[pr]} | ({"note": notes[pr]} if pr in notes else {})
                for pr in prs],
        "counts": {s: sum(1 for v in stage.values() if v == s) for s in sorted(set(stage.values()))},
        "missing_sdk_images": missing_sdk,
        "seconds": {k: round(v) for k, v in timings.items()},
    }
    tmp = run / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    tmp.replace(run / "manifest.json")
    log(f"done in {time.time() - t0:.0f} s -> {run / 'manifest.json'}")
    for s, n in manifest["counts"].items():
        print(f"    {n:>5}  {s}")
    if manifest["missing_sdk_images"]:
        print(f"  WARNING: SDK images could not be built: {manifest['missing_sdk_images']}")
    print(f"\nnext: python cluster/submit.py pipeline --run {name}")


def seed_verdicts(src: Path, run: Path, prs: list[int], stage: dict) -> int:
    """Take the triage verdicts of an earlier run for these PRs: the verdict
    file and its trace are copied and the triage stage is marked done, so the
    pipeline job validates them without triaging again. Returns how many."""
    import status
    n = 0
    for pr in prs:
        verdict = src / "triage" / f"{pr}.json"
        old = status.read(src, pr).get("triage")
        if stage.get(pr) != "to triage" or not verdict.exists() or not old or old.get("state") != "done":
            continue
        (run / "triage").mkdir(exist_ok=True)
        for name in (f"{pr}.json", f"{pr}.trace.json"):
            if (src / "triage" / name).exists():
                (run / "triage" / name).write_bytes((src / "triage" / name).read_bytes())
        status.write_stage(run, pr, "triage", old | {"seeded_from": src.name})
        n += 1
    return n


def prepare_nuttx(args, cfg: dict, run: Path, env: dict, done: dict) -> None:
    """A NuttX run: the unit is a nuttx-apps PR that changes testing/ostest/.
    Each PR's record (the PR, its timeline, the changed tests) is fetched into
    the node-local data copy, or read from the pre-warmed shared one; there is
    no dry run, SDK image or Zephyr clone."""
    t0 = time.time()
    sys.path.insert(0, str(REPO / "scripts"))
    import nuttx_candidates as nc
    prs = list(dict.fromkeys(args.pr or nc.ostest_apps_prs()))
    stage, notes = {}, {}
    for pr in prs:
        if pr in done:
            e = done[pr]
            stage[pr], notes[pr] = "in ledger", f"{e['outcome']} in run {e['run']} ({e['date'][:10]})"

    def fetch(pr: int) -> None:
        try:
            nc.apps_record(pr)
            stage[pr] = "to triage"
        except Exception as e:              # a PR GitHub cannot serve right now
            stage[pr], notes[pr] = "enrich failed", str(e)[:200]
    with ThreadPoolExecutor(cfg["parallel"]["enrich"]) as pool:
        list(pool.map(fetch, [pr for pr in prs if pr not in stage]))
    log(f"{len(prs)} apps PRs, {sum(v == 'to triage' for v in stage.values())} to triage")
    if args.verdicts_from:
        seeded = seed_verdicts(Path(cfg["derived"]["runs"]) / args.verdicts_from, run, prs, stage)
        log(f"{seeded} triage verdicts taken from run {args.verdicts_from}")
    if cfg["nuttx"]["validate"]:            # the shared image, built once, not by every node
        import apptainer_util as au
        logdir = Path(cfg["derived"]["shared"]) / "logs"
        au.ensure_nuttx_base_image(logdir)
        import nuttx_platforms
        boards = {nuttx_platforms.image_recipe(t): t for t in nuttx_platforms.QEMU_BOARDS}
        for target in boards.values():      # one image per QEMU recipe
            au.ensure_nuttx_base_image(logdir, target)
    source = {"pr": args.pr} if args.pr else {"all_ostest": True}
    write_manifest(run, args.name, cfg, env, source | {"redo": args.redo}, prs, stage, notes,
                   {"records": time.time() - t0}, [], t0)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True, help="the run's name, its folder under runs/")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--since", help="fetch PRs merged on or after this date (YYYY-MM-DD)")
    src.add_argument("--pr", type=int, nargs="+", help="these PRs, hard filters applied")
    src.add_argument("--all-ostest", action="store_true",
                     help="project nuttx: every merged apps PR that changes testing/ostest/")
    p.add_argument("--no-filter", action="store_true", help="with --pr: skip the hard filters")
    p.add_argument("--redo", action="store_true",
                   help="also take PRs the ledger has already settled in an earlier run")
    p.add_argument("--until", help="with --since: merged on or before this date")
    p.add_argument("--verdicts-from", metavar="RUN",
                   help="project nuttx: take these PRs' triage verdicts from an earlier run "
                        "instead of triaging them again (with --pr)")
    p.add_argument("--config", help="default: cluster/config.yaml")
    args = p.parse_args()
    if args.since and not args.until:
        p.error("--since needs --until")
    if args.no_filter and not args.pr:
        p.error("--no-filter goes with --pr")
    if args.verdicts_from and not args.pr:
        p.error("--verdicts-from goes with --pr")

    cfg = cluster_config.load_or_exit(args.config)
    if args.all_ostest and cfg["project"] != "nuttx":
        p.error("--all-ostest needs project: nuttx in the config")
    if args.verdicts_from and cfg["project"] != "nuttx":
        p.error("--verdicts-from needs project: nuttx in the config")
    run = Path(cfg["derived"]["runs"]) / args.name
    if (run / "manifest.json").exists():
        sys.exit(f"{run} is already prepared; pick another --name "
                 "(or delete that run folder yourself)")
    run.mkdir(parents=True, exist_ok=True)
    env = cluster_config.env_for(cfg, run)
    os.environ.update(env)              # for the modules imported below
    if cfg["project"] == "nuttx":
        prepare_nuttx(args, cfg, run, env,
                      {} if args.redo else ledger.latest(Path(cfg["derived"]["ledger"])))
        return
    logs = run / "logs" / "prepare"
    par = cfg["parallel"]
    t0 = time.time()
    timings = {}
    log(f"preparing {run}")

    # 1. the PRs; those an earlier run settled (ledger.py) are skipped
    stage, notes = {}, {}
    done = {} if args.redo else ledger.latest(Path(cfg["derived"]["ledger"]))

    def skip_settled(prs):
        for pr in prs:
            if pr in done:
                e = done[pr]
                stage[pr], notes[pr] = "in ledger", f"{e['outcome']} in run {e['run']} ({e['date'][:10]})"
        n = sum(1 for pr in prs if pr in done)
        if n:
            log(f"{n} already settled in earlier runs (ledger); --redo takes them again")

    if args.pr:
        prs = list(dict.fromkeys(args.pr))
        source = {"pr": prs, "hard_filter": not args.no_filter, "redo": args.redo}
        skip_settled(prs)
        fresh = [pr for pr in prs if pr not in stage]
        if not args.no_filter and fresh:
            log("hard filters")
            script("filter_candidates.py", "fetch", "--pr", *map(str, fresh), env=env,
                   logfile=logs / "fetch.log")
            sys.path.insert(0, str(REPO / "scripts"))
            import filter_candidates as fc
            for pr in fresh:
                f = fc.CACHE / f"{pr}.json"
                ok, why = fc.classify(json.loads(f.read_text())) if f.exists() else (False, "fetch failed")
                if not ok:
                    stage[pr], notes[pr] = "hard filter", why
    else:
        source = {"since": args.since, "until": args.until, "redo": args.redo}
        log(f"fetching PRs merged {args.since} .. {args.until}")
        if script("filter_candidates.py", "fetch", "--since", args.since, "--until", args.until,
                  env=env, logfile=logs / "fetch.log"):
            sys.exit(f"fetch failed; see {logs / 'fetch.log'}")
        if script("filter_candidates.py", "filter", env=env, logfile=logs / "filter.log"):
            sys.exit(f"filter failed; see {logs / 'filter.log'}")
        prs = [json.loads(l)["number"] for l in open(run / "candidates" / "candidates.jsonl")]
        skip_settled(prs)
    timings["select"] = time.time() - t0
    log(f"{len(prs)} PRs")

    # 2. enrichment, in parallel (filter_candidates.py enrich --parallel)
    t = time.time()
    log(f"enriching, {par['enrich']} at a time")
    kept = [pr for pr in prs if pr not in stage]
    if kept:
        script("filter_candidates.py", "enrich", "--parallel", str(par["enrich"]),
               "--pr", *map(str, kept), env=env, logfile=logs / "enrich.log")
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

    write_manifest(run, args.name, cfg, env, source, prs, stage, notes, timings,
                   [f"{sdk} (Python {py})" for py, sdk, _ in missing
                    if not au.sdk_image(sdk, py).exists()], t0)


if __name__ == "__main__":
    main()
