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
        group = [{"CONFIG_SCHED_HPWORK": "y", "CONFIG_SCHED_LPWORK": "y"}]
        good["settings"]["CONFIG_SCHED_LPWORK"] = "y"
        self.assertEqual(config.problems(good, {}, group), [])
        self.assertEqual(config.problems(bad, {}, group),
                         ["needs one of CONFIG_SCHED_HPWORK=y, CONFIG_SCHED_LPWORK=y",
                          "testing/ostest/wqueue.c is not built"])
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


WQUEUE_MAIN = (
    "#include <stdio.h>\n#define HELPER 1\n"
    "#if defined(CONFIG_TESTING_OSTEST_WQUEUE) && \\\n    defined(CONFIG_BUILD_FLAT)\n"
    '  printf("\\nuser_main: wqueue test\\n");\n  wqueue_test();\n#endif\n'
    "#if CONFIG_TLS_NELEM > 0\n  tls_test();\n#else\n  wqueue_test();\n#endif\n")
WQUEUE_SOURCE = (
    "#include <nuttx/config.h>\n#ifndef __WQ_H\n#define __WQ_H\n#endif\n"
    "#ifdef CONFIG_SCHED_LPWORK\n#  define QUEUE LPWORK\n#else\n#  define QUEUE HPWORK\n#endif\n"
    "void wqueue_test(void)\n{\n}\n")


class RequirementTests(unittest.TestCase):
    """Text-only analysis of the conditions a changed test needs."""

    def test_calls_and_config_branch_groups_are_found(self):
        found = probe.conditionals(WQUEUE_MAIN, ["wqueue_test", "tls_test"])
        self.assertEqual([(c["function"], c["line"]) for c in found["calls"]],
                         [("wqueue_test", 6), ("tls_test", 9), ("wqueue_test", 11)])
        self.assertEqual(found["calls"][0]["conditions"],
                         ["#if defined(CONFIG_TESTING_OSTEST_WQUEUE) && defined(CONFIG_BUILD_FLAT)"])
        groups = probe.conditionals(WQUEUE_SOURCE)["groups"]
        self.assertEqual(len(groups), 1)   # the include guard names no CONFIG_ option
        self.assertEqual([(b["directive"], b["start"], b["end"]) for b in groups[0]["branches"]],
                         [("#ifdef CONFIG_SCHED_LPWORK", 6, 6), ("#else", 8, 8)])

    def test_makefile_and_kconfig_conditions(self):
        makefile = ("CSRCS = getopt.c\nifeq ($(CONFIG_SCHED_WORKQUEUE),y)\n"
                    "ifneq ($(CONFIG_DISABLE_PTHREAD),y)\nCSRCS += wqueue.c\nendif\nendif\n")
        self.assertEqual(probe.makefile_conditions(makefile, "wqueue.c"),
                         ["ifeq ($(CONFIG_SCHED_WORKQUEUE),y) && ifneq ($(CONFIG_DISABLE_PTHREAD),y)"])
        self.assertEqual(probe.makefile_conditions(makefile, "getopt.c"), [""])
        kconfig = ("config TESTING_OSTEST_WQUEUE\n\tbool \"Work queue\"\n\tdefault y\n"
                   "\tdepends on (BUILD_FLAT && SCHED_WORKQUEUE) || LIBC_USRWORK\n"
                   "config OTHER\n\tbool \"x\"\n")
        self.assertEqual(probe.kconfig_depends(kconfig), {
            "CONFIG_TESTING_OSTEST_WQUEUE": "depends on (BUILD_FLAT && SCHED_WORKQUEUE) || LIBC_USRWORK"})

    def test_requirements_keep_only_what_is_certain(self):
        result = probe.requirements([
            "ifeq ($(CONFIG_SCHED_WORKQUEUE),y) && ifneq ($(CONFIG_DISABLE_PTHREAD),y)"
            .split(" && ")[0], "ifneq ($(CONFIG_DISABLE_PTHREAD),y)",
            "#if !defined(CONFIG_DISABLE_PTHREAD) && (defined(CONFIG_SCHED_LPWORK) || "
            "defined(CONFIG_SCHED_HPWORK))",
            "#ifdef CONFIG_SMP_CALL", "#if CONFIG_TLS_NELEM > 0", "#ifdef CONFIG_A ... #else"])
        self.assertEqual(result["required"], {"CONFIG_SCHED_WORKQUEUE": "y",
                                              "CONFIG_DISABLE_PTHREAD": "n",
                                              "CONFIG_SMP_CALL": "y"})
        self.assertEqual(result["one_of"], [{"CONFIG_SCHED_LPWORK": "y", "CONFIG_SCHED_HPWORK": "y"}])
        self.assertEqual(result["other"], ["CONFIG_TLS_NELEM > 0", "#ifdef CONFIG_A ... #else"])


    def test_test_requirements_come_from_build_call_kconfig_and_branches(self):
        files = {
            "testing/ostest/Makefile": "ifeq ($(CONFIG_TESTING_OSTEST_WQUEUE),y)\nCSRCS += wqueue.c\nendif\n",
            "testing/ostest/CMakeLists.txt": "if(CONFIG_TESTING_OSTEST_WQUEUE)\n  list(APPEND SRCS wqueue.c)\nendif()\n",
            "testing/ostest/ostest_main.c": "#if defined(CONFIG_SCHED_WORKQUEUE) && \\\n  (defined(CONFIG_SCHED_HPWORK) || defined(CONFIG_SCHED_LPWORK))\n"
                                            "  wqueue_test();\n#endif\n",
            "testing/ostest/Kconfig": "config TESTING_OSTEST_WQUEUE\n\tbool \"w\"\n\tdepends on BUILD_FLAT && !DISABLE_PTHREAD\n",
            "testing/ostest/wqueue.c": WQUEUE_SOURCE}
        result = config.test_requirements("testing/ostest/wqueue.c", ["wqueue_test"],
                                          lambda path: files.get(path, ""))
        self.assertEqual(result["required"], {"CONFIG_TESTING_OSTEST_WQUEUE": "y",
                                              "CONFIG_SCHED_WORKQUEUE": "y",
                                              "CONFIG_BUILD_FLAT": "y",
                                              "CONFIG_DISABLE_PTHREAD": "n"})
        self.assertEqual(result["one_of"], [{"CONFIG_SCHED_HPWORK": "y", "CONFIG_SCHED_LPWORK": "y"}])
        self.assertEqual([b["directive"] for b in result["branches"][0]],
                         ["#ifdef CONFIG_SCHED_LPWORK", "#else"])
        self.assertEqual(config.cmake_conditions(files["testing/ostest/CMakeLists.txt"], "wqueue.c"),
                         ["#ifdef CONFIG_TESTING_OSTEST_WQUEUE"])
        self.assertIn("one of: CONFIG_SCHED_HPWORK=y | CONFIG_SCHED_LPWORK=y",
                      config.describe_requirements([result]))


