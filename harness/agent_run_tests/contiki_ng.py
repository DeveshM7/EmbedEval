#!/usr/bin/env python3
"""
run_tests -- build and run the Contiki-NG tests you name. (The agent's runner
when the PR's tests are hidden; installed over the image's own runner in the run's
overlay by harness/hidden_tests.py. Python 3.5 compatible, like the image's
runner it reuses: the oldest images have no newer Python.)

    run_tests tests/08-native-runs/12-heapmem          a native unit-test folder
    run_tests tests/08-native-runs/01-ringbuf/test-ringbuf    one program of a folder
    run_tests <folder>:DEFINES=FOO=1                   with make options
    run_tests tests/07-simulation-base/01-hello.csc    a Cooja scenario
    run_tests --build-only <test>                      build only

A native unit-test folder has a Makefile; its programs are the names in its
CONTIKI_PROJECT (name `<folder>/<program>` to run only one). `:VAR=value`
options after a test go to make. A Cooja scenario is a .csc file in a tests/
suite folder with a Makefile; Cooja runs only where tools/cooja is checked out.
After an edit, only what changed is rebuilt.

Exit status:
    0  every test passed
    1  a test case failed, crashed or timed out
    2  a build failed
    3  a test did not run to completion, or reported no test case
    4  no valid test was given (before anything runs)
"""

import re
import sys
import types
from pathlib import Path

BASE = "/opt/embedeval/run_tests_base.py"     # the image's own runner

PROJECT_LINE = re.compile(r'^\s*CONTIKI_PROJECT\s*[:+]?=\s*(.+)$', re.M)


def load_base():
    mod = types.ModuleType("run_tests_base")
    with open(BASE) as fh:
        exec(compile(fh.read(), BASE, "exec"), mod.__dict__)
    return mod


def usage(msg):
    print(msg)
    print("usage: run_tests [--build-only] <test> [<test> ...]\n"
          "  <test>: a native unit-test folder (with a Makefile), <folder>/<program>, or a\n"
          "          Cooja scenario (a .csc file); append :VAR=value for make options")
    return 4


def programs(makefile):
    """The program names a Makefile's CONTIKI_PROJECT lines give."""
    names = []
    for value in PROJECT_LINE.findall(makefile.read_text(errors="replace")):
        for name in value.split("#")[0].split():
            if "$" not in name and name not in names:
                names.append(name)
    return names


def cooja_available(testbed):
    return any((testbed / "tools" / "cooja" / f).is_file() for f in ("build.gradle", "build.xml"))


def resolve(arg, testbed):
    """(units, error) for one argument: units in the shape of the image's
    bench.json, or an error message."""
    path, *options = arg.split(":")
    bad = [o for o in options if "=" not in o or o.startswith("=")]
    if bad:
        return None, "{}: make options are written :VAR=value".format(arg)
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.relative_to(testbed)
        except ValueError:
            return None, "{} is not under {}".format(path, testbed)
    rel = p.as_posix().rstrip("/")
    if not rel or rel.startswith("-") or ".." in p.parts:
        return None, "{} is not a test".format(arg)
    if rel.endswith(".csc") and (testbed / rel).is_file():
        folder = p.parent.as_posix()
        if options:
            return None, "{}: make options do not apply to a Cooja scenario".format(arg)
        if not (testbed / folder / "Makefile").is_file():
            return None, "{}: its folder has no Makefile".format(rel)
        if not cooja_available(testbed):
            return None, "{}: Cooja is not set up in this instance (no tools/cooja)".format(rel)
        return [{"id": rel, "kind": "cooja", "directory": folder, "suite_directory": folder,
                 "scenario": p.name, "make_options": [], "submodules": []}], None
    if (testbed / rel).is_dir():
        if not (testbed / rel / "Makefile").is_file():
            return None, "{} has no Makefile".format(rel)
        names = programs(testbed / rel / "Makefile")
        if not names:
            return None, ("{} names no program (no CONTIKI_PROJECT in its Makefile): "
                          "give <folder>/<program>".format(rel))
        folder = rel
    elif (testbed / p.parent / "Makefile").is_file() and rel != p.name:
        folder, names = p.parent.as_posix(), [p.name]
    else:
        return None, "{} is neither a folder with a Makefile nor <folder>/<program>".format(arg)
    units = []
    for name in names:
        units.append({"id": folder + "/" + name + (":" + ":".join(options) if options else ""),
                      "kind": "native", "directory": folder, "binary": name,
                      "make_options": list(options),
                      "suite_directory": Path(folder).parent.as_posix(),
                      "submodules": [], "cases": []})
    return units, None


def run_native(b, unit, lines):
    """(failed, incomplete) after building and running one native unit."""
    binary = b.native_binary(unit)
    output, done, timed_out, rc = b.run_native_binary(
        binary, b.log_path(unit, "run"), b.TESTBED / unit["directory"])
    observed, parsed_done = b.parse_native(output)
    failed = incomplete = False
    for description, status in observed.items():
        lines.append("  {} {}: {}".format(status, unit["id"], description))
        failed |= status == "failed"
    if not (done and parsed_done):
        if timed_out:
            lines.append("  timeout {}".format(unit["id"]))
            failed = True
        elif rc is not None and rc < 0:
            name = b.attribute_crash(unit, binary)
            lines.append("  error {}: crashed (signal {}) in case {}".format(
                unit["id"], -rc, name or "unknown"))
            failed = True
        else:
            lines.append("  incomplete {}: no completion marker".format(unit["id"]))
            incomplete = True
    elif not observed:
        lines.append("  incomplete {}: no test case reported".format(unit["id"]))
        incomplete = True
    return failed, incomplete


def main():
    args = sys.argv[1:]
    build_only = "--build-only" in args
    tests = [a for a in args if a != "--build-only"]
    if not tests:
        return usage("run_tests: name the test(s) to run.")
    b = load_base()
    units = []
    for t in tests:
        found, error = resolve(t, b.TESTBED)
        if error:
            return usage("run_tests: " + error)
        units += found
    b.LOGS.mkdir(parents=True, exist_ok=True)
    lines = []
    build_failed = failed = incomplete = False
    for unit in units:
        if unit["kind"] == "native":
            if not b.build_native(unit):
                print("{}: native build failed; see {}".format(unit["id"], b.log_path(unit, "build")))
                build_failed = True
            elif not build_only:
                f, i = run_native(b, unit, lines)
                failed |= f
                incomplete |= i
        elif build_only:
            print("{}: a Cooja scenario builds when run; not precompiled".format(unit["id"]))
        else:
            statuses, complete, compile_failed = b.run_cooja(unit)
            verdict = statuses[unit["id"]]
            lines.append("  {} {}".format(verdict, unit["id"]))
            if compile_failed:
                print("{}: Cooja build failed; see {}".format(unit["id"], b.log_path(unit, "run")))
                build_failed = True
            elif verdict in ("failed", "error", "timeout"):
                failed = True
            elif verdict != "passed":
                incomplete = True
    if build_only:
        print("==> run_tests: " + ("BUILD FAILED" if build_failed else "BUILT"))
        return 2 if build_failed else 0
    print("\n--- contiki-ng: results")
    for line in lines:
        print(line)
    print("--- end ---")
    if build_failed:
        print("==> run_tests: BUILD FAILED")
        return 2
    if failed:
        print("==> run_tests: FAILED")
        return 1
    if incomplete or not lines:
        print("==> run_tests: INCOMPLETE")
        return 3
    print("==> run_tests: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
