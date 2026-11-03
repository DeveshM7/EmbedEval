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

OpenThread instances ("pipeline": "cmake") take the same path. Their run_tests
prints a `--- openthread: results` block, one `<status> <name>` line per
expected test, and validity is judged per test (openthread_problem below): a
build failure is never a valid "before", and every fail_to_pass test must fail
before and every fail_to_pass and pass_to_pass test must pass after.

An instance whose instance.json says "observe": true (generate_instance.py
--observe) is observed instead: before and after the fix, the cases that flip
are derived and written to <instance>/observed.json (observe_instance below).

Each step runs in a throwaway container with a fresh writable overlay, so the
image itself is never changed:

    1. run_tests on the image as built     -> must FAIL (exit 1)
    2. apply the upstream fix (base_commit..fix_commit outside tests/)
    3. run_tests again                     -> must PASS (exit 0)

"Fail" is any failure -- a test, the build or the configuration -- but not
"nothing ran": a scenario filtered on the board (exit 3) is invalid, never a
failure to fix. A report is written to <instance>/validation.json.
"""

from __future__ import annotations

import argparse
import json
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
    kernel_merge_commit.
    """
    project = meta["project"]
    if project == "nuttx":
        repo, base, fix = meta["kernel_repo"], meta["kernel_base_commit"], meta["kernel_merge_commit"]
    else:
        repo, base, fix = meta["repo"], meta["base_commit"], meta["fix_commit"]

    clone = workdir / "repo"
    print(f"  cloning {repo} (blobless) ...")
    sh(["git", "clone", "--filter=blob:none", "--no-checkout", repo, str(clone)], timeout=900)
    sh(["git", "-C", str(clone), "fetch", "origin", base, fix], timeout=900)

    files = meta.get("files_changed_by_fix", [])
    cmd = ["git", "-C", str(clone), "diff", f"{base}..{fix}", "--"]
    cmd += files if files else [".", ":(exclude)tests/"]
    r = sh(cmd, timeout=300)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"could not produce a gold patch: {r.stderr.strip()[:200]}")
    return r.stdout


def local_fix_patch(meta: dict) -> str:
    """The upstream fix from the local clone: every change of the PR outside
    tests/. The same range the test patch comes from, so the two never
    overlap."""
    clone = layout.openthread_clone() if meta["project"] == "openthread" else layout.zephyr_clone()
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
OPENTHREAD_MEANING = {**RUN_TESTS_MEANING, 2: "build or configure failed"}


def summary(output: str) -> list[str]:
    """The per-test lines and verdict from run_tests' output."""
    keep, on = [], False
    for line in output.splitlines():
        if line.startswith(("--- twister:", "--- openthread:")):
            on = True
        if on or line.startswith("==> run_tests"):
            keep.append(line)
        if line.startswith(("--- suite:", "--- end")):
            on = False
    if len(keep) <= 40:
        return keep
    # Large suites: keep what matters -- every test that did not pass, the
    # assertion lines, and the verdict -- and count the passes.
    passed = [l for l in keep if l.strip().startswith("passed")]
    rest = [l for l in keep if not l.strip().startswith(("passed", "| PASS"))]
    return rest[:1] + [f"  ({len(passed)} tests passed)"] + rest[1:40]


def openthread_cases(output: str) -> dict[str, str]:
    """name -> status from the `--- openthread: results` block of run_tests."""
    cases, on = {}, False
    for line in output.splitlines():
        if line.startswith("--- openthread: results"):
            on = True
        elif on and line.startswith("--- end"):
            break
        elif on and line.startswith("  "):
            status, _, name = line.strip().partition(" ")
            cases[name] = status
    return cases


def openthread_problem(want: dict, cases: dict[str, str], after: bool) -> str | None:
    """Why an OpenThread instance is not valid, from its per-test results, or
    None. Before the fix every fail_to_pass test must have failed or errored
    (not `not_run`); with it every fail_to_pass and pass_to_pass test must pass."""
    f2p, p2p = want["fail_to_pass"], want["pass_to_pass"]
    if not after:
        bad = [n for n in f2p if cases.get(n) not in ("failed", "error")]
        return f"fail_to_pass tests did not fail before the fix: {bad}" if bad else None
    bad = [n for n in f2p + p2p if cases.get(n) != "passed"]
    return f"tests did not pass with the fix: {bad}" if bad else None


FAILED = ("failed", "error")


