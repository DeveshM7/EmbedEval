#!/usr/bin/env python3
"""
Verify an instance is well formed: its tests must FAIL at the base commit and
PASS once the upstream fix is applied.

    python scripts/validate_instance.py nuttx__nuttx-11889
    python scripts/validate_instance.py --repo riot
    python scripts/validate_instance.py zephyr__zephyr-65697 --patch outputs/.../patch.diff

No API key and no LLM involved, so this is the cheapest way to confirm a built
image actually works. Replaces validate_instance.sh and
validate_riot_instance.sh, and gives NuttX a validator for the first time --
previously it had only 16 hand-written per-PR scripts.

Zephyr instances from the twister pipeline (metadata "pipeline": "twister")
are validated in their Apptainer image instead, exactly as built:

    EMBEDEVAL_INSTANCES_DIR=generated python scripts/validate_instance.py zephyr__zephyr-65697

Each step runs in a throwaway container with a fresh writable overlay, so the
image itself is never changed:

    1. run_tests on the image as built     -> must FAIL (exit 1)
    2. apply the upstream fix (base_commit..fix_commit outside tests/)
    3. run_tests again                     -> must PASS (exit 0)

"Fail" is any failure -- a test, the build or the configuration -- but not
"nothing ran": a scenario filtered on the board (exit 3) is invalid, never a
failure to fix. A report is written to <instance>/validation.json.

NuttX instances rendered for Apptainer (metadata "runtime": "apptainer";
scripts/render_nuttx_instance.py --runtime apptainer) are validated the same
way in their image, with the NuttX steps of the Docker validator and the same
validation.json report.

So are Mynewt instances (scripts/render_mynewt_instance.py): the image is
unpacked to a writable sandbox on node-local disk (a .sif is very slow to
compile in on ARM64), and in it

    0. baseline: the commit before the test patch, run_tests --baseline passes
    1. run_tests -> exactly fail_to_pass fail, pass_to_pass pass (exit 1)
    2. apply the upstream fix (gold_base_commit|base_commit..fix_commit)
    3. run_tests -> everything passes (exit 0)

each judged on the runner's structured result (/tmp/mynewt-result.json), as in
Docker. A mynewt-core clone (EMBEDEVAL_MYNEWT_CLONE) supplies the fix.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import layout  # noqa: E402  (first: reads --run-dir)
import apptainer_util as au
from apptainer_util import nuttx_reconfigure
import build_config
import instances
import nuttx_config_probe as probe


def sh(cmd: list[str], timeout: int = 120, quiet: bool = True):
    return subprocess.run(cmd, capture_output=quiet, text=True, timeout=timeout)


def verify_structured_results(meta: dict, report: dict, phase: str) -> tuple[bool, str]:
    mode = meta.get("failure_mode", "runtime") if meta["project"] == "mynewt" else "runtime"
    if phase == "baseline":
        expected_fail = set()
        expected_pass = set(meta["baseline_tests"])
        expected_exit_code = 0
    else:
        expected_fail = set(meta["fail_to_pass"]) if phase == "before" else set()
        expected_pass = set(meta["pass_to_pass"])
        expected_exit_code = 1 if phase == "before" else 0
        if phase == "after":
            expected_pass |= set(meta["fail_to_pass"])
    actual_pass = set(report.get("passed", []))
    actual_fail = set(report.get("failed", []))
    inventory_valid = (
        report.get("build_ok") is True
        and actual_pass == expected_pass
        and actual_fail == expected_fail
        and not report.get("missing")
        and not report.get("unexpected")
        and not report.get("error")
        and report.get("exit_code") == expected_exit_code
    )
    if phase == "baseline":
        valid = (inventory_valid and report.get("failure_mode", "runtime") == "runtime"
                 and report.get("compile_failure", False) is False
                 and not report.get("diagnostic"))
    elif mode == "compile" and phase == "before":
        source = meta["compile_test_source"]
        api = meta["missing_api"]
        diagnostic = report.get("diagnostic")
        match = (re.fullmatch(r"(.+?):\d+(?::\d+)?:\s*(?:fatal )?error:\s*(.*)", diagnostic)
                 if isinstance(diagnostic, str) else None)
        matched_diagnostic = bool(
            match
            and (match.group(1) == source or match.group(1).endswith("/" + source))
            and re.search(rf"(?<![A-Za-z0-9_]){re.escape(api)}(?![A-Za-z0-9_])", match.group(2))
        )
        valid = (
            report.get("failure_mode") == "compile"
            and report.get("build_ok") is False
            and report.get("compile_failure") is True
            and report.get("compile_test_source") == source
            and report.get("missing_api") == api
            and matched_diagnostic
            and not actual_pass and not actual_fail
            and not report.get("missing") and not report.get("unexpected")
            and not report.get("error")
            and report.get("exit_code") == 1
        )
    elif mode == "compile":
        valid = (inventory_valid and report.get("failure_mode") == "compile"
                 and report.get("compile_failure") is False
                 and not report.get("diagnostic"))
    else:
        valid = inventory_valid
    detail = (
        f"expected pass={sorted(expected_pass)}, fail={sorted(expected_fail)}; "
        f"got pass={sorted(actual_pass)}, fail={sorted(actual_fail)}, "
        f"missing={report.get('missing', [])}, unexpected={report.get('unexpected', [])}, "
        f"error={report.get('error')}, exit_code={report.get('exit_code')} "
        f"(expected {expected_exit_code})"
    )
    return valid, detail

# GitHub lists at most this many files of a pull request.
GITHUB_PR_FILES_CAP = 3000


def gold_patch(meta: dict, workdir: Path) -> str:
    """
    Fetch the upstream fix as a diff, restricted to the files the fix should
    touch so test changes never leak in.

    Zephyr, RIOT, and Mynewt are single-repo. Their fix range starts at
    gold_base_commit when test-only support landed before the production fix,
    otherwise at base_commit. NuttX is two repos and the fix lives in the
    kernel one, between kernel_base_commit and kernel_merge_commit. With a
    local kernel clone (EMBEDEVAL_NUTTX_CLONE) the diff is taken there,
    fetching only a commit it lacks.
    """
    project = meta["project"]
    if project == "nuttx":
        repo, base, fix = meta["kernel_repo"], meta["kernel_base_commit"], meta["kernel_merge_commit"]
    else:
        repo = meta["repo"]
        base = meta.get("gold_base_commit", meta["base_commit"])
        fix = meta["fix_commit"]

    local = (layout.nuttx_clones()[0] if project == "nuttx"
             else au.mynewt_clone() if project == "mynewt" else None)
    if local:
        clone = local
        for sha in (base, fix):
            if sh(["git", "-C", str(clone), "cat-file", "-e", f"{sha}^{{commit}}"]).returncode:
                sh(["git", "-C", str(clone), "fetch", "-q", "origin", sha], timeout=900)
    else:
        clone = workdir / "repo"
        print(f"  cloning {repo} (blobless) ...")
        sh(["git", "clone", "--filter=blob:none", "--no-checkout", repo, str(clone)], timeout=900)
        sh(["git", "-C", str(clone), "fetch", "origin", base, fix], timeout=900)

    files = meta.get("files_changed_by_fix", [])
    if len(files) >= GITHUB_PR_FILES_CAP:
        files = []  # GitHub truncated the PR's file list: take the whole diff
    cmd = ["git", "-C", str(clone), "diff"]
    if project == "nuttx":
        cmd.append("--binary")
    cmd += [f"{base}..{fix}", "--"]
    cmd += files if files else [".", ":(exclude)tests/"]
    r = sh(cmd, timeout=300)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"could not produce a gold patch: {r.stderr.strip()[:200]}")
    return r.stdout


def nuttx_fix_patches(meta: dict, workdir: Path,
                      primary_override: Path | None = None) -> list[tuple[str, str]]:
    """Primary NuttX fix followed by the merged PRs it needs, in merge order."""
    patches = [("primary", primary_override.read_text() if primary_override
                else gold_patch(meta, workdir))]
    extras = meta.get("extra_fix_prs") or []
    if not extras:
        return patches
    clone = layout.nuttx_clones()[0] or workdir / "repo"
    for extra in extras:
        base, head = extra["base_commit"], extra["head_commit"]
        for sha in (base, head):
            if sh(["git", "-C", str(clone), "cat-file", "-e", f"{sha}^{{commit}}"]).returncode:
                fetched = sh(["git", "-C", str(clone), "fetch", "-q", "origin", sha], timeout=900)
                if fetched.returncode:
                    raise RuntimeError(f"could not fetch follow-up commit {sha}: {fetched.stderr[:200]}")
        files = extra["files_changed"]
        r = sh(["git", "-C", str(clone), "diff", "--binary", f"{base}..{head}", "--",
                *(files if len(files) < GITHUB_PR_FILES_CAP else ["."])], timeout=300)
        if r.returncode or not r.stdout.strip():
            raise RuntimeError(f"could not produce follow-up patch for #{extra['kernel_pr']}: "
                               f"{r.stderr[:200]}")
        patches.append((f"follow-up-{extra['kernel_pr']}", r.stdout))
    # In merge order: a needed PR that merged before the primary one (a
    # prerequisite) goes first.
    if meta.get("kernel_merged_at") and not primary_override:
        merged = {"primary": meta["kernel_merged_at"],
                  **{f"follow-up-{e['kernel_pr']}": e.get("merged_at", "") for e in extras}}
        patches.sort(key=lambda p: merged[p[0]])
    return patches


def config_fallbacks(makefile: str, kconfig: str, test_paths: list[str]) -> list[dict]:
    """Config sets that should build the changed ostest sources: the options the
    ostest Makefile tests before adding each source, plus one alternative of
    each such option's Kconfig "depends on" (at most three sets)."""
    required = {}
    for path in test_paths:
        if path.startswith("testing/ostest/") and path.endswith(".c"):
            for condition in probe.makefile_conditions(makefile, Path(path).name):
                if condition:
                    required.update(probe.requirements(condition.split(" && "))["required"])
    depends = probe.kconfig_depends(kconfig)
    sets = [dict(required)]
    for name, value in required.items():
        dep = re.match(r"depends on ([^;]+)", depends.get(name, ""))
        if value != "y" or not dep:
            continue
        alternatives = []
        for alternative in probe.split_top(probe.strip_parens(dep[1]), "||"):
            terms = [re.fullmatch(r"(!?)\s*(\w+)", probe.strip_parens(term))
                     for term in probe.split_top(probe.strip_parens(alternative), "&&")]
            if all(terms):
                alternatives.append({f"CONFIG_{t[2]}": "n" if t[1] else "y" for t in terms})
        if alternatives:
            sets = [{**s, **a} for s in sets for a in alternatives]
    return [s for s in sets if s][:3]


