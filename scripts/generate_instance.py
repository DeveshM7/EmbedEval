#!/usr/bin/env python3
"""
Build a complete Zephyr instance from a triage verdict.

    python scripts/generate_instance.py --pr 65697 --triage triage/65697.json
    python scripts/generate_instance.py --pr 65697 --triage triage/65697.json \
        --out /tmp/check

Writes the four files an instance needs -- Dockerfile, run_tests.sh,
test_patch.diff, metadata.json -- so that none of them is hand-written.

Deterministic on purpose. Given a triage verdict and an enriched PR record
there is nothing here to reason about, so no model is involved: the model
supplies four judgements (platform, problem_statement, fail_to_pass,
pass_to_pass) and everything else is derived. An agent only enters later, if
the generated instance fails to build or validate.

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
ENRICHED = REPO_ROOT / "candidates" / "enriched"
TEMPLATES = REPO_ROOT / "templates" / "zephyr"
DEFAULT_OUT = REPO_ROOT / "generated"

REPO_URL = "https://github.com/zephyrproject-rtos/zephyr"

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


CLONE_CACHE = REPO_ROOT / "candidates" / "zephyr.git"


def run(cmd: list[str], timeout: int = 300, check: bool = True):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        sys.exit(f"ERROR: {' '.join(cmd[:4])} ... failed:\n{r.stderr.strip()[:400]}")
    return r


def ensure_clone() -> Path:
    """A blobless mirror of Zephyr, reused across instances.

    Blobless means all history metadata without file contents until asked for,
    so any commit can be reached without a depth limit at a fraction of a full
    clone. The same trick validate_instance.py uses.
    """
    if not CLONE_CACHE.exists():
        CLONE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        print(f"  cloning {REPO_URL} (blobless, one-time) ...")
        run(["git", "clone", "--filter=blob:none", "--no-checkout", "--bare",
             f"{REPO_URL}.git", str(CLONE_CACHE)], timeout=1800)
    return CLONE_CACHE


def build_test_patch(base: str, head: str) -> str:
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
    clone = ensure_clone()
    run(["git", "-C", str(clone), "fetch", "-q", "origin", base, head], timeout=900)
    r = run(["git", "-C", str(clone), "diff", f"{base}..{head}", "--", "tests/"])
    return r.stdout


def check_applies(patch: str, base: str, inst: Path) -> None:
    """Refuse to emit an instance whose patch git will not accept.

    Without this the first sign of a malformed patch is `git apply` failing
    seven minutes into a Docker build, and only for instances someone happens
    to build. A dry run against the base commit costs about a second and covers
    every instance, every time -- so correctness stops depending on having
    anticipated git's format rules, and is decided by git.
    """
    clone = ensure_clone()
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


def pick_runner(platform: str) -> Path:
    """QEMU needs PID cleanup and process-group killing because it never exits
    on its own; native_sim runs as a host process that does. Genuinely two
    different scripts, not cosmetic drift."""
    return TEMPLATES / ("run_tests.qemu.sh" if platform.startswith("qemu")
                        else "run_tests.native.sh")


def generate(pr: int, triage: dict, out_root: Path, tag_suffix: str = "") -> Path:
    rec_path = ENRICHED / f"{pr}.json"
    if not rec_path.exists():
        sys.exit(f"ERROR: no enriched record at {rec_path}. Run `enrich` first.")
    rec = json.loads(rec_path.read_text())

    for field in ("platform", "problem_statement", "fail_to_pass"):
        if not triage.get(field):
            sys.exit(f"ERROR: triage verdict for {pr} has no {field!r}")

    platform = triage["platform"]

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
    test_files = [f["filename"] for f in rec["files"] if f["filename"].startswith("tests/")]
    test_path = max(suites, key=lambda r: sum(t.startswith(r + "/") for t in test_files))

    base_commit = rec.get("base_commit")
    head_commit = rec.get("head_commit")
    if not base_commit or not head_commit:
        sys.exit(f"ERROR: enriched record for {pr} predates base_commit/head_commit; re-enrich it")
    instance_id = f"zephyr__zephyr-{pr}"
    inst = out_root / instance_id
    inst.mkdir(parents=True, exist_ok=True)

    patch = build_test_patch(base_commit, head_commit)
    if not patch.strip():
        sys.exit(f"ERROR: no test-file diff for {pr}; instance would have no tests")
    (inst / "test_patch.diff").write_text(patch)
    check_applies(patch, base_commit, inst)

    dockerfile = (TEMPLATES / "Dockerfile.tmpl").read_text()
    for key, val in {
        "@@BASE_COMMIT@@": base_commit,
        "@@REPO@@": REPO_URL,
        "@@SDK_VERSION@@": SDK_FALLBACK,
        "@@PLATFORM@@": platform,
        "@@TEST_PATH@@": test_path,
        "@@EXTRA_BUILD_ARGS@@": extra_args,
        "@@SDK_TOOLCHAIN_FLAG@@": sdk_flag,
    }.items():
        dockerfile = dockerfile.replace(key, val)
    (inst / "Dockerfile").write_text(dockerfile)

    (inst / "run_tests.sh").write_text(pick_runner(platform).read_text())

    meta = {
        "instance_id": instance_id,
        "project": "zephyr",
        "repo": REPO_URL,
        # base_commit..fix_commit spans exactly this PR, which is the range
        # validate_instance.py diffs to reconstruct the upstream fix.
        "base_commit": base_commit,
        "fix_commit": head_commit,
        "test_commit": head_commit,
        "problem_statement": triage["problem_statement"],
        "platform": platform,
        "test_path": test_path,
        "build_command": f"west build -b {platform} {test_path}{extra_args}",
        # What the fallback would install. `west sdk install` overrides this
        # from the repo's own SDK_VERSION where that file exists, so the SDK an
        # image actually ends up with is not always this value -- recorded so
        # the variation across the corpus is visible rather than implicit.
        "sdk_fallback_version": SDK_FALLBACK,
        "extra_configs": extra,
        "run_command": "west build -t run",
        "docker_image": f"embedeval:zephyr-{pr}{tag_suffix}",
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


def main() -> None:
    p = argparse.ArgumentParser(description="Generate a Zephyr instance from a triage verdict.")
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

    inst = generate(args.pr, triage, Path(args.out), args.tag_suffix)
    print(f"  wrote {inst.relative_to(REPO_ROOT) if REPO_ROOT in inst.parents else inst}")
    for f in sorted(inst.iterdir()):
        print(f"    {f.name:<20} {f.stat().st_size:>7} B")


if __name__ == "__main__":
    main()
