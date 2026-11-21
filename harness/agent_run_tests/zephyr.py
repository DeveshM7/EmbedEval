#!/opt/venv/bin/python
"""
run_tests -- build and run the test scenarios you name, with twister, on this
instance's board. (The agent's runner when the PR's tests are hidden; installed
over the image's own runner in the run's overlay by harness/hidden_tests.py.)

    run_tests tests/kernel/common                       one test folder
    run_tests tests/lib/c_lib tests/my/new_test         several at once
    run_tests --build-only tests/kernel/common          build only

Each argument is a folder under /testbed holding a testcase.yaml (or
sample.yaml); every scenario in it that supports this board is built and run.
The board and the era's twister options are fixed. Output goes to
/testbed/twister-out, reused on every call (--no-clean), so after an edit only
what changed is rebuilt.

Exit status:
    0  every test that ran passed
    1  a test failed, or the build or configuration failed
    2  twister did not run or wrote no report, or no valid folder was given
    3  nothing ran: the scenarios were filtered or skipped on this board
"""

import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

BASE = Path("/opt/embedeval/run_tests_base.py")   # the image's own runner
CONFIG = Path("/opt/embedeval/instance.json")      # board and twister options only


def load_base():
    mod = types.ModuleType("run_tests_base")
    exec(compile(BASE.read_text(), str(BASE), "exec"), mod.__dict__)
    return mod


def usage(msg: str) -> int:
    print(msg)
    print("usage: run_tests [--build-only] <test folder> [<test folder> ...]\n"
          "  e.g. run_tests tests/kernel/common  (a folder with a testcase.yaml)")
    return 2


def main() -> int:
    b = load_base()
    cfg = json.loads(CONFIG.read_text())
    args = sys.argv[1:]
    build_only = "--build-only" in args
    folders = [a.rstrip("/") for a in args if a != "--build-only"]
    if not folders:
        return usage("run_tests: name the test folder(s) to run.")
    bad = [a for a in folders if a.startswith("-")]
    if bad:
        return usage(f"run_tests: options are fixed for this instance, not {' '.join(bad)}; "
                     "only test folders can be given.")
    os.chdir(b.TESTBED)
    paths = []
    for f in folders:
        p = Path(f)
        rel = p.relative_to(b.TESTBED) if p.is_absolute() and b.TESTBED in p.parents else p
        if not (b.TESTBED / rel).is_dir():
            return usage(f"run_tests: no such folder: {f}")
        paths.append(str(rel))
    cmd = ["scripts/twister", "-p", cfg["platform"]]
    for p in paths:
        cmd += ["-T", p]
    cmd += ["-O", str(b.OUT), "--no-clean", "-v", "--inline-logs"] + cfg.get("twister_args", [])
    if build_only:
        cmd.append("--build-only")
    print("$ " + " ".join(cmd), flush=True)
    start = time.time()
    with open(b.LOG, "w") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
    tail = b.LOG.read_text(errors="replace").splitlines()
    if build_only:
        print("\n".join(tail[-5:]))
        return 0 if rc == 0 else 1

    suite, tests = b.results()
    print(f"\n--- twister: {' '.join(paths)} on {cfg['platform']} (twister rc={rc}) ---")
    for name, status, reason in tests:
        print(f"  {status:<9} {name}  {reason}".rstrip())
    # Console lines from this call's runs only (the output folder is reused).
    for h in b.OUT.rglob("handler.log"):
        if h.stat().st_mtime < start:
            continue
        for l in h.read_text(errors="replace").splitlines():
            s = l.strip()
            if s.startswith(("PASS -", "FAIL -", "SKIP -")) or "Assertion failed" in s \
                    or "ASSERTION FAIL" in s or "PROJECT EXECUTION" in s:
                print(f"  | {s}")
    print(f"--- suite: {suite} ---")

    statuses = {t[1] for t in tests} | set(suite.split(","))
    if suite == "no report":
        print("\n".join(tail[-25:]))
        print("==> run_tests: TWISTER DID NOT RUN (no report)")
        return 2
    if statuses & b.FAILING:
        for bl in b.OUT.rglob("build.log"):
            if bl.stat().st_mtime >= start and not any(t[1] == "passed" for t in tests):
                print(f"\n--- {bl.relative_to(b.TESTBED)} (last 30 lines) ---")
                print("\n".join(bl.read_text(errors="replace").splitlines()[-30:]))
        print("==> run_tests: FAILED")
        return 1
    if any(t[1] == "passed" for t in tests):
        print("==> run_tests: PASSED")
        return 0
    print("\n".join(tail[-10:]))
    print(f"==> run_tests: NOTHING RAN (suite {suite}: filtered or skipped on this board)")
    return 3


if __name__ == "__main__":
    sys.exit(main())
