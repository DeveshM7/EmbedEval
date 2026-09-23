from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scripts_instances = load_module("scripts_instances", "scripts/instances.py")
build_config = load_module("mynewt_build_config", "scripts/build_config.py")
harness_paths = load_module("harness_paths", "harness/paths.py")
projects = load_module("mynewt_projects", "harness/projects.py")
validate_instance = load_module("mynewt_validate_instance", "scripts/validate_instance.py")


class MynewtProjectConfigurationTests(unittest.TestCase):
    def test_existing_project_order_is_preserved(self):
        self.assertEqual(scripts_instances.PROJECTS[:3], ("zephyr", "nuttx", "riot"))
        self.assertEqual(harness_paths.PROJECTS[:3], ("zephyr", "nuttx", "riot"))

    def test_mynewt_instance_ids_are_supported(self):
        self.assertEqual(
            scripts_instances.split_instance("mynewt__mynewt-3680"),
            ("mynewt", "3680"),
        )
        self.assertEqual(
            harness_paths.split_instance("mynewt__mynewt-3680"),
            ("mynewt", "3680"),
        )
        self.assertIn("mynewt", scripts_instances.PROJECTS)
        self.assertIn("mynewt", harness_paths.PROJECTS)

    def test_mynewt_uses_amd64_without_qemu_cleanup(self):
        self.assertEqual(build_config.config("mynewt")["platform"], "linux/amd64")
        self.assertEqual(projects.config("mynewt")["docker_platform"], "linux/amd64")
        self.assertFalse(projects.config("mynewt")["needs_qemu_cleanup"])


class MynewtInstanceDefinitionTests(unittest.TestCase):
    def test_all_three_instances_have_current_metadata(self):
        for pr in (2809, 3299, 3680):
            instance_id = f"mynewt__mynewt-{pr}"
            path = REPO_ROOT / "docker" / "instances" / instance_id / "metadata.json"
            with self.subTest(instance_id=instance_id):
                meta = json.loads(path.read_text())
                self.assertEqual(meta["project"], "mynewt")
                self.assertEqual(meta["instance_id"], instance_id)
                self.assertEqual(meta["docker_platform"], "linux/amd64")
                self.assertTrue(meta["fail_to_pass"])
                self.assertTrue(meta["pass_to_pass"])
                self.assertTrue(meta["files_changed_by_fix"])
                self.assertEqual(meta["build_command"], "true")
                self.assertEqual(
                    meta["structured_test_results"],
                    "/tmp/mynewt-result.json",
                )

    def test_2809_preserves_compiler_compatibility_flag(self):
        path = (
            REPO_ROOT
            / "docker/instances/mynewt__mynewt-2809/metadata.json"
        )
        meta = json.loads(path.read_text())
        self.assertIn("-Wno-error=stringop-overflow", meta["compatibility_cflags"])

    def test_3680_describes_both_required_boundaries(self):
        path = (
            REPO_ROOT
            / "docker/instances/mynewt__mynewt-3680/metadata.json"
        )
        statement = json.loads(path.read_text())["problem_statement"]
        self.assertIn("JSON_ATTR_MAX", statement)
        self.assertIn("JSON_ERR_STRLONG", statement)

    def test_3299_requests_three_way_gold_patch_application(self):
        path = (
            REPO_ROOT
            / "docker/instances/mynewt__mynewt-3299/metadata.json"
        )
        meta = json.loads(path.read_text())
        self.assertTrue(meta["gold_patch_three_way"])


