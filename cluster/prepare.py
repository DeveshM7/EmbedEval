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

--project mynewt (or `project: mynewt` in the config) prepares a Mynewt run
instead (plan_mynewt): the PRs come from scripts/mynewt_candidates.py (fetch,
filter: every merged PR that changes a unit-test package; --all takes the
whole history, --since/--until a window of merge dates) and are enriched
into <run>/candidates/mynewt/enriched/; there is no dry run, and the shared
Mynewt image is built by the first validation worker (on x86). It needs the
`gh` CLI and the clone (paths.mynewt_clone). --instances DIR prepares a
revalidation of existing instances instead (each <id>/metadata.json and
test_patch.diff is copied into <run>/sources/): no triage, every PR "to
validate". Then `submit.py triage`, and `submit.py validate --after <job>`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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


def plan_mynewt(args, cfg: dict, run: Path, env: dict, logs: Path, skip_known, stage: dict,
                notes: dict, timings: dict) -> tuple[list[int], dict, dict]:
    """Steps 1-5 for Mynewt: (the PRs, the manifest's source, extra manifest
    fields per PR). Every PR ends in `stage`."""
    clone = Path(cfg["derived"]["mynewt_clone"])
    if not (clone / "objects").is_dir() and not (clone / ".git").is_dir():
        sys.exit(f"no mynewt-core clone at {clone}; `git clone --bare https://github.com/apache/"
                 "mynewt-core.git` there (or set paths.mynewt_clone)")
    extra: dict[int, dict] = {}
    t = time.time()
    if args.instances:
        sys.path.insert(0, str(REPO / "scripts"))
        import instances as inst_mod
        root = Path(args.instances).resolve()
        ids = sorted(d.name for d in root.iterdir() if (d / "metadata.json").exists())
        prs = []
        for iid in ids:
            project, pr = inst_mod.split_instance(iid)
            m = re.fullmatch(r"(\d+)(?:-t(\d+))?", pr)
            if project != "mynewt" or not m:
                sys.exit(f"{iid}: only mynewt__mynewt-<PR> (or <fix PR>-t<test PR>) instances "
                         "can be revalidated")
            pr = m.group(2) or m.group(1)      # a run keys a PR by its tests' PR, as triage does
            src = run / "sources" / iid
            src.mkdir(parents=True, exist_ok=True)
            for f in ("metadata.json", "test_patch.diff", "observed_before.log"):
                if (root / iid / f).exists():
                    shutil.copy2(root / iid / f, src / f)
            prs.append(int(pr))
            stage[int(pr)] = "to validate"
            extra[int(pr)] = {"instance": iid}
        timings["select"] = time.time() - t
        log(f"{len(prs)} instances from {root}, to validate")
        return prs, {"instances": str(root), "redo": args.redo}, extra

    data = run / "candidates" / "mynewt"
    if args.pr:
        prs = list(dict.fromkeys(args.pr))
        source = {"pr": prs, "redo": args.redo}
        skip_known(prs)
        fresh = [pr for pr in prs if pr not in stage]
        if fresh:
            script("mynewt_candidates.py", "fetch", "--pr", *map(str, fresh), env=env,
                   logfile=logs / "fetch.log", timeout=None)
    else:
        source = {"all": True} if args.all else {"since": args.since, "until": args.until}
        source["redo"] = args.redo
        log("fetching every PR of apache/mynewt-core")
        if script("mynewt_candidates.py", "fetch", env=env, logfile=logs / "fetch.log", timeout=None):
            sys.exit(f"fetch failed; see {logs / 'fetch.log'}")
    if script("mynewt_candidates.py", "filter", env=env, logfile=logs / "filter.log"):
        sys.exit(f"filter failed; see {logs / 'filter.log'}")
    found = {json.loads(l)["number"]: json.loads(l) for l in open(data / "candidates.jsonl")}
    if args.pr:
        for pr in (p for p in prs if p not in stage):
            if pr not in found:
                f = data / "cache" / f"{pr}.json"
                stage[pr], notes[pr] = ("hard filter", "changes no unit-test package, or not merged, "
                                        "or a revert") if f.exists() else ("fetch failed", f"see {logs / 'fetch.log'}")
    else:
        prs = sorted(n for n, c in found.items()
                     if args.all or args.since <= (c.get("merged_at") or "")[:10] <= args.until)
        skip_known(prs)
    timings["select"] = time.time() - t
    log(f"{len(prs)} PRs")

    t = time.time()
    kept = [pr for pr in prs if pr not in stage]
    log(f"enriching {len(kept)}, {cfg['parallel']['enrich']} at a time")
    if kept:
        script("mynewt_candidates.py", "enrich", "--parallel", str(cfg["parallel"]["enrich"]),
               "--pr", *map(str, kept), env=env, logfile=logs / "enrich.log", timeout=None)
    for pr in kept:
        f = data / "enriched" / f"{pr}.json"
        if not f.exists():
            stage[pr], notes[pr] = "enrich failed", f"see {logs / 'enrich.log'}"
        elif not json.loads(f.read_text()).get("test_suites"):
            stage[pr], notes[pr] = "enrich: no runnable test", "no unit-test package at the PR's head"
        else:
            stage[pr] = "to triage"
    timings["enrich"] = time.time() - t
    log(f"enriched in {timings['enrich']:.0f} s: {sum(1 for p in kept if stage[p] == 'to triage')} to triage")
    return prs, source, extra


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True, help="the run's name, its folder under runs/")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--since", help="fetch PRs merged on or after this date (YYYY-MM-DD)")
    src.add_argument("--pr", type=int, nargs="+", help="these PRs, hard filters applied")
    src.add_argument("--all", action="store_true", help="Mynewt: every PR in the history")
    src.add_argument("--instances", help="Mynewt: revalidate the instances in this folder "
                     "(e.g. apptainer/instances/mynewt)")
    p.add_argument("--project", choices=cluster_config.PROJECTS,
                   help="default: the config's `project`")
    p.add_argument("--no-filter", action="store_true", help="with --pr: skip the hard filters")
    p.add_argument("--redo", action="store_true",
                   help="also take PRs the ledger has already settled in an earlier run")
    p.add_argument("--until", help="with --since: merged on or before this date")
    p.add_argument("--config", help="default: cluster/config.yaml")
    args = p.parse_args()
    if args.since and not args.until:
        p.error("--since needs --until")
    if args.no_filter and not args.pr:
        p.error("--no-filter goes with --pr")

    cfg = cluster_config.load_or_exit(args.config)
    project = args.project or cfg["project"]
    if project == "zephyr" and (args.all or args.instances):
        p.error("--all and --instances are Mynewt only")
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

    if project == "mynewt":
        prs, source, extra = plan_mynewt(args, cfg, run, env, logs, skip_known, stage, notes, timings)
        write_manifest(args, cfg, project, run, env, source, prs, stage, notes, timings, t0, [], extra)
        print(f"\nnext: python cluster/submit.py {'validate' if args.instances else 'triage'} "
              f"--run {args.name}" + ("" if args.instances else
                                      f"; then submit.py validate --run {args.name} --after <job>"))
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
    write_manifest(args, cfg, project, run, env, source, prs, stage, notes, timings, t0,
                   [f"{sdk} (Python {py})" for py, sdk, _ in missing if not au.sdk_image(sdk, py).exists()])
    print(f"\nnext: python cluster/submit.py pipeline --run {args.name}")


def write_manifest(args, cfg: dict, project: str, run: Path, env: dict, source: dict, prs: list[int],
                   stage: dict, notes: dict, timings: dict, t0: float, missing_images: list[str],
                   extra: dict | None = None) -> None:
    """Step 6: the manifest, the ledger's settled outcomes, and the summary."""
    git = lambda *a: subprocess.run(["git", "-C", str(REPO), *a], capture_output=True,
                                    text=True).stdout.strip()
    manifest = {
        "name": args.name, "prepared": datetime.now().isoformat(timespec="seconds"),
        **({"project": project} if project != "zephyr" else {}),
        "source": source,
        "code": {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))},
        "config": {k: v for k, v in cfg.items() if k != "derived"},
        "environment": env,
        "prs": [{"pr": pr, "stage": stage[pr]} | ({"note": notes[pr]} if pr in notes else {})
                | (extra or {}).get(pr, {}) for pr in prs],
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
        print(f"  WARNING: SDK images could not be built: {manifest['missing_sdk_images']}")


if __name__ == "__main__":
    main()
