#!/usr/bin/env python3
"""
Build a complete Zephyr (Apptainer recipe) or Apache Mynewt (Docker) instance
from a triage verdict.

    python scripts/generate_instance.py --pr 65697 --triage triage/ideal_verdicts/65697.json
    python scripts/generate_instance.py --pr 65697 --triage verdict.json --out /tmp/check
    python scripts/generate_instance.py --repo mynewt --pr 3299 \
        --triage triage/mynewt-3299.json --out /tmp/check

Zephyr: writes an Apptainer recipe and everything it copies in, so none of it is
hand-written. Mynewt: writes Dockerfile, run_tests.sh, test_patch.diff and
metadata.json.

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

PROJECTS = {
    "mynewt": {
        "repo_url": "https://github.com/apache/mynewt-core",
        "enriched": REPO_ROOT / "candidates" / "mynewt" / "enriched",
        "templates": REPO_ROOT / "templates" / "mynewt",
        "clone": REPO_ROOT / "candidates" / "mynewt.git",
    },
}


def run(cmd: list[str], timeout: int = 300, check: bool = True):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        sys.exit(f"ERROR: {' '.join(cmd[:4])} ... failed:\n{r.stderr.strip()[:400]}")
    return r


def ensure_clone(project: str = "zephyr") -> Path:
    """A bare upstream mirror, reused across instances (Zephyr: full, shared
    with triage_prs.py; other projects: blobless).

    Full rather than blobless. Blobless (all history, file contents fetched on
    first use) is enough for building a test patch, but triage searches the
    tree with git grep, and in a blobless clone that downloads every file under
    the searched path one at a time -- so searches had to be capped at a few
    hundred files, and the model spent its tool rounds guessing narrower
    directories. A full clone is a few GB, once, and makes any search instant.
    """
    if project != "zephyr":
        cfg = PROJECTS[project]
        clone = cfg["clone"]
        if not clone.exists():
            clone.parent.mkdir(parents=True, exist_ok=True)
            print(f"  cloning {cfg['repo_url']} (blobless, one-time) ...")
            run(["git", "clone", "--filter=blob:none", "--no-checkout", "--bare",
                 f"{cfg['repo_url']}.git", str(clone)], timeout=1800)
        return clone
    if not CLONE_CACHE.exists():
        CLONE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        print(f"  cloning {REPO_URL} (full, one-time; a few GB) ...")
        run(["git", "clone", "--bare", f"{REPO_URL}.git", str(CLONE_CACHE)], timeout=3600)
    return CLONE_CACHE


def is_test_path(path: str, project: str) -> bool:
    if project == "zephyr":
        return path.startswith("tests/")
    return any(
        part == "selftest" or part.startswith("selftest-")
        for part in path.split("/")
    )


def is_source_path(path: str, project: str) -> bool:
    if is_test_path(path, project):
        return False
    if project == "zephyr":
        return not path.startswith(("doc/", ".github/", "samples/"))
    return not path.startswith(("docs/", ".github/", "test/"))


def changed_test_paths(record: dict, project: str) -> list[str]:
    """Every test-side path needed to reproduce additions, edits, and renames."""
    paths = []
    for changed in record["files"]:
        previous = changed.get("previous_filename")
        current = changed["filename"]
        if previous and is_test_path(previous, project):
            paths.append(previous)
        if is_test_path(current, project):
            paths.append(current)
    return list(dict.fromkeys(paths))


def build_test_patch(
    base: str, head: str, project: str = "zephyr", test_files: list[str] | None = None
) -> str:
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
    clone = ensure_clone(project)
    for sha in (base, head):
        if run(["git", "-C", str(clone), "cat-file", "-e", f"{sha}^{{commit}}"],
               check=False).returncode:
            run(["git", "-C", str(clone), "fetch", "-q", "origin", sha], timeout=900)
    paths = ["tests/"] if project == "zephyr" else (test_files or [])
    if not paths:
        return ""
    r = run(["git", "-C", str(clone), "diff", f"{base}..{head}", "--", *paths])
    return r.stdout


def check_applies(
    patch: str, base: str, inst: Path, project: str = "zephyr"
) -> None:
    """Refuse to emit an instance whose patch git will not accept.

    Without this the first sign of a malformed patch is `git apply` failing
    minutes into an image build, and only for instances someone happens to
    build. A dry run against the base commit costs about a second and covers
    every instance, every time.
    """
    clone = ensure_clone(project)
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


def generate_zephyr(pr: int, triage: dict, out_root: Path, force: bool = False) -> Path:
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


def generate_mynewt(
    pr: int,
    triage: dict,
    out_root: Path,
    tag_suffix: str = "",
    enriched_root: Path | None = None,
    runtime: str = "docker",
) -> Path:
    project = "mynewt"
    cfg = PROJECTS[project]
    templates = cfg["templates"]
    rec_path = (enriched_root or cfg["enriched"]) / f"{pr}.json"
    if not rec_path.exists():
        sys.exit(f"ERROR: no enriched record at {rec_path}. Run `enrich` first.")
    rec = json.loads(rec_path.read_text())
    linked = rec.get("linked_issues") or []
    if not linked:
        sys.exit(f"ERROR: enriched Mynewt record for {pr} has no linked issue")

    for field in ("platform", "problem_statement", "fail_to_pass"):
        if not triage.get(field):
            sys.exit(f"ERROR: triage verdict for {pr} has no {field!r}")

    platform = triage["platform"]
    if platform != "native":
        sys.exit("ERROR: Mynewt platform must be 'native'; QEMU is not supported")
    if "baseline_tests" not in triage:
        sys.exit(f"ERROR: triage verdict for {pr} has no 'baseline_tests'")

    suites = rec.get("test_suites") or {}
    if not suites:
        sys.exit(f"ERROR: enriched record for {pr} has no test_suites")
    # One PR can touch several suites; the one holding the changed tests is
    # the one with the most test files in the diff.
    test_files = changed_test_paths(rec, project)
    test_path = max(suites, key=lambda r: sum(t.startswith(r + "/") for t in test_files))

    base_commit = rec.get("base_commit")
    head_commit = rec.get("head_commit")
    if not base_commit or not head_commit:
        sys.exit(f"ERROR: enriched record for {pr} predates base_commit/head_commit; re-enrich it")
    test_commit = triage.get("test_commit", head_commit)
    support_paths = triage.get("test_support_paths", [])
    if support_paths and "test_commit" not in triage:
        sys.exit("ERROR: test_support_paths requires test_commit so production fix "
                 "changes cannot leak into the test patch")
    if "test_commit" in triage:
        commit_shas = {commit["sha"] for commit in rec.get("commits", [])}
        if test_commit not in commit_shas:
            sys.exit(f"ERROR: test_commit {test_commit} is not a commit in PR {pr}")
    changed_paths = {changed["filename"] for changed in rec["files"]}
    unknown_support = [path for path in support_paths if path not in changed_paths]
    if unknown_support:
        sys.exit("ERROR: test_support_paths not changed by the PR: "
                 + ", ".join(unknown_support))
    test_files = list(dict.fromkeys([*test_files, *support_paths]))
    instance_id = f"{project}__{project}-{pr}"
    inst = out_root / instance_id
    inst.mkdir(parents=True, exist_ok=True)

    patch = build_test_patch(base_commit, test_commit, project, test_files)
    if not patch.strip():
        sys.exit(f"ERROR: no test-file diff for {pr}; instance would have no tests")
    failure_mode = triage.get("failure_mode", "runtime")
    if failure_mode not in ("runtime", "compile"):
        sys.exit("ERROR: Mynewt failure_mode must be 'runtime' or 'compile'")
    compile_test_source = triage.get("compile_test_source")
    missing_api = triage.get("missing_api")
    if failure_mode == "compile":
        if not isinstance(compile_test_source, str) or not compile_test_source.strip():
            sys.exit("ERROR: compile mode requires compile_test_source")
        if not isinstance(missing_api, str) or not missing_api.strip():
            sys.exit("ERROR: compile mode requires missing_api")
        clone = ensure_clone(project)
        changed = run([
            "git", "-C", str(clone), "diff", "--name-only",
            f"{base_commit}..{test_commit}", "--", *test_files,
        ]).stdout.splitlines()
        if (Path(compile_test_source).suffix not in (".c", ".cc", ".cpp", ".cxx", ".C")
                or not is_test_path(compile_test_source, project)
                or compile_test_source not in changed):
            sys.exit("ERROR: compile_test_source must be a changed test-side C/C++ source in the test patch")
        source = run([
            "git", "-C", str(clone), "show", f"{test_commit}:{compile_test_source}",
        ]).stdout
        if missing_api not in source:
            sys.exit("ERROR: missing_api is absent from compile_test_source")
    (inst / "test_patch.diff").write_text(patch)
    check_applies(patch, base_commit, inst, project)

    if runtime == "docker":
        dockerfile = (templates / "Dockerfile.tmpl").read_text()
        for key, val in {"@@BASE_COMMIT@@": base_commit, "@@REPO@@": cfg["repo_url"]}.items():
            dockerfile = dockerfile.replace(key, val)
        (inst / "Dockerfile").write_text(dockerfile)
        (inst / "run_tests.sh").write_text((templates / "run_tests.sh").read_text())

    meta = {
        "instance_id": instance_id,
        "project": project,
        "repo": cfg["repo_url"],
        # base_commit..fix_commit spans exactly this PR. When test-only support
        # precedes the production fix, gold_base_commit below narrows the range
        # validate_instance.py uses to reconstruct that fix.
        "base_commit": base_commit,
        "fix_commit": head_commit,
        "test_commit": test_commit,
        "problem_statement": triage["problem_statement"],
        "platform": platform,
        "test_path": test_path,
        "build_command": "true",
        "run_command": "run_tests",
        "docker_image": f"embedeval:{project}-{pr}{tag_suffix}",
        "fail_to_pass": triage["fail_to_pass"],
        "pass_to_pass": triage.get("pass_to_pass", []),
        "files_changed_by_fix": [
            f["filename"] for f in rec["files"]
            if is_source_path(f["filename"], project)
        ],
        "pr_url": rec["url"],
        "change_type": triage.get("change_type"),
        "generated": True,
    }
    meta.update({
        "issue_url": f"https://github.com/apache/mynewt-core/issues/{linked[0]['number']}",
        "failure_mode": failure_mode,
        "compile_test_source": compile_test_source,
        "missing_api": missing_api,
        "docker_platform": "linux/amd64",
        "structured_test_results": "/tmp/mynewt-result.json",
        "baseline_tests": triage["baseline_tests"],
        "protected_paths": [suite.rstrip("/") + "/" for suite in suites],
        "compatibility_cflags": triage.get("compatibility_cflags", []),
    })
    if triage.get("gold_patch_three_way"):
        meta["gold_patch_three_way"] = True
    if test_commit != head_commit:
        meta["gold_base_commit"] = test_commit
    (inst / "metadata.json").write_text(json.dumps(meta, indent=4) + "\n")
    if runtime == "apptainer":
        # An Apptainer recipe instead of the Dockerfile (scripts/render_mynewt_instance.py).
        import render_mynewt_instance
        with tempfile.TemporaryDirectory(prefix="embedeval-render-") as tmp:
            rendered = render_mynewt_instance.render(meta, inst / "test_patch.diff", Path(tmp))
            for f in rendered.iterdir():
                shutil.copy2(f, inst / f.name)
    return inst



def generate(pr: int, triage: dict, out_root: Path, tag_suffix: str = "",
             project: str = "zephyr", force: bool = False,
             enriched_root: Path | None = None, runtime: str = "docker") -> Path:
    if project == "mynewt":
        return generate_mynewt(pr, triage, out_root, tag_suffix, enriched_root, runtime)
    if project == "zephyr":
        return generate_zephyr(pr, triage, out_root, force)
    sys.exit(f"ERROR: unknown project {project!r}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Generate a Zephyr or Mynewt instance from a triage verdict."
    )
    p.add_argument("--repo", choices=("zephyr", "mynewt"), default="zephyr")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--triage", required=True, help="path to the triage verdict JSON")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help="output root (default: generated/, never docker/instances/)")
    p.add_argument("--force", action="store_true",
                   help="generate even if the board/scenario check fails (for testing)")
    p.add_argument("--tag-suffix", default="",
                   help="Mynewt only: append to the image tag, e.g. -gen, so building a "
                        "generated instance cannot overwrite an existing image")
    p.add_argument("--runtime", choices=("docker", "apptainer"), default="docker",
                   help="Mynewt only: write an Apptainer recipe (instance.def, for hosts "
                        "without Docker such as Scholar) instead of a Dockerfile")
    args = p.parse_args()

    triage = json.loads(Path(args.triage).read_text())
    if triage.get("verdict") == "reject":
        sys.exit(f"triage rejected PR {args.pr}: {triage.get('reason','no reason given')}")

    inst = generate(args.pr, triage, Path(args.out), args.tag_suffix, args.repo, args.force,
                    runtime=args.runtime)
    print(f"  wrote {layout.show(inst)}")
    for f in sorted(inst.iterdir()):
        print(f"    {f.name:<20} {f.stat().st_size:>7} B")


if __name__ == "__main__":
    main()
