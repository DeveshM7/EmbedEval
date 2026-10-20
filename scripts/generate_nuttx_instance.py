#!/usr/bin/env python3
"""Generate a native NuttX instance from an enriched pair and triage verdict.

    python scripts/generate_nuttx_instance.py \
        --triage triage/nuttx_runs/<run>/apps-2329-kernel-11898.json

    # a pair proposed by the per-apps-PR search (high or medium confidence)
    python scripts/generate_nuttx_instance.py \
        --triage triage/nuttx_runs/<run>_apps/apps-3790.json --kernel-pr 20112

    # rewrite an existing instance's run_tests.sh from the current template
    python scripts/generate_nuttx_instance.py \
        --rerender-runner docker/instances/nuttx__nuttx-8832

The apps test patch is diffed from the apps PR branch, and the validator's
kernel fix range is the kernel PR branch. Neither range ends at a merge commit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import nuttx_candidates as candidates
import nuttx_platforms as platforms
from render_nuttx_instance import DEFAULT_OUT, ENTRY_DEF, ostest_banners, render, render_runner


ROOT = Path(__file__).resolve().parent.parent
ENRICHED = ROOT / "candidates" / "nuttx" / "enriched"
APPS_CLONE = ROOT / "candidates" / "nuttx" / "apps.git"
KERNEL_CLONE = ROOT / "candidates" / "nuttx" / "kernel.git"


def test_patch_paths(record: dict, selected: list[str]) -> list[str]:
    """Include changed build and call sites for the selected test sources."""
    contents = record.get("test_contents") or {}
    sources = [p for p in selected if p.endswith(".c") and not p.endswith("/ostest_main.c")]
    functions = {name for p in sources for name in ENTRY_DEF.findall(contents.get(p, ""))}
    names = {Path(p).name for p in sources}
    paths = list(selected)
    for entry in record["apps"]["files"]:
        path = entry["filename"]
        if path in paths:
            continue
        text = contents.get(path, "")
        if path in ("testing/ostest/ostest_main.c", "testing/ostest/ostest.h"):
            relevant = any(re.search(rf"\b{re.escape(name)}\s*\(", text) for name in functions)
        elif path in ("testing/ostest/Makefile", "testing/ostest/CMakeLists.txt"):
            relevant = any(name in text for name in names)
        elif path == "testing/ostest/Kconfig":   # options the selected tests read
            relevant = any(re.search(rf"^\s*config\s+{re.escape(option)}\s*$", text, re.MULTILINE)
                           for p in sources for option in re.findall(r"\bCONFIG_(\w+)",
                                                                     contents.get(p, "")))
        else:
            relevant = False
        if relevant:
            paths.append(path)
    return paths


def metadata(record: dict, kernel_number: int, verdict: dict, host_arch: str | None = None) -> dict:
    if verdict.get("verdict") != "accept":
        raise ValueError("triage verdict must accept the pair")
    platform = verdict.get("platform")
    qemu = platforms.is_qemu(platform or "")
    # Readiness is the model's own doubt about the recipe (runtime_other is always
    # needs_fixture); an accepted high/medium pair goes to validation, which decides.
    if platform != "sim:nsh" and not qemu:
        raise ValueError(f"the generator supports sim:nsh and the QEMU boards of nuttx_platforms.py, not {platform}")
    apps = record["apps"]
    case = f"apps-{apps['number']}-kernel-{kernel_number}"
    if verdict.get("case", case) != case or verdict.get("kernel_pr", kernel_number) != kernel_number:
        raise ValueError("triage verdict selects a different PR pair")
    matches = [k for k in record.get("kernel_candidates", []) if k["number"] == kernel_number]
    if len(matches) > 1:
        raise ValueError(f"kernel PR #{kernel_number} is not an unambiguous linked lead")
    # A kernel PR the triage found by search rather than by link.
    kernel = matches[0] if matches else candidates.kernel_record(kernel_number)
    if not kernel.get("merged_at"):
        raise ValueError(f"kernel PR #{kernel_number} was not merged")
    test_paths = verdict.get("test_paths") or []
    fix_paths = verdict.get("fix_paths") or []
    apps_changed = {f["filename"] for f in apps["files"]}
    kernel_changed = {f["filename"] for f in kernel["files"]}
    if not test_paths or not set(test_paths) <= apps_changed or \
            any(not p.startswith("testing/ostest/") for p in test_paths):
        raise ValueError("test_paths must be changed ostest files in the apps PR")
    test_paths = test_patch_paths(record, test_paths)
    if not fix_paths or not set(fix_paths) <= kernel_changed:
        raise ValueError("fix_paths must be changed files in the kernel PR")
    for name in ("problem_statement", "fail_to_pass"):
        if not verdict.get(name):
            raise ValueError(f"triage verdict lacks {name}")
    fail_type = verdict.get("fail_type")
    if fail_type not in ("runtime_assert", "runtime_other", "compile_error"):
        raise ValueError("fail_type must be runtime_assert, runtime_other or compile_error")
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
        # Several apps tests can pair with one kernel fix; the apps PR keeps
        # their instances apart.
        "instance_id": f"nuttx__nuttx-{kernel_number}-a{apps['number']}",
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
        "files_changed_by_fix": [f["filename"] for f in kernel["files"]],
        "files_changed_by_test_patch": test_paths,
        "sim_config": platforms.canonical(platform),
        "test_app": "ostest",
        "fail_type": fail_type,
        "problem_statement": verdict["problem_statement"],
        "fail_to_pass": verdict["fail_to_pass"],
        "pass_to_pass": verdict.get("pass_to_pass", []),
        "config_overrides": {**verdict.get("config_overrides", {}),
                             **platforms.alias_overrides(platform)},
        "clean_after_patch": verdict.get("clean_after_patch", False),
    }
    if host_arch:
        meta["host_arch"] = host_arch   # the instance builds, validates and runs on this architecture only
    if fail_type == "compile_error":
        meta["compile_test_source"] = verdict["compile_test_source"]
        meta["missing_api"] = verdict["missing_api"]
    issues = kernel.get("linked_issues") or []
    if issues:
        meta["issue_url"] = issues[0]["url"]
    # Kernel PRs the fix needs as well (follow-ups); validation applies them
    # with the primary PR in merge order.
    extras = needed_prs(record, verdict.get("also_needs") or [], "also_needs")
    if extras:
        meta["kernel_merged_at"] = kernel["merged_at"]
        meta["extra_fix_prs"] = extras
    # Prerequisites: merged before the fix but after it branched, and needed by
    # the test (an API it calls). They go in the base, not in the fix, so the
    # pre-fix run fails for the bug rather than for their missing names.
    prereqs = needed_prs(record, verdict.get("base_needs") or [], "base_needs")
    if prereqs:
        meta["base_prs"] = prereqs
    return meta


def needed_prs(record: dict, entries: list[dict], field: str) -> list[dict]:
    """The merged kernel PRs a verdict field names, in merge order."""
    prs = []
    for entry in entries:
        number = entry["kernel_pr"]
        pr = next((k for k in record.get("kernel_candidates", []) if k["number"] == number),
                  None) or candidates.kernel_record(number)
        if not pr.get("merged_at") or not pr.get("base_commit") or not pr.get("head_commit"):
            raise ValueError(f"{field} kernel PR #{number} must be merged with branch commits")
        # "files" limits a needed PR to some of its files (validation's
        # build-wiring follow-ups take only their build files).
        files = [f["filename"] for f in pr["files"]
                 if not entry.get("files") or f["filename"] in entry["files"]]
        prs.append({"kernel_pr": number, "url": pr["url"], "merged_at": pr["merged_at"],
                    "base_commit": pr["base_commit"], "head_commit": pr["head_commit"],
                    "files_changed": files})
    return sorted(prs, key=lambda e: e["merged_at"])


def run(*cmd: str, timeout: int = 900) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}: {result.stderr.strip()[:400]}")
    return result.stdout


def kernel_clone() -> Path:
    if os.environ.get("EMBEDEVAL_NUTTX_CLONE"):
        return Path(os.environ["EMBEDEVAL_NUTTX_CLONE"])
    clone = Path(os.environ["EMBEDEVAL_NUTTX_DATA"]) / "kernel.git" if os.environ.get(
        "EMBEDEVAL_NUTTX_DATA") else KERNEL_CLONE
    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        run("git", "clone", "--bare", "--filter=blob:none",
            "https://github.com/apache/nuttx.git", str(clone), timeout=1800)
    return clone


def base_patch(prs: list[dict]) -> str:
    """The prerequisite PRs' changes, to commit onto the kernel base."""
    clone = kernel_clone()
    ensure_apps_commits(clone, *(sha for pr in prs for sha in (pr["base_commit"], pr["head_commit"])))
    return "".join(run("git", "-C", str(clone), "diff", "--binary",
                       f"{pr['base_commit']}..{pr['head_commit']}", "--", *pr["files_changed"])
                   for pr in prs)


