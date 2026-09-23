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
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENRICHED = REPO_ROOT / "candidates" / "enriched"
TEMPLATES = REPO_ROOT / "templates" / "zephyr"
DEFAULT_OUT = REPO_ROOT / "generated"

REPO_URL = "https://github.com/zephyrproject-rtos/zephyr"

# Only the fallback path in the Dockerfile uses this -- `west sdk install`
# picks its own version when it works. Approximate mapping from when the PR
# merged to an SDK that existed then.
SDK_BY_YEAR = {2021: "0.13.2", 2022: "0.15.2", 2023: "0.16.8"}
SDK_DEFAULT = "0.17.0"


def sdk_version(merged_at: str | None) -> str:
    if not merged_at:
        return SDK_DEFAULT
    return SDK_BY_YEAR.get(int(merged_at[:4]), SDK_DEFAULT)


def build_test_patch(record: dict) -> str:
    """Reassemble a unified diff of the PR's test-file changes.

    Built from this PR's own file list, so it cannot pick up a neighbouring
    PR's changes -- the failure mode that produced the contaminated NuttX
    patches, where a range diff swept in whatever else touched those paths
    between two commits.
    """
    out = []
    for f in record["files"]:
        name = f["filename"]
        if not name.startswith("tests/") or not f.get("patch"):
            continue
        old = "/dev/null" if f["status"] == "added" else f"a/{name}"
        new = "/dev/null" if f["status"] == "removed" else f"b/{name}"
        out.append(f"diff --git a/{name} b/{name}")
        out.append(f"--- {old}")
        out.append(f"+++ {new}")
        out.append(f["patch"].rstrip("\n"))
    return "\n".join(out) + "\n" if out else ""


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

    patch = build_test_patch(rec)
    if not patch:
        sys.exit(f"ERROR: no test-file diff for {pr}; instance would have no tests")
    (inst / "test_patch.diff").write_text(patch)

    dockerfile = (TEMPLATES / "Dockerfile.tmpl").read_text()
    for key, val in {
        "@@BASE_COMMIT@@": base_commit,
        "@@REPO@@": REPO_URL,
        "@@SDK_VERSION@@": sdk_version(rec.get("merged_at")),
        "@@PLATFORM@@": platform,
        "@@TEST_PATH@@": test_path,
        "@@EXTRA_BUILD_ARGS@@": extra_args,
    }.items():
        dockerfile = dockerfile.replace(key, val)
    (inst / "Dockerfile").write_text(dockerfile)

    (inst / "run_tests.sh").write_text(pick_runner(platform).read_text())
    (inst / "ZEPHYR_GUIDE.md").write_text((TEMPLATES / "ZEPHYR_GUIDE.md").read_text())

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
