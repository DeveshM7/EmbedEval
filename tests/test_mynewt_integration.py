import importlib.util
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


if __name__ == "__main__":
    unittest.main()