@unittest.skipUnless(shutil.which("make") and shutil.which("gcc"), "needs make and gcc")
class ProbeTests(unittest.TestCase):
    """The probe's Makefile and #if evaluation, on a miniature ostest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.top = Path(self.tmp.name)
        self.ostest = self.top / "apps" / "testing" / "ostest"
        self.ostest.mkdir(parents=True)
        (self.ostest / "Makefile").write_text(
            "include $(APPDIR)/Make.defs\nCSRCS = getopt.c\n"
            "ifeq ($(CONFIG_TESTING_OSTEST_WQUEUE),y)\nCSRCS += wqueue.c\nendif\n"
            "MAINSRC = ostest_main.c\ninclude $(APPDIR)/Application.mk\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_probe(self, config_text):
        (self.top / ".config").write_text(config_text)
        header = probe.config_header(probe.read_config(self.top / ".config"))
        built = probe.built_sources(self.ostest, self.top / ".config")
        calls = probe.evaluate(probe.conditionals(WQUEUE_MAIN, ["wqueue_test", "tls_test"]),
                               header)["calls"]
        branches = probe.evaluate(probe.conditionals(WQUEUE_SOURCE), header)["groups"][0]
        return (built, [(c["function"], c["line"], c["compiled"]) for c in calls],
                [b["compiled"] for b in branches["branches"]])

    def test_gating_follows_the_effective_config(self):
        built, calls, branches = self.run_probe(
            "CONFIG_TESTING_OSTEST_WQUEUE=y\nCONFIG_BUILD_FLAT=y\nCONFIG_TLS_NELEM=4\n"
            "CONFIG_SCHED_LPWORK=y\n")
        self.assertIn("wqueue.c", built)
        self.assertEqual(calls, [("wqueue_test", 6, True), ("tls_test", 9, True),
                                 ("wqueue_test", 11, False)])
        self.assertEqual(branches, [True, False])
        built, calls, branches = self.run_probe("# CONFIG_TESTING_OSTEST_WQUEUE is not set\n"
                                                "CONFIG_TLS_NELEM=0\n")
        self.assertNotIn("wqueue.c", built)
        self.assertEqual(calls, [("wqueue_test", 6, False), ("tls_test", 9, False),
                                 ("wqueue_test", 11, True)])
        self.assertEqual(branches, [False, True])


if __name__ == "__main__":
    unittest.main()
