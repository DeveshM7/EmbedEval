#!/usr/bin/env python3
"""
Build a complete Zephyr or Apache Mynewt instance from a triage verdict.

    python scripts/generate_instance.py --pr 65697 --triage triage/65697.json
    python scripts/generate_instance.py --repo mynewt --pr 3299 \
        --triage triage/mynewt-3299.json \
        --out /tmp/check

Writes the four files an instance needs -- Dockerfile, run_tests.sh,
test_patch.diff, metadata.json -- so that none of them is hand-written.

Deterministic on purpose. Given a triage verdict and an enriched PR record
there is nothing here to reason about, so no model is involved: the model
supplies the platform, problem statement, and expected testcase inventory;
everything else is derived. An agent only enters later, if the generated
instance fails to build or validate.

Output goes to generated/ by default, never to docker/instances/, so testing
this cannot disturb an existing instance. Pass --out to put it elsewhere.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "generated"

PROJECTS = {
    "zephyr": {
        "repo_url": "https://github.com/zephyrproject-rtos/zephyr",
        "enriched": REPO_ROOT / "candidates" / "enriched",
        "templates": REPO_ROOT / "templates" / "zephyr",
        "clone": REPO_ROOT / "candidates" / "zephyr.git",
    },
    "mynewt": {
        "repo_url": "https://github.com/apache/mynewt-core",
        "enriched": REPO_ROOT / "candidates" / "mynewt" / "enriched",
        "templates": REPO_ROOT / "templates" / "mynewt",
        "clone": REPO_ROOT / "candidates" / "mynewt.git",
    },
}

# Used only by the Dockerfile's fallback path. Where the base commit carries a
# SDK_VERSION file at the repo root -- from roughly 2024-06 -- `west sdk
# install` reads it and installs exactly what the project specified, and this
# constant is never consulted.
#
# 0.16.8 rather than something newer because it is the version this repo has
# evidence for on precisely this path: four hand-built instances install it by
# manual download, including 33690, whose base commit is from 2021. 0.17.x is
# only ever reached here through `west sdk install`, so its manual-download
# path is untested.
#
# Deliberately not derived from the base commit's era. Zephyr's CMake matches a
# minimum compatible SDK rather than an exact one, so a modern SDK builds an old
# tree: 33690's own docs ask for 0.12.3 and it is built with 0.16.8. Deriving a
# version per era would mean scraping a version out of prose in a docs page
# whose path moved, to obtain a number these instances show is not required,
# and then handling three packaging formats -- .run for 0.12/0.13, .tar.gz for
# 0.14/0.15, .tar.xz since 0.16.
SDK_FALLBACK = "0.16.8"


def run(cmd: list[str], timeout: int = 300, check: bool = True):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        sys.exit(f"ERROR: {' '.join(cmd[:4])} ... failed:\n{r.stderr.strip()[:400]}")
    return r


def ensure_clone(project: str = "zephyr") -> Path:
    """A blobless upstream mirror, reused across instances.

    Blobless means all history metadata without file contents until asked for,
    so any commit can be reached without a depth limit at a fraction of a full
    clone. The same trick validate_instance.py uses.
    """
    cfg = PROJECTS[project]
    clone = cfg["clone"]
    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        print(f"  cloning {cfg['repo_url']} (blobless, one-time) ...")
        run(["git", "clone", "--filter=blob:none", "--no-checkout", "--bare",
             f"{cfg['repo_url']}.git", str(clone)], timeout=1800)
    return clone


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

    The range is safe because both ends are on the PR's own branch: base is its
    merge base, head its tip, and the branch contains nothing that landed on
    main after it forked. Contamination came from ranges ending at a *merge
    commit* -- base..merge_commit traverses everyone else's work, which is what
    put seven foreign files into the nuttx-11889 patch.
    """
    clone = ensure_clone(project)
    run(["git", "-C", str(clone), "fetch", "-q", "origin", base, head], timeout=900)
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
    seven minutes into a Docker build, and only for instances someone happens
    to build. A dry run against the base commit costs about a second and covers
    every instance, every time -- so correctness stops depending on having
    anticipated git's format rules, and is decided by git.
    """
    clone = ensure_clone(project)
    patch_file = (inst / "test_patch.diff").resolve()
    with tempfile.TemporaryDirectory(prefix="embedeval-applycheck-") as tmp:
        # Absolute paths throughout: `git -C <clone>` resolves relative paths
        # against the clone, not the caller's directory.
        work = Path(tmp) / "tree"
        run(["git", "-C", str(clone), "worktree", "add", "-q", "--detach",
             str(work), base], timeout=300)
        try:
            r = run(["git", "-C", str(work), "apply", "--check", str(patch_file)],
                    check=False)
            if r.returncode != 0:
                sys.exit("ERROR: generated test_patch.diff does not apply at "
                         f"{base[:12]}:\n{r.stderr.strip()[:400]}")
        finally:
            run(["git", "-C", str(clone), "worktree", "remove", "--force", str(work)],
                timeout=120, check=False)


def pick_runner(platform: str, project: str = "zephyr") -> Path:
    """QEMU needs PID cleanup and process-group killing because it never exits
    on its own; native_sim runs as a host process that does. Genuinely two
    different scripts, not cosmetic drift."""
    templates = PROJECTS[project]["templates"]
    if project == "mynewt":
        return templates / "run_tests.sh"
    return templates / ("run_tests.qemu.sh" if platform.startswith("qemu")
                        else "run_tests.native.sh")


def generate(
    pr: int,
    triage: dict,
    out_root: Path,
    tag_suffix: str = "",
    project: str = "zephyr",
    enriched_root: Path | None = None,
) -> Path:
    if project not in PROJECTS:
        sys.exit(f"ERROR: unknown project {project!r}")
    cfg = PROJECTS[project]
    templates = cfg["templates"]
    rec_path = (enriched_root or cfg["enriched"]) / f"{pr}.json"
    if not rec_path.exists():
        sys.exit(f"ERROR: no enriched record at {rec_path}. Run `enrich` first.")
    rec = json.loads(rec_path.read_text())
    linked = rec.get("linked_issues") or []
    if project == "mynewt" and not linked:
        sys.exit(f"ERROR: enriched Mynewt record for {pr} has no linked issue")

    for field in ("platform", "problem_statement", "fail_to_pass"):
        if not triage.get(field):
            sys.exit(f"ERROR: triage verdict for {pr} has no {field!r}")

    platform = triage["platform"]
    if project == "mynewt" and platform != "native":
        sys.exit("ERROR: Mynewt platform must be 'native'; QEMU is not supported")
    if project == "mynewt" and "baseline_tests" not in triage:
        sys.exit(f"ERROR: triage verdict for {pr} has no 'baseline_tests'")

    # Kconfig overrides for the scenario this test belongs to. west build reads
    # prj.conf only, so without these a test that depends on a scenario's
    # extra_configs runs in the wrong variant and passes on broken code.
    # -t installs one toolchain, -T installs none, and omitting both installs
    # every architecture. qemu_* cross-compiles so it needs x86_64-zephyr-elf;
    # native_sim uses the host gcc and needs nothing, so -T saves the download.
    sdk_flag = "-T" if platform.startswith("native") else "-t x86_64-zephyr-elf"

    extra = triage.get("extra_configs") or []
    extra_args = (" -- " + " ".join(f"-D{c}" for c in extra)) if extra else ""
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
    (inst / "test_patch.diff").write_text(patch)
    check_applies(patch, base_commit, inst, project)

    dockerfile = (templates / "Dockerfile.tmpl").read_text()
    replacements = {
        "@@BASE_COMMIT@@": base_commit,
        "@@REPO@@": cfg["repo_url"],
    }
    if project == "zephyr":
        replacements.update({
            "@@SDK_VERSION@@": SDK_FALLBACK,
            "@@PLATFORM@@": platform,
            "@@TEST_PATH@@": test_path,
            "@@EXTRA_BUILD_ARGS@@": extra_args,
            "@@SDK_TOOLCHAIN_FLAG@@": sdk_flag,
        })
    for key, val in replacements.items():
        dockerfile = dockerfile.replace(key, val)
    (inst / "Dockerfile").write_text(dockerfile)

    (inst / "run_tests.sh").write_text(pick_runner(platform, project).read_text())

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
        "build_command": (
            f"west build -b {platform} {test_path}{extra_args}"
            if project == "zephyr" else "true"
        ),
        "run_command": "west build -t run" if project == "zephyr" else "run_tests",
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
    if project == "zephyr":
        # `west sdk install` overrides this from the repo's own SDK_VERSION
        # where present. Record the fallback so variation is explicit.
        meta.update({
            "sdk_fallback_version": SDK_FALLBACK,
            "extra_configs": extra,
        })
    else:
        meta.update({
            "issue_url": f"https://github.com/apache/mynewt-core/issues/{linked[0]['number']}",
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
    return inst


def main() -> None:
    p = argparse.ArgumentParser(
        description="Generate a Zephyr or Mynewt instance from a triage verdict."
    )
    p.add_argument("--repo", choices=PROJECTS, default="zephyr")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--triage", required=True, help="path to the triage verdict JSON")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help="output root (default: generated/, never docker/instances/)")
    p.add_argument("--tag-suffix", default="",
                   help="append to the image tag, e.g. -gen, so building a generated "
                        "instance cannot overwrite the image of an existing one")
    args = p.parse_args()

    triage = json.loads(Path(args.triage).read_text())
    if triage.get("verdict") == "reject":
        sys.exit(f"triage rejected PR {args.pr}: {triage.get('reason','no reason given')}")

    inst = generate(args.pr, triage, Path(args.out), args.tag_suffix, args.repo)
    print(f"  wrote {inst.relative_to(REPO_ROOT) if REPO_ROOT in inst.parents else inst}")
    for f in sorted(inst.iterdir()):
        print(f"    {f.name:<20} {f.stat().st_size:>7} B")


if __name__ == "__main__":
    main()
