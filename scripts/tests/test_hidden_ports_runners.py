"""The pure logic of the agent runners of Mynewt, NuttX and Contiki-NG.

    python3 -m unittest discover -s scripts/tests -p 'test_hidden_ports_runners.py'
"""

import importlib.util
import io
import py_compile
import sys
import tempfile
import tokenize
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
RUNNERS = REPO / "harness" / "agent_run_tests"


def load(name):
    spec = importlib.util.spec_from_file_location("runner_" + name, RUNNERS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OSTEST = """\
NuttShell (NSH)
nsh> ostest
user_main: Begin argument test
user_main: getopt() test
getopt: ok
user_main: semaphore test
thread_signaler: ERROR waiter state = 1 != COND_WAITING
user_main: cond test
cond_test: ERROR something broke
cond_test: ERROR second
user_main: mutex tests
user_main: ERROR: not a banner
user_main: Exiting
ostest_main: Exiting with status 0
run_tests: ERROR in output
run_tests: result=1
"""


class NuttxRunnerTest(unittest.TestCase):
    def setUp(self):
        self.m = load("nuttx")

    def test_sections(self):
        log, notes = self.m.split_output(OSTEST)
        self.assertEqual(notes, ["run_tests: ERROR in output", "run_tests: result=1"])
        tests = self.m.split_tests(log)
        self.assertEqual([(n, s) for n, s, _ in tests],
                         [("Begin argument test", "passed"), ("getopt() test", "passed"), ("semaphore test", "passed"),
                          ("cond test", "failed"), ("mutex tests", "failed")])
        self.assertEqual(len(tests[3][2]), 2)
        self.assertIn("user_main: ERROR: not a banner", tests[4][2])

    def test_unfinished(self):
        log = ["user_main: a test", "ok", "user_main: b test", "Assertion failed: x"]
        self.assertEqual([t[1] for t in self.m.split_tests(log)], ["passed", "failed"])
        log = ["user_main: a test", "ok", "user_main: b test", "working"]
        self.assertEqual([t[1] for t in self.m.split_tests(log)], ["passed", "unfinished"])

    def test_name_matching(self):
        tests = self.m.split_tests(self.m.split_output(OSTEST)[0])
        self.assertEqual(self.m.not_run(["SEMAPHORE", "getopt", "user_main: cond test"], tests), [])
        self.assertEqual(self.m.not_run(["wqueue", "mutex"], tests), ["wqueue"])


class MynewtRunnerTest(unittest.TestCase):
    def setUp(self):
        self.m = load("mynewt")

    def test_heredoc_of_the_real_runner_defines_the_helpers(self):
        with patch.object(self.m, "BASE", REPO / "templates" / "mynewt" / "run_tests.sh"):
            base = self.m.load_base()
        self.assertTrue(callable(base.sync_dependencies))
        self.assertTrue(callable(base.observe_testutil))
        self.assertFalse(hasattr(base, "parser"))

    def test_cases(self):
        out = "[pass] s/a\n[FAIL] s/b\n[pass] s/b\n[pass] s/c\n"
        self.assertEqual(self.m.parse_cases(out), (["s/a", "s/c"], ["s/b"]))

    def test_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tb = Path(tmp)
            (tb / "kernel/os/selftest").mkdir(parents=True)
            (tb / "kernel/os/selftest/pkg.yml").write_text("pkg.name: x\n")
            (tb / "kernel/os/src").mkdir()
            with patch.object(self.m, "TESTBED", tb):
                self.assertEqual(self.m.resolve("kernel/os/selftest/"), "kernel/os/selftest")
                self.assertEqual(self.m.resolve(str(tb / "kernel/os/selftest")), "kernel/os/selftest")
                for bad in ("kernel/os/src", "-x", "../x", "/etc", "nope"):
                    self.assertIsNone(self.m.resolve(bad), bad)
                with patch.object(sys, "argv", ["run_tests"]):
                    self.assertEqual(self.m.main(), 4)
                with patch.object(sys, "argv", ["run_tests", "--foo"]):
                    self.assertEqual(self.m.main(), 4)


class ContikiRunnerTest(unittest.TestCase):
    def setUp(self):
        self.m = load("contiki_ng")
        self.tmp = tempfile.TemporaryDirectory()
        self.tb = Path(self.tmp.name)
        t = self.tb / "tests/08-native-runs"
        for name, text in (("12-heapmem", "CONTIKI_PROJECT = test-heapmem test-b # c\nCONTIKI_PROJECT += $(X) test-c\n"),
                           ("13-none", "all:\n"), ("14-one", "CONTIKI_PROJECT := only\n")):
            (t / name).mkdir(parents=True)
            (t / name / "Makefile").write_text(text)
        (self.tb / "tests/07-sim").mkdir(parents=True)
        (self.tb / "tests/07-sim/Makefile").write_text("all:\n")
        (self.tb / "tests/07-sim/a.csc").write_text("<simconf/>")

    def tearDown(self):
        self.tmp.cleanup()

    def ids(self, arg):
        units, error = self.m.resolve(arg, self.tb)
        self.assertIsNone(error, error)
        return [u["id"] for u in units]

    def test_folder_programs_and_options(self):
        f = "tests/08-native-runs/12-heapmem"
        self.assertEqual(self.ids(f), [f + "/test-heapmem", f + "/test-b", f + "/test-c"])
        self.assertEqual(self.ids(f + "/test-b"), [f + "/test-b"])
        self.assertEqual(self.ids(f + "/test-b:DEFINES=HEAPMEM_DEBUG=0:X=1"),
                         [f + "/test-b:DEFINES=HEAPMEM_DEBUG=0:X=1"])
        units, _ = self.m.resolve(f + "/test-b:A=1", self.tb)
        self.assertEqual(units[0], {
            "id": f + "/test-b:A=1", "kind": "native", "directory": f, "binary": "test-b",
            "make_options": ["A=1"], "suite_directory": "tests/08-native-runs",
            "submodules": [], "cases": []})
        self.assertEqual(self.ids(str(self.tb / "tests/08-native-runs/14-one") + "/"),
                         ["tests/08-native-runs/14-one/only"])

    def test_errors(self):
        for arg in ("tests/08-native-runs/13-none", "tests/nope", "tests/08-native-runs/12-heapmem:oops",
                    "-x", "/etc/passwd", "tests/../x", "tests/08-native-runs/12-heapmem/x:y"):
            units, error = self.m.resolve(arg, self.tb)
            self.assertIsNone(units, arg)
            self.assertTrue(error, arg)

    def test_cooja_needs_cooja(self):
        units, error = self.m.resolve("tests/07-sim/a.csc", self.tb)
        self.assertIn("Cooja is not set up", error)
        (self.tb / "tools/cooja").mkdir(parents=True)
        (self.tb / "tools/cooja/build.gradle").write_text("")
        units, error = self.m.resolve("tests/07-sim/a.csc", self.tb)
        self.assertEqual(units[0], {"id": "tests/07-sim/a.csc", "kind": "cooja",
                                    "directory": "tests/07-sim", "suite_directory": "tests/07-sim",
                                    "scenario": "a.csc", "make_options": [], "submodules": []})

    def test_no_test_is_a_usage_error(self):
        with patch.object(sys, "argv", ["run_tests"]):
            self.assertEqual(self.m.usage("x"), 4)


def report(description, result="success", line=10):
    """One case as the unit-test framework's unit_test_print_report prints it."""
    return ("\nUnit test: {}\nResult: {}\nExit point: test-only.c:{}\nAssertions executed: 1\n"
            "Start: 0\nEnd: 0\nDuration: 0 ticks\nTicks per second: 1000\n").format(description, result, line)


class ContikiRunnerOutputTest(unittest.TestCase):
    """A test that does not pass shows the last 40 lines of its output (as main's
    RIOT runner) under the file that holds it; one that passes shows nothing more.
    The base is the image's own runner with its builds and runs faked."""

    UNIT = "tests/08-native-runs/14-one/only"

    def setUp(self):
        self.m = load("contiki_ng")
        self.tmp = tempfile.TemporaryDirectory()
        self.tb = Path(self.tmp.name)
        (self.tb / "tests/08-native-runs/14-one").mkdir(parents=True)
        (self.tb / "tests/08-native-runs/14-one/Makefile").write_text("CONTIKI_PROJECT = only\n")
        self.suite = self.tb / "tests/07-sim"
        self.suite.mkdir(parents=True)
        (self.suite / "Makefile").write_text("all:\n")
        (self.suite / "22-x.csc").write_text("<simconf/>")
        (self.tb / "tools/cooja").mkdir(parents=True)
        (self.tb / "tools/cooja/build.xml").write_text("")
        with patch.object(self.m, "BASE", str(REPO / "templates/contiki-ng/apptainer/run_tests.py")):
            self.b = self.m.load_base()
        self.b.TESTBED, self.b.LOGS = self.tb, self.tb / ".embedeval/logs"
        self.b.build_native = lambda unit: True
        self.b.native_binary = lambda unit: self.tb / "only.native"

    def tearDown(self):
        self.tmp.cleanup()

    def run_tests(self, *args):
        out = io.StringIO()
        with patch.object(self.m, "load_base", return_value=self.b), \
                patch.object(sys, "argv", ["run_tests"] + list(args)), redirect_stdout(out):
            code = self.m.main()
        return code, out.getvalue()

    def native(self, output, done=True, timed_out=False, rc=0):
        def run(binary, path, cwd=None):
            path.write_text(output)
            return output, done, timed_out, rc
        self.b.run_native_binary = run
        return self.run_tests("tests/08-native-runs/14-one")

    def shown(self, out, path, text):
        """Assert `out` shows the last 40 lines of `text` under `path`'s header."""
        head = "\n--- {} (last 40 lines) ---\n".format(path)
        self.assertIn(head, out)
        want = "".join("    | " + l + "\n" for l in text.splitlines()[-40:])
        self.assertTrue(out.split(head, 1)[1].startswith(want), out)

    def test_failed_case_shows_its_exit_point_and_the_output_tail(self):
        output = ("Running unit tests\n" + report("first", "failure", 42)
                  + "".join(report("later {}".format(i)) for i in range(5)) + "=check-me= DONE\n")
        code, out = self.native(output)
        self.assertEqual(code, 1)
        self.assertIn("  failed {}: first\n    | Exit point: test-only.c:42\n  passed {}: later 0\n"
                      .format(self.UNIT, self.UNIT), out)
        log = self.b.log_path({"id": self.UNIT}, "run")
        self.shown(out, log, output)
        # The failed case's report is beyond the last 40 lines: only its own line shows it.
        self.assertEqual(out.count("Exit point: test-only.c:42"), 1)
        self.assertTrue(out.rstrip().endswith("==> run_tests: FAILED"))

    def test_timeout_shows_the_output_tail(self):
        output = "Running unit tests\n" + report("first") + "waiting for the timer\n"
        code, out = self.native(output, done=False, timed_out=True, rc=-15)
        self.assertEqual(code, 1)
        self.assertIn("  timeout {}\n".format(self.UNIT), out)
        self.shown(out, self.b.log_path({"id": self.UNIT}, "run"), output)

    def test_incomplete_run_shows_the_output_tail(self):
        output = report("first") + "TEST FAILED\n"       # exit(1) before the DONE marker
        code, out = self.native(output, done=False, rc=1)
        self.assertEqual(code, 3)
        self.shown(out, self.b.log_path({"id": self.UNIT}, "run"), output)

    def test_passing_unit_shows_no_output(self):
        code, out = self.native(report("first") + report("second") + "=check-me= DONE\n")
        self.assertEqual(code, 0)
        self.assertNotIn("last 40 lines", out)
        self.assertNotIn("    | ", out)

    def test_failed_cooja_scenario_shows_its_logs(self):
        # Ant-era simexec exits 0 and dumps the Cooja and script logs; its verdict
        # goes to <scenario>.testlog, which the base runner reads.
        run_log = "\n".join(["Running test 22-x with random Seed 1: FAIL", "==== 22-x.1.coojalog ===="]
                            + ["INFO [sim] mote {} booted".format(i) for i in range(45)]
                            + ["==== 22-x.1.scriptlog ====", "TEST FAILED: no reply from node 2",
                               "========== Summary ==========",
                               "22-x  TEST FAIL      0/1 -- failed seeds: 1"]) + "\n"
        testlog = "22-x  TEST FAIL      0/1 -- failed seeds: 1\n"

        def make(cmd, path, **kw):
            path.write_text(run_log)
            (self.suite / "22-x.testlog").write_text(testlog)
            return 0, run_log
        self.b.run_logged = make
        code, out = self.run_tests("tests/07-sim/22-x.csc")
        self.assertEqual(code, 1)
        self.assertIn("  failed tests/07-sim/22-x.csc\n", out)
        log = self.b.log_path({"id": "tests/07-sim/22-x.csc"}, "run")
        self.shown(out, log, run_log)
        self.shown(out, self.suite / "22-x.testlog", testlog)
        self.assertLess(out.index(str(log)), out.index(str(self.suite / "22-x.testlog")))

    def test_passing_cooja_scenario_shows_no_logs(self):
        def make(cmd, path, **kw):
            path.write_text("done\n")
            (self.suite / "COOJA.testlog").write_text("TEST OK\n")
            return 0, "done\n"
        self.b.run_logged = make
        code, out = self.run_tests("tests/07-sim/22-x.csc")
        self.assertEqual(code, 0)
        self.assertNotIn("last 40 lines", out)


class PythonCompatibilityTest(unittest.TestCase):
    def test_contiki_runner_compiles_without_fstrings(self):
        path = RUNNERS / "contiki_ng.py"
        py_compile.compile(str(path), cfile=str(Path(tempfile.gettempdir()) / "contiki_ng_check.pyc"),
                           doraise=True)
        text = path.read_text()
        self.assertNotIn("capture_output", text)
        self.assertNotIn("text=True", text)
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.STRING:
                prefix = tok.string.split('"')[0].split("'")[0]
                self.assertNotIn("f", prefix.lower(), tok.string[:40])
            self.assertNotEqual(tok.type, getattr(tokenize, "FSTRING_START", -1))


if __name__ == "__main__":
    unittest.main()
