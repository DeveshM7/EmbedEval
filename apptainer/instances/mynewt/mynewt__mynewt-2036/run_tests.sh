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


def sync_mbedtls():
    package = Path("/testbed/crypto/mbedtls/pkg.yml")
    if not package.exists():
        return
    block = re.search(
        r"(?m)^repository\.mbedtls:[ \t]*\n((?:[ \t]+[^\n]*(?:\n|$))*)",
        package.read_text(),
    )
    if not block:
        return
    fields = dict(re.findall(r"(?m)^\s+(\w+):\s*([^\s#]+)", block[1]))
    fields = {key: value.strip("\"'") for key, value in fields.items()}
    if fields.get("type") != "github" or not fields.get("vers", "").endswith("-commit"):
        raise ValueError("unsupported repository.mbedtls dependency specification")
    # Newt's -commit suffix names a Git revision directly, not a Newt release.
    revision = fields["vers"][:-len("-commit")]
    url = f"https://github.com/{fields['user']}/{fields['repo']}.git"
    repo = Path("/project/repos/mbedtls")
    repo.mkdir(parents=True, exist_ok=True)
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, timeout=120)
    if not (repo / ".git").exists():
        git("init", "-q")
    ref = "refs/embedeval/" + revision
    found = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", ref],
                           capture_output=True).returncode == 0
    if not found:
        git("fetch", "-q", "--depth", "1", url, revision)
        git("update-ref", ref, "FETCH_HEAD")
    git("checkout", "-q", "--detach", "--force", ref)


def classify(output, returncode, expected):
    passed = set(re.findall(r"^\[pass\] (\S+)", output, re.MULTILINE))
    failed = set(re.findall(r"^\[FAIL\] (\S+)", output, re.MULTILINE))
    if returncode < 0:
        # Old tests (before 2019) use plain assert(): a failing case kills the
        # whole binary with SIGABRT and prints no [FAIL] line. The cases that
        # never reported are the ones that did not pass; a validator holding
        # the exact inventory still rejects a crash that fails the wrong set.
        failed |= set(expected) - passed - failed
        returncode = 1
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
    linker_diagnostic = False
    missing_function = False
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if "undefined reference to" in line:
            match = re.fullmatch(
                r"(?:/[^:]+/ld:\s*)?(.+?):\d+:\([^\n]+\): undefined reference to [`‘]([^'’]+)['’]",
                line,
            )
            if not match or not (
                (match[1] == source or match[1].endswith("/" + source))
                and match[2] == api
            ):
                return None
            diagnostic = diagnostic or line
            linker_diagnostic = True
            continue
        if linker_diagnostic and re.fullmatch(r"collect2: error: ld returned \d+ exit status", line):
            continue
        if not re.search(r"(?:fatal )?error:", line):
            continue
        match = re.match(r"(.+?):(\d+)(?::(\d+))?:\s*(?:fatal )?error:\s*(.*)", line)
        if not match:
            return None
        path, row, column, message = match.groups()
        if not (path == source or path.endswith("/" + source)):
            return None
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(api)}(?![A-Za-z0-9_])", message):
            if diagnostic is None:
                diagnostic = line
            if re.search(rf"implicit declaration of function ['‘]{re.escape(api)}['’]", message):
                missing_function = True
            continue
        # GCC reports later assignments from an undeclared function as int-to-
        # pointer conversions. Attribute only direct calls at this error's location.
        excerpt = re.match(rf"\s*{row}\s*\| (.*)$", lines[index + 1]) if index + 1 < len(lines) else None
        assignment = re.fullmatch(
            rf"\s*[A-Za-z_]\w*\s*(?P<operator>=)\s*{re.escape(api)}\s*\([^();]*\);\s*",
            excerpt[1],
        ) if excerpt else None
        if not (
            missing_function
            and re.fullmatch(
                r"assignment to ['‘][^'’]+\*['’] from ['‘]int['’] "
                r"makes pointer from integer without a cast \[-Werror=int-conversion\]",
                message,
            )
            and assignment
            and column
            and int(column) == assignment.start("operator") + 1
        ):
            return None
    return diagnostic


parser = argparse.ArgumentParser()
parser.add_argument("--baseline", action="store_true")
parser.add_argument("--install-deps", action="store_true")
parser.add_argument("--baseline-test-path", help="original unit-test package before relocation")
args = parser.parse_args()
if args.baseline_test_path and not args.baseline:
    parser.error("--baseline-test-path requires --baseline")
if args.install_deps:
    sync_mbedtls()
    raise SystemExit(0)
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
phase = "dependency setup"
try:
    sync_mbedtls()
    phase = "build"
    # Fresh build and runtime directories prevent stale binaries or simulated flash.
    shutil.rmtree("/project/bin", ignore_errors=True)
    build = subprocess.run(
        ["newt", "test", "@apache-mynewt-core/" + (args.baseline_test_path or meta["test_path"])],
        cwd="/project",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
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
        phase = "test execution"
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
            encoding="utf-8",
            errors="backslashreplace",
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
except (subprocess.CalledProcessError, ValueError, KeyError) as exc:
    result["error"] = f"{phase} failed: {exc}"

result["exit_code"] = code
Path("/tmp/mynewt-result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result), flush=True)
raise SystemExit(code)
PY
