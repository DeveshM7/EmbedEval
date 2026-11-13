#!/usr/bin/env python3
"""
run_tests -- rebuild NuttX, run ostest to the end and report a status for each
ostest test. (The agent's runner when the PR's tests are hidden; installed over
the image's own runner in the run's overlay by harness/hidden_tests.py.)

    run_tests                      build, run ostest, report every test
    run_tests semaphore getopt     also require that these tests ran
    run_tests --build-only         build only

The build is `make -j$(nproc)` in /testbed, incremental. The image's own runner
(/opt/embedeval/run_tests_base.sh, with no test names left in it) boots the build
and runs the whole ostest suite; its log is split here into one test per
"user_main: <name> test" banner. A name you give matches (case-insensitively)
every test whose name contains it; each must match a test that ran.

Exit status:
    0  the build succeeded, ostest ran to the end with no error, and every test
       you named ran
    1  the build failed, a test reported an error or an assertion failed, ostest
       did not finish, or a test you named did not run
    2  timed out (a hang or an infinite loop in your change)
"""

import re
import subprocess
import sys
from pathlib import Path

BASE = "/opt/embedeval/run_tests_base.sh"    # the image's own runner (banners cut)
TESTBED = "/testbed"

START = re.compile(r"^user_main: (?!ERROR)(.+ tests?)\s*$")
EXITING = "user_main: Exiting"
# cond_test's signaler can run before the waiter on SMP; the image's runners do not
# count that line (see their comment).
HARMLESS = "ERROR waiter state"
ASSERT_TEXT = ("Assertion failed", "ASSERTION")


def split_output(text):
    """(ostest log lines, the runner's own `run_tests:` lines)."""
    log, notes = [], []
    for line in text.splitlines():
        (notes if line.startswith("run_tests: ") else log).append(line)
    return log, notes


def split_tests(log):
    """[(name, status, error lines)] in log order; status is passed, failed or
    unfinished (the log ends inside the test, without `user_main: Exiting`)."""
    tests = []
    current = None
    finished = False
    for line in log:
        match = START.match(line)
        if match:
            current = [match.group(1), []]
            tests.append(current)
            continue
        if EXITING in line:
            finished = True
            current = None
            continue
        if current is not None and (("ERROR" in line and HARMLESS not in line)
                                    or any(t in line for t in ASSERT_TEXT)):
            current[1].append(line.strip())
    result = []
    for i, (name, errors) in enumerate(tests):
        if errors:
            status = "failed"
        elif i == len(tests) - 1 and not finished:
            status = "unfinished"
        else:
            status = "passed"
        result.append((name, status, errors))
    return result


def not_run(names, tests):
    """The names that match no test that ran."""
    ran = [t[0].lower() for t in tests]
    missing = []
    for n in names:
        want = n.lower()
        if want.startswith("user_main: "):
            want = want[len("user_main: "):]
        if not any(want in r for r in ran):
            missing.append(n)
    return missing


def run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)


def main():
    args = sys.argv[1:]
    build_only = "--build-only" in args
    names = [a for a in args if a != "--build-only"]
    bad = [a for a in names if a.startswith("-")]
    if bad:
        print("run_tests: options are fixed for this instance, not {}; only --build-only and "
              "ostest test names can be given.".format(" ".join(bad)))
        return 1
    build = run("make -j$(nproc)", shell=True, cwd=TESTBED)
    if build.returncode != 0:
        print("\n".join(build.stdout.decode(errors="replace").splitlines()[-40:]))
        print("==> run_tests: BUILD FAILED")
        return 1
    print("build ok")
    if build_only:
        print("==> run_tests: BUILT")
        return 0
    result = run(["bash", BASE])
    text = result.stdout.decode(errors="replace")
    log, notes = split_output(text)
    tests = split_tests(log)
    print("--- ostest: {} tests ---".format(len(tests)))
    for name, status, errors in tests:
        if status == "passed":
            print("  passed   " + name)
        elif status == "failed":
            print("  FAILED   " + name)
            for line in errors[:5]:
                print("      " + line)
        else:
            print("  NOT FINISHED " + name)
    for line in notes:
        print(line)
    finished = any(EXITING in line for line in log)
    if not finished:
        print("--- ostest did not finish: the end of its log ---")
        print("\n".join(log[-30:]))
    missing = not_run(names, tests) if names else []
    for n in missing:
        print("did not run: " + n)
    if missing:
        print("tests that ran: " + (", ".join(t[0] for t in tests) or "none"))
    if result.returncode == 2:
        print("==> run_tests: TIMED OUT")
        return 2
    ok = (result.returncode == 0 and not missing
          and all(t[1] == "passed" for t in tests))
    print("==> run_tests: " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