def observe_lists(before: dict[str, str], after: dict[str, str]) -> dict:
    """Derive the observed lists from the per-case statuses of the two runs. A
    case absent from a run (a crash before it) counts as not_run; a timeout is
    never a failure."""
    names = list(dict.fromkeys([*before, *after]))
    b = {n: before.get(n, "not_run") for n in names}
    a = {n: after.get(n, "not_run") for n in names}
    pick = lambda f: [n for n in names if f(b[n], a[n])]
    return {"fail_to_pass": pick(lambda x, y: x in FAILED and y == "passed"),
            "pass_to_pass": pick(lambda x, y: x == y == "passed"),
            "failing_both": pick(lambda x, y: x in FAILED and y in FAILED),
            "regressed": pick(lambda x, y: x == "passed" and y in FAILED),
            "timeout": pick(lambda x, y: "timeout" in (x, y)),
            "not_run": pick(lambda x, y: "not_run" in (x, y)),
            "cases": {n: {"before": b[n], "after": a[n]} for n in names}}


SETUP_ERROR = re.compile(r"ninja: error: unknown target|^CMake Error", re.M)
MISSING_SOURCES = re.compile(r"Cannot find source file|No SOURCES given to target")


def build_failure(out: str, compiled: str) -> str:
    """Why an observe run ended rc 2. A configure error or a target ninja does
    not know means nothing was compiled (the selected target is not built in
    our profile): a setup error. Only a compiler error in a source file says
    the code does not build. A configure error about missing sources is the
    test patch adding an executable whose sources only the fix adds: it does
    not build against base, like a compile error."""
    if MISSING_SOURCES.search(out) and "CMake Error" in out:
        return compiled
    if SETUP_ERROR.search(out):
        return "selected target not built in the profile (setup error)"
    return compiled


BUILD_ERROR = re.compile(r"\berror\b|undefined reference", re.I)


def build_errors(out: str, limit: int = 20) -> list[str]:
    """The first compiler `error:` / CMake Error lines of a build log, trimmed."""
    return [l.strip()[:300] for l in out.splitlines()
            if "error:" in l or l.startswith("CMake Error")][:limit]


GCC_CONTEXT = re.compile(r"^\s*\d*\s*\||note:")


def error_text(out: str) -> str:
    """The lines of a build log that report an error: compiler and linker error
    lines with the source echo and notes gcc prints under them (an error inside
    a macro is reported at the macro; the call site is in the notes), and a
    CMake Error line with the lines that name its subject."""
    lines, keep = out.splitlines(), []
    for i, l in enumerate(lines):
        if l.startswith("CMake Error"):
            keep += lines[i:i + 8]
        elif BUILD_ERROR.search(l):
            keep.append(l)
            for c in lines[i + 1:i + 11]:
                if not GCC_CONTEXT.search(c):
                    break
                keep.append(c)
    return "\n".join(keep)


def compile_attribution(out: str, symbols: list) -> str | None:
    """The first compile_symbol (an identifier, or a path whose file name is
    searched) the build log mentions on an error line, or None."""
    text = error_text(out)
    for sym in symbols:
        needle = sym.rsplit("/", 1)[-1]
        if re.search(rf"(?<![\w]){re.escape(needle)}(?![\w])", text):
            return sym
    return None


def after_check(report: dict, step, run_dir: Path, patch_override, meta: dict, timeout: int) -> None:
    """Diagnostic only, after the base build failed: does the same profile build
    and pass the selected tests with the upstream fix? If so, the failure is the
    fix's own change (compile-only); if not, suspect the setup. The verdict
    stays as recorded."""
    print("\n  Diagnostic: the same profile with the upstream fix")
    patch_text = Path(patch_override).read_text() if patch_override else local_fix_patch(meta)
    (run_dir / "fix.diff").write_text(patch_text)
    rca, outa, _ = step("git apply", ["git", "-C", "/testbed", "apply",
                                      "/embedeval-run/fix.diff"], "apply.log", 300)
    if rca != 0:
        report["after_check"] = "the fix does not apply"
        return
    rc2, out2, s2 = step("run_tests", ["run_tests"], "after.log", timeout)
    cases = openthread_cases(out2) if rc2 != 2 else {}
    report["after"] = {"rc": rc2, "seconds": round(s2), "log": str(Path(report["logs"]) / "after.log"),
                       **({"cases": cases} if cases else {})}
    if rc2 == 2:
        report["after_check"] = "does not build with the fix either (setup suspect)"
    elif cases and all(s == "passed" for s in cases.values()):
        report["after_check"] = "builds and passes with the fix (compile-only)"
    else:
        report["after_check"] = "builds with the fix, not all selected cases pass"
    print(f"  after_check: {report['after_check']}")


