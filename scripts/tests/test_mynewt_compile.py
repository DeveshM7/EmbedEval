"""Compile oracles: every (test source, API) pair a before-build fails on, and
a build order that is the same in every run."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import generate_instance as gi  # noqa: E402

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=master"]


def git(repo, *args):
    return subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def write(repo: Path, files: dict) -> None:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)


class DeriveTest(unittest.TestCase):
    """A base where foo_self_test is compiled out behind FOO_C; a fix that
    turns FOO_C on and adds bar_get; tests that call both."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q")
        write(self.repo, {
            "lib/include/config.h": "//#define FOO_C\n",
            "lib/src/foo.c": "#include \"config.h\"\n#if defined(FOO_C)\nint foo_self_test(int v)\n{\n"
                             "    return v;\n}\n#endif\n",
            "lib/src/bar.c": "int bar_old(void)\n{\n    return 0;\n}\n",
            "lib/test/src/old.c": "void old(void) { bar_old(); }\n"})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        write(self.repo, {
            "lib/include/config.h": "#define FOO_C\n",
            "lib/src/bar.c": "int bar_old(void)\n{\n    return 0;\n}\nint bar_get(void)\n{\n    return 1;\n}\n",
            "lib/test/src/foo_test.c": "void t1(void) { TEST_ASSERT(foo_self_test(1) == 0); }\n",
            "lib/test/src/bar_test.c": "void t2(void) { int x = bar_get(); bar_old(); helper_in_test(x); }\n"})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "fix")
        self.fix = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def derive(self, source, api):
        return gi.derive_compile_expectations(
            self.repo, self.base, self.fix, self.fix, ["lib/include/config.h", "lib/src/bar.c"],
            ["lib/test/src/foo_test.c", "lib/test/src/bar_test.c"], "mynewt", source, api)

    def test_added_and_enabled_apis_are_both_expected(self):
        expectations, gates = self.derive("lib/test/src/bar_test.c", "bar_get")
        self.assertEqual(expectations, {"lib/test/src/bar_test.c": ["bar_get"],
                                        "lib/test/src/foo_test.c": ["foo_self_test"]})
        self.assertEqual(gates, {"foo_self_test": {"path": "lib/src/foo.c", "line": 3,
                                                   "condition": "#if defined(FOO_C)", "macro": "FOO_C"}})

    def test_an_api_that_already_builds_is_not_derived_from(self):
        self.assertIsNone(self.derive("lib/test/src/bar_test.c", "bar_old"))

    def test_an_ungated_existing_definition_is_not_enabled_by_the_fix(self):
        write(self.repo, {"lib/src/foo.c": "int foo_self_test(int v)\n{\n    return v;\n}\n"})
        git(self.repo, "commit", "-qam", "ungate")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.assertIsNone(self.derive("lib/test/src/foo_test.c", "foo_self_test"))


class DisabledTestsTest(unittest.TestCase):
    SRC = """TEST_CASE(a) { }
TEST_CASE(b) { }
TEST_CASE(c) { }
TEST_SUITE(s)
{
    a();
#if 0
    b();
#if FOO
    nested();
#endif
#else
    c();
#endif
}
"""

    def test_cases_called_only_inside_if_0_never_run(self):
        import triage_mynewt_prs as t
        self.assertEqual(t.suite_cases(t.without_disabled(self.SRC)), ["s/a", "s/c"])
        self.assertEqual(t.disabled_cases([self.SRC]), ["s/b"])
        record = {"test_suites": {"pkg/test": {"files": {"src/t.c": {"content": self.SRC}}}}}
        self.assertEqual(t.inventory(record), {"pkg/test": ["s/a", "s/c"]})

    def test_other_conditions_are_kept(self):
        import triage_mynewt_prs as t
        src = self.SRC.replace("#if 0", "#if MYNEWT_VAL(X)")
        self.assertEqual(t.disabled_cases([src]), [])


class RunnerTest(unittest.TestCase):
    def test_the_build_is_single_threaded_where_newt_allows(self):
        text = (SCRIPTS.parent / "templates" / "mynewt" / "run_tests.sh").read_text()
        self.assertIn('jobs = ["-j", "1"] if re.search(r"--jobs\\b", help_text) else []', text)
        self.assertIn('["newt", "test", *jobs, ', text)


if __name__ == "__main__":
    unittest.main()
