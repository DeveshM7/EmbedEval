#!/usr/bin/env python3
"""
run_tests -- build and run the tests you name on RIOT's native board. (The
agent's runner when the PR's tests are hidden; installed over the image's own
runner in the run's overlay by harness/hidden_tests.py.)

    run_tests tests-core                      a unit-test group (tests/unittests/tests-core)
    run_tests tests/thread_basic              a test program (a folder under tests/)
    run_tests tests-core tests-fmt tests/xtimer_usleep    several at once
    run_tests --build-only tests-core         build only

A unit-test group passes when every test in it passes; a test program, when
its test script does. After an edit, only what changed is rebuilt.

Exit status:
    0  every test passed
    1  a test failed, or a build failed
    2  no valid test was given
"""

import json
import os
import sys
import types

BASE = "/opt/embedeval/run_tests_base.py"     # the image's own runner
CONFIG = "/opt/embedeval/instance.json"        # the board only


def load_base():
    mod = types.ModuleType("run_tests_base")
    with open(BASE) as fh:
        exec(compile(fh.read(), BASE, "exec"), mod.__dict__)
    return mod


def usage(msg):
    print(msg)
    print("usage: run_tests [--build-only] <test> [<test> ...]\n"
          "  <test>: a unit-test group, e.g. tests-core (a folder in tests/unittests),\n"
          "          or a test program folder, e.g. tests/thread_basic")
    return 2


def main():
    b = load_base()
    with open(CONFIG) as fh:
        cfg = json.load(fh)
    args = sys.argv[1:]
    build_only = "--build-only" in args
    names = [a.rstrip("/") for a in args if a != "--build-only"]
    if not names:
        return usage("run_tests: name the test(s) to run.")
    groups, programs = [], []
    for n in names:
        rel = n[len(b.TESTBED) + 1:] if n.startswith(b.TESTBED + "/") else n
        if rel.startswith("-"):
            return usage("run_tests: only tests can be given, not {}".format(n))
        group = rel.split("/")[-1] if rel.startswith(b.UNIT_TESTS + "/") else rel
        if "/" not in group and os.path.isdir(os.path.join(b.TESTBED, b.UNIT_TESTS, group)):
            groups.append(group)
        elif os.path.isfile(os.path.join(b.TESTBED, rel, "Makefile")):
            programs.append(rel)
        else:
            return usage("run_tests: {} is neither a unit-test group in {} nor a folder "
                         "with a Makefile".format(n, b.UNIT_TESTS))
    env = b.environment(offline=True)
    results = [b.run_group(g, [], cfg["board"], env, build_only) for g in groups]
    results += [b.run_program(p, cfg["board"], env, build_only) for p in programs]
    ok = all(results)
    print("==> run_tests: " + ("BUILT" if build_only and ok else "PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