def local_fix_patch(meta: dict) -> str:
    """The upstream fix from the local Zephyr clone: every change of the PR
    outside tests/. The same range the test patch comes from, so the two
    never overlap."""
    clone = layout.zephyr_clone()
    base, fix = meta["base_commit"], meta["fix_commit"]
    for sha in (base, fix):
        if sh(["git", "-C", str(clone), "cat-file", "-e", f"{sha}^{{commit}}"]).returncode:
            sh(["git", "-C", str(clone), "fetch", "-q", "origin", sha], timeout=900)
    r = sh(["git", "-C", str(clone), "diff", f"{base}..{fix}", "--", ".", ":(exclude)tests/"],
           timeout=300)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"could not produce the fix patch: {r.stderr.strip()[:200]}")
    return r.stdout


RUN_TESTS_MEANING = {0: "passed", 1: "failed", 2: "twister did not run", 3: "nothing ran"}


def summary(output: str) -> list[str]:
    """The per-test lines and verdict from run_tests' output."""
    keep, on = [], False
    for line in output.splitlines():
        if line.startswith("--- twister:"):
            on = True
        if on or line.startswith("==> run_tests"):
            keep.append(line)
        if line.startswith("--- suite:"):
            on = False
    if len(keep) <= 40:
        return keep
    # Large suites: keep what matters -- every test that did not pass, the
    # assertion lines, and the verdict -- and count the passes.
    passed = [l for l in keep if l.strip().startswith("passed")]
    rest = [l for l in keep if not l.strip().startswith(("passed", "| PASS"))]
    return rest[:1] + [f"  ({len(passed)} tests passed)"] + rest[1:40]


