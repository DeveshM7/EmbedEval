#!/usr/bin/env python3
"""
run_tests -- build and run the Mynewt test packages you name, for the native
simulator. (The agent's runner when the PR's tests are hidden; installed over the
image's own runner in the run's overlay by harness/hidden_tests.py.)

    run_tests kernel/os/selftest                    one test package
    run_tests kernel/os/selftest fs/fcb2/selftest   several at once

A test package is a folder under /testbed with a pkg.yml (selftest, or test in
older trees). Each is built with `newt test` in the newt project at /project
(incrementally: /project/bin is kept) and its test program is run directly, so
that every test case is reported, passing or not. A test that hangs is stopped.

Exit status (the worst of the packages, in this order):
    2  a build or a test run timed out
    3  a build failed, a package ran no test case, or dependencies could not be set up
    1  a test case failed, or the test program crashed
    0  every test case passed
    4  no valid test package was given (before anything runs)
"""

import json
import re
import subprocess
import sys
import types
from pathlib import Path

BASE = Path("/opt/embedeval/run_tests_base.sh")      # the image's own runner (bash + python)
CONFIG = Path("/opt/benchmark/metadata.json")        # mcumgr_commit and test_observer only
TESTBED = Path("/testbed")
PROJECT = Path("/project")
RUNTIME = Path("/tmp/mynewt-runtime")
BUILD_TIMEOUT = 480
RUN_TIMEOUT = 90


def load_base():
    """The functions of the image's runner (dependency checkout, the gdb observer):
    its python program up to the argument parsing."""
    text = BASE.read_text()
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.rstrip().endswith("<<'PY'")) + 1
    end = max(i for i, l in enumerate(lines) if l == "PY")
    code = "\n".join(lines[start:end])
    code = code.split("\nparser = argparse.ArgumentParser()")[0]
    mod = types.ModuleType("run_tests_base")
    exec(compile(code, str(BASE), "exec"), mod.__dict__)
    return mod


def usage(msg):
    print(msg)
    print("usage: run_tests <test package> [<test package> ...]\n"
          "  <test package>: a folder under /testbed with a pkg.yml, e.g. kernel/os/selftest")
    return 4


def resolve(arg):
    """The package folder relative to /testbed, or None."""
    path = Path(arg)
    if path.is_absolute():
        try:
            path = path.relative_to(TESTBED)
        except ValueError:
            return None
    rel = path.as_posix().rstrip("/")
    if not rel or rel.startswith("-") or ".." in path.parts or rel == ".":
        return None
    return rel if (TESTBED / rel / "pkg.yml").is_file() else None


def parse_cases(output):
    """(passed, failed) case names, as the image's runner reads them."""
    passed = set(re.findall(r"\[pass\] (\S+)", output))
    failed = set(re.findall(r"\[FAIL\] (\S+)", output))
    return sorted(passed - failed), sorted(failed)


def test_binary(output):
    """The one ELF test program `newt test` reported executing, or None."""
    binaries = []
    for name in re.findall(r"^Executing test: (\S+)$", output, re.MULTILINE):
        binary = Path(name).resolve()
        if PROJECT / "bin" in binary.parents and binary.is_file():
            with binary.open("rb") as handle:
                if handle.read(4) == b"\x7fELF":
                    binaries.append(binary)
    return binaries[0] if len(binaries) == 1 else None


def decode(data):
    return (data or b"").decode("utf-8", errors="backslashreplace")


def run_package(base, cfg, index, pkg):
    """Returns (code, verdict) for one package."""
    print("\n--- {} ---".format(pkg), flush=True)
    cmd = ["newt", "test", "@apache-mynewt-core/" + pkg]
    print("$ " + " ".join(cmd), flush=True)
    try:
        build = subprocess.run(cmd, cwd=str(PROJECT), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("  the build timed out after {}s".format(BUILD_TIMEOUT))
        return 2, "TIMED OUT"
    output = decode(build.stdout)
    binary = test_binary(output)
    if binary is None:
        print("\n".join(output.splitlines()[-30:]))
        print("  {}: BUILD FAILED (no test program was built)".format(pkg))
        return 3, "BUILD FAILED"
    runtime = RUNTIME / str(index)
    runtime.mkdir(parents=True, exist_ok=True)
    command = [str(binary)]
    if cfg.get("test_observer") == "testutil-gdb":
        command = base.observe_testutil(binary, runtime)
    try:
        test = subprocess.run(command, cwd=str(runtime), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("  the test program timed out after {}s".format(RUN_TIMEOUT))
        return 2, "TIMED OUT"
    output = decode(test.stdout)
    passed, failed = parse_cases(output)
    for name in passed:
        print("  passed   " + name)
    for name in failed:
        print("  FAILED   " + name)
    code = 0
    if test.returncode < 0:
        print("  the test program was killed by signal {} after its last reported case"
              .format(-test.returncode))
        print("\n".join(output.splitlines()[-15:]))
        code = 1
    if not passed and not failed:
        print("  no test case ran")
        print("\n".join(output.splitlines()[-15:]))
        return 3, "NO TEST CASE"
    if failed:
        code = 1
    return code, "PASSED" if code == 0 else "FAILED"


def main():
    args = sys.argv[1:]
    if not args:
        return usage("run_tests: name the test package(s) to run.")
    packages = []
    for a in args:
        pkg = resolve(a)
        if pkg is None:
            return usage("run_tests: {} is not a test package (a folder under /testbed with "
                         "a pkg.yml).".format(a))
        packages.append(pkg)
    base = load_base()
    cfg = json.loads(CONFIG.read_text())
    try:
        base.sync_dependencies(cfg)
    except Exception as exc:    # a failed checkout or an unsupported dependency
        print("run_tests: dependency setup failed: {}".format(exc))
        print("==> run_tests: BUILD FAILED")
        return 3
    results = [run_package(base, cfg, i, p) for i, p in enumerate(packages)]
    codes = [c for c, _ in results]
    for code in (2, 3, 1):
        if code in codes:
            verdict = next(v for c, v in results if c == code)
            print("==> run_tests: " + verdict)
            return code
    print("==> run_tests: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
