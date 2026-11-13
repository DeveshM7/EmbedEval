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

    --verdicts-from RUN (project nuttx): the PRs' triage verdicts are taken from
    an earlier run, so the job only validates them.

    --instances DIR (project nuttx): a revalidation of existing instances,
    each rendered again into <run>/instances/ with the current templates; no
    triage. Its phases are separate jobs (submit.py statement, validate).

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


def write_nuttx_manifest(run: Path, name: str, cfg: dict, env: dict, source: dict, prs: list,
                   stage: dict, notes: dict, timings: dict, missing_sdk: list, t0: float,
                   extra: dict | None = None) -> None:
    git = lambda *a: subprocess.run(["git", "-C", str(REPO), *a], capture_output=True,
                                    text=True).stdout.strip()
    manifest = {
        "name": name, "prepared": datetime.now().isoformat(timespec="seconds"),
        "project": cfg["project"], "source": source,
        "code": {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))},
        "config": {k: v for k, v in cfg.items() if k != "derived"},
        "environment": env, "ledger": cfg["derived"]["ledger"],
        "prs": [{"pr": pr, "stage": stage[pr]} | ({"note": notes[pr]} if pr in notes else {})
                | (extra or {}).get(pr, {}) for pr in prs],
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
    if extra:
        archs = sorted({e["arch"] for e in extra.values()})
        print(f"\nnext: python cluster/submit.py statement --run {name}; then, --after it, "
              + "; ".join(f"submit.py validate --run {name} --arch {a}" for a in archs))
    elif cfg["project"] == "nuttx":
        print(f"\nnext: python cluster/submit.py triage --run {name} (none left with --verdicts-from); "
              f"then submit.py validate --run {name} --arch aarch64 (and x86_64) --after <job>")
    else:
        print(f"\nnext: python cluster/submit.py pipeline --run {name}")


INSTANCE_ID = re.compile(r"nuttx__nuttx-(\d+)-a(\d+)")


def instance_entry(folder: Path) -> tuple[int, dict]:
    """(the apps PR a run keys the instance by, its manifest fields). The id
    nuttx__nuttx-<kernel PR>-a<apps PR> must agree with the metadata. The
    instance's architecture: its host_arch, else where it last validated."""
    meta = json.loads((folder / "metadata.json").read_text())
    m = INSTANCE_ID.fullmatch(folder.name)
    if not m or meta.get("instance_id") != folder.name or \
            (int(m[1]), int(m[2])) != (meta.get("kernel_pr"), meta.get("apps_pr")):
        raise ValueError(f"{folder.name}: not a nuttx__nuttx-<kernel PR>-a<apps PR> instance "
                         "matching its metadata")
    receipt = folder / "validation.json"
    arch = meta.get("host_arch") or (json.loads(receipt.read_text()).get("host") if receipt.exists() else None)
    if arch not in ("aarch64", "x86_64"):
        raise ValueError(f"{folder.name}: no host_arch and no validation host")
    return int(m[2]), {"instance": folder.name, "arch": arch}


def prepare_instances(args, cfg: dict, run: Path, env: dict) -> None:
    """A revalidation of existing NuttX instances: each is rendered again
    into <run>/instances/ with the current templates
    (render_nuttx_instance.py --instance), keyed by its apps PR, "to
    validate", with no triage."""
    t0 = time.time()
    sys.path.insert(0, str(REPO / "scripts"))
    import render_nuttx_instance as rn
    if cfg["nuttx"]["data"] and not os.environ.get("EMBEDEVAL_NUTTX_CLONE"):
        # the renderer reads the kernel at its base (render_nuttx_instance.needs_version_file)
        os.environ["EMBEDEVAL_NUTTX_CLONE"] = str(Path(cfg["nuttx"]["data"]) / "kernel.git")
    root = Path(args.instances).resolve()
    folders = sorted(d for d in root.iterdir() if (d / "metadata.json").exists())
    prs, stage, extra = [], {}, {}
    for folder in folders:
        pr, fields = instance_entry(folder)
        if pr in extra:
            sys.exit(f"apps PR {pr} has two instances ({extra[pr]['instance']}, {folder.name}); "
                     "a run keys instances by apps PR")
        rn.rerender(folder, run / "instances")
        prs.append(pr)
        stage[pr], extra[pr] = "to validate", fields
    log(f"{len(prs)} instances from {root}, rendered into {run / 'instances'}")
    write_nuttx_manifest(run, args.name, cfg, env, {"instances": str(root)}, prs, stage, {},
                   {"render": time.time() - t0}, [], t0, extra)


