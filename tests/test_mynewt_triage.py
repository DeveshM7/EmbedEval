"""Behavior checks for the Mynewt PR triage runner."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import triage_mynewt_prs as triage

SUITE = "fs/demo/selftest"


def record(**overrides):
    rec = {
        "number": 42, "base_commit": "b" * 40, "head_commit": "h" * 40,
        "commits": [{"sha": "t" * 40, "message": "tests"}, {"sha": "h" * 40, "message": "fix"}],
        "files": [{"filename": f"{SUITE}/src/testcases/demo_new.c"},
                  {"filename": "fs/demo/src/demo.c"}],
        "linked_issues": [{"number": 7, "title": "bug", "body": "it breaks"}],
        "test_suites": {SUITE: {"config": "pkg.yml", "files": {
            "pkg.yml": {"content": "pkg.name: fs/demo/selftest"},
            "src/demo_test.c": {"content": "TEST_SUITE(demo_suite) { demo_old(); demo_new(); }"},
            "src/testcases/demo_new.c": {"content": "TEST_CASE_SELF(demo_new) { demo_api(); }"},
            "src/testcases/demo_old.c": {"content": "TEST_CASE_SELF(demo_old) {}"}}}},
    }
    rec.update(overrides)
    return rec


def accept(**overrides):
    verdict = {"pr": 42, "verdict": "accept", "confidence": "high", "change_type": "fix",
               "platform": "native", "failure_mode": "runtime",
               "problem_statement": "Demo fails when full.",
               "fail_to_pass": ["demo_suite/demo_new"],
               "pass_to_pass": ["demo_suite/demo_old"],
               "baseline_tests": ["demo_suite/demo_old"], "compatibility_cflags": [],
               "detecting_test": "demo_new asserts the result.", "reason": "Detects the fix."}
    verdict.update(overrides)
    return verdict


class CheckVerdictTests(unittest.TestCase):
    def test_complete_accept_and_minimal_reject_pass(self):
        self.assertEqual(triage.check_verdict(accept(), "42", record()), [])
        reject = {"pr": 42, "verdict": "reject", "confidence": "high", "reason": "No test."}
        self.assertEqual(triage.check_verdict(reject, "42", record()), [])

    def test_no_reply_or_wrong_pr(self):
        self.assertEqual(len(triage.check_verdict(None, "42", record())), 1)
        issues = triage.check_verdict(accept(pr="42"), "42", record())
        self.assertIn("pr must be the integer 42", issues)

    def test_accept_without_linked_issue_says_to_reject(self):
        issues = triage.check_verdict(accept(), "42", record(linked_issues=[]))
        self.assertTrue(any("linked_issues is empty" in i and '"reject"' in i for i in issues))

    def test_non_native_platform_is_refused(self):
        issues = triage.check_verdict(accept(platform="qemu"), "42", record())
        self.assertTrue(any(i.startswith("platform must be \"native\"") for i in issues))

    def test_testcase_names_must_be_suite_slash_case_from_sources(self):
        issues = triage.check_verdict(accept(fail_to_pass=["demo_new"]), "42", record())
        self.assertTrue(any("not in suite/testcase form: ['demo_new']" in i for i in issues))
        issues = triage.check_verdict(accept(pass_to_pass=["demo_suite/ghost"]), "42", record())
        self.assertTrue(any("does not appear" in i and "demo_suite/ghost" in i for i in issues))
        issues = triage.check_verdict(accept(fail_to_pass=[]), "42", record())
        self.assertTrue(any(i.startswith("fail_to_pass must be a non-empty list") for i in issues))

    def test_a_testcase_cannot_be_both_fail_and_pass(self):
        issues = triage.check_verdict(
            accept(pass_to_pass=["demo_suite/demo_old", "demo_suite/demo_new"]), "42", record())
        self.assertIn("remove ['demo_suite/demo_new'] from pass_to_pass", issues[0])

    def test_compile_mode_needs_changed_test_source_containing_api(self):
        ok = accept(failure_mode="compile",
                    compile_test_source=f"{SUITE}/src/testcases/demo_new.c", missing_api="demo_api")
        self.assertEqual(triage.check_verdict(ok, "42", record()), [])
        issues = triage.check_verdict({**ok, "missing_api": "absent_api"}, "42", record())
        self.assertTrue(any(i.startswith("missing_api") for i in issues))
        issues = triage.check_verdict({**ok, "compile_test_source": "fs/demo/src/demo.c"},
                                      "42", record())
        self.assertTrue(any(i.startswith("compile_test_source") for i in issues))

    def test_test_commit_and_support_paths_match_the_generator(self):
        issues = triage.check_verdict(accept(test_commit="x" * 40), "42", record())
        self.assertTrue(any("one of the PR's commits" in i and "t" * 40 in i for i in issues))
        issues = triage.check_verdict(accept(test_support_paths=["fs/demo/src/demo.c"]),
                                      "42", record())
        self.assertTrue(any(i.startswith("test_support_paths requires test_commit") for i in issues))
        issues = triage.check_verdict(accept(test_commit="t" * 40,
                                             test_support_paths=["fs/other.c"]), "42", record())
        self.assertIn("remove ['fs/other.c'] from test_support_paths", issues[0])
        good = accept(test_commit="t" * 40, test_support_paths=["fs/demo/src/demo.c"])
        self.assertEqual(triage.check_verdict(good, "42", record()), [])


class RecordAndScoreTests(unittest.TestCase):
    def test_render_keeps_suite_sources_and_bounds_long_fields(self):
        rendered = json.loads(triage.render_record("42", record()))
        self.assertEqual(rendered["case"], "42")
        self.assertIn("demo_suite", rendered["test_suites"][SUITE]["src/demo_test.c"])
        rendered = json.loads(triage.render_record("42", record(body="x" * 30), field_limit=10))
        self.assertIn("[truncated 20 characters]", rendered["body"])

    def test_eval_scores_errors_as_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            gold = Path(tmp) / "gold.json"
            gold.write_text(json.dumps({"accept": {"1": {}}, "reject": {"2": "x"},
                                        "hard_filter_reject": {"3": "y"}}))
            run = Path(tmp) / "run"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"cases": ["1", "2"]}))
            (run / "1.json").write_text(json.dumps({"verdict": "accept", "reason": "ok"}))
            (run / "2.json").write_text(json.dumps({"verdict": "error", "reason": "bad"}))
            with patch.object(triage, "GOLD", gold), patch("builtins.print"):
                result = triage.eval_run(run)
            self.assertEqual((result["correct"], result["scored"]), (1, 2))
            self.assertTrue((run / "scorecard.md").exists())


class ToolTests(unittest.TestCase):
    """The tools read a real (tiny) bare clone at the PR's commits."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        work = Path(cls.tmp.name) / "work"
        git = lambda *a: subprocess.run(["git", "-C", str(work), *a], check=True,
                                        capture_output=True, text=True).stdout.strip()
        work.mkdir()
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (work / "demo.c").write_text("int demo(void) { return 1; }\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        base = git("rev-parse", "HEAD")
        (work / "demo.c").write_text("int demo(void) { return 2; }\n")
        git("commit", "-q", "-am", "fix")
        head = git("rev-parse", "HEAD")
        cls.clone = Path(cls.tmp.name) / "clone.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(work), str(cls.clone)], check=True)
        cls.rec = record(base_commit=base, head_commit=head,
                         commits=[{"sha": head, "message": "fix"}])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def tool(self, name, **args):
        with patch.object(triage, "CLONE", self.clone):
            return triage.run_tool(name, args, self.rec)

    def test_read_file_at_base_and_head(self):
        self.assertIn("return 1", self.tool("read_file", path="demo.c", ref="base"))
        self.assertIn("return 2", self.tool("read_file", path="demo.c", ref="head"))

    def test_grep_list_dir_and_commit_diff(self):
        self.assertIn("demo.c:1:", self.tool("grep", pattern="return [0-9]", path="", ref="head"))
        self.assertIn("demo.c", self.tool("list_dir", path="", ref="base"))
        diff = self.tool("commit_diff", sha=self.rec["head_commit"][:10])
        self.assertIn("+int demo(void) { return 2; }", diff)

    def test_bad_ref_and_path_are_explained(self):
        self.assertIn("ref must be 'base', 'head'", self.tool("read_file", path="demo.c", ref="main"))
        self.assertEqual(self.tool("read_file", path="../x", ref="base"),
                         "error: invalid repository-relative path")


if __name__ == "__main__":
    unittest.main()
