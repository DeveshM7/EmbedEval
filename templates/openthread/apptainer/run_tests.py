#!/usr/bin/env python3
"""
run_tests -- build and run this instance's OpenThread tests with CMake.

    run_tests                  build (incrementally) and run, report each test
    run_tests --build-only     configure and build only; used once while the
                               image is built

The suite, target, configure arguments and the expected test names are fixed per
instance in /opt/embedeval/instance.json. With "observe": true there are no
expected names: "targets" are built, and every case of the "gtest" binaries and
every "unit" ctest name is reported. The build directory is /testbed/build,
configured once and reused, so after an edit only what changed is rebuilt.

    gtest   the target's binary runs once with --gtest_output=json; every
            expected case is mapped. A crash marks the running case `error` and
            the cases after it `not_run`; a hang is `timeout`, not `error`.
    unit    each expected ctest name runs as `ctest -R '^name$'`; a non-zero
            exit is `failed`, a ctest timeout `timeout`.

Exit status:
    0  every expected test passed
    1  an expected test failed or errored
    2  the configure or the build failed; no test ran
    3  nothing ran: an expected test is missing from the output
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

CONFIG = Path("/opt/embedeval/instance.json")
TESTBED = Path("/testbed")
BUILD = TESTBED / "build"
LOG = BUILD / "run_tests.log"
RUN_TIMEOUT = 600       # seconds for the gtest binary
CTEST_TIMEOUT = 300     # seconds per unit test


def gtest_json(data: dict) -> dict[str, str]:
    """name -> status from a --gtest_output=json report."""
    out = {}
    for suite in data.get("testsuites", []):
        for t in suite.get("testsuite", []):
            if t.get("failures"):
                status = "failed"
            elif t.get("status") == "RUN" and t.get("result") == "COMPLETED":
                status = "passed"
            else:
                status = "not_run"      # skipped or filtered out
            out[f"{suite['name']}.{t['name']}"] = status
    return out


_GTEST_LINE = re.compile(r"^\[\s*(RUN|OK|FAILED|SKIPPED)\s*\]\s+([^\s,]+\.[^\s,]+)")


def gtest_stdout(text: str) -> dict[str, str]:
    """name -> status from the console output, for a run that wrote no report
    (a crash or a timeout). A case that started and never finished is `error`."""
    out = {}
    for line in text.splitlines():
        m = _GTEST_LINE.match(line)
        if m:
            out[m.group(2)] = {"RUN": "error", "OK": "passed", "FAILED": "failed",
                               "SKIPPED": "not_run"}[m.group(1)]
    return out


def exit_code(statuses: list[str]) -> int:
    if any(s in ("failed", "error", "timeout") for s in statuses):
        return 1
    return 0 if statuses and all(s == "passed" for s in statuses) else 3


def run(cmd: list[str], timeout: int | None = None, env: dict | None = None) -> tuple[int, str]:
    """(returncode, output). A signal gives a negative code; a timeout, 'timeout'."""
    try:
        r = subprocess.run(cmd, cwd=TESTBED, capture_output=True, text=True,
                           errors="replace", timeout=timeout, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        return "timeout", out.decode(errors="replace") if isinstance(out, bytes) else out


def build(cfg: dict) -> bool:
    """Configure (only when there is no configured build directory) and build.
    Configuring is upstream's own `cfg["configure"] <cmake_args>` (script/
    cmake-build), which also builds the targets it is given.
    The output goes to the log; the end of it is shown when this fails."""
    BUILD.mkdir(exist_ok=True)
    # The compiler's temporary files go on the build directory's disk: the
    # container's /tmp is a small in-memory filesystem (16 MB on Scholar), which
    # a parallel rebuild of the core after a widely included header changes fills.
    tmp = BUILD / "tmp"
    tmp.mkdir(exist_ok=True)
    if cfg.get("observe"):
        targets = cfg["targets"]
    else:
        targets = [cfg["target"]]
        if cfg["suite"] == "unit":      # the other ctest names are separate executables
            targets += [n for n in cfg["fail_to_pass"] + cfg["pass_to_pass"] if n not in targets]
    steps = []
    if not (BUILD / "build.ninja").exists():
        env = {**os.environ, "TMPDIR": str(tmp), "OT_CMAKE_BUILD_DIR": str(BUILD),
               "OT_CMAKE_NINJA_TARGET": " ".join(targets)}
        steps.append((cfg["configure"].split() + cfg["cmake_args"], env))
    steps.append((["cmake", "--build", "build", "--target", *targets],
                  {**os.environ, "TMPDIR": str(tmp)}))
    LOG.write_text("")
    for cmd, env in steps:
        print("$ " + " ".join(cmd), flush=True)
        rc, out = run(cmd, env=env)
        with LOG.open("a") as fh:
            fh.write(out)
        if rc != 0:
            print("\n".join(out.splitlines()[-40:]))
            return False
    return True


def run_gtest(cfg: dict) -> tuple[dict[str, str], str]:
    binaries = [p for p in BUILD.rglob(cfg["target"]) if p.is_file()]
    if not binaries:
        return {}, f"no binary named {cfg['target']} under build/"
    report = BUILD / f"{cfg['target']}.gtest.json"
    report.unlink(missing_ok=True)
    cmd = [str(binaries[0]), f"--gtest_output=json:{report}"]
    print("$ " + " ".join(cmd), flush=True)
    rc, out = run(cmd, RUN_TIMEOUT)
    try:
        return gtest_json(json.loads(report.read_text())), out
    except (OSError, ValueError):       # no report: it crashed or timed out
        cases = gtest_stdout(out)
        if rc == "timeout":             # a hang is not a failure of the case
            cases = {n: "timeout" if s == "error" else s for n, s in cases.items()}
        return cases, out


def run_unit(names: list[str]) -> tuple[dict[str, str], str]:
    out, shown = {}, []
    for name in names:
        cmd = ["ctest", "--test-dir", "build", "-R", f"^{name}$", "--output-on-failure",
               "--timeout", str(CTEST_TIMEOUT)]
        print("$ " + " ".join(cmd), flush=True)
        rc, text = run(cmd)
        out[name] = ("not_run" if "No tests were found" in text
                     else "passed" if rc == 0
                     else "timeout" if rc == "timeout" or "***Timeout" in text
                     else "failed")
        if out[name] != "passed":
            shown.append(f"--- {name} ---\n" + "\n".join(text.splitlines()[-40:]))
    return out, "\n".join(shown)


def observe(cfg: dict) -> int:
    """Observe mode (instance.json "observe": true): no expected lists. Every
    case of each selected gtest binary and every selected unit executable is
    reported, in the same results block."""
    statuses, detail = {}, []
    for target in cfg["gtest"]:
        cases, text = run_gtest({"target": target})
        statuses.update(cases)
        detail.append(text)
    if cfg["unit"]:
        cases, text = run_unit(cfg["unit"])
        statuses.update(cases)
        detail.append(text)
    if any(s in ("failed", "error", "timeout") for s in statuses.values()):
        print("\n--- output of the failing run (last 60 lines) ---")
        print("\n".join("\n".join(detail).splitlines()[-60:]))
    print("\n--- openthread: results")
    for name, status in statuses.items():
        print(f"  {status} {name}")
    print("--- end ---")
    rc = exit_code(list(statuses.values()))
    print({0: "==> run_tests: PASSED", 1: "==> run_tests: FAILED",
           3: "==> run_tests: NOTHING RAN"}[rc])
    return rc


def main() -> int:
    cfg = json.loads(CONFIG.read_text())
    os.chdir(TESTBED)
    if not build(cfg):
        # "Build failure" is what evaluate_patches.py looks for to set build_ok.
        print("==> run_tests: Build failure: the configure or the build failed, no test ran")
        return 2
    if "--build-only" in sys.argv[1:]:
        return 0

    if cfg.get("observe"):
        return observe(cfg)
    expected = cfg["fail_to_pass"] + cfg["pass_to_pass"]
    if cfg["suite"] == "gtest":
        observed, detail = run_gtest(cfg)
    else:
        observed, detail = run_unit(expected)
    statuses = {n: observed.get(n, "not_run") for n in expected}
    if any(s in ("failed", "error", "timeout") for s in statuses.values()):
        print("\n--- output of the failing run (last 60 lines) ---")
        print("\n".join(detail.splitlines()[-60:]))
    print("\n--- openthread: results")
    for name, status in statuses.items():
        print(f"  {status} {name}")
    print("--- end ---")

    rc = exit_code(list(statuses.values()))
    print({0: "==> run_tests: PASSED", 1: "==> run_tests: FAILED",
           3: "==> run_tests: NOTHING RAN (an expected test is missing from the output)"}[rc])
    return rc


if __name__ == "__main__":
    sys.exit(main())