class StructuredResultValidationTests(unittest.TestCase):
    meta = {
        "fail_to_pass": ["suite/regression"],
        "pass_to_pass": ["suite/existing"],
    }

    @staticmethod
    def report(**overrides):
        report = {
            "build_ok": True,
            "passed": ["suite/existing"],
            "failed": ["suite/regression"],
            "missing": [],
            "unexpected": [],
            "error": None,
        }
        report.update(overrides)
        return report

    def test_before_accepts_only_the_expected_regression_failure(self):
        valid, _ = validate_instance.verify_structured_results(
            self.meta, self.report(), "before"
        )
        self.assertTrue(valid)

    def test_before_rejects_a_failed_build(self):
        valid, _ = validate_instance.verify_structured_results(
            self.meta, self.report(build_ok=False), "before"
        )
        self.assertFalse(valid)


    def test_before_rejects_the_wrong_failed_test(self):
        valid, _ = validate_instance.verify_structured_results(
            self.meta,
            self.report(failed=["suite/other"], missing=["suite/regression"]),
            "before",
        )
        self.assertFalse(valid)

    def test_before_rejects_a_missing_test(self):
        valid, _ = validate_instance.verify_structured_results(
            self.meta, self.report(failed=[], missing=["suite/regression"]), "before"
        )
        self.assertFalse(valid)

    def test_after_accepts_all_expected_tests_passing(self):
        valid, _ = validate_instance.verify_structured_results(
            self.meta,
            self.report(
                passed=["suite/existing", "suite/regression"],
                failed=[],
            ),
            "after",
        )
        self.assertTrue(valid)

    def test_after_rejects_a_remaining_regression_failure(self):
        valid, _ = validate_instance.verify_structured_results(
            self.meta, self.report(), "after"
        )
        self.assertFalse(valid)

    def test_after_rejects_an_unexpected_test(self):
        valid, _ = validate_instance.verify_structured_results(
            self.meta,
            self.report(
                passed=["suite/existing", "suite/regression", "suite/unexpected"],
                failed=[],
                unexpected=["suite/unexpected"],
            ),
            "after",
        )
        self.assertFalse(valid)


class ValidatorPatchApplicationTests(unittest.TestCase):
    @staticmethod
    def metadata(**overrides):
        meta = {
            "project": "mynewt",
            "docker_image": "embedeval-mynewt-test:latest",
            "build_command": "true",
        }
        meta.update(overrides)
        return meta

    @staticmethod
    def result(cmd, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    def test_three_way_gold_patch_uses_three_way_git_apply(self):
        commands = []
        run_count = 0

        def fake_sh(cmd, timeout=120, quiet=True):
            nonlocal run_count
            commands.append(cmd)
            if cmd[:2] == ["docker", "run"]:
                return self.result(cmd, stdout="container-id\n")
            if cmd[:2] == ["docker", "exec"] and cmd[-1].endswith("run_tests"):
                run_count += 1
                return self.result(cmd, returncode=1 if run_count == 1 else 0)
            return self.result(cmd)

        with tempfile.TemporaryDirectory() as tmp:
            patch_path = Path(tmp) / "fix.diff"
            patch_path.write_text("test patch\n")
            with (
                mock.patch.object(
                    validate_instance.instances,
                    "load_metadata",
                    return_value=self.metadata(gold_patch_three_way=True),
                ),
                mock.patch.object(
                    validate_instance.build_config,
                    "config",
                    return_value={"platform": "linux/amd64", "clean_paths": []},
                ),
                mock.patch.object(validate_instance, "sh", side_effect=fake_sh),
                redirect_stdout(io.StringIO()),
            ):
                valid = validate_instance.validate("mynewt__mynewt-test", patch_path)

        self.assertTrue(valid)
        docker_exec_commands = [
            cmd[-1] for cmd in commands if cmd[:2] == ["docker", "exec"]
        ]
        self.assertIn(
            "cd /testbed && git apply --3way /tmp/fix.diff",
            docker_exec_commands,
        )

    def test_verbose_apply_failure_reports_the_failure_without_crashing(self):
        def fake_sh(cmd, timeout=120, quiet=True):
            if cmd[:2] == ["docker", "run"]:
                return self.result(cmd, stdout="container-id\n")
            if cmd[:2] == ["docker", "exec"] and cmd[-1].endswith("run_tests"):
                return self.result(cmd, returncode=1)
            if cmd[:2] == ["docker", "exec"] and "git apply" in cmd[-1]:
                return self.result(cmd, returncode=1, stdout=None, stderr=None)
            return self.result(cmd)

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            patch_path = Path(tmp) / "fix.diff"
            patch_path.write_text("test patch\n")
            with (
                mock.patch.object(
                    validate_instance.instances,
                    "load_metadata",
                    return_value=self.metadata(),
                ),
                mock.patch.object(
                    validate_instance.build_config,
                    "config",
                    return_value={"platform": "linux/amd64", "clean_paths": []},
                ),
                mock.patch.object(validate_instance, "sh", side_effect=fake_sh),
                redirect_stdout(output),
            ):
                valid = validate_instance.validate(
                    "mynewt__mynewt-test", patch_path, verbose=True
                )

        self.assertFalse(valid)
        self.assertIn("FAIL: git apply failed:", output.getvalue())
        self.assertNotIn("AttributeError", output.getvalue())


if __name__ == "__main__":
    unittest.main()
