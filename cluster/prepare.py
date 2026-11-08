#!/usr/bin/env python3
"""
Prepare a run: everything before the cluster job, on the node you run it from.

    python cluster/prepare.py --name 2025-09 --since 2025-09-01 --until 2025-09-30
    python cluster/prepare.py --name gold --pr 65697 85079 ...
    python cluster/prepare.py --name reval --instances apptainer/instances/contiki-ng   # Contiki-NG

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

Contiki-NG --instances DIR...: a revalidation of existing instances (each DIR
an instance folder, or a folder of them) under the current runner: their
instance.json, metadata.json and test_patch.diff are copied to <run>/sources/,
and `submit.py validate` renders each again from there (generate_instance.py
--render), builds and validates it. No triage; the ledger is not consulted.
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


def code_identity(repo: Path, project: str) -> dict:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True).stdout.strip()

    git_root = git("rev-parse", "--show-toplevel")
    archive = project == "contiki-ng" and (not git_root or Path(git_root).resolve() != repo.resolve())
    if archive:
        stamp = repo / ".embedeval-revision"
        return {"commit": (stamp.read_text().strip() or None) if stamp.exists() else None,
                "dirty": None, "archive": True}
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


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


SOURCE_FILES = ("instance.json", "metadata.json", "test_patch.diff")


def instance_sources(dirs: list[Path], run: Path) -> dict[int, Path]:
    """PR -> <run>/sources/<instance>, copied from the given instance folders
    (or folders of them). One instance per PR; an id must round-trip."""
    import shutil
    sys.path.insert(0, str(REPO / "scripts"))
    import instances as inst_mod
    found: dict[int, Path] = {}
    for d in dirs:
        d = Path(d).resolve()
        folders = [d] if (d / "metadata.json").exists() else sorted(
            x for x in d.iterdir() if (x / "metadata.json").exists())
        for f in folders:
            project, pr = inst_mod.split_instance(f.name)
            if project != "contiki-ng" or f.name != f"contiki-ng__contiki-ng-{int(pr)}":
                sys.exit(f"{f}: not a contiki-ng__contiki-ng-<PR> instance folder")
            missing = [n for n in SOURCE_FILES if not (f / n).exists()]
            if missing:
                sys.exit(f"{f}: missing {', '.join(missing)}")
            if json.loads((f / "metadata.json").read_text()).get("instance_id") != f.name:
                sys.exit(f"{f}: metadata.json names another instance")
            if int(pr) in found:
                sys.exit(f"PR {pr} twice: {found[int(pr)]} and {f}")
            found[int(pr)] = f
    out = {}
    for pr, f in found.items():
        dst = run / "sources" / f.name
        dst.mkdir(parents=True, exist_ok=True)
        for n in SOURCE_FILES:
            shutil.copy2(f / n, dst / n)
        out[pr] = dst
    if not out:
        sys.exit("no instance folders found")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True, help="the run's name, its folder under runs/")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--since", help="fetch PRs merged on or after this date (YYYY-MM-DD)")
    src.add_argument("--pr", type=int, nargs="+", help="these PRs, hard filters applied")
    src.add_argument("--instances", nargs="+", type=Path, metavar="DIR",
                     help="Contiki-NG: revalidate these instance folders (or folders of them)")
    p.add_argument("--no-filter", action="store_true", help="with --pr: skip the hard filters")
    p.add_argument("--redo", action="store_true",
                   help="also take PRs the ledger has already settled in an earlier run")
    p.add_argument("--until", help="with --since: merged on or before this date")
    p.add_argument("--config", help="default: cluster/config.yaml")
    p.add_argument("--observe-only", action="store_true",
                   help="Contiki: a model-free observation run (observe/finalize/statement jobs); "
                        "it never holds PRs for triage")
    p.add_argument("--observe-profile", choices=("native", "cooja", "script"),
                   help="with --observe-only: observe every PR under this profile, so PRs whose "
                        "units span profiles are covered by one run per profile")
    args = p.parse_args()
    if args.since and not args.until:
        p.error("--since needs --until")
    if args.no_filter and not args.pr:
        p.error("--no-filter goes with --pr")
    if args.observe_profile and not args.observe_only:
        p.error("--observe-profile goes with --observe-only")

    cfg = cluster_config.load_or_exit(args.config)
    project = cfg["project"]
    if args.instances and (project != "contiki-ng" or args.observe_only or args.no_filter):
        p.error("--instances is Contiki-NG only, without --observe-only or --no-filter")
    run = Path(cfg["derived"]["runs"]) / args.name
    if (run / "manifest.json").exists():
        sys.exit(f"{run} is already prepared; pick another --name "
                 "(or delete that run folder yourself)")
    run.mkdir(parents=True, exist_ok=True)
    env = cluster_config.env_for(cfg, run)
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
    taken = status.unfinished(Path(cfg["derived"]["runs"]), but=run, project=project)

    sys.path.insert(0, str(REPO / "scripts"))
    if project == "contiki-ng":
        import contiki_ng_candidates as candidates
    else:
        import filter_candidates as candidates

    def candidate_cmd(command: str, *more: str, logfile: Path, timeout=None) -> int:
        return script("filter_candidates.py", command, "--repo", project, *more,
                      env=env, logfile=logfile, timeout=timeout)

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

    sources = {}
    if args.instances:
        sources = instance_sources(args.instances, run)
        prs = sorted(sources)
        source = {"instances": [str(Path(d).resolve()) for d in args.instances]}
        # Not held back by another run's unfinished triage or validation of the
        # PR: a revalidation writes only its own run folder.
        for pr in prs:
            stage[pr] = "to validate"
        log(f"{len(prs)} instances to revalidate")
    elif args.pr:
        prs = list(dict.fromkeys(args.pr))
        source = {"pr": prs, "hard_filter": not args.no_filter, "redo": args.redo}
        skip_known(prs)
        fresh = [pr for pr in prs if pr not in stage]
        if not args.no_filter and fresh:
            log("hard filters")
            if candidate_cmd("fetch", "--pr", *map(str, fresh),
                             logfile=logs / "fetch.log"):
                sys.exit(f"fetch failed; see {logs / 'fetch.log'}")
            for pr in fresh:
                if project == "contiki-ng":
                    try:
                        rec = candidates.find_pr(pr)
                    except (KeyError, ValueError):
                        rec = None
                    if rec is None:
                        stage[pr], notes[pr] = "fetch failed", f"see {logs / 'fetch.log'}"
                        continue
                    ok, why = candidates.classify(rec)
                else:
                    f = candidates.CACHE / f"{pr}.json"
                    if not f.exists():
                        stage[pr], notes[pr] = "fetch failed", f"see {logs / 'fetch.log'}"
                        continue
                    ok, why = candidates.classify(json.loads(f.read_text()))
                if not ok:
                    stage[pr], notes[pr] = "hard filter", why
    else:
        source = {"since": args.since, "until": args.until, "redo": args.redo}
        log(f"fetching PRs merged {args.since} .. {args.until}")
        if candidate_cmd("fetch", "--since", args.since, "--until", args.until,
                         logfile=logs / "fetch.log"):
            sys.exit(f"fetch failed; see {logs / 'fetch.log'}")
        if candidate_cmd("filter", logfile=logs / "filter.log"):
            sys.exit(f"filter failed; see {logs / 'filter.log'}")
        candidate_file = (run / "candidates" / ("contiki-ng" if project == "contiki-ng" else "")
                          / "candidates.jsonl")
        prs = [json.loads(l)["number"] for l in open(candidate_file)]
        skip_known(prs)
    timings["select"] = time.time() - t0
    log(f"{len(prs)} PRs")

    # 2. enrichment, in parallel (filter_candidates.py enrich --parallel)
    t = time.time()
    log(f"enriching, {par['enrich']} at a time")
    kept = [pr for pr in prs if pr not in stage]
    if sources:
        kept = []                       # a revalidation takes no new PR records
    if kept:
        candidate_cmd("enrich", "--parallel", str(par["enrich"]),
                      "--pr", *map(str, kept), logfile=logs / "enrich.log")
    enriched = [pr for pr in kept if (run / "enriched" / f"{pr}.json").exists()]
    timings["enrich"] = time.time() - t
    log(f"enriched {len(enriched)}/{len(kept)} in {timings['enrich']:.0f} s")

    # 3. dry runs, in parallel: one process per PR
    t = time.time()
    log(f"dry runs, {par['dry_run']} at a time")
    if project == "zephyr":
        with ThreadPoolExecutor(par["dry_run"]) as pool:
            list(pool.map(lambda pr: script("dry_run.py", str(pr), env=env,
                                            logfile=logs / "dryrun" / f"{pr}.log", timeout=1800),
                          enriched))
    timings["dry_run"] = time.time() - t

    for pr in kept:
        f = run / "dryrun" / f"{pr}.json"
        if pr not in enriched:
            stage[pr] = "enrich failed"
        elif project == "contiki-ng":
            stage[pr] = "to triage"
        elif not f.exists():
            stage[pr] = "dry run error"
        elif json.loads(f.read_text())["auto_reject"]:
            stage[pr] = "dry run: no runnable pair"
        else:
            stage[pr] = "to triage"
    to_triage = [pr for pr in prs if stage[pr] in ("to triage", "to validate")]
    log(f"dry runs done in {timings['dry_run']:.0f} s: {len(to_triage)} to triage")

    # 4. shared SDK images the triaged PRs may need, each built once
    t = time.time()
    missing = []
    if project == "zephyr":
        import apptainer_util as au
        need = {}
        for pr in to_triage:
            tw = json.loads((run / "enriched" / f"{pr}.json").read_text())["twister"]
            need[(tw["python"], tw["sdk"], tw["sdk_archive"])] = pr
        missing = [k for k in need if not au.sdk_image(k[1], k[0]).exists()]
        for py, sdk, archive in missing:
            log(f"building SDK image {sdk} (Python {py})")
            try:
                au.ensure_base_images(py, sdk, archive, Path(cfg["derived"]["shared"]) / "logs")
            except RuntimeError as e:
                log(f"  FAILED: {e}")
    timings["sdk_images"] = time.time() - t

    # 5. every commit a worker reads, fetched now: workers never fetch
    t = time.time()
    if project == "zephyr":
        import zephyr_env
    shas = set()
    for pr in to_triage:
        rec = json.loads(((sources[pr] / "metadata.json") if pr in sources
                          else run / "enriched" / f"{pr}.json").read_text())
        shas |= {s for s in (rec.get("base_commit"), rec.get("head_commit"),
                             rec.get("merge_commit_sha"), rec.get("fix_commit")) if s}
    try:
        if project == "contiki-ng":
            candidates.ensure_commits(*sorted(shas))
        else:
            zephyr_env.ensure_commits(*sorted(shas))
        fetched = "ok"
    except RuntimeError as e:
        fetched = str(e)[:300]
    timings["commits"] = time.time() - t
    log(f"commits: {len(shas)} present ({fetched})")

    # 6. the manifest
    manifest = {
        "name": args.name, "project": project,
        "prepared": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "code": code_identity(REPO, project),
        "config": {k: v for k, v in cfg.items() if k != "derived"},
        "environment": env,
        "prs": [{"pr": pr, "stage": stage[pr]} | ({"note": notes[pr]} if pr in notes else {})
                | ({"instance": sources[pr].name} if pr in sources else {})
                | ({"observe_profile": args.observe_profile}
                   if args.observe_profile and stage[pr] == "to triage" else {})
                for pr in prs],
        **({"observe_only": True} if args.observe_only else {}),
        **({"revalidation": True} if sources else {}),
        "counts": {s: sum(1 for v in stage.values() if v == s) for s in sorted(set(stage.values()))},
        "missing_sdk_images": ([f"{sdk} (Python {py})" for py, sdk, _ in missing
                                if not au.sdk_image(sdk, py).exists()] if project == "zephyr" else []),
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
    mode = (("validate" if sources else "observe" if args.observe_only else "triage")
            if project == "contiki-ng" else "pipeline")
    print(f"\nnext: python cluster/submit.py {mode} --run {args.name}")


if __name__ == "__main__":
    main()