def validate_apptainer(instance_id: str, patch_override: Path | None, timeout: int,
                       verbose: bool) -> bool:
    meta = instances.load_metadata(instance_id)
    image = au.instance_image(instance_id)
    print(f"\n{'=' * 70}\n{instance_id}  ({image})\n{'=' * 70}")
    print(f"  {meta['test_path']}/{meta['scenario']} on {meta['platform']}")
    report = {"instance_id": instance_id, "image": str(image), "platform": meta["platform"],
              "scenario": f"{meta['test_path']}/{meta['scenario']}", "valid": False,
              # what was validated: promote_instance.py refuses a recipe that
              # changed after this point
              "recipe_sha256": au.recipe_fingerprint(instances.instance_dir(instance_id)),
              "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not image.exists():
        print(f"  ERROR: image not built: {image}")
        report["reason"] = "image not built"
        return _finish(instance_id, report)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = au.path("runs", f"{instance_id}-{stamp}", "x").parent
    overlay = run_dir / "overlay"
    overlay.mkdir(parents=True)
    logdir = au.path("logs", instance_id, f"validate-{stamp}", "x").parent
    logdir.mkdir(parents=True, exist_ok=True)
    report["logs"] = str(logdir)
    container = au.exec_args(image, binds={run_dir: "/embedeval-run"}, overlay=overlay)

    def step(label: str, cmd: list[str], log: str, t: int):
        print(f"    {label} ...", end="", flush=True)
        t0 = time.time()
        try:
            r = subprocess.run(container + cmd, capture_output=True, text=True, timeout=t)
            rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired as e:
            rc, out = "timeout", str(e.stdout or "")
        (logdir / log).write_text(out)
        secs = time.time() - t0
        print(f" {secs:.0f}s (rc={rc})")
        if verbose:
            print(out)
        return rc, out, secs

    try:
        print("\n  Step 1: expecting run_tests to FAIL on the image as built")
        rc1, out1, s1 = step("run_tests", ["run_tests"], "before.log", timeout)
        report["before"] = {"rc": rc1, "meaning": RUN_TESTS_MEANING.get(rc1, str(rc1)),
                            "seconds": round(s1), "summary": summary(out1)}
        for line in summary(out1):
            print(f"      {line}")
        if rc1 != 1:
            why = {0: "tests PASSED before the fix -- the instance does not detect the bug",
                   3: "nothing ran: the scenario is filtered or skipped on this board",
                   2: "twister did not run (environment problem)"}.get(
                       rc1, f"run_tests ended with {rc1}")
            print(f"  INVALID: {why}")
            report["reason"] = why
            return _finish(instance_id, report)

        print("\n  Step 2: applying the upstream fix")
        patch_text = (Path(patch_override).read_text() if patch_override
                      else local_fix_patch(meta))
        (run_dir / "fix.diff").write_text(patch_text)
        rca, outa, _ = step("git apply", ["git", "-C", "/testbed", "apply",
                                          "/embedeval-run/fix.diff"], "apply.log", 300)
        if rca != 0:
            print(f"  INVALID: the fix does not apply: {outa.strip()[:300]}")
            report["reason"] = "the fix does not apply"
            return _finish(instance_id, report)

        print("\n  Step 3: expecting run_tests to PASS with the fix")
        rc2, out2, s2 = step("run_tests", ["run_tests"], "after.log", timeout)
        report["after"] = {"rc": rc2, "meaning": RUN_TESTS_MEANING.get(rc2, str(rc2)),
                           "seconds": round(s2), "summary": summary(out2)}
        for line in summary(out2):
            print(f"      {line}")
        if rc2 != 0:
            why = {1: "tests still fail with the fix",
                   3: "nothing ran with the fix",
                   2: "twister did not run (environment problem)"}.get(
                       rc2, f"run_tests ended with {rc2}")
            print(f"  INVALID: {why}")
            report["reason"] = why
            return _finish(instance_id, report)

        print("\n  VALID: fails before the fix, passes after")
        report["valid"] = True
        report["reason"] = "fails before the fix, passes after"
        return _finish(instance_id, report)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        report["reason"] = f"error: {type(e).__name__}: {e}"
        return _finish(instance_id, report)
    finally:
        au.remove(run_dir)


def _finish(instance_id: str, report: dict) -> bool:
    (instances.instance_dir(instance_id) / "validation.json").write_text(
        json.dumps(report, indent=2) + "\n")
    return report["valid"]


def error_lines(output: str) -> str:
    """The lines of a failed build that look like errors, indented. A parallel
    build's last lines are usually other files still compiling."""
    out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output)
    lines = [line.strip() for line in re.split(r"[\r\n]+", out)
             if re.search(r"error:|undefined reference|Illegal instruction|\*\*\*",
                          line, re.IGNORECASE)]
    text = "\n".join(lines[-15:]) if lines else out[-400:]
    return "  " + text.strip().replace("\n", "\n  ")


