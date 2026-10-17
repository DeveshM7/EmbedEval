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
import build_instances  # noqa: E402
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
            self.assertIn('if [ "sim:nsh" = sim:nsh ] && [ "$(uname -m)" = aarch64 ]', recipe)
            self.assertIn("git checkout -q 9a28cd2000af50ef15b8ecd08d09a1df34db71da", recipe)
            self.assertIn("git -C apps checkout -q 922f9932a6361cb6b25328e3995a4b6886e47d51",
                          recipe)
            # Each override is its own %post line (set -e), verified after it is set.
            self.assertIn("\n    printf '%s\\n' 'CONFIG_SCHED_LPWORK=y' >> .config\n"
                          "    grep -q '^CONFIG_SCHED_LPWORK=y$' .config\n", recipe)
            self.assertEqual(recipe.count("make olddefconfig"), 2)
            self.assertEqual(recipe.count("CONFIG_SCHED_LPWORK=y' >> .config"), 2)
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

    def test_qemu_armv8a_instance_uses_board_recipe_and_console_runner(self):
        src = ROOT / "docker" / "instances" / "nuttx__nuttx-11889"
        meta = {**instance_meta("nuttx__nuttx-11889"), "sim_config": "qemu-armv8a:nsh_smp"}
        with tempfile.TemporaryDirectory() as tmp:
            out = render.render(meta, src / "test_patch.diff", Path(tmp), runtime="apptainer")
            recipe = (out / "instance.def").read_text()
            self.assertIn("./tools/configure.sh -a ./apps qemu-armv8a:nsh_smp", recipe)
            self.assertIn('if [ "qemu-armv8a:nsh_smp" = sim:nsh ] && '
                          '[ "$(uname -m)" = aarch64 ]', recipe)
            self.assertIn("qemu-system-aarch64", (out / "run_tests.sh").read_text())
            self.assertEqual(json.loads((out / "metadata.json").read_text())["sim_config"],
                             "qemu-armv8a:nsh_smp")

    def test_qemu_armv8a_build_uses_cross_toolchain_base(self):
        src = ROOT / "docker" / "instances" / "nuttx__nuttx-11889"
        meta = {**instance_meta("nuttx__nuttx-11889"), "sim_config": "qemu-armv8a:nsh_smp"}
        with tempfile.TemporaryDirectory() as tmp:
            out = render.render(meta, src / "test_patch.diff", Path(tmp), runtime="apptainer")
            image = Path(tmp) / "image.sif"
            image.write_bytes(b"image")
            base = Path(tmp) / "qemu-base.sif"
            with patch.object(build_instances.instances, "instance_dir", return_value=out), \
                 patch.object(build_instances.instances, "load_metadata", return_value=meta), \
                 patch.object(build_instances.layout, "nuttx_clones", return_value=(None, None)), \
                 patch.object(apptainer_util, "instance_image", return_value=image), \
                 patch.object(apptainer_util, "path", side_effect=lambda *p: Path(tmp).joinpath(*p)), \
                 patch.object(apptainer_util, "ensure_nuttx_base_image", return_value=base) as ensure, \
                 patch.object(apptainer_util, "build", return_value=True) as build:
                self.assertTrue(build_instances.build_nuttx_apptainer("nuttx__nuttx-11889"))
            self.assertEqual(ensure.call_args.args[1], "qemu-armv8a:nsh_smp")
            self.assertEqual(build.call_args.args[2]["BASE_IMAGE"], str(base))


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
        self.assertIsNotNone(validate_instance.compile_diagnostic(
            linker, "/testbed/apps/testing/ostest/nxevent.c:40:(.text.nxevent_test+0xf10): "
                    "undefined reference to `nxevent_init'\n"))

    def test_compile_diagnostic_accepts_header_added_by_fix(self):
        meta = {"compile_test_source": "testing/ostest/nxevent.c", "missing_api": "nxevent_init",
                "files_changed_by_fix": ["include/nuttx/event.h"]}
        error = "nxevent.c:26:10: fatal error: nuttx/event.h: No such file or directory\n"
        self.assertIsNotNone(validate_instance.compile_diagnostic(meta, error))
        self.assertIsNone(validate_instance.compile_diagnostic(
            {**meta, "files_changed_by_fix": ["include/nuttx/other.h"]}, error))

    def test_nuttx_apptainer_instances_take_the_apptainer_validator(self):
        meta = {"project": "nuttx", "runtime": "apptainer"}
        with patch.object(validate_instance.instances, "load_metadata", return_value=meta), \
                patch.object(validate_instance, "validate_nuttx_apptainer",
                             return_value=True) as nuttx:
            self.assertTrue(validate_instance.validate("nuttx__nuttx-1"))
        nuttx.assert_called_once()

    def test_compile_error_instance_that_builds_before_fix_is_run(self):
        meta = {"fail_type": "compile_error", "build_command": "make"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "image.sif"
            image.touch()
            with patch.object(validate_instance.instances, "load_metadata", return_value=meta), \
                    patch.object(validate_instance.instances, "instance_dir", return_value=root), \
                    patch.object(apptainer_util, "instance_image", return_value=image), \
                    patch.object(apptainer_util, "recipe_fingerprint", return_value="test"), \
                    patch.object(apptainer_util, "path", side_effect=lambda *p: root.joinpath(*p)), \
                    patch.object(apptainer_util, "exec_args", return_value=[]), \
                    patch.object(apptainer_util, "remove"), \
                    patch.object(validate_instance.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], 0, "", "")):
                self.assertFalse(validate_instance.validate_nuttx_apptainer(
                    "nuttx__nuttx-1", None, 30, False))
            report = json.loads((root / "validation.json").read_text())
            self.assertEqual(report["before"]["build_rc"], 0)
            self.assertEqual(report["before"]["rc"], 0)
            self.assertEqual(report["reason"],
                             "tests PASSED before the fix -- the instance does not isolate the bug")

    def test_build_error_in_changed_test_counts_as_failure_for_a_runtime_case(self):
        meta = {"project": "nuttx", "fail_type": "runtime_assert", "build_command": "make",
                "files_changed_by_test_patch": ["testing/ostest/nxevent.c",
                                                "testing/ostest/Makefile"]}
        cases = (("/testbed/apps/testing/ostest/nxevent.c:339:(.text.nxevent_test+0xa78): "
                  "undefined reference to `nxevent_clear'\n", "the primary fix does not apply"),
                 ("/testbed/sched/sched.c:1: error: broken\n", "the runtime test did not build (rc=2)"))
        for output, reason in cases:
            with self.subTest(reason), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                image = root / "image.sif"
                image.touch()
                steps = [subprocess.CompletedProcess([], 2, output, ""),
                         subprocess.CompletedProcess([], 1, "error: patch failed\n", "")]
                with patch.object(validate_instance.instances, "load_metadata", return_value=meta), \
                        patch.object(validate_instance.instances, "instance_dir", return_value=root), \
                        patch.object(validate_instance, "nuttx_fix_patches",
                                     return_value=[("primary", "diff\n")]), \
                        patch.object(apptainer_util, "instance_image", return_value=image), \
                        patch.object(apptainer_util, "recipe_fingerprint", return_value="test"), \
                        patch.object(apptainer_util, "path", side_effect=lambda *p: root.joinpath(*p)), \
                        patch.object(apptainer_util, "exec_args", return_value=[]), \
                        patch.object(apptainer_util, "remove"), \
                        patch.object(validate_instance.subprocess, "run", side_effect=steps):
                    self.assertFalse(validate_instance.validate_nuttx_apptainer(
                        "nuttx__nuttx-1", None, 30, False))
                report = json.loads((root / "validation.json").read_text())
                self.assertEqual(report["reason"], reason)

    def test_runtime_test_absent_before_fix_is_nonisolating(self):
        meta = {"project": "nuttx", "fail_type": "runtime_assert", "build_command": "make"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "image.sif"
            image.touch()
            steps = [subprocess.CompletedProcess([], 0, "", ""),
                     subprocess.CompletedProcess([], 1,
                         "ostest_main: Exiting with status 0\n"
                         "run_tests: changed test did not run: 'user_main: wqueue test'\n", "")]
            with patch.object(validate_instance.instances, "load_metadata", return_value=meta), \
                    patch.object(validate_instance.instances, "instance_dir", return_value=root), \
                    patch.object(apptainer_util, "instance_image", return_value=image), \
                    patch.object(apptainer_util, "recipe_fingerprint", return_value="test"), \
                    patch.object(apptainer_util, "path", side_effect=lambda *p: root.joinpath(*p)), \
                    patch.object(apptainer_util, "exec_args", return_value=[]), \
                    patch.object(apptainer_util, "remove"), \
                    patch.object(validate_instance.subprocess, "run", side_effect=steps):
                self.assertFalse(validate_instance.validate_nuttx_apptainer(
                    "nuttx__nuttx-1", None, 30, False))
            report = json.loads((root / "validation.json").read_text())
            self.assertEqual(report["before"]["rc"], 1)
            self.assertEqual(report["reason"], "changed test did not run before the fix")

    def test_hang_before_fix_counts_as_a_failure_only_for_runtime_other(self):
        for fail_type, reason in (("runtime_other", "the primary fix does not apply"),
                                  ("runtime_assert", "expected a test failure (rc=1), got rc=2")):
            meta = {"project": "nuttx", "fail_type": fail_type, "build_command": "make"}
            with self.subTest(fail_type), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                image = root / "image.sif"
                image.touch()
                steps = [subprocess.CompletedProcess([], 0, "", ""),
                         subprocess.CompletedProcess([], 2, "run_tests: result=2 (timed out)\n", ""),
                         subprocess.CompletedProcess([], 1, "error: patch failed\n", "")]
                with patch.object(validate_instance.instances, "load_metadata", return_value=meta), \
                        patch.object(validate_instance.instances, "instance_dir", return_value=root), \
                        patch.object(validate_instance, "nuttx_fix_patches",
                                     return_value=[("primary", "diff\n")]), \
                        patch.object(apptainer_util, "instance_image", return_value=image), \
                        patch.object(apptainer_util, "recipe_fingerprint", return_value="test"), \
                        patch.object(apptainer_util, "path", side_effect=lambda *p: root.joinpath(*p)), \
                        patch.object(apptainer_util, "exec_args", return_value=[]), \
                        patch.object(apptainer_util, "remove"), \
                        patch.object(validate_instance.subprocess, "run", side_effect=steps):
                    self.assertFalse(validate_instance.validate_nuttx_apptainer(
                        "nuttx__nuttx-1", None, 30, False))
                report = json.loads((root / "validation.json").read_text())
                self.assertEqual(report["reason"], reason)

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
            diagram = repo / "diagram.png"
            diagram.write_bytes(b"\x89PNG\r\n\x1a\n\x00base")
            git("add", "sched.c", "diagram.png")
            git("commit", "-m", "base")
            base = git("rev-parse", "HEAD")
            source.write_text("primary\n")
            diagram.write_bytes(b"\x89PNG\r\n\x1a\n\x00primary")
            git("commit", "-am", "primary")
            primary = git("rev-parse", "HEAD")
            source.write_text("followup\n")
            git("commit", "-am", "followup")
            followup = git("rev-parse", "HEAD")
            meta = {"project": "nuttx", "kernel_repo": "https://github.com/apache/nuttx",
                    "kernel_base_commit": base, "kernel_merge_commit": primary,
                    "files_changed_by_fix": ["sched.c", "diagram.png"],
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
            self.assertEqual(diagram.read_bytes(), b"\x89PNG\r\n\x1a\n\x00primary")

    def test_reconfigure_restores_overrides_lost_before_the_fix(self):
        # A stub `make olddefconfig` that, like Kconfig, drops a symbol the
        # kernel lacks and writes `n` as "is not set".
        script = validate_instance.nuttx_reconfigure(
            {"CONFIG_HRTIMER": "y", "CONFIG_SMP_NCPUS": "4", "CONFIG_DEBUG_SCHED_INFO": "n"})
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            make = work / "bin" / "make"
            make.parent.mkdir()
            # The script targets GNU sed; `-i.orig` works with BSD sed too.
            sed = work / "bin" / "sed"
            sed.write_text('#!/bin/sh\n[ "$1" = -i ] && { shift; exec /usr/bin/sed -i.orig "$@"; }\n'
                           'exec /usr/bin/sed "$@"\n')
            sed.chmod(0o755)

            def run_with(unknown: str):
                make.write_text(f"#!/bin/sh\nsed -i -e '/^{unknown}=/d' "
                               "-e 's/^\\(CONFIG_[A-Z0-9_]*\\)=n$/# \\1 is not set/' .config\n")
                make.chmod(0o755)
                (work / ".config").write_text("CONFIG_SIM=y\nCONFIG_DEBUG_SCHED_INFO=y\n")
                return subprocess.run(["bash", "-c", script], cwd=work, capture_output=True,
                                      text=True, env={"PATH": f"{make.parent}:/usr/bin:/bin"})

            r = run_with("CONFIG_NONE")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            config = (work / ".config").read_text().splitlines()
            self.assertIn("CONFIG_HRTIMER=y", config)
            self.assertIn("CONFIG_SMP_NCPUS=4", config)
            self.assertNotIn("CONFIG_DEBUG_SCHED_INFO=y", config)

            r = run_with("CONFIG_HRTIMER")
            self.assertEqual(r.returncode, 3)
            self.assertIn("config override dropped: CONFIG_HRTIMER=y", r.stdout)


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