def observe_instance(meta: dict, report: dict, step, run_dir: Path, patch_override,
                     timeout: int) -> None:
    """Observe mode: run the selected executables as built and with the upstream
    fix, and record what flipped in `report` (written to observed.json)."""
    def done(valid: bool, why: str) -> None:
        print(f"  {'OBSERVED VALID' if valid else 'NO ORACLE'}: {why}")
        report["valid"], report["reason"] = valid, why

    print("\n  Step 1: observing the image as built")
    rc1, out1, s1 = step("run_tests", ["run_tests"], "before.log", timeout)
    report["before"] = {"rc": rc1, "seconds": round(s1), "log": str(Path(report["logs"]) / "before.log")}
    if rc1 == 2:
        report["build_errors"] = build_errors(out1)
        done(False, build_failure(out1, "test patch does not build against base"))
        return after_check(report, step, run_dir, patch_override, meta, timeout)
    before = report["before"]["cases"] = openthread_cases(out1)
    if not any(s in FAILED for s in before.values()):
        return done(False, "no case failed before the fix")

    print("\n  Step 2: applying the upstream fix")
    patch_text = Path(patch_override).read_text() if patch_override else local_fix_patch(meta)
    (run_dir / "fix.diff").write_text(patch_text)
    rca, outa, _ = step("git apply", ["git", "-C", "/testbed", "apply",
                                      "/embedeval-run/fix.diff"], "apply.log", 300)
    if rca != 0:
        return done(False, f"the fix does not apply: {outa.strip()[:300]}")

    print("\n  Step 3: observing with the fix")
    rc2, out2, s2 = step("run_tests", ["run_tests"], "after.log", timeout)
    report["after"] = {"rc": rc2, "seconds": round(s2), "log": str(Path(report["logs"]) / "after.log")}
    if rc2 == 2:
        return done(False, build_failure(out2, "the fix does not build in our profile (infra or era issue)"))
    after = report["after"]["cases"] = openthread_cases(out2)
    report.update(observe_lists(before, after))
    if not report["fail_to_pass"]:
        return done(False, "no case failed before and passed after")
    if report["regressed"]:
        return done(False, f"cases regressed with the fix: {report['regressed']}")
    done(True, f"{len(report['fail_to_pass'])} fail_to_pass, "
               f"{len(report['pass_to_pass'])} pass_to_pass")


