"""NuttX instances as Apptainer recipes: rendering, and how each tool picks
the Apptainer path. The images themselves are only built on a cluster node."""

import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "harness"))
import apptainer_util  # noqa: E402
import render_nuttx_instance as render  # noqa: E402
import validate_instance  # noqa: E402
import evaluate_patches  # noqa: E402
import run  # noqa: E402

DOCKER_INSTANCES = sorted((ROOT / "docker" / "instances").glob("nuttx__nuttx-*"))


def instance_meta(name: str) -> dict:
    return json.loads((ROOT / "docker" / "instances" / name / "metadata.json").read_text())


class RenderTests(unittest.TestCase):
    def test_docker_instances_render_unchanged(self):
        # The Docker path must not move: each committed instance re-renders
        # to the same files.
        self.assertTrue(DOCKER_INSTANCES)
        for inst in DOCKER_INSTANCES:
            with self.subTest(inst.name), tempfile.TemporaryDirectory() as tmp:
                out = render.render(json.loads((inst / "metadata.json").read_text()),
                                    inst / "test_patch.diff", Path(tmp), tag_suffix="")
                for name in ("Dockerfile", "run_tests.sh", "test_patch.diff", "metadata.json"):
                    self.assertEqual((out / name).read_text(), (inst / name).read_text(), name)

    def test_apptainer_recipe_carries_commits_and_config_overrides(self):
        src = ROOT / "docker" / "instances" / "nuttx__nuttx-8832"
        with tempfile.TemporaryDirectory() as tmp:
            out = render.render(instance_meta("nuttx__nuttx-8832"), src / "test_patch.diff",
                                Path(tmp), runtime="apptainer")
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["instance.def", "metadata.json", "run_tests.sh", "test_patch.diff"])
            recipe = (out / "instance.def").read_text()
            self.assertNotIn("@@", recipe)
            self.assertIn("git checkout -q 9a28cd2000af50ef15b8ecd08d09a1df34db71da", recipe)
            self.assertIn("git -C apps checkout -q 922f9932a6361cb6b25328e3995a4b6886e47d51",
                          recipe)
            # Each override is its own %post line (set -e), verified after it is set.
            self.assertIn("\n    printf '%s\\n' 'CONFIG_SCHED_LPWORK=y' >> .config\n"
                          "    grep -q '^CONFIG_SCHED_LPWORK=y$' .config\n", recipe)
            self.assertIn("echo /apps/ >> .git/info/exclude", recipe)
            self.assertEqual((out / "run_tests.sh").read_text(),
                             (src / "run_tests.sh").read_text())
            meta = json.loads((out / "metadata.json").read_text())
            self.assertEqual(meta["runtime"], "apptainer")
            self.assertEqual(meta["apptainer_image"], "nuttx__nuttx-8832.sif")
            self.assertNotIn("docker_image", meta)
            self.assertTrue(meta["clean_after_patch"])

    def test_unknown_runtime_is_refused(self):
        src = ROOT / "docker" / "instances" / "nuttx__nuttx-11889"
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "runtime"):
            render.render(instance_meta("nuttx__nuttx-11889"), src / "test_patch.diff",
                          Path(tmp), runtime="podman")


class ValidatorTests(unittest.TestCase):
    META = {"compile_test_source": "testing/ostest/sighand.c", "missing_api": "SIGSET_FMT"}

    def test_compile_diagnostic_names_the_missing_api_in_the_test_source(self):
        found = validate_instance.compile_diagnostic(
            self.META, "CC: sighand.c\nsighand.c:120:7: error: 'SIGSET_FMT' undeclared\n")
        self.assertIsNotNone(found)
        self.assertIsNone(validate_instance.compile_diagnostic(
            self.META, "sighelper.c:12:1: error: 'SIGSET_FMT' undeclared\n"))
        self.assertIsNone(validate_instance.compile_diagnostic(
            self.META, "sighand.c:120:7: error: 'other' undeclared\n"))
        linker = {"compile_test_source": "testing/ostest/nxevent.c", "missing_api": "nxevent_init"}
        self.assertIsNotNone(validate_instance.compile_diagnostic(
            linker, "ld: /testbed/apps/testing/ostest/nxevent.c:40: undefined reference to "
                    "`nxevent_init'\n"))

    def test_nuttx_apptainer_instances_take_the_apptainer_validator(self):
        meta = {"project": "nuttx", "runtime": "apptainer"}
        with patch.object(validate_instance.instances, "load_metadata", return_value=meta), \
                patch.object(validate_instance, "validate_nuttx_apptainer",
                             return_value=True) as nuttx:
            self.assertTrue(validate_instance.validate("nuttx__nuttx-1"))
        nuttx.assert_called_once()

    def test_kernel_fix_patches_apply_primary_then_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "kernel"
            repo.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git("init")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            source = repo / "sched.c"
            source.write_text("base\n")
            git("add", "sched.c")
            git("commit", "-m", "base")
            base = git("rev-parse", "HEAD")
            source.write_text("primary\n")
            git("commit", "-am", "primary")
            primary = git("rev-parse", "HEAD")
            source.write_text("followup\n")
            git("commit", "-am", "followup")
            followup = git("rev-parse", "HEAD")
            meta = {"project": "nuttx", "kernel_repo": "https://github.com/apache/nuttx",
                    "kernel_base_commit": base, "kernel_merge_commit": primary,
                    "files_changed_by_fix": ["sched.c"],
                    "extra_fix_prs": [{"kernel_pr": 9, "base_commit": primary,
                                       "head_commit": followup, "files_changed": ["sched.c"]}]}
            with patch.object(validate_instance.layout, "nuttx_clones", return_value=(repo, None)):
                patches = validate_instance.nuttx_fix_patches(meta, Path(tmp))
            self.assertEqual(len(patches), 2)
            git("checkout", "--detach", base)
            for _, text in patches:
                diff = Path(tmp) / "fix.diff"
                diff.write_text(text)
                git("apply", str(diff))
            self.assertEqual(source.read_text(), "followup\n")


class HarnessTests(unittest.TestCase):
    def test_nuttx_apptainer_instances_are_graded_in_apptainer(self):
        meta = {"project": "nuttx", "runtime": "apptainer"}
        with patch.object(evaluate_patches.paths, "load_metadata", return_value=meta), \
                patch.object(evaluate_patches, "evaluate_apptainer",
                             return_value={"grade": "pass"}) as graded:
            evaluate_patches.evaluate_patch("nuttx__nuttx-1", "claude-opus-4-6")
        graded.assert_called_once()

    def test_agent_environment_for_a_nuttx_apptainer_instance(self):
        made = {}

        class FakeEnvironment:
            def __init__(self, **kwargs):
                made.update(kwargs)

        fake = types.ModuleType("apptainer_env")
        fake.ApptainerEnvironment = FakeEnvironment
        meta = {"project": "nuttx", "runtime": "apptainer", "instance_id": "nuttx__nuttx-1"}
        image = Path("/work/images/instances/nuttx__nuttx-1.sif")
        with patch.dict(sys.modules, {"apptainer_env": fake}), \
                patch.object(apptainer_util, "instance_image", return_value=image):
            run.make_environment(meta)
        # No setup commit: the tests live in apps/, outside the kernel's git.
        self.assertEqual(made["setup_commands"], [])
        self.assertEqual(made["cwd"], "/testbed")
        self.assertEqual(made["image"], image)


if __name__ == "__main__":
    unittest.main()
