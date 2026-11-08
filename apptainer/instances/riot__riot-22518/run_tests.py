#!/usr/bin/env python3
"""
run_tests -- build and run this instance's tests on RIOT's native board.

    run_tests                build (incrementally) and run, report each test
    run_tests --build-only   build only; used while the instance is built

The board and tests are fixed in /opt/embedeval/instance.json. After an edit,
only what changed is rebuilt. A unit-test group passes when every test the
instance expects to pass does; a test program, when its test script does.

Exit status:
    0  every expected test passed
    1  a test failed, or a build failed
    3  nothing to run
"""

import json
import os
import re
import subprocess
import sys
import threading

CONFIG = "/opt/embedeval/instance.json"
TESTBED = "/testbed"
UNIT_TESTS = "tests/unittests"
BUILD_TIMEOUT, TEST_TIMEOUT, UNIT_TIMEOUT = 900, 600, 120
END_OF_UNITS = "</TestRun>"
NOT_SUITE = {"TestRun", "Statistics", "Tests", "Failures", "Location", "File", "Line",
             "Message", "Name"}


def environment(offline):
    env = dict(os.environ, RIOT_CI_BUILD="1", CC_NOCOLOR="1", BUILD_IN_DOCKER="0",
               CARGO_HOME="/opt/embedeval/cargo", GIT_CACHE_DIR="/opt/embedeval/gitcache",
               TMPDIR="/opt/embedeval/tmp")
    if offline:
        env["CARGO_NET_OFFLINE"] = "true"
    os.makedirs(env["TMPDIR"], exist_ok=True)
    return env


def run(cmd, env, timeout, stdin=None, until=None):
    """(exit code or 'timeout', output). Stops the command once `until` is printed."""
    proc = subprocess.Popen(cmd, cwd=TESTBED, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            universal_newlines=True, errors="replace", start_new_session=True)
    expired = threading.Event()

    def stop():
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass

    def expire():
        expired.set()
        stop()

    timer = threading.Timer(timeout, expire)
    timer.start()
    try:
        proc.stdin.write(stdin or "")
        proc.stdin.close()
    except BrokenPipeError:
        pass
    lines = []
    for line in proc.stdout:
        lines.append(line)
        if until and until in line:
            stop()
            break
    proc.stdout.close()
    rc = proc.wait()
    timer.cancel()
    stop()
    text = "".join(lines)
    return ("timeout" if expired.is_set() else 0 if until and until in text else rc), text


def parse_units(output):
    tests, suite, kind = {}, "", None
    for line in output.splitlines():
        line = line.strip()
        m = re.fullmatch(r"<(Test|FailedTest) id=\"\d+\">", line)
        if m:
            kind = m.group(1)
            continue
        m = re.fullmatch(r"<Name>(.*)</Name>", line)
        if m and kind:
            tests["{}.{}".format(suite, m.group(1))] = "passed" if kind == "Test" else "failed"
            kind = None
            continue
        m = re.fullmatch(r"<(\w+)>", line)
        if m and m.group(1) not in NOT_SUITE:
            suite = m.group(1)
    return tests


def make(folder, board, goal, extra=()):
    return ["make", "--no-print-directory", "-C", folder, "BOARD=" + board, "BUILD_IN_DOCKER=0",
            *extra, goal]


def tail(text, lines=40):
    print("\n".join("    | " + l for l in text.splitlines()[-lines:]))


def run_group(group, expected, board, env, build_only):
    """A group is built alone, in its own folder, with output naming every test."""
    print("--- unit-test group {} ---".format(group), flush=True)
    base = "{}/{}/bin/{}".format(TESTBED, UNIT_TESTS, group)
    extra = ["UNIT_TESTS=" + group, "BINDIRBASE=" + base]
    build_env = dict(env, CFLAGS=(env.get("CFLAGS", "") + " -DOUTPUT=OUTPUT_XML").strip())
    rc, text = run(make(UNIT_TESTS, board, "all", ["-j8", *extra]), build_env, BUILD_TIMEOUT)
    if rc != 0:
        print("  build failed" + (" (timeout)" if rc == "timeout" else ""))
        tail(text)
        return False
    if build_only:
        return True
    elf = "{}/{}/tests_unittests.elf".format(base, board)
    rc, text = run([elf], env, UNIT_TIMEOUT, stdin="s\n", until=END_OF_UNITS)
    tests = parse_units(text)
    for name, result in sorted(tests.items()):
        print("  {:7} {}".format(result, name))
    missing = [t for t in expected if t not in tests]
    for name in missing:
        print("  {:7} {}".format("not run", name))
    if END_OF_UNITS not in text:
        print("  the run did not finish" + (" (timeout)" if rc == "timeout" else " (crashed?)"))
        tail(text)
    want = expected or list(tests)
    return bool(want) and all(tests.get(t) == "passed" for t in want)


def run_program(folder, board, env, build_only):
    print("--- test program {} ---".format(folder), flush=True)
    rc, text = run(make(folder, board, "all", ["-j8"]), env, BUILD_TIMEOUT)
    if rc != 0:
        print("  build failed" + (" (timeout)" if rc == "timeout" else ""))
        tail(text)
        return False
    if build_only:
        return True
    # INSIDE_DOCKER: some programs otherwise hand "make test" to Docker
    rc, text = run(make(folder, board, "test", ["INSIDE_DOCKER=1"]), env, TEST_TIMEOUT)
    print("  " + {0: "passed", "timeout": "failed (timeout)"}.get(rc, "failed"))
    if rc != 0:
        tail(text)
    return rc == 0


def main():
    build_only = "--build-only" in sys.argv
    online = "--online" in sys.argv      # while the instance is built: may download
    with open(CONFIG) as fh:
        cfg = json.load(fh)
    env = environment(offline=not online)
    results = [run_group(g, tests, cfg["board"], env, build_only)
               for g, tests in sorted(cfg["units"].items())]
    results += [run_program(p, cfg["board"], env, build_only) for p in cfg["programs"]]
    if not results:
        print("==> run_tests: NOTHING RAN")
        return 3
    ok = all(results)
    print("==> run_tests: " + ("BUILT" if build_only and ok else "PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
