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


def generate_openthread(pr: int, verdict: dict, out_root: Path) -> Path:
    """Build `openthread__openthread-<pr>` from an accepted verdict. The
    verdict is checked again here, mechanically, so an unchecked one is refused."""
    import openthread_candidates
    import triage_prs
    rec_path = openthread_candidates.enriched_dir() / f"{pr}.json"
    if not rec_path.exists():
        sys.exit(f"ERROR: no enriched record at {rec_path}. Run `openthread_candidates.py enrich` first.")
    rec = json.loads(rec_path.read_text())
    if verdict.get("verdict") != "accept":
        sys.exit(f"ERROR: verdict for {pr} is {verdict.get('verdict')!r}, not accept: "
                 f"{verdict.get('reason', 'no reason given')}")
    triage_prs.use_project("openthread")
    issues = triage_prs.check_verdict_openthread(verdict, rec)
    if issues:
        sys.exit(f"ERROR: verdict for {pr} fails the check:\n  " + "\n  ".join(issues))

    base, fix = rec["base_commit"], rec["fix_commit"]
    cmake_args = merge_cmake_args(OPENTHREAD_CMAKE_ARGS, verdict.get("cmake_options", [])
                                  ) + [OPENTHREAD_LAST_ARG]
    instance_id = f"openthread__openthread-{pr}"
    inst = out_root / instance_id
    inst.mkdir(parents=True, exist_ok=True)

    clone = layout.openthread_clone()
    patch = build_test_patch(base, fix, clone)
    if not patch.strip():
        sys.exit(f"ERROR: no tests/ diff for {pr}; instance would have no tests")
    (inst / "test_patch.diff").write_text(patch)
    check_applies(patch, base, inst, clone)

    f2p, p2p = verdict["fail_to_pass"], verdict.get("pass_to_pass", [])
    (inst / "instance.json").write_text(json.dumps(
        {"suite": verdict["suite"], "target": verdict["target"],
         "configure": OPENTHREAD_CONFIGURE, "cmake_args": cmake_args,
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
        "problem_statement": verdict["problem_statement"],
        "fail_to_pass": f2p,
        "pass_to_pass": p2p,
        "files_changed_by_fix": rec["source_files"],
        "pr_url": rec["url"],
        "change_type": verdict["change_type"],
        "run_command": "run_tests",
        "apptainer_image": f"{instance_id}.sif",
        "generated": True,
    }
    (inst / "metadata.json").write_text(json.dumps(meta, indent=4) + "\n")
    return inst


def main() -> None:
    p = argparse.ArgumentParser(description="Generate an instance from a triage verdict.")
    p.add_argument("--project", choices=("zephyr", "openthread"), default="zephyr")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--triage", required=True, help="path to the triage verdict JSON")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help="output root (default: generated/, never docker/instances/)")
    p.add_argument("--force", action="store_true",
                   help="generate even if the board/scenario check fails (for testing)")
    args = p.parse_args()

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
