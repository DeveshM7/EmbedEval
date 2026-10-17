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
import build_config
import instances


def sh(cmd: list[str], timeout: int = 120, quiet: bool = True):
    return subprocess.run(cmd, capture_output=quiet, text=True, timeout=timeout)


def gold_patch(meta: dict, workdir: Path) -> str:
    """
    Fetch the upstream fix as a diff, restricted to the files the fix should
    touch so test changes never leak in.

    Zephyr and RIOT are single-repo (base_commit..fix_commit). NuttX is two
    repos and the fix lives in the kernel one, between kernel_base_commit and
    kernel_merge_commit. With a local kernel clone (EMBEDEVAL_NUTTX_CLONE) the
    diff is taken there, fetching only a commit it lacks.
    """
    project = meta["project"]
    if project == "nuttx":
        repo, base, fix = meta["kernel_repo"], meta["kernel_base_commit"], meta["kernel_merge_commit"]
    else:
        repo, base, fix = meta["repo"], meta["base_commit"], meta["fix_commit"]

    local = layout.nuttx_clones()[0] if project == "nuttx" else None
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
        r = sh(["git", "-C", str(clone), "diff", "--binary", f"{base}..{head}", "--",
                *extra["files_changed"]], timeout=300)
        if r.returncode or not r.stdout.strip():
            raise RuntimeError(f"could not produce follow-up patch for #{extra['kernel_pr']}: "
                               f"{r.stderr[:200]}")
        patches.append((f"follow-up-{extra['kernel_pr']}", r.stdout))
    return patches


def nuttx_reconfigure(overrides: dict) -> str:
    """Shell that re-applies the pair's config overrides after the kernel fix.

    An override whose symbol only exists once the fix is applied does not survive
    the pre-fix configure and build, so append each one again, let olddefconfig
    resolve its dependencies, and fail if one was still dropped.
    """
    cmds = []
    for name, value in sorted(overrides.items()):
        cmds.append(f"sed -i '/^{name}=/d; /^# {name} is not set/d' .config")
        cmds.append(f"printf '%s\\n' '{name}={value}' >> .config")
    cmds.append("make olddefconfig")
    for name, value in sorted(overrides.items()):
        if value == "n":
            cmds.append(f"{{ ! grep -q '^{name}=' .config || "
                        f"{{ echo 'config override dropped: {name}={value}'; exit 3; }}; }}")
        else:
            cmds.append(f"{{ grep -qx '{name}={value}' .config || "
                        f"{{ echo 'config override dropped: {name}={value}'; exit 3; }}; }}")
    return " && ".join(cmds)


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

        1. build                -> runtime_assert: must build, run_tests must FAIL (exit 1)
                                   compile_error: must fail, naming missing_api in
                                   compile_test_source
        2. apply the kernel fix (kernel_base_commit..kernel_merge_commit,
           files_changed_by_fix only); make clean if clean_after_patch
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
              "recipe_sha256": au.recipe_fingerprint(instances.instance_dir(instance_id),
                                                     au.NUTTX_RECIPE_FILES),
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
        if compile_case:
            if rc == 0:
                return invalid("compile test built before the fix -- the instance does not "
                               "isolate the bug")
            diagnostic = compile_diagnostic(meta, out)
            if not diagnostic:
                return invalid("expected a compile or link error in the changed test source "
                               "naming the missing API", error_lines(out))
            report["before"]["diagnostic"] = diagnostic.group(0).strip()
            print(f"  ok: expected test-side build error: {report['before']['diagnostic']}")
        else:
            if rc != 0:
                return invalid(f"the runtime test did not build (rc={rc})", error_lines(out))
            rc = run_tests("before")
            if any("run_tests: changed test did not run:" in line
                   for line in report["before"]["summary"]):
                return invalid("changed test did not run before the fix")
            # A crash or ERROR line gives rc=1; a hang (runtime_other only) rc=2.
            if rc != 1 and not (rc == 2 and meta.get("fail_type") == "runtime_other"):
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
        rc, out, _ = step("reconfigure", nuttx_reconfigure(meta.get("config_overrides") or {}),
                          "config-after.log", 300)
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


def validate(instance_id: str, patch_override: Path | None = None, timeout: int = 600,
             verbose: bool = False) -> bool:
    meta = instances.load_metadata(instance_id)
    if meta.get("pipeline") == "twister":
        return validate_apptainer(instance_id, patch_override, max(timeout, 1800), verbose)
    if meta["project"] == "nuttx" and meta.get("runtime") == "apptainer":
        return validate_nuttx_apptainer(instance_id, patch_override, timeout, verbose)
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

    def step_output(r) -> str:
        """Full output of the step that produced r; call before the next dexec."""
        out = (r.stdout or "") + (r.stderr or "")
        if not out and verbose:
            out = sh(["docker", "exec", cid, "cat", step_log], timeout=60).stdout or ""
        return out

    def build_errors(r) -> str:
        return error_lines(step_output(r))

    try:
        # --- Step 1: tests must FAIL on the unfixed code -------------------
        print("\n  Step 1: expecting tests to FAIL at the base commit")
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
        ra = dexec("cd /testbed && git apply /tmp/fix.diff", 120, "git apply")
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
