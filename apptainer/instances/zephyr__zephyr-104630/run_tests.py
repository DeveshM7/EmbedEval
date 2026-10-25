#!/opt/venv/bin/python
"""
run_tests -- build and run this instance's test scenario with twister.

    run_tests                  build (incrementally) and run, report each test
    run_tests --build-only     build only; used once while the image is built

The board, scenario and era-specific twister options are fixed per instance in
/opt/embedeval/instance.json. Output goes to /testbed/twister-out, reused on
every call (--no-clean), so after an edit only what changed is rebuilt.

--no-clean rather than --test-only: on 2022-era twister a test whose last
build failed is skipped by --test-only, and an instance often starts from
exactly that state (the test uses an API the fix adds).

Exit status:
    0  every test that ran passed
    1  a test failed, or the build or configuration failed
    2  twister itself did not run or wrote no report
    3  nothing ran: the scenario was filtered or skipped on this board
"""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CONFIG = Path("/opt/embedeval/instance.json")
TESTBED = Path("/testbed")
OUT = TESTBED / "twister-out"
LOG = TESTBED / "twister-out.log"

FAILING = {"failed", "error", "blocked", "timeout", "fail", "block"}


def twister_command(cfg: dict, extra: list[str]) -> list[str]:
    """The scenario is given twice. Twister up to 2025 matches -s against
    "<path>/<scenario>"; by 2026 suites are named by the bare scenario and only
    that matches. Each era ignores the form it does not use, and -T limits the
    search to this suite, so both together select exactly this scenario."""
    tp, scen = cfg["test_path"], cfg["scenario"]
    return (["scripts/twister", "-p", cfg["platform"], "-T", tp,
             "-s", f"{tp}/{scen}", "-s", scen,
             "-O", str(OUT), "--no-clean", "-v", "--inline-logs"]
            + cfg.get("twister_args", []) + extra)


def results() -> tuple[str, list[tuple[str, str, str]]]:
    """(suite status, [(test, status, reason)]) from whatever report this era
    of twister writes: twister.json (2022-05 on, or --json-report from
    2021-01), else the suite-level twister.xml."""
    j = OUT / "twister.json"
    if j.exists():
        data = json.loads(j.read_text())
        suites = data.get("testsuites", [])
        tests, statuses = [], []
        for s in suites:
            if s.get("status"):
                statuses.append(str(s["status"]).lower())
            for t in s.get("testcases", []):
                name = t.get("identifier") or t.get("testcase") or "?"
                tests.append((name, str(t.get("status") or "none").lower(), t.get("reason") or ""))
        if statuses:
            suite = statuses[0] if len(set(statuses)) == 1 else ",".join(sorted(set(statuses)))
        else:  # 2021-style report: test entries only
            st = {t[1] for t in tests}
            suite = "failed" if st & FAILING else ("passed" if "passed" in st else "skipped")
        return suite, tests
    x = OUT / "twister.xml"
    if x.exists():
        tests = []
        for tc in ET.parse(x).getroot().iter("testcase"):
            status = "passed"
            for child in tc:
                if child.tag in ("failure", "error", "skipped"):
                    status = {"failure": "failed"}.get(child.tag, child.tag)
            tests.append((tc.get("name") or "?", status, ""))
        st = {t[1] for t in tests}
        return ("failed" if st & FAILING else "passed" if "passed" in st else "skipped"), tests
    return "no report", []


def handler_lines() -> list[str]:
    """Per-test PASS/FAIL lines from the console log -- the only per-test
    record from twister before 2021-01, and readable in every era."""
    lines = []
    for h in OUT.rglob("handler.log"):
        for l in h.read_text(errors="replace").splitlines():
            s = l.strip()
            if s.startswith(("PASS -", "FAIL -", "SKIP -")) or "Assertion failed" in s \
                    or "ASSERTION FAIL" in s or "PROJECT EXECUTION" in s:
                lines.append(s)
    return lines


def main() -> int:
    cfg = json.loads(CONFIG.read_text())
    extra = sys.argv[1:]
    os.chdir(TESTBED)
    cmd = twister_command(cfg, extra)
    print("$ " + " ".join(cmd), flush=True)
    with open(LOG, "w") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
    tail = LOG.read_text(errors="replace").splitlines()

    if "--build-only" in extra:
        print("\n".join(tail[-5:]))
        return 0 if rc == 0 else 1

    suite, tests = results()
    print(f"\n--- twister: {cfg['test_path']}/{cfg['scenario']} on {cfg['platform']} "
          f"(twister rc={rc}) ---")
    for name, status, reason in tests:
        print(f"  {status:<9} {name}  {reason}".rstrip())
    for l in handler_lines():
        print(f"  | {l}")
    print(f"--- suite: {suite} ---")

    statuses = {t[1] for t in tests} | set(suite.split(","))
    if suite == "no report":
        print("\n".join(tail[-25:]))
        print("==> run_tests: TWISTER DID NOT RUN (no report)")
        return 2
    if statuses & FAILING:
        # Show why: the end of the build log for a build/configuration failure.
        for b in OUT.rglob("build.log"):
            if suite in ("error", "failed") and not any(t[1] == "passed" for t in tests):
                print(f"\n--- {b.relative_to(TESTBED)} (last 30 lines) ---")
                print("\n".join(b.read_text(errors="replace").splitlines()[-30:]))
        print("==> run_tests: FAILED")
        return 1
    if any(t[1] == "passed" for t in tests) and suite in ("passed", "none"):
        print("==> run_tests: PASSED")
        return 0
    print("\n".join(tail[-10:]))
    print(f"==> run_tests: NOTHING RAN (suite {suite}: filtered or skipped on this board)")
    return 3


if __name__ == "__main__":
    sys.exit(main())
