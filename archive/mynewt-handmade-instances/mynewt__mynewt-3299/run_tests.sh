#!/usr/bin/env bash
# Build and execute this instance's native Mynewt selftests. The embedded
# result parser rejects missing tests, unexpected tests, and crashed binaries.
set -euo pipefail

exec python3 - "$@" <<'PY'
import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path


def classify(output, returncode, expected):
    passed = set(re.findall(r"^\[pass\] (\S+)", output, re.MULTILINE))
    failed = set(re.findall(r"^\[FAIL\] (\S+)", output, re.MULTILINE))
    result = {
        "passed": sorted(passed - failed),
        "failed": sorted(failed),
        "missing": sorted(set(expected) - passed - failed),
        "unexpected": sorted((passed | failed) - set(expected)),
    }
    if result["missing"] or result["unexpected"] or returncode not in (0, 1):
        return 3, result
    if failed:
        return 1, result
    return (0 if returncode == 0 else 3), result


parser = argparse.ArgumentParser()
parser.add_argument("--baseline", action="store_true")
args = parser.parse_args()
meta = json.loads(Path("/opt/benchmark/metadata.json").read_text())
expected = (
    meta["baseline_tests"]
    if args.baseline
    else meta["fail_to_pass"] + meta["pass_to_pass"]
)
result = {"build_ok": False, "passed": [], "failed": [], "error": None}
code = 3
try:
    # Fresh build and runtime directories prevent stale binaries or simulated flash.
    shutil.rmtree("/project/bin", ignore_errors=True)
    build = subprocess.run(
        ["newt", "test", "@apache-mynewt-core/" + meta["test_path"]],
        cwd="/project",
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = build.stdout + build.stderr
    print(output, flush=True)
    binaries = list(Path("/project/bin").rglob("*.elf"))
    if (
        "Executing test: " not in output
        or len(binaries) != 1
        or build.returncode not in (0, 1)
    ):
        result["error"] = "build did not reach native test execution"
    else:
        result["build_ok"] = True
        # Newt hides stdout for passing suites, so execute the ELF directly to
        # capture every individual testcase in both passing and failing runs.
        runtime = Path("/tmp/mynewt-runtime")
        shutil.rmtree(runtime, ignore_errors=True)
        runtime.mkdir()
        test = subprocess.run(
            [str(binaries[0])],
            cwd=runtime,
            capture_output=True,
            text=True,
            timeout=90,
        )
        output = test.stdout + test.stderr
        print(output, flush=True)
        code, cases = classify(output, test.returncode, expected)
        result.update(cases)
        if code == 0 and build.returncode != 0:
            code = 3
            result["error"] = "Newt and direct execution disagree"
except subprocess.TimeoutExpired:
    code = 2
    result["error"] = "build or native test execution timed out"

result["exit_code"] = code
Path("/tmp/mynewt-result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result), flush=True)
raise SystemExit(code)
PY