def compile_diagnostic(meta: dict, output: str):
    """A NuttX compile_error instance's expected diagnostic in a build's
    output, or None.

    Covers both a compiler diagnostic (missing symbol in the test source) and
    a linker diagnostic (missing symbol at link time, e.g. a test calling a
    kernel API the unfixed kernel doesn't implement yet) -- same bucket: the
    unfixed tree can't build.
    """
    source, missing_api = meta.get("compile_test_source"), meta.get("missing_api")
    if not source or not missing_api:
        return None
    prefix = (rf"(?:^|[\s/]){re.escape(Path(source).name)}:\d+(?::\d+)?:\s*"
              r"(?:\([^\n)]*\):\s*)?")
    diagnostic = re.search(
        prefix + rf"(?:(?:fatal )?error:|undefined reference to)[^\n]*{re.escape(missing_api)}",
        output,
    )
    if diagnostic:
        return diagnostic
    # A newly added header can be absent altogether before the fix, so the
    # compiler names that header instead of any API it declares.
    header = re.search(prefix + r"fatal error:\s*([^\s:]+): No such file or directory", output)
    if header and f"include/{header.group(1)}" in meta.get("files_changed_by_fix", []):
        return header
    return None


def test_build_error(meta: dict, output: str):
    """A compile or link error located in one of the changed test sources, or None."""
    for path in meta.get("files_changed_by_test_patch", []):
        if path.endswith(".c"):
            diagnostic = re.search(
                rf"(?:^|[\s/]){re.escape(Path(path).name)}:\d+(?::\d+)?:\s*(?:\([^\n)]*\):\s*)?"
                r"(?:(?:fatal )?error:|undefined reference to)[^\n]*", output)
            if diagnostic:
                return diagnostic
    return None


NUTTX_RUN_TESTS_MEANING = {0: "passed", 1: "failed", 2: "timed out"}


def nuttx_summary(output: str) -> list[str]:
    """run_tests' verdict lines, and the lines of ostest's output that say
    what went wrong."""
    return [l for l in output.splitlines()
            if l.startswith(("run_tests:", "ostest_main: Exiting"))
            or "ERROR" in l or re.search(r"assert", l, re.IGNORECASE)][-20:]


