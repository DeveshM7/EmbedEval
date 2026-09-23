import importlib.util
import json
from pathlib import Path
import unittest


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


if __name__ == "__main__":
    unittest.main()
