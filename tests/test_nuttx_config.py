"""Checks for the effective-configuration resolver and its in-image probe."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nuttx_config as config
import nuttx_config_probe as probe


def side(settings=None, tests=None, overrides=None):
    return {"kernel": "k" * 40, "apps": "a" * 40, "platform": "sim:nsh", "ostest": True,
            "overrides": overrides or [], "settings": settings or {},
            "tests": tests or [], "makefile_ok": True}


class ResolverTests(unittest.TestCase):
    def test_script_follows_the_template_then_adds_overrides(self):
        text = config.script("k1", "a1", {"CONFIG_SCHED_LPWORK": "y", "CONFIG_A": "n"},
                             "qemu-armv8a:nsh_smp")
        template = (Path(config.SCRIPTS).parent / "templates" / "nuttx" / "Dockerfile.tmpl").read_text()
        self.assertIn("./tools/configure.sh -a ./apps qemu-armv8a:nsh_smp", text)
        for step in ("CONFIG_TESTING_OSTEST=y' 'CONFIG_NSH_CONSOLE_LOGIN=n' 'CONFIG_NDEBUG=n'",
                     "grep -q '^# CONFIG_NDEBUG is not set$' .config"):
            self.assertIn(step, text)
            self.assertIn(step, template)
        first = text.index("CONFIG_A=n")
        self.assertLess(first, text.index("CONFIG_SCHED_LPWORK=y"))   # sorted
        self.assertLess(text.index("CONFIG_SCHED_LPWORK=y"), text.rindex("make olddefconfig"))
        self.assertIn("git --git-dir=/repos/nuttx.git archive k1", text)

    def test_overrides_are_checked_like_the_renderer(self):
        self.assertEqual(config.check_overrides({"CONFIG_SMP_NCPUS": "4", "CONFIG_X": '"s"'}), [])
        self.assertTrue(config.check_overrides({"CONFIG_NDEBUG": "n"})[0].startswith("reserved"))
        self.assertTrue(config.check_overrides({"SCHED_HPWORK": "y"})[0].startswith("invalid"))
        self.assertTrue(config.check_overrides({"CONFIG_X": True}))
        with self.assertRaisesRegex(ValueError, "platform"):
            config.resolve("k", "a", {}, [], platform="sim")

    def test_results_are_cached_and_unavailable_images_say_where_to_run(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(config, "CACHE", Path(tmp)), \
             patch.object(config, "available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Spark node"):
                config.resolve("k", "a", {}, [])
            key_file = None
            with patch.object(config, "available", return_value=True), \
                 patch.object(config, "ensure_commit"), \
                 patch.object(config.subprocess, "run") as run:
                def fake(cmd, **kwargs):
                    work = Path(cmd[cmd.index("--bind") + 1].split(",")[-1].split(":")[0])
                    (work / "result.json").write_text(json.dumps(side()))
                    return type("Done", (), {"returncode": 0, "stderr": ""})()
                run.side_effect = fake
                first = config.resolve("k", "a", {"CONFIG_X": "y"}, [])
                config.resolve("k", "a", {"CONFIG_X": "y"}, [])
                key_file = list(Path(tmp).glob("*.json"))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(key_file), 1)
            self.assertEqual(first["platform"], "sim:nsh")

    def test_problems_name_missing_settings_and_tests_that_do_not_run(self):
        good = side({"CONFIG_SCHED_WORKQUEUE": "y"},
                    [{"path": "testing/ostest/wqueue.c", "built": True, "runs": True, "calls": []}])
        self.assertEqual(config.problems(good, {"CONFIG_SCHED_WORKQUEUE": "y"}), [])
        bad = side({"CONFIG_SCHED_WORKQUEUE": "n"},
                   [{"path": "testing/ostest/wqueue.c", "built": False, "runs": False, "calls": []}])
        self.assertEqual(config.problems(bad, {"CONFIG_SCHED_WORKQUEUE": "y"}),
                         ["CONFIG_SCHED_WORKQUEUE=n, needs y",
                          "testing/ostest/wqueue.c is not built"])

    def test_description_explains_dropped_overrides(self):
        dropped = {"name": "CONFIG_T", "requested": "y", "final": "n", "status": "dropped",
                   "depends_on": "SCHED_WORKQUEUE=n && BUILD_FLAT=y",
                   "unmet": ["SCHED_WORKQUEUE=n"]}
        test = {"path": "testing/ostest/t.c", "built": False, "runs": False,
                "calls": [{"function": "t_test", "line": 9, "compiled": False,
                           "conditions": ["#ifdef CONFIG_T"]}]}
        text = config.describe({"fixed": side(tests=[test], overrides=[dropped])})
        self.assertIn("CONFIG_T=y: dropped (now n); depends on SCHED_WORKQUEUE=n", text)
        self.assertIn("unmet: SCHED_WORKQUEUE=n", text)
        self.assertIn("t_test() at ostest_main.c:9 compiled out under #ifdef CONFIG_T", text)
        self.assertIn("NOT RUNNABLE: testing/ostest/t.c is not built", text)

    def test_changed_tests_are_ostest_sources_with_their_entry_points(self):
        tests = config.changed_tests(
            ["testing/ostest/wq.c", "testing/ostest/ostest_main.c", "testing/ostest/Makefile",
             "testing/other/x.c"],
            {"testing/ostest/wq.c": "static void helper(void)\n{\n}\n\nvoid wq_test(void)\n{\n}\n"})
        self.assertEqual(tests, [{"path": "testing/ostest/wq.c", "entries": ["wq_test"]}])


@unittest.skipUnless(shutil.which("make") and shutil.which("gcc"), "needs make and gcc")
class ProbeTests(unittest.TestCase):
    """The probe's Makefile and #if evaluation, on a miniature ostest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.top = Path(self.tmp.name)
        ostest = self.top / "apps" / "testing" / "ostest"
        ostest.mkdir(parents=True)
        (ostest / "Makefile").write_text(
            "include $(APPDIR)/Make.defs\nCSRCS = getopt.c\n"
            "ifeq ($(CONFIG_TESTING_OSTEST_WQUEUE),y)\nCSRCS += wqueue.c\nendif\n"
            "MAINSRC = ostest_main.c\ninclude $(APPDIR)/Application.mk\n")
        (ostest / "ostest_main.c").write_text(
            "#include <stdio.h>\n#define HELPER 1\n"
            "#if defined(CONFIG_TESTING_OSTEST_WQUEUE) && \\\n    defined(CONFIG_BUILD_FLAT)\n"
            '  printf("\\nuser_main: wqueue test\\n");\n  wqueue_test();\n#endif\n'
            "#if CONFIG_TLS_NELEM > 0\n  tls_test();\n#else\n  wqueue_test();\n#endif\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_probe(self, config_text):
        (self.top / ".config").write_text(config_text)
        ostest = self.top / "apps" / "testing" / "ostest"
        values = probe.read_config(self.top / ".config")
        built = probe.built_sources(ostest, self.top / ".config")
        calls = probe.call_sites(ostest / "ostest_main.c", ["wqueue_test", "tls_test"],
                                 probe.config_header(values))
        return built, [(c["function"], c["line"], c["compiled"]) for c in calls], calls

    def test_gating_follows_the_effective_config(self):
        built, calls, raw = self.run_probe(
            "CONFIG_TESTING_OSTEST_WQUEUE=y\nCONFIG_BUILD_FLAT=y\nCONFIG_TLS_NELEM=4\n")
        self.assertIn("wqueue.c", built)
        self.assertEqual(calls, [("wqueue_test", 6, True), ("tls_test", 9, True),
                                 ("wqueue_test", 11, False)])
        self.assertEqual(raw[0]["conditions"],
                         ["#if defined(CONFIG_TESTING_OSTEST_WQUEUE) && defined(CONFIG_BUILD_FLAT)"])
        built, calls, _ = self.run_probe("# CONFIG_TESTING_OSTEST_WQUEUE is not set\n"
                                         "CONFIG_TLS_NELEM=0\n")
        self.assertNotIn("wqueue.c", built)
        self.assertEqual(calls, [("wqueue_test", 6, False), ("tls_test", 9, False),
                                 ("wqueue_test", 11, True)])


if __name__ == "__main__":
    unittest.main()