def validate_apptainer(instance_id: str, patch_override: Path | None, timeout: int,
                       verbose: bool) -> bool:
    meta = instances.load_metadata(instance_id)
    image = au.instance_image(instance_id)
    print(f"\n{'=' * 70}\n{instance_id}  ({image})\n{'=' * 70}")
    cmake = meta.get("pipeline") == "cmake"     # OpenThread: no board or scenario
    if not cmake:
        print(f"  {meta['test_path']}/{meta['scenario']} on {meta['platform']}")
    meanings = OPENTHREAD_MEANING if cmake else RUN_TESTS_MEANING
    observe = cmake and json.loads(
        (instances.instance_dir(instance_id) / "instance.json").read_text()).get("observe")
    report = {"instance_id": instance_id, "image": str(image),
              **({} if cmake else {"platform": meta["platform"],
                                   "scenario": f"{meta['test_path']}/{meta['scenario']}"}),
              **({"observe": True} if observe else {}),
              "valid": False,
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
        if observe:
            observe_instance(meta, report, step, run_dir, patch_override, timeout)
            return _finish(instance_id, report)
        want = json.loads((instances.instance_dir(instance_id) / "instance.json").read_text()) if cmake else {}
        compile_oracle = want.get("oracle") == "compile"
        print("\n  Step 1: expecting run_tests to " + ("fail to BUILD" if compile_oracle else "FAIL")
              + " on the image as built")
        rc1, out1, s1 = step("run_tests", ["run_tests"], "before.log", timeout)
        report["before"] = {"rc": rc1, "meaning": meanings.get(rc1, str(rc1)),
                            "seconds": round(s1), "summary": summary(out1)}
        for line in summary(out1):
            print(f"      {line}")
        if compile_oracle:
            symbol = None
            if rc1 != 2:
                why = "the test target builds before the fix: not a compile oracle"
            elif build_failure(out1, "build") != "build":
                why = "the before run failed in setup, not in the build"
            elif not (symbol := compile_attribution(out1, want.get("compile_symbols", []))):
                why = "build fails before the fix for an unrelated reason"
            report["before"]["build_errors"] = build_errors(out1)
            if symbol is None:
                print(f"  INVALID: {why}")
                report["reason"] = why
                return _finish(instance_id, report)
            report["before"]["compile_symbol"] = symbol
        elif rc1 != 1:
            why = {0: "tests PASSED before the fix -- the instance does not detect the bug",
                   3: "nothing ran: the scenario is filtered or skipped on this board",
                   2: ("the build or configure failed: not a valid before (no test ran)" if cmake
                       else "twister did not run (environment problem)")}.get(
                       rc1, f"run_tests ended with {rc1}")
            print(f"  INVALID: {why}")
            report["reason"] = why
            return _finish(instance_id, report)

        if cmake and not compile_oracle:
            cases1 = openthread_cases(out1)
            report["before"]["cases"] = cases1
            # Failing before the fix is not held against a pass_to_pass test;
            # it is recorded as the baseline's health.
            report["before"]["pass_to_pass_failing"] = [
                n for n in want["pass_to_pass"] if cases1.get(n) != "passed"]
            why = openthread_problem(want, cases1, after=False)
            if why:
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
        report["after"] = {"rc": rc2, "meaning": meanings.get(rc2, str(rc2)),
                           "seconds": round(s2), "summary": summary(out2)}
        for line in summary(out2):
            print(f"      {line}")
        if rc2 != 0:
            why = {1: "tests still fail with the fix",
                   3: "nothing ran with the fix",
                   2: ("the build or configure failed with the fix" if cmake
                       else "twister did not run (environment problem)")}.get(
                       rc2, f"run_tests ended with {rc2}")
            print(f"  INVALID: {why}")
            report["reason"] = why
            return _finish(instance_id, report)

        if cmake:
            report["after"]["cases"] = cases2 = openthread_cases(out2)
            why = openthread_problem(want, cases2, after=True)
            if why:
                print(f"  INVALID: {why}")
                report["reason"] = why
                return _finish(instance_id, report)

        reason = (f"fails to build before the fix ({symbol}), passes after" if compile_oracle
                  else "fails before the fix, passes after")
        print(f"\n  VALID: {reason}")
        report["valid"] = True
        report["reason"] = reason
        return _finish(instance_id, report)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        report["reason"] = f"error: {type(e).__name__}: {e}"
        return _finish(instance_id, report)
    finally:
        au.remove(run_dir)


def _finish(instance_id: str, report: dict) -> bool:
    name = "observed.json" if report.get("observe") else "validation.json"
    (instances.instance_dir(instance_id) / name).write_text(
        json.dumps(report, indent=2) + "\n")
    return report["valid"]


def validate(instance_id: str, patch_override: Path | None = None, timeout: int = 600,
             verbose: bool = False) -> bool:
    meta = instances.load_metadata(instance_id)
    if meta.get("pipeline") in au.APPTAINER_PIPELINES:
        return validate_apptainer(instance_id, patch_override, max(timeout, 1800), verbose)
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

    def dexec(cmd: str, t: int, label: str | None = None):
        # With --verbose the container output streams live; otherwise it is
        # captured and only shown when a step fails. Either way each step
        # announces itself and reports how long it took, so a step that is
        # silently skipped or suspiciously fast is visible.
        if label:
            print(f"    {label} ...", end="", flush=True)
        t0 = time.time()
        r = sh(["docker", "exec", cid, "bash", "-c", cmd], timeout=t, quiet=not verbose)
        if label:
            print(f" {time.time() - t0:.1f}s (rc={r.returncode})")
        return r

    try:
        # --- Step 1: tests must FAIL on the unfixed code -------------------
        print("\n  Step 1: expecting tests to FAIL at the base commit")
        dexec(f"cd /testbed && {build_cmd}", 900, "build")
        r1 = dexec("cd /testbed && run_tests", timeout, "run_tests")
        if r1.returncode == 0:
            print("  FAIL: tests PASSED before the fix -- the instance does not "
                  "isolate the bug (test patch may not apply, or the bug is absent)")
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
            print(f"  FAIL: git apply failed: {ra.stderr.strip()[:300]}")
            return False

        cleaned = cfg["clean_paths"]
        if cleaned:
            dexec("rm -rf " + " ".join(cleaned), 60, f"clean {' '.join(cleaned)}")
        else:
            print(f"    clean: nothing to remove for {meta['project']}")
        rb = dexec(f"cd /testbed && {build_cmd}", 900, "rebuild")
        if rb.returncode != 0:
            print(f"  FAIL: build failed after applying the fix (rc={rb.returncode})")
            print("  " + ((rb.stdout or "") + (rb.stderr or ""))[-400:].replace("\n", "\n  "))
            return False

        r2 = dexec("cd /testbed && run_tests", timeout, "run_tests")
        if r2.returncode != 0:
            print(f"  FAIL: tests still failing after the fix (rc={r2.returncode})")
            print("  " + ((r2.stdout or "") + (r2.stderr or ""))[-400:].replace("\n", "\n  "))
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