def seed_verdicts(src: Path, run: Path, prs: list[int], stage: dict,
                  confidence=("high", "medium")) -> int:
    """Take the triage verdicts of an earlier run for these PRs: the verdict
    file and its trace are copied and the triage stage is marked done, so the
    pipeline job validates them without triaging again; accept or reject is
    decided again by this run's nuttx.validate_confidence. Returns how many."""
    import status
    from node_worker import nuttx_verdict_info
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
        info = nuttx_verdict_info(json.loads(verdict.read_text()), confidence)
        status.write_stage(run, pr, "triage", old | info | {"seeded_from": src.name})
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
        seeded = seed_verdicts(Path(cfg["derived"]["runs"]) / args.verdicts_from, run, prs, stage,
                               cfg["nuttx"]["validate_confidence"])
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
    write_nuttx_manifest(run, args.name, cfg, env, source | {"redo": args.redo}, prs, stage, notes,
                   {"records": time.time() - t0}, [], t0)


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
    src.add_argument("--all", action="store_true", help="Mynewt: every PR in the history")
    src.add_argument("--all-ostest", action="store_true",
                     help="project nuttx: every merged apps PR that changes testing/ostest/")
    src.add_argument("--instances", nargs="+", type=Path, metavar="DIR",
                     help="Mynewt, NuttX or Contiki-NG: revalidate existing instances. Mynewt "
                          "and NuttX take one folder of them (e.g. apptainer/instances/mynewt, "
                          "apptainer/instances/nuttx; NuttX instances are rendered again with "
                          "the current templates); Contiki-NG takes instance folders, or "
                          "folders of them")
    p.add_argument("--project", choices=cluster_config.PROJECTS,
                   help="default: the config's `project` (nuttx needs it in the config)")
    p.add_argument("--no-filter", action="store_true", help="with --pr: skip the hard filters")
    p.add_argument("--redo", action="store_true",
                   help="also take PRs the ledger has already settled in an earlier run")
    p.add_argument("--until", help="with --since: merged on or before this date")
    p.add_argument("--verdicts-from", metavar="RUN",
                   help="project nuttx: take these PRs' triage verdicts from an earlier run "
                        "instead of triaging them again (with --pr)")
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
    if args.verdicts_from and not args.pr:
        p.error("--verdicts-from goes with --pr")

    cfg = cluster_config.load_or_exit(args.config)
    project = args.project or cfg["project"]
    if project == "zephyr" and args.instances:
        p.error("--instances is for Mynewt, NuttX and Contiki-NG runs")
    if args.all and project != "mynewt":
        p.error("--all is Mynewt only")
    if project == "nuttx" and cfg["project"] != "nuttx":
        # NuttX's settings, ledger and phases read the config's project
        p.error("a NuttX run needs project: nuttx in the config")
    if args.all_ostest and project != "nuttx":
        p.error("--all-ostest needs project: nuttx in the config")
    if args.verdicts_from and project != "nuttx":
        p.error("--verdicts-from needs project: nuttx in the config")
    if args.observe_only and project != "contiki-ng":
        p.error("--observe-only is Contiki-NG only")
    if args.instances and project == "contiki-ng" and (args.observe_only or args.no_filter):
        p.error("--instances goes without --observe-only or --no-filter")
    if args.instances and project in ("mynewt", "nuttx"):
        if len(args.instances) > 1:
            p.error(f"--instances takes one folder of {project} instances")
        args.instances = args.instances[0]
    run = Path(cfg["derived"]["runs"]) / args.name
    if (run / "manifest.json").exists():
        sys.exit(f"{run} is already prepared; pick another --name "
                 "(or delete that run folder yourself)")
    run.mkdir(parents=True, exist_ok=True)
    env = cluster_config.env_for(cfg, run, project)
    os.environ.update(env)              # for the modules imported below
    if project == "nuttx":
        if args.instances:
            prepare_instances(args, cfg, run, env)
        else:
            prepare_nuttx(args, cfg, run, env,
                          {} if args.redo else ledger.latest(Path(cfg["derived"]["ledger"])))
        return
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

    if project == "mynewt":
        prs, source, extra = plan_mynewt(args, cfg, run, env, logs, skip_known, stage, notes, timings)
        write_manifest(args, cfg, project, run, env, source, prs, stage, notes, timings, t0, [], extra)
        print(f"\nnext: python cluster/submit.py {'validate' if args.instances else 'triage'} "
              f"--run {args.name}" + ("" if args.instances else
                                      f"; then submit.py validate --run {args.name} --after <job>"))
        return
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
            # Zephyr marks a PR GitHub did not serve below, as it always has
            if candidate_cmd("fetch", "--pr", *map(str, fresh),
                             logfile=logs / "fetch.log") and project == "contiki-ng":
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
    write_manifest(args, cfg, project, run, env, source, prs, stage, notes, timings, t0,
                   [f"{sdk} (Python {py})" for py, sdk, _ in missing if not au.sdk_image(sdk, py).exists()]
                   if project == "zephyr" else [], sources=sources)
    mode = (("validate" if sources else "observe" if args.observe_only else "triage")
            if project == "contiki-ng" else "pipeline")
    print(f"\nnext: python cluster/submit.py {mode} --run {args.name}")


def write_manifest(args, cfg: dict, project: str, run: Path, env: dict, source: dict, prs: list[int],
                   stage: dict, notes: dict, timings: dict, t0: float, missing_images: list[str],
                   extra: dict | None = None, sources: dict | None = None) -> None:
    """Step 6: the manifest, the ledger's settled outcomes, and the summary.
    `sources`: a Contiki-NG revalidation's instance folders, per PR."""
    sources = sources or {}
    observe_profile = getattr(args, "observe_profile", None)
    manifest = {
        "name": args.name, "prepared": datetime.now().isoformat(timespec="seconds"),
        **({"project": project} if project != "zephyr" else {}),
        "source": source,
        "code": code_identity(REPO, project),
        "config": {k: v for k, v in cfg.items() if k != "derived"},
        "environment": env,
        "prs": [{"pr": pr, "stage": stage[pr]} | ({"note": notes[pr]} if pr in notes else {})
                | (extra or {}).get(pr, {})
                | ({"instance": sources[pr].name} if pr in sources else {})
                | ({"observe_profile": observe_profile}
                   if observe_profile and stage[pr] == "to triage" else {})
                for pr in prs],
        **({"observe_only": True} if getattr(args, "observe_only", False) else {}),
        **({"revalidation": True} if sources else {}),
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
