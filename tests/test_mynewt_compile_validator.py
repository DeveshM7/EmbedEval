from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_instance


SOURCE = "kernel/os/selftest/src/testcases/os_msys_test_cases.c"
API = "os_msys_get_free"
DIAGNOSTIC = f"{SOURCE}:41:3: error: implicit declaration of function '{API}'"
META = {
    "project": "mynewt",
    "docker_image": "mynewt-test-image",
    "build_command": "newt build native",
    "structured_test_results": "/tmp/mynewt-result.json",
    "baseline_tests": ["baseline_a", "baseline_b"],
    "fail_to_pass": ["fixed_a"],
    "pass_to_pass": ["stable_a"],
    "failure_mode": "compile",
    "compile_test_source": SOURCE,
    "missing_api": API,
}


def report(**changes):
    result = {
        "failure_mode": "compile",
        "build_ok": False,
        "compile_failure": True,
        "compile_test_source": SOURCE,
        "missing_api": API,
        "diagnostic": DIAGNOSTIC,
        "passed": [],
        "failed": [],
        "missing": [],
        "unexpected": [],
        "error": None,
        "exit_code": 1,
    }
    result.update(changes)
    return result


class StructuredResultTests(unittest.TestCase):
    def test_old_runtime_report_without_mode_or_compile_flag_remains_valid(self):
        meta = {k: v for k, v in META.items() if k not in ("failure_mode", "compile_test_source", "missing_api")}
        old_report = {
            "build_ok": True,
            "passed": ["stable_a"],
            "failed": ["fixed_a"],
            "missing": [],
            "unexpected": [],
            "error": None,
            "exit_code": 1,
        }
        self.assertTrue(validate_instance.verify_structured_results(meta, old_report, "before")[0])
        old_report.update(passed=["stable_a", "fixed_a"], failed=[], exit_code=0)
        self.assertTrue(validate_instance.verify_structured_results(meta, old_report, "after")[0])

    def test_compile_before_accepts_matched_evidence(self):
        self.assertTrue(validate_instance.verify_structured_results(META, report(), "before")[0])

    def test_compile_before_rejects_wrong_source_token_or_diagnostic(self):
        invalid = [
            {"compile_test_source": "wrong.c"},
            {"missing_api": "other_api"},
            {"diagnostic": None},
            {"diagnostic": ""},
            {"diagnostic": f"{SOURCE}:41:3: warning: implicit declaration of function '{API}'"},
            {"diagnostic": f"wrong.c:41:3: error: implicit declaration of function '{API}'"},
            {"diagnostic": f"{SOURCE}:41:3: error: implicit declaration of function 'other_api'"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertFalse(validate_instance.verify_structured_results(META, report(**changes), "before")[0])

    def test_compile_before_rejects_runtime_inventory_and_unrelated_error(self):
        invalid = [
            {"passed": ["stable_a"]}, {"failed": ["fixed_a"]},
            {"missing": ["fixed_a"]}, {"unexpected": ["other"]},
            {"error": "compiler crash"}, {"build_ok": True},
            {"compile_failure": False}, {"exit_code": 3},
            {"failure_mode": "runtime"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertFalse(validate_instance.verify_structured_results(META, report(**changes), "before")[0])

    def test_compile_after_requires_complete_exact_pass_inventory(self):
        good = report(build_ok=True, compile_failure=False, diagnostic=None,
                      passed=["fixed_a", "stable_a"], exit_code=0)
        self.assertTrue(validate_instance.verify_structured_results(META, good, "after")[0])
        for changes in ({"passed": ["fixed_a"]}, {"passed": ["fixed_a", "stable_a", "extra"]},
                        {"failed": ["fixed_a"]}, {"compile_failure": True},
                        {"build_ok": False}, {"exit_code": 1}):
            with self.subTest(changes=changes):
                self.assertFalse(validate_instance.verify_structured_results(
                    META, {**good, **changes}, "after")[0])

    def test_baseline_requires_exact_inventory_and_no_failure(self):
        good = report(failure_mode="runtime", build_ok=True, compile_failure=False,
                      diagnostic=None, passed=["baseline_a", "baseline_b"], exit_code=0)
        self.assertTrue(validate_instance.verify_structured_results(META, good, "baseline")[0])
        old_report = {k: v for k, v in good.items()
                      if k not in ("failure_mode", "compile_failure", "diagnostic")}
        self.assertTrue(validate_instance.verify_structured_results(META, old_report, "baseline")[0])
        for changes in ({"passed": ["baseline_a"]},
                        {"passed": ["baseline_a", "baseline_b", "extra"]},
                        {"failed": ["baseline_b"]}, {"compile_failure": True},
                        {"build_ok": False}, {"error": "failed"}, {"exit_code": 1}):
            with self.subTest(changes=changes):
                self.assertFalse(validate_instance.verify_structured_results(
                    META, {**good, **changes}, "baseline")[0])


class BaselineContainerTests(unittest.TestCase):
    def run_validation(self, baseline_exit):
        calls = []
        original = report(build_ok=True, compile_failure=False, diagnostic=None,
                          passed=["fixed_a", "stable_a"], exit_code=0)
        baseline = report(failure_mode="runtime", build_ok=True, compile_failure=False,
                          diagnostic=None, passed=["baseline_a", "baseline_b"],
                          exit_code=0)
        reports = {"baseline-cid": baseline, "original-cid": original}
        runs = iter(["original-cid", "baseline-cid"])

        def fake_sh(cmd, timeout=120, quiet=True):
            calls.append(cmd)
            if cmd[:3] == ["docker", "run", "-d"]:
                return subprocess.CompletedProcess(cmd, 0, next(runs) + "\n", "")
            if cmd[:2] == ["docker", "exec"]:
                cid = cmd[2]
                if cmd[3] == "cat":
                    return subprocess.CompletedProcess(cmd, 0, json.dumps(reports[cid]), "")
                shell = cmd[-1]
                if "run_tests --baseline" in shell:
                    return subprocess.CompletedProcess(cmd, baseline_exit, "", "")
                if "run_tests" in shell and cid == "original-cid":
                    if not any("git apply" in part for call in calls for part in call):
                        reports[cid] = report()
                        return subprocess.CompletedProcess(cmd, 1, "", "")
                    reports[cid] = original
                    return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            patch_file = Path(tmp) / "fix.diff"
            patch_file.write_text("fix")
            with patch.object(validate_instance.instances, "load_metadata", return_value=META), \
                 patch.object(validate_instance.build_config, "config", return_value={"platform": None, "clean_paths": []}), \
                 patch.object(validate_instance, "sh", side_effect=fake_sh):
                valid = validate_instance.validate("mynewt__test", patch_override=patch_file)
        return valid, calls

    def test_baseline_uses_second_container_detached_at_parent(self):
        valid, calls = self.run_validation(0)
        self.assertTrue(valid)
        run_calls = [cmd for cmd in calls if cmd[:3] == ["docker", "run", "-d"]]
        self.assertEqual(len(run_calls), 2)
        self.assertEqual(run_calls[0], run_calls[1])
        self.assertIn("mynewt-test-image", run_calls[0])
        self.assertTrue(any(cmd[:3] == ["docker", "exec", "baseline-cid"] and
                            "cd /testbed && git checkout --detach HEAD^" in cmd[-1]
                            for cmd in calls))
        self.assertTrue(any(cmd[:3] == ["docker", "exec", "baseline-cid"] and
                            "run_tests --baseline" in cmd[-1] for cmd in calls))
        self.assertFalse(any(cmd[:3] == ["docker", "exec", "original-cid"] and
                             "checkout" in cmd[-1] for cmd in calls))
        self.assertTrue(any(cmd[:3] == ["docker", "exec", "original-cid"] and
                            "git apply" in cmd[-1] for cmd in calls))
        self.assertIn(["docker", "rm", "-f", "baseline-cid"], calls)

    def test_nonzero_baseline_exit_stops_before_pre_fix_check(self):
        valid, calls = self.run_validation(3)
        self.assertFalse(valid)
        self.assertFalse(any(cmd[:3] == ["docker", "exec", "original-cid"] for cmd in calls))
        self.assertIn(["docker", "rm", "-f", "baseline-cid"], calls)


if __name__ == "__main__":
    unittest.main()
