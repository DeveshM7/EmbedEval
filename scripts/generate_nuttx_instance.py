#!/usr/bin/env python3
"""Generate a native NuttX instance from an enriched pair and triage verdict.

    python scripts/generate_nuttx_instance.py \
        --triage triage/nuttx_runs/<run>/apps-2329-kernel-11898.json

The apps test patch is diffed from the apps PR branch, and the validator's
kernel fix range is the kernel PR branch. Neither range ends at a merge commit.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

from render_nuttx_instance import DEFAULT_OUT, render


ROOT = Path(__file__).resolve().parent.parent
ENRICHED = ROOT / "candidates" / "nuttx" / "enriched"
APPS_CLONE = ROOT / "candidates" / "nuttx" / "apps.git"


def metadata(record: dict, kernel_number: int, verdict: dict) -> dict:
    if verdict.get("verdict") != "accept":
        raise ValueError("triage verdict must accept the pair")
    if "readiness" in verdict and verdict["readiness"] != "ready_to_generate":
        raise ValueError("triage readiness must be ready_to_generate; review the recipe first")
    if verdict.get("platform") != "sim:nsh":
        raise ValueError("the current generator supports sim:nsh; retain QEMU leads for later")
    apps = record["apps"]
    case = f"apps-{apps['number']}-kernel-{kernel_number}"
    if verdict.get("case", case) != case or verdict.get("kernel_pr", kernel_number) != kernel_number:
        raise ValueError("triage verdict selects a different PR pair")
    matches = [k for k in record["kernel_candidates"] if k["number"] == kernel_number]
    if len(matches) != 1:
        raise ValueError(f"kernel PR #{kernel_number} is not an unambiguous linked lead")
    kernel = matches[0]
    test_paths = verdict.get("test_paths") or []
    fix_paths = verdict.get("fix_paths") or []
    apps_changed = {f["filename"] for f in apps["files"]}
    kernel_changed = {f["filename"] for f in kernel["files"]}
    if not test_paths or not set(test_paths) <= apps_changed or \
            any(not p.startswith("testing/ostest/") for p in test_paths):
        raise ValueError("test_paths must be changed ostest files in the apps PR")
    if not fix_paths or not set(fix_paths) <= kernel_changed:
        raise ValueError("fix_paths must be changed files in the kernel PR")
    for name in ("problem_statement", "test_success_marker", "test_failure_marker",
                 "fail_to_pass"):
        if not verdict.get(name):
            raise ValueError(f"triage verdict lacks {name}")
    fail_type = verdict.get("fail_type")
    if fail_type not in ("runtime_assert", "compile_error"):
        raise ValueError("fail_type must be runtime_assert or compile_error")
    if fail_type == "compile_error":
        source = verdict.get("compile_test_source")
        token = verdict.get("missing_api")
        if source not in test_paths or not token or \
                token not in (record.get("test_contents") or {}).get(source, ""):
            raise ValueError("compile_test_source must be a changed test source containing missing_api")
    for pr, name in ((apps, "apps"), (kernel, "kernel")):
        if not pr.get("base_commit") or not pr.get("head_commit"):
            raise ValueError(f"{name} PR lacks exact branch commit boundaries")

    meta = {
        "instance_id": f"nuttx__nuttx-{kernel_number}",
        "project": "nuttx",
        "kernel_repo": "https://github.com/apache/nuttx",
        "apps_repo": "https://github.com/apache/nuttx-apps",
        "kernel_pr": kernel_number,
        "apps_pr": apps["number"],
        "kernel_pr_url": kernel["url"],
        "apps_pr_url": apps["url"],
        "kernel_base_commit": kernel["base_commit"],
        # Legacy metadata calls this a merge commit; use the PR branch tip so
        # base..tip contains this PR only, without unrelated mainline merges.
        "kernel_merge_commit": kernel["head_commit"],
        "apps_base_commit": apps["base_commit"],
        "apps_merge_commit": apps["head_commit"],
        "files_changed_by_fix": fix_paths,
        "files_changed_by_test_patch": test_paths,
        "sim_config": "sim:nsh",
        "test_app": "ostest",
        "fail_type": fail_type,
        "problem_statement": verdict["problem_statement"],
        "test_success_marker": verdict["test_success_marker"],
        "test_failure_marker": verdict["test_failure_marker"],
        "test_failure_match": verdict.get("test_failure_match", "literal"),
        "test_failure_context_marker": verdict.get("test_failure_context_marker", ""),
        "fail_to_pass": verdict["fail_to_pass"],
        "pass_to_pass": verdict.get("pass_to_pass", []),
        "config_overrides": verdict.get("config_overrides", {}),
        "clean_after_patch": verdict.get("clean_after_patch", False),
    }
    if fail_type == "compile_error":
        meta["compile_test_source"] = verdict["compile_test_source"]
        meta["missing_api"] = verdict["missing_api"]
    issues = kernel.get("linked_issues") or []
    if issues:
        meta["issue_url"] = issues[0]["url"]
    return meta


def run(*cmd: str, timeout: int = 900) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}: {result.stderr.strip()[:400]}")
    return result.stdout


def apps_clone() -> Path:
    if not APPS_CLONE.exists():
        APPS_CLONE.parent.mkdir(parents=True, exist_ok=True)
        run("git", "clone", "--bare", "--filter=blob:none",
            "https://github.com/apache/nuttx-apps.git", str(APPS_CLONE), timeout=1800)
    return APPS_CLONE


def build_test_patch(base: str, head: str, paths: list[str]) -> str:
    clone = apps_clone()
    run("git", "-C", str(clone), "fetch", "-q", "origin", base, head)
    changed = run("git", "-C", str(clone), "diff", "--name-only",
                  f"{base}..{head}", "--", *paths).splitlines()
    if set(changed) != set(paths):
        raise ValueError("reviewed test_paths do not all change in the apps PR branch")
    patch = run("git", "-C", str(clone), "diff", f"{base}..{head}", "--", *paths)
    if not patch.strip():
        raise ValueError("empty apps test patch")
    with tempfile.TemporaryDirectory(prefix="embedeval-nuttx-apply-") as tmp:
        work = Path(tmp) / "apps"
        patch_file = Path(tmp) / "test.diff"
        patch_file.write_text(patch)
        run("git", "-C", str(clone), "worktree", "add", "--detach", str(work), base)
        try:
            run("git", "-C", str(work), "apply", "--check", str(patch_file))
        finally:
            run("git", "-C", str(clone), "worktree", "remove", "--force", str(work))
    return patch


def generate(apps_number: int, kernel_number: int, verdict: dict,
             out_root: Path = DEFAULT_OUT, tag_suffix: str = "-gen") -> Path:
    pair_path = ENRICHED / f"apps-{apps_number}-kernel-{kernel_number}.json"
    record_path = pair_path if pair_path.exists() else ENRICHED / f"{apps_number}.json"
    record = json.loads(record_path.read_text())
    meta = metadata(record, kernel_number, verdict)
    patch = build_test_patch(meta["apps_base_commit"], meta["apps_merge_commit"],
                             meta["files_changed_by_test_patch"])
    with tempfile.TemporaryDirectory(prefix="embedeval-nuttx-testpatch-") as tmp:
        patch_file = Path(tmp) / "test.diff"
        patch_file.write_text(patch)
        return render(meta, patch_file, out_root, tag_suffix)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apps-pr", type=int)
    parser.add_argument("--kernel-pr", type=int)
    parser.add_argument("--triage", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--tag-suffix", default="-gen")
    args = parser.parse_args()
    verdict = json.loads(args.triage.read_text())
    pair = re.fullmatch(r"apps-(\d+)-kernel-(\d+)", verdict.get("case") or "")
    apps_number = args.apps_pr if args.apps_pr is not None else int(pair[1]) if pair else None
    kernel_number = args.kernel_pr if args.kernel_pr is not None else int(pair[2]) if pair else None
    if apps_number is None or kernel_number is None:
        parser.error("supply --apps-pr and --kernel-pr, or a triage JSON with a pair case ID")
    print(generate(apps_number, kernel_number, verdict, args.out,
                   args.tag_suffix))


if __name__ == "__main__":
    main()
