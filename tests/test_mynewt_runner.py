"""Unit checks for the inline Python in the native Mynewt runner."""

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "templates/mynewt/run_tests.sh"
SOURCE = "kernel/os/selftest/src/testcases/os_msys_test_cases.c"
API = "os_msys_get_free"
ERROR = (
    f"{SOURCE}:41:3: error: implicit declaration of function '{API}'"
)


def runner_classifiers():
    python = runner_python()
    definitions = python.split("parser = argparse.ArgumentParser()", 1)[0]
    namespace = {}
    exec(definitions, namespace)
    return namespace


def runner_python():
    return SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


def run_runner(build_output, build_code, *, baseline=False, timeout=False, runtime_output="[pass] case_a\n"):
    python = runner_python()
    metadata = {
        "failure_mode": "compile",
        "compile_test_source": SOURCE,
        "missing_api": API,
        "test_path": "kernel/os/selftest",
        "baseline_tests": ["case_a"],
        "fail_to_pass": ["case_a"],
        "pass_to_pass": [],
    }
    saved = {}

    def run(command, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(command, 180)
        if command[0] == "newt":
            return SimpleNamespace(stdout=build_output, stderr="", returncode=build_code)
        return SimpleNamespace(stdout=runtime_output, stderr="", returncode=0)

    def write_result(_path, content):
        saved.update(json.loads(content))

    with (
        patch("pathlib.Path.read_text", return_value=json.dumps(metadata)),
        patch("pathlib.Path.write_text", write_result),
        patch("pathlib.Path.rglob", return_value=[Path("/tmp/native.elf")]),
        patch("pathlib.Path.mkdir"),
        patch("shutil.rmtree"),
        patch("subprocess.run", side_effect=run),
        patch.object(sys, "argv", ["run_tests.sh"] + (["--baseline"] if baseline else [])),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        with unittest.TestCase().assertRaises(SystemExit) as raised:
            exec(python, {"__name__": "__main__"})
    return raised.exception.code, saved


class CompileDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.classifiers = runner_classifiers()

    def diagnostic(self, output):
        return self.classifiers["compile_diagnostic"](output, SOURCE, API)

    def test_matches_expected_source_and_api_error(self):
        self.assertEqual(self.diagnostic(f"compiling\n{ERROR}\n"), ERROR)

    def test_rejects_warning_for_expected_source_and_api(self):
        warning = ERROR.replace("error:", "warning:")
        self.assertIsNone(self.diagnostic(warning))

    def test_matches_fatal_error_for_missing_header(self):
        header = "sys/missing_api.h"
        line = f"{SOURCE}:8:10: fatal error: {header}: No such file or directory"
        self.assertEqual(self.classifiers["compile_diagnostic"](line, SOURCE, header), line)

    def test_rejects_error_in_another_source(self):
        self.assertIsNone(self.diagnostic(ERROR.replace(SOURCE, "other/os_msys_test_cases.c")))

    def test_rejects_error_for_another_api(self):
        self.assertIsNone(self.diagnostic(ERROR.replace(API, "os_msys_get_used")))

    def test_rejects_longer_api_token(self):
        self.assertIsNone(self.diagnostic(ERROR.replace(API, API + "_v2")))

    def test_rejects_compiler_crash_even_with_matching_error(self):
        self.assertIsNone(self.diagnostic(ERROR + "\ninternal compiler error: Segmentation fault\n"))

    def test_rejects_unqualified_build_failure(self):
        self.assertIsNone(self.diagnostic("newt test failed with exit code 1"))

    def test_missing_signature_is_not_an_expected_regression(self):
        self.assertIsNone(self.classifiers["compile_diagnostic"](ERROR, None, API))

    def test_retains_exact_runtime_inventory_after_successful_build(self):
        classify = self.classifiers["classify"]
        expected = ["case_a", "case_b"]
        self.assertEqual(classify("[pass] case_a\n", 0, expected)[0], 3)
        self.assertEqual(classify("[pass] case_a\n[pass] case_b\n[pass] stray\n", 0, expected)[0], 3)
        self.assertEqual(classify("[pass] case_a\n[pass] case_b\n", 0, expected)[0], 0)

    def test_compile_failure_writes_evidence_and_returns_one(self):
        code, result = run_runner(ERROR, 1)
        self.assertEqual(code, 1)
        self.assertEqual(result["failure_mode"], "compile")
        self.assertFalse(result["build_ok"])
        self.assertTrue(result["compile_failure"])
        self.assertEqual(result["compile_test_source"], SOURCE)
        self.assertEqual(result["missing_api"], API)
        self.assertEqual(result["diagnostic"], ERROR)
        self.assertEqual(result["exit_code"], 1)

    def test_positive_newt_failure_code_can_still_be_a_compile_regression(self):
        code, result = run_runner(ERROR, 2)
        self.assertEqual(code, 1)
        self.assertTrue(result["compile_failure"])

    def test_baseline_never_accepts_compile_failure(self):
        code, result = run_runner(ERROR, 1, baseline=True)
        self.assertEqual(code, 3)
        self.assertEqual(result["failure_mode"], "runtime")
        self.assertFalse(result["compile_failure"])
        self.assertIsNone(result["diagnostic"])

    def test_unrelated_build_failure_returns_three_without_evidence(self):
        code, result = run_runner(ERROR.replace(SOURCE, "other.c"), 1)
        self.assertEqual(code, 3)
        self.assertFalse(result["compile_failure"])
        self.assertIsNone(result["diagnostic"])

    def test_timeout_writes_code_two(self):
        code, result = run_runner("", 0, timeout=True)
        self.assertEqual(code, 2)
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(result["compile_failure"])

    def test_successful_build_runs_exact_inventory(self):
        code, result = run_runner("Executing test: selftest", 0)
        self.assertEqual(code, 0)
        self.assertTrue(result["build_ok"])
        self.assertFalse(result["compile_failure"])
        self.assertEqual(result["passed"], ["case_a"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["unexpected"], [])

    def test_successful_build_with_missing_case_returns_three(self):
        code, result = run_runner("Executing test: selftest", 0, runtime_output="")
        self.assertEqual(code, 3)
        self.assertEqual(result["missing"], ["case_a"])

    def test_successful_build_with_unexpected_case_returns_three(self):
        code, result = run_runner(
            "Executing test: selftest", 0,
            runtime_output="[pass] case_a\n[pass] stray\n",
        )
        self.assertEqual(code, 3)
        self.assertEqual(result["unexpected"], ["stray"])


if __name__ == "__main__":
    unittest.main()
