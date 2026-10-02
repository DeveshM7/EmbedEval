#!/usr/bin/env bash
# Build and execute one native Mynewt selftest package. The result parser
# rejects missing tests, unexpected tests, and crashed binaries.
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


def compile_diagnostic(output, source, api):
    if not source or not api:
        return None
    if re.search(r"internal compiler error", output, re.IGNORECASE):
        return None
    diagnostic = None
    for line in output.splitlines():
        if not re.search(r"(?:fatal )?error:", line):
            continue
        match = re.match(r"(.+?):\d+(?::\d+)?:\s*(?:fatal )?error:\s*(.*)", line)
        if not match:
            return None
        path, message = match.groups()
        if not (path == source or path.endswith("/" + source)) or not re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(api)}(?![A-Za-z0-9_])", message
        ):
            return None
        if diagnostic is None:
            diagnostic = line
    return diagnostic


parser = argparse.ArgumentParser()
parser.add_argument("--baseline", action="store_true")
args = parser.parse_args()
meta = json.loads(Path("/opt/benchmark/metadata.json").read_text())
mode = "runtime" if args.baseline else meta.get("failure_mode", "runtime")
expected = (
    meta["baseline_tests"]
    if args.baseline
    else meta["fail_to_pass"] + meta["pass_to_pass"]
)
result = {
    "failure_mode": mode,
    "build_ok": False,
    "compile_failure": False,
    "compile_test_source": meta.get("compile_test_source"),
    "missing_api": meta.get("missing_api"),
    "diagnostic": None,
    "passed": [],
    "failed": [],
    "missing": [],
    "unexpected": [],
    "error": None,
}
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
    if mode == "compile" and build.returncode > 0 and "Executing test: " not in output:
        diagnostic = compile_diagnostic(
            output, meta.get("compile_test_source"), meta.get("missing_api")
        )
        if diagnostic is not None:
            result["compile_failure"] = True
            result["diagnostic"] = diagnostic
            code = 1
        else:
            result["error"] = "build failed without expected compile diagnostic"
    elif (
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