def apps_clone() -> Path:
    clone = Path(os.environ["EMBEDEVAL_NUTTX_DATA"]) / "apps.git" if os.environ.get(
        "EMBEDEVAL_NUTTX_DATA") else APPS_CLONE
    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        run("git", "clone", "--bare", "--filter=blob:none",
            "https://github.com/apache/nuttx-apps.git", str(clone), timeout=1800)
    return clone


def ensure_apps_commits(clone: Path, *commits: str) -> None:
    for sha in commits:
        if subprocess.run(["git", "-C", str(clone), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode:
            run("git", "-C", str(clone), "fetch", "-q", "origin", sha)


def apps_master_at(date: str) -> str:
    """The apps master commit current at date."""
    sha = run("git", "-C", str(apps_clone()), "rev-list", "-1", "--first-parent",
              f"--before={date}", "refs/heads/master").strip()
    if not sha:
        raise ValueError(f"no apps master commit before {date}")
    return sha


def build_test_patch(base: str, head: str, paths: list[str],
                     banners_required: bool = True, onto: str | None = None) -> tuple[str, list[str]]:
    """The apps test patch, and the ostest banners of the tests it changes.
    With onto, the patch is moved (3-way) from base onto that apps commit."""
    clone = apps_clone()
    ensure_apps_commits(clone, base, head)
    changed = run("git", "-C", str(clone), "diff", "--name-only",
                  f"{base}..{head}", "--", *paths).splitlines()
    if set(changed) != set(paths):
        raise ValueError("reviewed test_paths do not all change in the apps PR branch")
    patch = run("git", "-C", str(clone), "diff", f"{base}..{head}", "--", *paths)
    if not patch.strip():
        raise ValueError("empty apps test patch")
    if onto and onto != base:
        patch = moved_patch(onto, patch, paths)
    with patched_apps(onto or base, patch) as work:
        return patch, ostest_banners(work / "testing" / "ostest", paths, banners_required, patch)


def moved_patch(onto: str, patch: str, paths: list[str]) -> str:
    """patch applied 3-way at apps commit onto, as a diff from onto."""
    clone = apps_clone()
    ensure_apps_commits(clone, onto)
    with tempfile.TemporaryDirectory(prefix="embedeval-nuttx-move-") as tmp:
        work = Path(tmp) / "apps"
        patch_file = Path(tmp) / "test.diff"
        patch_file.write_text(patch)
        run("git", "-C", str(clone), "worktree", "add", "--detach", str(work), onto)
        try:
            if subprocess.run(["git", "-C", str(work), "apply", "--3way", str(patch_file)],
                              capture_output=True, text=True).returncode:
                raise ValueError(f"the apps test patch does not apply on apps {onto[:10]}")
            return run("git", "-C", str(work), "diff", "HEAD", "--", *paths)
        finally:
            run("git", "-C", str(clone), "worktree", "remove", "--force", str(work))


@contextmanager
def patched_apps(base: str, patch: str):
    """A temporary apps worktree at base with patch applied."""
    clone = apps_clone()
    ensure_apps_commits(clone, base)
    with tempfile.TemporaryDirectory(prefix="embedeval-nuttx-apply-") as tmp:
        work = Path(tmp) / "apps"
        patch_file = Path(tmp) / "test.diff"
        patch_file.write_text(patch)
        run("git", "-C", str(clone), "worktree", "add", "--detach", str(work), base)
        try:
            run("git", "-C", str(work), "apply", str(patch_file))
            yield work
        finally:
            run("git", "-C", str(clone), "worktree", "remove", "--force", str(work))


def rerender_runner(inst: Path) -> Path:
    """Rewrite inst/run_tests.sh from the template, keeping its other files."""
    meta = json.loads((inst / "metadata.json").read_text())
    patch = (inst / "test_patch.diff").read_text()
    changed = re.findall(r"^diff --git a/(\S+) b/\1$", patch, re.MULTILINE)
    with patched_apps(meta["apps_base_commit"], patch) as work:
        banners = ostest_banners(work / "testing" / "ostest", changed, patch=patch)
    (inst / "run_tests.sh").write_text(render_runner(banners))
    return inst / "run_tests.sh"


def generate(apps_number: int, kernel_number: int, verdict: dict,
             out_root: Path = DEFAULT_OUT, tag_suffix: str = "-gen",
             runtime: str = "docker", host_arch: str | None = None) -> Path:
    pair_path = ENRICHED / f"apps-{apps_number}-kernel-{kernel_number}.json"
    record_path = pair_path if pair_path.exists() else ENRICHED / f"{apps_number}.json"
    record = (json.loads(record_path.read_text()) if record_path.exists()
              else candidates.apps_record(apps_number))
    meta = metadata(record, kernel_number, verdict, host_arch)
    base = meta["apps_base_commit"]
    if verdict.get("apps_at_kernel_date"):
        # Validation's retry when the apps PR's own base does not build against
        # the kernel base outside the test (the two PRs branched far apart): the
        # test patch goes on the apps master of the kernel base's date instead.
        meta["apps_pr_base_commit"] = base
        meta["apps_base_commit"] = apps_master_at(
            candidates.commit_record(candidates.KERNEL, meta["kernel_base_commit"])["date"])
    patch, meta["test_banners"] = build_test_patch(
        base, meta["apps_merge_commit"],
        meta["files_changed_by_test_patch"], meta["fail_type"] != "compile_error",
        onto=meta["apps_base_commit"])
    with tempfile.TemporaryDirectory(prefix="embedeval-nuttx-testpatch-") as tmp:
        patch_file = Path(tmp) / "test.diff"
        patch_file.write_text(patch)
        return render(meta, patch_file, out_root, tag_suffix, runtime,
                      base_patch(meta["base_prs"]) if meta.get("base_prs") else "")


def pair_verdict(verdict: dict, kernel_number: int | None) -> tuple[int, int, dict]:
    """The generator-ready verdict for one pair of a per-apps-PR triage
    verdict: (apps PR, kernel PR, verdict). Only high and medium pairs are
    built; low ones are recorded leads."""
    apps = re.fullmatch(r"apps-(\d+)", verdict.get("case") or "")
    if not apps:
        raise ValueError("a per-apps-PR verdict needs a case ID apps-<N>")
    sent = [p for p in verdict.get("pairs", []) if p.get("confidence") in ("high", "medium")]
    if kernel_number is None:
        if len(sent) != 1:
            raise ValueError("choose the pair with --kernel-pr: "
                             f"{[p.get('kernel_pr') for p in sent]}")
        kernel_number = sent[0]["kernel_pr"]
    pair = next((p for p in verdict.get("pairs", []) if p.get("kernel_pr") == kernel_number), None)
    if pair is None:
        raise ValueError(f"the verdict proposes no pair with kernel PR #{kernel_number}")
    if pair not in sent:
        raise ValueError(f"kernel PR #{kernel_number} has low confidence; it is only a lead")
    case = f"apps-{apps[1]}-kernel-{kernel_number}"
    return int(apps[1]), kernel_number, {**pair, "verdict": "accept", "case": case}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apps-pr", type=int)
    parser.add_argument("--kernel-pr", type=int)
    parser.add_argument("--triage", type=Path)
    parser.add_argument("--rerender-runner", type=Path, metavar="INSTANCE_DIR",
                        help="only rewrite this instance's run_tests.sh")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--tag-suffix", default="-gen")
    parser.add_argument("--runtime", choices=("docker", "apptainer"), default="docker")
    parser.add_argument("--host-arch", choices=("x86_64", "aarch64"),
                        help="record that the instance runs on this architecture only "
                             "(metadata host_arch), e.g. x86_64 for eras with no ARM64 simulator")
    args = parser.parse_args()
    if args.rerender_runner:
        print(rerender_runner(args.rerender_runner))
        return
    if not args.triage:
        parser.error("supply --triage or --rerender-runner")
    verdict = json.loads(args.triage.read_text())
    if "pairs" in verdict:
        try:
            apps_number, kernel_number, verdict = pair_verdict(verdict, args.kernel_pr)
        except ValueError as exc:
            parser.error(str(exc))
        print(generate(apps_number, kernel_number, verdict, args.out, args.tag_suffix,
                       args.runtime, args.host_arch))
        return
    pair = re.fullmatch(r"apps-(\d+)-kernel-(\d+)", verdict.get("case") or "")
    apps_number = args.apps_pr if args.apps_pr is not None else int(pair[1]) if pair else None
    kernel_number = args.kernel_pr if args.kernel_pr is not None else int(pair[2]) if pair else None
    if apps_number is None or kernel_number is None:
        parser.error("supply --apps-pr and --kernel-pr, or a triage JSON with a pair case ID")
    print(generate(apps_number, kernel_number, verdict, args.out,
                   args.tag_suffix, args.runtime, args.host_arch))


if __name__ == "__main__":
    main()
