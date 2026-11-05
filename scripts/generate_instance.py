#!/usr/bin/env python3
"""
Build a complete Zephyr instance recipe from a triage verdict.

    python scripts/generate_instance.py --pr 65697 --triage triage/ideal_verdicts/65697.json
    python scripts/generate_instance.py --pr 65697 --triage verdict.json --out /tmp/check

Writes an Apptainer recipe and everything it copies in, so none of it is
hand-written:

    instance.def      the image recipe, FROM the shared SDK image of the PR's era
    instance.json     board, scenario and twister options, read by run_tests
    run_tests.py      becomes /usr/local/bin/run_tests in the image
    test_patch.diff   the PR's test changes, written by git
    metadata.json     the instance's description

Nothing is built here; scripts/build_instances.py builds the image and
scripts/validate_instance.py validates it.

Deterministic on purpose. The verdict supplies the judgements -- the board and
scenario, the problem statement, the test lists -- and everything else comes
from the enriched record's `twister` block (scripts/zephyr_env.py): the base
and head commits, the era's Python and SDK, and which twister options exist.

Twister replaces `west build` throughout. It applies the scenario's own
settings (extra_configs, extra_args), so there is no extra_configs field to
copy by hand, and it reports a status per test.

Output goes to generated/ by default, never to docker/instances/, so this
cannot disturb an existing instance.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)

REPO_ROOT = Path(__file__).resolve().parent.parent
ENRICHED = layout.enriched()
DRYRUN = layout.dryrun()
TEMPLATES = REPO_ROOT / "templates" / "zephyr" / "apptainer"
DEFAULT_OUT = layout.generated()

REPO_URL = "https://github.com/zephyrproject-rtos/zephyr"
CLONE_CACHE = layout.zephyr_clone()


def run(cmd: list[str], timeout: int = 300, check: bool = True):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        sys.exit(f"ERROR: {' '.join(cmd[:4])} ... failed:\n{r.stderr.strip()[:400]}")
    return r


def ensure_clone() -> Path:
    """A full bare mirror of Zephyr, reused across instances and by
    triage_prs.py.

    Full rather than blobless. Blobless (all history, file contents fetched on
    first use) is enough for building a test patch, but triage searches the
    tree with git grep, and in a blobless clone that downloads every file under
    the searched path one at a time -- so searches had to be capped at a few
    hundred files, and the model spent its tool rounds guessing narrower
    directories. A full clone is a few GB, once, and makes any search instant.
    """
    if not CLONE_CACHE.exists():
        CLONE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        print(f"  cloning {REPO_URL} (full, one-time; a few GB) ...")
        run(["git", "clone", "--bare", f"{REPO_URL}.git", str(CLONE_CACHE)], timeout=3600)
    return CLONE_CACHE


def build_test_patch(base: str, head: str, clone: Path | None = None) -> str:
    """The PR's test-file changes, as a diff written by git.

    Produced by git rather than assembled from GitHub's per-file `patch`
    fields. Those carry the changed lines but not the headers around them, so
    assembling by hand means reproducing git's format exactly -- and a missing
    `new file mode` line silently breaks every PR that creates a test file,
    which is how 43405, 74435 and 82272 came to fail. Letting git write it
    removes that whole class of error rather than the one instance of it.

    All of tests/, including testcase.yaml: a scenario setting the PR adds
    there is part of how its test detects the bug (85079 adds
    CONFIG_RTIO_SUBMIT_SEM=n to its scenario), and twister applies it.

    The range is safe because both ends are on the PR's own branch: base is its
    merge base, head its tip, and the branch contains nothing that landed on
    main after it forked. Contamination came from ranges ending at a *merge
    commit* -- base..merge_commit traverses everyone else's work, which is what
    put seven foreign files into the nuttx-11889 patch.
    """
    clone = clone or ensure_clone()
    for sha in (base, head):
        if run(["git", "-C", str(clone), "cat-file", "-e", f"{sha}^{{commit}}"],
               check=False).returncode:
            run(["git", "-C", str(clone), "fetch", "-q", "origin", sha], timeout=900)
    r = run(["git", "-C", str(clone), "diff", f"{base}..{head}", "--", "tests/"])
    return r.stdout


def check_applies(patch: str, base: str, inst: Path, clone: Path | None = None) -> None:
    """Refuse to emit an instance whose patch git will not accept.

    Without this the first sign of a malformed patch is `git apply` failing
    minutes into an image build, and only for instances someone happens to
    build. A dry run against the base commit costs about a second and covers
    every instance, every time.
    """
    clone = clone or ensure_clone()
    patch_file = (inst / "test_patch.diff").resolve()
    with tempfile.TemporaryDirectory(prefix="embedeval-applycheck-") as tmp:
        work = Path(tmp) / "tree"
        run(["git", "-C", str(clone), "worktree", "add", "-q", "--detach",
             str(work), base], timeout=300)
        try:
            r = run(["git", "-C", str(work), "apply", "--check", str(patch_file)], check=False)
            if r.returncode != 0:
                sys.exit("ERROR: generated test_patch.diff does not apply at "
                         f"{base[:12]}:\n{r.stderr.strip()[:400]}")
        finally:
            run(["git", "-C", str(clone), "worktree", "remove", "--force", str(work)],
                timeout=120, check=False)


def check_choice(pr: int, facts: dict, platform: str, test_path: str, scenario: str,
                 force: bool) -> None:
    """The verdict's board and scenario must exist at this commit and, when a
    dry run has been done, be a pair twister selects."""
    problems = []
    if platform not in [b for b in facts["boards"].values() if b]:
        problems.append(f"board {platform!r} is not a candidate board at the base commit "
                        f"(have {facts['boards']})")
    suite = facts["suites"].get(test_path)
    if not suite:
        problems.append(f"{test_path!r} is not a suite this PR changes "
                        f"(have {sorted(facts['suites'])})")
    elif scenario not in suite.get("scenarios", {}):
        problems.append(f"scenario {scenario!r} is not in {test_path} "
                        f"(have {sorted(suite.get('scenarios', {}))})")
    dr = DRYRUN / f"{pr}.json"
    if dr.exists():
        pairs = json.loads(dr.read_text())["pairs"]
        hit = next((p for p in pairs if p["suite"] == test_path and p["scenario"] == scenario
                    and p["board"] == platform), None)
        if hit and hit["status"] != "selected":
            problems.append(f"the dry run filtered {scenario} on {platform}: {hit['reason']}")
    else:
        print(f"  note: no dry run for {pr} (candidates/dryrun/{pr}.json); choice not checked")
    if problems:
        msg = "; ".join(problems)
        if not force:
            sys.exit(f"ERROR: verdict for {pr}: {msg}")
        print(f"  WARNING (--force): {msg}")


def generate(pr: int, triage: dict, out_root: Path, force: bool = False) -> Path:
    rec_path = ENRICHED / f"{pr}.json"
    if not rec_path.exists():
        sys.exit(f"ERROR: no enriched record at {rec_path}. Run `enrich` first.")
    rec = json.loads(rec_path.read_text())
    facts = rec.get("twister")
    if not facts or "error" in facts:
        sys.exit(f"ERROR: enriched record for {pr} has no twister facts; "
                 f"run `filter_candidates.py enrich-twister --pr {pr}`")

    for field in ("platform", "test_path", "scenario", "problem_statement", "fail_to_pass"):
        if not triage.get(field):
            sys.exit(f"ERROR: triage verdict for {pr} has no {field!r}")
    platform, test_path, scenario = triage["platform"], triage["test_path"], triage["scenario"]
    check_choice(pr, facts, platform, test_path, scenario, force)

    base, head = facts["base_commit"], facts["head_commit"]
    instance_id = f"zephyr__zephyr-{pr}"
    inst = out_root / instance_id
    inst.mkdir(parents=True, exist_ok=True)

    patch = build_test_patch(base, head)
    if not patch.strip():
        sys.exit(f"ERROR: no test-file diff for {pr}; instance would have no tests")
    (inst / "test_patch.diff").write_text(patch)
    check_applies(patch, base, inst)

    # Era-specific twister options. -W in every era: twister turns warnings
    # into errors by default, and a newer compiler than upstream CI's warns
    # about old code that CI built cleanly. --json-report only where that
    # commit's twister has it (2021-01-25 to 2022-05-13); later twister writes
    # the JSON report by default and rejects the option.
    twister_args = ["-W"] + (["--json-report"] if facts["twister"]["json_report"] else [])
    (inst / "instance.json").write_text(json.dumps(
        {"platform": platform, "test_path": test_path, "scenario": scenario,
         "twister_args": twister_args}, indent=2) + "\n")
    shutil.copy(TEMPLATES / "run_tests.py", inst / "run_tests.py")

    definition = (TEMPLATES / "instance.def.tmpl").read_text()
    for key, val in {"@@INSTANCE_ID@@": instance_id, "@@REPO@@": REPO_URL,
                     "@@BASE_COMMIT@@": base, "@@PYTHON@@": facts["python"],
                     "@@SDK@@": facts["sdk"]}.items():
        definition = definition.replace(key, val)
    (inst / "instance.def").write_text(definition)

    cmd = (f"scripts/twister -p {platform} -T {test_path} -s {test_path}/{scenario} -s {scenario} "
           f"-O twister-out --no-clean {' '.join(twister_args)}")
    meta = {
        "instance_id": instance_id,
        "project": "zephyr",
        "pipeline": "twister",
        "repo": REPO_URL,
        # base_commit..fix_commit spans exactly this PR, which is the range
        # validate_instance.py diffs to reconstruct the upstream fix.
        "base_commit": base,
        "fix_commit": head,
        "test_commit": head,
        "problem_statement": triage["problem_statement"],
        "platform": platform,
        "test_path": test_path,
        "scenario": scenario,
        "twister_command": cmd,
        "twister_args": twister_args,
        "run_command": "run_tests",
        "python_version": facts["python"],
        "sdk_version": facts["sdk"],
        "sdk_archive": facts["sdk_archive"],
        "apptainer_image": f"{instance_id}.sif",
        "fail_to_pass": triage["fail_to_pass"],
        "pass_to_pass": triage.get("pass_to_pass", []),
        "files_changed_by_fix": [
            f["filename"] for f in rec["files"]
            if not f["filename"].startswith(("tests/", "doc/", ".github/", "samples/"))
        ],
        "pr_url": rec["url"],
        "change_type": triage.get("change_type"),
        "generated": True,
    }
    (inst / "metadata.json").write_text(json.dumps(meta, indent=4) + "\n")
    return inst


# --- OpenThread --------------------------------------------------------------
OPENTHREAD_URL = "https://github.com/openthread/openthread"
OPENTHREAD_TEMPLATES = REPO_ROOT / "templates" / "openthread" / "apptainer"

# THE arguments of an OpenThread instance's `script/cmake-build simulation`
# (instance.json "configure"), for both suites; the verdict's cmake_options are
# added to them. From the smoke control (candidates/openthread/research/smoke.md):
# upstream unit.yml's first simulation step, which cmake-build completes with its
# own option list. Plain cmake with hand-picked flags does not configure.
OPENTHREAD_CONFIGURE = "script/cmake-build simulation"
OPENTHREAD_CMAKE_ARGS = ["-DOT_BUILD_GTEST=ON", "-DOT_BORDER_ROUTING=ON",
                         "-DOT_BORDER_ROUTING_DHCP6_PD=ON", "-DOT_WAKE_INITIATOR=ON",
                         "-DOT_WAKE_LISTENER=ON"]
# The other profiles are upstream's own CI test jobs (openthread_candidates.profile_defs()): the
# record holds their `configure` and `cmake_args`. The toranj one (toranj-unittest, tests/toranj/
# build.sh `all`) is plain `cmake -GNinja`; run_tests adds `-S /testbed -B /testbed/build`.
OPENTHREAD_TORANJ_CONFIGURE = "cmake -GNinja"
# Always last, after the verdict's: cmake-build passes -Werror, and a newer
# compiler's warnings on older code are not the oracle.
OPENTHREAD_LAST_ARG = "-DOT_COMPILE_WARNING_AS_ERROR=OFF"


def merge_cmake_args(base: list[str], extra: list[str]) -> list[str]:
    """base args, minus any whose option the verdict sets again, then the
    verdict's. The union of CI options can hold ON and OFF of one option; the
    verdict must pick one."""
    key = lambda a: a.split("=")[0]
    if len({key(a) for a in extra}) != len(extra):
        sys.exit(f"ERROR: verdict cmake_options set an option twice: {extra}")
    return [a for a in base if key(a) not in {key(e) for e in extra}] + extra


def observed_config(prior: dict, obs: dict, inventory: list | None = None) -> dict:
    """instance.json fields of the verdict-style instance an observation
    finalizes into. run_tests holds one gtest target or the unit tests, so a
    selection of both is refused. Several gtest targets finalize to the one
    that holds every fail_to_pass case (inventory: the record's gtest list)."""
    f2p, p2p = obs["fail_to_pass"], obs["pass_to_pass"]
    if prior["gtest"] and not prior["unit"] and len(prior["gtest"]) == 1:
        return {"suite": "gtest", "target": prior["gtest"][0], "fail_to_pass": f2p,
                "pass_to_pass": p2p}
    if prior["unit"] and not prior["gtest"]:
        return {"suite": "unit", "target": f2p[0], "fail_to_pass": f2p, "pass_to_pass": p2p}
    if prior["gtest"] and not prior["unit"] and inventory:
        owners = lambda c: [g["target"] for g in inventory if c in g["cases"]]
        targets = {t for c in f2p for t in owners(c)}
        if len(targets) == 1 and all(len(owners(c)) == 1 for c in f2p):
            target = targets.pop()
            return {"suite": "gtest", "target": target, "fail_to_pass": f2p,
                    "pass_to_pass": [c for c in p2p if owners(c) == [target]]}
    sys.exit("ERROR: --from-observed handles one gtest target or unit tests only, not "
             f"gtest {prior['gtest']} with unit {prior['unit']}")


def generate_openthread(pr: int, verdict: dict | None, out_root: Path, observe: bool = False,
                        observed: Path | None = None, observe_profile: str | None = None) -> Path:
    """Build `openthread__openthread-<pr>` from an accepted verdict. The
    verdict is checked again here, mechanically, so an unchecked one is refused.

    observe: no verdict; the executables are those whose sources the PR's test
    diff changed (instance.json "observe": true, validate_instance.py observes
    which cases flip). observed: the directory of an observed-valid instance,
    regenerated as a normal instance from observed.json (with the profile and
    arguments of the observation). observe_profile: observe under this profile
    instead of the record's, one of its `extra_observe` entries (only the
    targets listed there, whose test bodies the chosen profile compiles out)."""
    import openthread_candidates
    import triage_prs
    rec_path = openthread_candidates.enriched_dir() / f"{pr}.json"
    if not rec_path.exists():
        sys.exit(f"ERROR: no enriched record at {rec_path}. Run `openthread_candidates.py enrich` first.")
    rec = json.loads(rec_path.read_text())
    if openthread_candidates.stale_record(rec):
        sys.exit(f"ERROR: the enriched record of {pr} is enrich_version {rec.get('enrich_version')}, not "
                 f"{openthread_candidates.ENRICH_VERSION}. Run `python3 scripts/openthread_candidates.py enrich --local`.")
    extra_meta = {}
    if observed:
        obs = json.loads((observed / "observed.json").read_text())
        if not obs.get("valid"):
            sys.exit(f"ERROR: {observed} is not observed-valid: {obs.get('reason')}")
        prior = json.loads((observed / "instance.json").read_text())
        old = json.loads((observed / "metadata.json").read_text())
        cfg = observed_config(prior, obs, rec["test_inventory"]["gtest"])
        configure, cmake_args = prior["configure"], prior["cmake_args"]
        profile = prior.get("profile", "simulation")
        statement, change_type = old["problem_statement"], old["change_type"]
        extra_meta = {"oracle": "observed", "oracle_note": "observed by validate_instance.py "
                      "(fail before, pass after the fix); the unit oracle is per executable, "
                      "so check attribution of the cases to the PR's change"}
    elif observe:
        profile = observe_profile or rec.get("profile", "simulation")
        extra = {e["profile"]: e["targets"] for e in rec["extra_observe"]}
        if profile != rec.get("profile", "simulation") and profile not in extra:
            sys.exit(f"ERROR: {profile!r} is neither the chosen profile ({rec.get('profile', 'simulation')}) nor in "
                     f"extra_observe of {pr} ({list(extra) or 'empty'})")
        sel = openthread_candidates.select_tests(rec)
        if profile in extra:
            sel = {k: [t for t in rec["test_inventory"][k] if t["target"] in extra[profile]] for k in ("unit", "gtest")}
        gated = openthread_candidates.gated_targets(rec)
        if not sel["unit"] and not sel["gtest"] and gated:
            opts = sorted({o for _, m in gated for o in m})
            sys.exit(f"ERROR: no runnable target for {pr} in our profile: its tests are only "
                     f"built with {', '.join(opts)} (profile-gated)")
        if not sel["unit"] and not sel["gtest"]:
            sys.exit(f"ERROR: no runnable target for {pr}: its changed test files "
                     f"{rec['test_files']} are compiled into no unit or gtest executable")
        configure, cmake_args = rec["profiles"][profile]["configure"], rec["profiles"][profile]["cmake_args"]
        cfg = {"observe": True, "unit": [t["ctest_name"] for t in sel["unit"]],
               "gtest": [t["target"] for t in sel["gtest"]],
               "targets": [t["target"] for t in sel["unit"] + sel["gtest"]],
               "fail_to_pass": [], "pass_to_pass": []}
        statement = change_type = None
    else:
        if verdict.get("verdict") != "accept":
            sys.exit(f"ERROR: verdict for {pr} is {verdict.get('verdict')!r}, not accept: "
                     f"{verdict.get('reason', 'no reason given')}")
        triage_prs.use_project("openthread")
        issues = triage_prs.check_verdict_openthread(verdict, rec)
        if issues:
            sys.exit(f"ERROR: verdict for {pr} fails the check:\n  " + "\n  ".join(issues))
        profile = verdict.get("profile") or rec.get("profile", "simulation")
        configure, cmake_args = rec["profiles"][profile]["configure"], rec["profiles"][profile]["cmake_args"]
        if profile == "simulation":
            cmake_args = merge_cmake_args(OPENTHREAD_CMAKE_ARGS, verdict.get("cmake_options", [])) + [OPENTHREAD_LAST_ARG]
        cfg = {"suite": verdict["suite"], "target": verdict["target"],
               "fail_to_pass": verdict["fail_to_pass"],
               "pass_to_pass": verdict.get("pass_to_pass", [])}
        if verdict.get("oracle") == "compile":
            # the build failure before the fix is the oracle (validate_instance.py)
            cfg |= {"oracle": "compile", "compile_symbols": verdict["compile_symbols"]}
            extra_meta = {"oracle": "compile", "compile_symbols": verdict["compile_symbols"]}
        statement, change_type = verdict["problem_statement"], verdict["change_type"]

    base, fix = rec["base_commit"], rec["fix_commit"]
    instance_id = f"openthread__openthread-{pr}"
    inst = out_root / instance_id
    inst.mkdir(parents=True, exist_ok=True)

    clone = layout.openthread_clone()
    patch = build_test_patch(base, fix, clone)
    if not patch.strip():
        sys.exit(f"ERROR: no tests/ diff for {pr}; instance would have no tests")
    (inst / "test_patch.diff").write_text(patch)
    check_applies(patch, base, inst, clone)

    f2p, p2p = cfg["fail_to_pass"], cfg["pass_to_pass"]
    (inst / "instance.json").write_text(json.dumps(
        {**{k: v for k, v in cfg.items() if k not in ("fail_to_pass", "pass_to_pass")},
         **({"profile": profile} if profile != "simulation" else {}),
         "configure": configure,
         "cmake_args": cmake_args,
         "fail_to_pass": f2p, "pass_to_pass": p2p}, indent=2) + "\n")
    shutil.copy(OPENTHREAD_TEMPLATES / "run_tests.py", inst / "run_tests.py")

    definition = (OPENTHREAD_TEMPLATES / "instance.def.tmpl").read_text()
    for key, val in {"@@INSTANCE_ID@@": instance_id, "@@REPO@@": OPENTHREAD_URL,
                     "@@BASE_COMMIT@@": base}.items():
        definition = definition.replace(key, val)
    (inst / "instance.def").write_text(definition)

    meta = {
        "instance_id": instance_id,
        "project": "openthread",
        "pipeline": "cmake",
        "repo": OPENTHREAD_URL,
        "base_commit": base,
        "fix_commit": fix,
        "test_commit": fix,
        "problem_statement": statement,
        "fail_to_pass": f2p,
        "pass_to_pass": p2p,
        "files_changed_by_fix": rec["source_files"],
        "pr_url": rec["url"],
        "change_type": change_type,
        "run_command": "run_tests",
        "apptainer_image": f"{instance_id}.sif",
        "generated": True,
        **extra_meta,
    }
    (inst / "metadata.json").write_text(json.dumps(meta, indent=4) + "\n")
    return inst


def main() -> None:
    p = argparse.ArgumentParser(description="Generate an instance from a triage verdict.")
    p.add_argument("--project", choices=("zephyr", "openthread", "contiki-ng"), default="zephyr")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--triage", help="path to the triage verdict JSON")
    p.add_argument("--observe", action="store_true",
                   help="OpenThread: no verdict; run the test executables the PR changed and "
                        "observe which cases flip (validate_instance.py writes observed.json)")
    p.add_argument("--profile", help="with --observe: observe under this profile, one of the record's "
                                     "extra_observe entries (default: the record's chosen profile)")
    p.add_argument("--from-observed", type=Path, metavar="DIR",
                   help="OpenThread: regenerate an observed-valid instance directory as a "
                        "normal instance with the observed test lists")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help="output root (default: generated/, never docker/instances/)")
    p.add_argument("--force", action="store_true",
                   help="generate even if the board/scenario check fails (for testing)")
    args = p.parse_args()

    if args.project == "contiki-ng":
        from contiki_ng_pipeline import generate as generate_contiki
        verdict = json.loads(Path(args.triage).read_text()) if args.triage else None
        if not (args.observe or args.from_observed or verdict):
            p.error("--triage, --observe or --from-observed required")
        from contiki_ng_pipeline import NotObservable
        try:
            inst = generate_contiki(args.pr, verdict, Path(args.out), args.observe, args.from_observed, args.profile)
        except NotObservable as e:
            print(f"  not observable: {e}")
            sys.exit(3)
        print(f"  wrote {layout.show(inst)}")
        return

    if args.observe or args.from_observed:
        if args.project != "openthread" or args.triage:
            p.error("--observe and --from-observed are OpenThread only and take no --triage")
        if args.profile and not args.observe:
            p.error("--profile goes with --observe")
        inst = generate_openthread(args.pr, None, Path(args.out), args.observe, args.from_observed, args.profile)
    else:
        if not args.triage:
            p.error("--triage is required")
        triage = json.loads(Path(args.triage).read_text())
        if triage.get("verdict") == "reject":
            sys.exit(f"triage rejected PR {args.pr}: {triage.get('reason','no reason given')}")
        inst = (generate_openthread(args.pr, triage, Path(args.out)) if args.project == "openthread"
                else generate(args.pr, triage, Path(args.out), args.force))
    print(f"  wrote {layout.show(inst)}")
    for f in sorted(inst.iterdir()):
        print(f"    {f.name:<20} {f.stat().st_size:>7} B")


if __name__ == "__main__":
    main()