def validate_nuttx_apptainer(instance_id: str, patch_override: Path | None, timeout: int,
                             verbose: bool) -> bool:
    """A NuttX instance in its Apptainer image (metadata "runtime":
    "apptainer"). The steps of the Docker validator below, in one writable
    overlay on the image, and a validation.json report as for Zephyr:

        1. build                -> fails with an error in a changed test source
                                   (compile_error: naming missing_api in
                                   compile_test_source, if it can), or builds and
                                   run_tests must FAIL (exit 1) or hang (exit 2); if the
                                   changed test did not run, retry with config_fallbacks()
        2. apply the kernel fix (kernel_base_commit..kernel_merge_commit,
           files_changed_by_fix only), re-apply config_overrides; make clean if
           clean_after_patch
        3. build, run_tests     -> must PASS (exit 0)
    """
    meta = instances.load_metadata(instance_id)
    au.require_host_arch(meta)
    image = au.instance_image(instance_id)
    build_cmd = meta["build_command"]
    compile_case = meta.get("fail_type") == "compile_error"
    print(f"\n{'=' * 70}\n{instance_id}  ({image})\n{'=' * 70}")
    report = {"instance_id": instance_id, "image": str(image), "host": platform.machine(),
              "fail_type": meta.get("fail_type"), "valid": False,
              "recipe_sha256": au.recipe_fingerprint(
                  instances.instance_dir(instance_id),
                  au.nuttx_recipe_files(instances.instance_dir(instance_id))),
              "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not image.exists():
        print(f"  ERROR: image not built: {image}")
        report["reason"] = "image not built"
        return _finish(instance_id, report)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = au.path("runs", f"{instance_id}-{stamp}", "x").parent
    overlay = run_dir / "overlay"
    overlay.mkdir(parents=True)
    logdir = au.path("logs", instance_id, f"validate-{stamp}", "x").parent
    logdir.mkdir(parents=True, exist_ok=True)
    report["logs"] = str(logdir)
    container = au.exec_args(image, binds={run_dir: "/embedeval-run"}, overlay=overlay)

    def step(label: str, cmd: str, log: str, t: int):
        print(f"    {label} ...", end="", flush=True)
        t0 = time.time()
        try:
            r = subprocess.run(container + ["bash", "-c", f"cd /testbed && {cmd}"],
                               capture_output=True, text=True, timeout=t)
            rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            rc = "timeout"
        (logdir / log).write_text(out)
        secs = time.time() - t0
        print(f" {secs:.0f}s (rc={rc})")
        if verbose:
            print(out)
        return rc, out, secs

    def invalid(why: str, detail: str = "") -> bool:
        print(f"  INVALID: {why}")
        if detail:
            print(detail)
        report["reason"] = why
        return _finish(instance_id, report)

    def run_tests(when: str):
        rc, out, secs = step("run_tests", "run_tests", f"{when}.log", timeout)
        report[when].update(rc=rc, meaning=NUTTX_RUN_TESTS_MEANING.get(rc, str(rc)),
                            seconds=round(secs), summary=nuttx_summary(out))
        for line in report[when]["summary"]:
            print(f"      {line}")
        return rc

    try:
        print("\n  Step 1: expecting the tests to FAIL at the base commit")
        rc, out, secs = step("build", build_cmd, "build-before.log", 900)
        report["before"] = {"build_rc": rc, "build_seconds": round(secs)}
        overrides = meta.get("config_overrides") or {}

        def did_not_run() -> bool:
            return any("run_tests: changed test did not run:" in line
                       for line in report["before"].get("summary", []))

        def config_fallback(rc: int):
            """Retry the pre-fix run with config sets that build the changed tests;
            the run's rc and the overrides that ran them, or None."""
            _, makefile, _ = step("read Makefile", "cat apps/testing/ostest/Makefile",
                                  "fallback-makefile.log", 60)
            _, kconfig, _ = step("read Kconfig", "cat apps/testing/ostest/Kconfig",
                                 "fallback-kconfig.log", 60)
            tried = report["before"]["config_fallback"] = []
            sets = config_fallbacks(makefile, kconfig, meta.get("files_changed_by_test_patch", []))
            for n, extra in enumerate(sets, 1):
                tried.append(extra)
                trial = {**overrides, **extra}
                if step(f"fallback config {n}", nuttx_reconfigure(trial),
                        f"config-fallback{n}.log", 300)[0] != 0:
                    continue
                if step("build", build_cmd, f"build-fallback{n}.log", 900)[0] != 0:
                    continue
                rc = run_tests("before")
                if not did_not_run():
                    return rc, trial
            return rc, None

        # The predicted fail_type only picks the expected diagnostic; what the
        # pre-fix build does decides: a build error in a changed test source is a
        # compile failure, and a clean build is judged by its ostest run.
        if rc != 0:
            diagnostic = (compile_case and compile_diagnostic(meta, out)) or \
                test_build_error(meta, out)
            if not diagnostic:
                return invalid("expected a compile or link error in the changed test source "
                               "naming the missing API" if compile_case else
                               f"the runtime test did not build (rc={rc})", error_lines(out))
            report["before"]["diagnostic"] = diagnostic.group(0).strip()
            print(f"  ok: expected test-side build error: {report['before']['diagnostic']}")
        else:
            rc = run_tests("before")
            if did_not_run():
                rc, found = config_fallback(rc)
                if not found:
                    return invalid("changed test did not run before the fix")
                overrides = report["config_overrides_used"] = found
            # A crash or ERROR line gives rc=1, a hang rc=2 (or "timeout" when the
            # runner outlasted this step).
            if rc not in (1, 2, "timeout"):
                return invalid({0: "tests PASSED before the fix -- the instance does not "
                                   "isolate the bug"}.get(rc, f"expected a test failure (rc=1), "
                                                              f"got rc={rc}"))

        print("\n  Step 2: applying the upstream fix, expecting the tests to PASS")
        patches = nuttx_fix_patches(meta, run_dir, patch_override)
        for label, patch_text in patches:
            (run_dir / "fix.diff").write_text(patch_text)
            rc, out, _ = step(f"git apply {label}", "git apply /embedeval-run/fix.diff",
                              f"apply-{label}.log", 300)
            if rc != 0:
                return invalid(f"the {label} fix does not apply", "  " + out.strip()[:300])
        # The fix can introduce Kconfig defaults required by its new source files,
        # and the overrides it introduces could not be set before it.
        rc, out, _ = step("reconfigure", nuttx_reconfigure(overrides), "config-after.log", 300)
        dropped = re.search(r"config override dropped: \S+", out)
        if dropped:
            return invalid(f"{dropped.group(0)} after the fix (unmet dependency?)")
        if rc != 0:
            return invalid(f"reconfigure failed (rc={rc})", error_lines(out))
        if meta.get("clean_after_patch"):
            rc, out, _ = step("make clean", "make clean", "clean.log", 300)
            if rc != 0:
                return invalid(f"make clean failed (rc={rc})", error_lines(out))
        rc, out, secs = step("rebuild", build_cmd, "build-after.log", 900)
        report["after"] = {"build_rc": rc, "build_seconds": round(secs)}
        if rc != 0:
            return invalid(f"the build failed with the fix (rc={rc})", error_lines(out))
        rc = run_tests("after")
        if rc != 0:
            return invalid({1: "tests still fail with the fix",
                            2: "run_tests timed out with the fix"}.get(rc, f"run_tests ended "
                                                                           f"with {rc}"))

        print("\n  VALID: fails before the fix, passes after")
        report["valid"] = True
        report["reason"] = "fails before the fix, passes after"
        return _finish(instance_id, report)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        report["reason"] = f"error: {type(e).__name__}: {e}"
        return _finish(instance_id, report)
    finally:
        au.remove(run_dir)


def validate_mynewt_apptainer(instance_id: str, patch_override: Path | None, timeout: int,
                              verbose: bool) -> bool:
    """A Mynewt instance in its Apptainer image (metadata "runtime": "apptainer")."""
    meta = instances.load_metadata(instance_id)
    au.require_host_arch(meta)
    image = au.instance_image(instance_id)
    print(f"\n{'=' * 70}\n{instance_id}  ({image})\n{'=' * 70}")
    idir = instances.instance_dir(instance_id)
    report = {"instance_id": instance_id, "image": str(image), "host": platform.machine(),
              "failure_mode": meta.get("failure_mode", "runtime"), "valid": False,
              "recipe_sha256": au.recipe_fingerprint(idir, au.mynewt_recipe_files(idir)),
              "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not image.exists():
        print(f"  ERROR: image not built: {image}")
        report["reason"] = "image not built"
        return _finish(instance_id, report)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = au.path("runs", f"{instance_id}-{stamp}", "x").parent
    logdir = au.path("logs", instance_id, f"validate-{stamp}", "x").parent
    logdir.mkdir(parents=True, exist_ok=True)
    report["logs"] = str(logdir)
    sandbox = None

    def step(label: str, cmd: str, log: str, t: int):
        """Run cmd in the sandbox; returns (rc, output, structured result)."""
        print(f"    {label} ...", end="", flush=True)
        t0 = time.time()
        full = (f"cd /testbed && {cmd}; rc=$?; echo ==MYNEWT-RESULT==; "
                "cat /tmp/mynewt-result.json 2>/dev/null; exit $rc")
        try:
            r = subprocess.run(au.sandbox_exec_args(sandbox, binds={run_dir: "/embedeval-run"})
                               + ["bash", "-c", full], capture_output=True, text=True, timeout=t)
            rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            rc = "timeout"
        (logdir / log).write_text(out)
        result = None
        if "==MYNEWT-RESULT==" in out:
            try:
                result = json.loads(out.split("==MYNEWT-RESULT==", 1)[1])
            except ValueError:
                pass
        print(f" {time.time() - t0:.0f}s (rc={rc})")
        if verbose:
            print(out)
        return rc, out, result

    def invalid(why: str, detail: str = "") -> bool:
        print(f"  INVALID: {why}")
        if detail:
            print(detail)
        report["reason"] = why
        return _finish(instance_id, report)

    def judge(phase: str, rc, result, want_rc: int) -> str | None:
        """None if the phase matches expectations, else why not."""
        if rc == "timeout":
            return f"{phase}: run_tests timed out"
        if result is None:
            return f"{phase}: no structured result (rc={rc})"
        ok, detail = verify_structured_results(meta, result, phase)
        report[phase] = {"rc": rc, "passed": result.get("passed", []),
                         "failed": result.get("failed", []), "error": result.get("error"),
                         "missing": result.get("missing", []),
                         "unexpected": result.get("unexpected", [])}
        if not ok or rc != want_rc:
            return f"{phase}: {detail}"
        return None

    try:
        t0 = time.time()
        sandbox = au.unpack_sandbox(image)
        print(f"    unpacked sandbox in {time.time() - t0:.0f}s")
        if meta["project"] != "mynewt":
            return invalid("not a Mynewt instance")

        print("\n  Baseline: expecting the existing tests to PASS before the test patch")
        rc, out, result = step(
            "run_tests --baseline",
            "ORIG=$(git rev-parse HEAD) && git checkout -q --detach HEAD^ && "
            "{ run_tests --baseline; r=$?; git checkout -q --detach $ORIG; (exit $r); }",
            "baseline.log", timeout + 300)
        why = judge("baseline", rc, result, 0)
        if why:
            return invalid(why, error_lines(out))

        print("\n  Step 1: expecting exactly fail_to_pass to FAIL at the base commit")
        rc, out, result = step("run_tests", "run_tests", "before.log", timeout + 300)
        why = judge("before", rc, result, 1)
        if why:
            return invalid(why, error_lines(out))

        print("\n  Step 2: applying the upstream fix, expecting everything to PASS")
        patch = patch_override.read_text() if patch_override else gold_patch(meta, run_dir)
        (run_dir / "fix.diff").write_text(patch)
        three_way = " --3way" if meta.get("gold_patch_three_way") else ""
        rc, out, _ = step("git apply", f"git apply{three_way} /embedeval-run/fix.diff",
                          "apply.log", 120)
        if rc != 0:
            return invalid("the fix does not apply", "  " + out.strip()[:300])
        rc, out, result = step("run_tests", "run_tests", "after.log", timeout + 300)
        why = judge("after", rc, result, 0)
        if why:
            return invalid(why, error_lines(out))

        print("\n  VALID: fails before the fix, passes after")
        report["valid"] = True
        report["reason"] = "fails before the fix, passes after"
        return _finish(instance_id, report)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        report["reason"] = f"error: {type(e).__name__}: {e}"
        return _finish(instance_id, report)
    finally:
        if sandbox:
            au.remove_sandbox(sandbox)
        au.remove(run_dir)


def validate(instance_id: str, patch_override: Path | None = None, timeout: int = 600,
             verbose: bool = False) -> bool:
    meta = instances.load_metadata(instance_id)
    if meta.get("pipeline") == "twister":
        return validate_apptainer(instance_id, patch_override, max(timeout, 1800), verbose)
    if meta["project"] == "nuttx" and meta.get("runtime") == "apptainer":
        return validate_nuttx_apptainer(instance_id, patch_override, timeout, verbose)
    if meta["project"] == "mynewt" and meta.get("runtime") == "apptainer":
        return validate_mynewt_apptainer(instance_id, patch_override, timeout, verbose)
    if meta["project"] == "zephyr":
        print(f"  SKIP: {instance_id} is a Docker Zephyr instance, kept as reference "
              f"metadata only; Zephyr instances are validated from the twister pipeline "
              f"(EMBEDEVAL_INSTANCES_DIR=generated).")
        return False
    cfg = build_config.config(meta["project"])
    image = meta["docker_image"]
    build_cmd = meta.get("build_command")
    if not build_cmd:
        print(f"  ERROR: {instance_id} has no build_command")
        return False

    print(f"\n{'=' * 70}\n{instance_id}  ({image})\n{'=' * 70}")

    run_args = ["docker", "run", "-d", "--rm"]
    if cfg["platform"]:
        run_args += ["--platform", cfg["platform"]]
    run_args += [image, "sleep", "1800"]

    r = sh(run_args, timeout=120)
    if r.returncode != 0:
        print(f"  ERROR: docker run failed: {r.stderr.strip()[:200]}")
        return False
    cid = r.stdout.strip()
    workdir = Path(tempfile.mkdtemp())

    step_log = "/tmp/embedeval_step.log"

    def dexec(cmd: str, t: int, label: str | None = None, capture: bool = False):
        # With --verbose the container output streams live; otherwise it is
        # captured and only shown when a step fails. Either way each step
        # announces itself and reports how long it took, so a step that is
        # silently skipped or suspiciously fast is visible.
        if label:
            print(f"    {label} ...", end="", flush=True)
        t0 = time.time()
        stream = verbose and not capture
        if stream:
            # Streamed output isn't captured, so keep a copy in the container
            # for step_output() to read back if this step fails.
            cmd = f"set -o pipefail; ( {cmd} ) 2>&1 | tee {step_log}"
        r = sh(["docker", "exec", cid, "bash", "-c", cmd], timeout=t,
               quiet=not stream)
        if capture and verbose:
            print()
            if r.stdout:
                print(r.stdout, end="")
            if r.stderr:
                print(r.stderr, end="", file=sys.stderr)
        if label:
            print(f" {time.time() - t0:.1f}s (rc={r.returncode})")
        return r

    def structured_results_match(phase: str, container_id: str = cid) -> bool:
        result_path = meta.get("structured_test_results")
        if not result_path:
            return True
        result = sh(["docker", "exec", container_id, "cat", result_path], timeout=30)
        if result.returncode != 0:
            print(f"  FAIL: could not read structured test results at {result_path}")
            return False
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f"  FAIL: invalid structured test results at {result_path}: {e}")
            return False
        valid, detail = verify_structured_results(meta, report, phase)
        if not valid:
            print(f"  FAIL: {phase} structured test results do not match: {detail}")
            return False
        print(f"  ok: {phase} structured test results match the expected cases")
        return True

    def step_output(r) -> str:
        """Full output of the step that produced r; call before the next dexec."""
        out = (r.stdout or "") + (r.stderr or "")
        if not out and verbose:
            out = sh(["docker", "exec", cid, "cat", step_log], timeout=60).stdout or ""
        return out

    def build_errors(r) -> str:
        return error_lines(step_output(r))

    try:
        if meta["project"] == "mynewt":
            print("\n  Baseline: expecting existing tests to PASS before test patch")
            baseline_run = sh(run_args, timeout=120)
            if baseline_run.returncode != 0:
                print(f"  FAIL: baseline docker run failed: {baseline_run.stderr.strip()[:200]}")
                return False
            baseline_cid = baseline_run.stdout.strip()
            try:
                checkout = sh(["docker", "exec", baseline_cid, "bash", "-c",
                               "cd /testbed && git checkout --detach HEAD^"], timeout=120,
                              quiet=not verbose)
                if checkout.returncode != 0:
                    print(f"  FAIL: baseline checkout failed (rc={checkout.returncode})")
                    return False
                baseline = sh(["docker", "exec", baseline_cid, "bash", "-c",
                               "cd /testbed && run_tests --baseline"], timeout=timeout,
                              quiet=not verbose)
                if not structured_results_match("baseline", baseline_cid):
                    return False
                if baseline.returncode != 0:
                    print(f"  FAIL: expected baseline run_tests exit 0, got {baseline.returncode}")
                    return False
                print("  ok: baseline tests passed")
            finally:
                sh(["docker", "rm", "-f", baseline_cid], timeout=60)

        # --- Step 1: tests must FAIL on the unfixed code -------------------
        print("\n  Step 1: expecting tests to FAIL at the base commit")
        if meta["project"] == "mynewt":
            dexec(f"cd /testbed && {build_cmd}", 900, "build")
            r1 = dexec("cd /testbed && run_tests", timeout, "run_tests")
            if not structured_results_match("before"):
                return False
            if r1.returncode != 1:
                print(f"  FAIL: expected run_tests exit 1 before the fix, got "
                      f"{r1.returncode} -- the regression did not fail cleanly")
                return False
            print(f"  ok: run_tests exited {r1.returncode} as expected")
        else:
            compile_before = meta["project"] == "nuttx" and meta.get("fail_type") == "compile_error"
            before_build = dexec(f"cd /testbed && {build_cmd}", 900, "build",
                                 capture=compile_before)
            if compile_before:
                output = (before_build.stdout or "") + (before_build.stderr or "")
                diagnostic = compile_diagnostic(meta, output)
                if before_build.returncode == 0 or not diagnostic:
                    print("  FAIL: expected a compile or link error in the changed test "
                          "source naming the missing API")
                    if before_build.returncode != 0:
                        print(build_errors(before_build))
                    return False
                print(f"  ok: expected test-side build error: {diagnostic.group(0).strip()}")
            else:
                if meta["project"] == "nuttx" and before_build.returncode != 0:
                    print(f"  FAIL: runtime test did not build (rc={before_build.returncode})")
                    print(build_errors(before_build))
                    return False
                r1 = dexec("cd /testbed && run_tests", timeout, "run_tests")
                if r1.returncode == 0:
                    print("  FAIL: tests PASSED before the fix -- the instance does not "
                          "isolate the bug (test patch may not apply, or the bug is absent)")
                    return False
                if meta["project"] == "nuttx" and r1.returncode != 1:
                    print(f"  FAIL: expected a test failure (rc=1), got rc={r1.returncode}")
                    return False
                print(f"  ok: run_tests exited {r1.returncode} as expected")

        # --- Step 2: tests must PASS with the fix applied ------------------
        print("\n  Step 2: applying the fix, expecting tests to PASS")
        if patch_override:
            patch_text = Path(patch_override).read_text()
            print(f"  using supplied patch {patch_override}")
        else:
            patch_text = gold_patch(meta, workdir)
        pf = workdir / "fix.diff"
        pf.write_text(patch_text)

        sh(["docker", "cp", str(pf), f"{cid}:/tmp/fix.diff"], timeout=60)
        three_way = " --3way" if meta.get("gold_patch_three_way") else ""
        ra = dexec(f"cd /testbed && git apply{three_way} /tmp/fix.diff", 120, "git apply")
        if ra.returncode != 0:
            print(f"  FAIL: git apply failed: {step_output(ra).strip()[:300]}")
            return False

        if meta["project"] == "nuttx":
            configured = dexec("cd /testbed && "
                               + nuttx_reconfigure(meta.get("config_overrides") or {}),
                               300, "reconfigure")
            if configured.returncode != 0:
                print(f"  FAIL: reconfigure failed (rc={configured.returncode})")
                print(build_errors(configured))
                return False

        if meta["project"] == "nuttx" and meta.get("clean_after_patch"):
            clean = dexec("cd /testbed && make clean", 300, "make clean")
            if clean.returncode != 0:
                print(f"  FAIL: make clean failed (rc={clean.returncode})")
                print(build_errors(clean))
                return False
        cleaned = cfg["clean_paths"]
        if cleaned:
            dexec("rm -rf " + " ".join(cleaned), 60, f"clean {' '.join(cleaned)}")
        else:
            print(f"    clean: nothing to remove for {meta['project']}")
        rb = dexec(f"cd /testbed && {build_cmd}", 900, "rebuild")
        if rb.returncode != 0:
            print(f"  FAIL: build failed after applying the fix (rc={rb.returncode})")
            print(build_errors(rb))
            return False

        r2 = dexec("cd /testbed && run_tests", timeout, "run_tests")
        if not structured_results_match("after"):
            return False
        if r2.returncode != 0:
            print(f"  FAIL: tests still failing after the fix (rc={r2.returncode})")
            print("  " + step_output(r2)[-400:].replace("\n", "\n  "))
            return False

        print("\n  VALID: fails before the fix, passes after")
        return True

    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return False
    finally:
        sh(["docker", "rm", "-f", cid], timeout=60)
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Validate instance fail-then-pass behaviour.")
    p.add_argument("instance", nargs="*", help="instance id(s)")
    p.add_argument("--repo", choices=instances.PROJECTS, help="every instance for one project")
    p.add_argument("--all", action="store_true")
    p.add_argument("--patch", help="validate this patch instead of the upstream fix")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--verbose", "-v", action="store_true",
                   help="stream build and test output instead of capturing it")
    args = p.parse_args()

    if args.all:
        targets = instances.discover_instances()
    elif args.repo:
        targets = instances.discover_instances(args.repo)
    elif args.instance:
        targets = args.instance
    else:
        p.error("give an instance id, or --repo, or --all")

    if args.patch and len(targets) != 1:
        p.error("--patch applies to exactly one instance")

    start = time.time()
    results = {t: validate(t, Path(args.patch) if args.patch else None, args.timeout,
                           args.verbose)
               for t in targets}

    print(f"\n{'=' * 70}\nSUMMARY  ({(time.time() - start) / 60:.1f} min)\n{'=' * 70}")
    for t, ok in results.items():
        print(f"  [{'VALID' if ok else 'FAIL '}] {t}")
    ok = sum(results.values())
    print(f"\n  {ok}/{len(results)} valid")
    if ok != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
