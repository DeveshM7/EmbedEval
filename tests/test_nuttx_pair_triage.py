"""Behavior checks for triaging a supplied NuttX apps/kernel PR pair."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nuttx_candidates as candidates
import triage_nuttx_prs as triage
import generate_nuttx_instance as generator
import render_nuttx_instance as render


def pair_record():
    return {
        "apps": {"number": 7, "merged_at": "2024-01-02", "base_commit": "a0",
                 "head_commit": "a1", "files": [{"filename": "testing/ostest/example.c"}]},
        "kernel_candidates": [
            {"number": 11, "merged_at": "2024-01-01", "base_commit": "k0",
             "head_commit": "k1", "files": [{"filename": "sched/example.c"}]}
        ],
        "test_contents": {"testing/ostest/example.c": "void example_test(void) {}"},
    }


class SuppliedPairTests(unittest.TestCase):
    def test_pair_pool_becomes_distinct_triage_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pairs.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in [
                {"case": "apps-7-kernel-11", "apps_pr": 7, "kernel_pr": 11},
                {"case": "apps-7-kernel-11", "apps_pr": 7, "kernel_pr": 11},
                {"case": "apps-8-kernel-12", "apps_pr": 8, "kernel_pr": 12},
            ]) + "\n")
            self.assertEqual(triage.pair_cases(path),
                             ["apps-7-kernel-11", "apps-8-kernel-12"])

    def test_pair_pool_rejects_mismatched_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pairs.jsonl"
            path.write_text(json.dumps({"case": "apps-7-kernel-12",
                                        "apps_pr": 7, "kernel_pr": 11}) + "\n")
            with self.assertRaisesRegex(ValueError, "line 1"):
                triage.pair_cases(path)

    def test_explicit_pair_enrichment_does_not_search_for_partners(self):
        apps = {"number": 7, "body": "", "head_commit": "apps-head",
                "files": [{"filename": "testing/ostest/example.c", "status": "added"}]}
        kernel = {"number": 11, "body": "", "base_commit": "kernel-base",
                  "files": [{"filename": "sched/example.c", "status": "modified"}]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(candidates, "ENRICHED", Path(tmp)), \
             patch.object(candidates, "full_pr", side_effect=[apps, kernel]), \
             patch.object(candidates, "source_at", return_value="source"), \
             patch.object(candidates, "closing_issues", return_value=[]), \
             patch.object(candidates, "inverse_kernel_refs", side_effect=AssertionError("searched")):
            record = candidates.enrich(7, kernel_number=11)
            self.assertEqual(record["pairing_source"], "explicit_pair")
            self.assertEqual([pr["number"] for pr in record["kernel_candidates"]], [11])
            self.assertEqual(record["test_contents"]["testing/ostest/example.c"], "source")
            self.assertTrue((Path(tmp) / "apps-7-kernel-11.json").exists())

    def test_pair_case_loads_the_supplied_pair(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(candidates, "ENRICHED", Path(tmp)):
            (Path(tmp) / "apps-7-kernel-11.json").write_text(json.dumps(pair_record()))
            record = triage.load_record("apps-7-kernel-11")
            self.assertEqual(record["apps"]["number"], 7)
            self.assertEqual([pr["number"] for pr in record["kernel_candidates"]], [11])

    def test_accept_requires_a_detecting_test_and_review_readiness(self):
        case = "apps-7-kernel-11"
        verdict = {
            "case": case, "verdict": "accept", "reason": "Changed assertion checks the fix",
            "kernel_pr": 11, "test_paths": ["testing/ostest/example.c"],
            "fix_paths": ["sched/example.c"], "platform": "sim:nsh",
            "evidence_status": "needs_validation", "issue_url": None,
            "confidence": "medium", "change_type": "fix",
            "problem_statement": "The scheduler returns the wrong result.",
            "detecting_test": "example_test asserts the corrected result; it should fail before and pass after.",
            "fail_to_pass": ["example_test"], "fail_type": "runtime_assert",
            "config_overrides": {}, "readiness": "needs_reviewed_recipe",
        }
        self.assertEqual(triage.check_verdict(verdict, case, pair_record()), [])
        verdict["fail_to_pass"] = ["invented_test"]
        self.assertTrue(any("fail_to_pass" in issue for issue in
                            triage.check_verdict(verdict, case, pair_record())))
        verdict["fail_to_pass"] = ["example_test"]
        del verdict["detecting_test"]
        self.assertIn("detecting_test is required", triage.check_verdict(verdict, case, pair_record()))
        verdict["detecting_test"] = "example_test asserts the corrected result"
        verdict["readiness"] = "needs_fixture"
        self.assertTrue(any("readiness" in issue for issue in
                            triage.check_verdict(verdict, case, pair_record())))
        verdict["readiness"] = "needs_reviewed_recipe"
        verdict["fail_type"] = "compile_error"
        self.assertTrue(any("compile_test_source" in issue for issue in
                            triage.check_verdict(verdict, case, pair_record())))
        record = pair_record()
        record["apps"]["files"][0]["filename"] = "examples/hello/hello_main.c"
        record["test_contents"] = {"examples/hello/hello_main.c": "void example_test(void) {}"}
        verdict["fail_type"] = "runtime_assert"
        verdict["test_paths"] = ["examples/hello/hello_main.c"]
        verdict["readiness"] = "needs_fixture"
        self.assertEqual(triage.check_verdict(verdict, case, record), [])

    def test_accept_needs_merged_prs_and_branch_boundaries(self):
        record = pair_record()
        record["kernel_candidates"][0]["base_commit"] = None
        record["apps"]["merged_at"] = None
        verdict = {"case": "apps-7-kernel-11", "verdict": "accept", "reason": "candidate",
                   "kernel_pr": 11, "test_paths": ["testing/ostest/example.c"],
                   "fix_paths": ["sched/example.c"], "platform": "sim:nsh",
                   "evidence_status": "needs_validation", "issue_url": None,
                   "confidence": "low", "change_type": "fix", "problem_statement": "Wrong result",
                   "detecting_test": "example_test checks result", "fail_to_pass": ["example_test"],
                   "fail_type": "runtime_assert", "config_overrides": {},
                   "readiness": "needs_reviewed_recipe"}
        issues = triage.check_verdict(verdict, verdict["case"], record)
        self.assertTrue(any("merged" in issue for issue in issues))
        self.assertTrue(any("commit" in issue for issue in issues))

    def test_generator_can_use_explicit_pair_record(self):
        record = pair_record()
        record["apps"].update({"url": "https://example.test/apps", "base_commit": "a0",
                               "head_commit": "a1"})
        record["kernel_candidates"][0].update(
            {"url": "https://example.test/kernel", "base_commit": "k0",
             "head_commit": "k1"})
        verdict = {"verdict": "accept", "platform": "sim:nsh",
                   "test_paths": ["testing/ostest/example.c"],
                   "fix_paths": ["sched/example.c"], "fail_type": "runtime_assert",
                   "problem_statement": "Wrong result", "fail_to_pass": ["example_test"]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(generator, "ENRICHED", Path(tmp)), \
             patch.object(generator, "build_test_patch",
                          return_value=("diff", ["user_main: example test"])), \
             patch.object(generator, "render", return_value=Path(tmp) / "instance"):
            (Path(tmp) / "apps-7-kernel-11.json").write_text(json.dumps(record))
            self.assertEqual(generator.generate(7, 11, verdict), Path(tmp) / "instance")

    def test_ready_recipe_can_feed_generator_directly(self):
        record = pair_record()
        record["apps"]["url"] = "https://example.test/apps"
        record["kernel_candidates"][0]["url"] = "https://example.test/kernel"
        verdict = {
            "case": "apps-7-kernel-11", "verdict": "accept", "reason": "new test checks result",
            "kernel_pr": 11, "test_paths": ["testing/ostest/example.c"],
            "fix_paths": ["sched/example.c"], "platform": "sim:nsh",
            "evidence_status": "needs_validation", "issue_url": None,
            "confidence": "medium", "change_type": "fix",
            "problem_statement": "The scheduler returns the wrong result.",
            "detecting_test": "example_test checks the changed result",
            "fail_to_pass": ["example_test"], "fail_type": "runtime_assert",
            "config_overrides": {}, "readiness": "ready_to_generate",
        }
        self.assertEqual(triage.check_verdict(verdict, verdict["case"], record), [])
        self.assertEqual(generator.metadata(record, 11, verdict)["fail_to_pass"],
                         ["example_test"])

        verdict["readiness"] = "needs_reviewed_recipe"
        with self.assertRaisesRegex(ValueError, "readiness"):
            generator.metadata(record, 11, verdict)

    def test_banners_come_from_ostest_main_calls_of_changed_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            ostest = Path(tmp)
            (ostest / "ostest_main.c").write_text(
                'static void stdio_test(void)\n{\n}\n'
                '  printf("\\nuser_main: cancel test\\n");\n  cancel_test();\n'
                '  printf("\\nuser_main: condition variable test\\n");\n'
                '  pthread_attr_init(&attr);\n  cond_test();\n')
            (ostest / "cancel.c").write_text(
                "static void helper(void)\n{\n}\n\nvoid cancel_test(void)\n{\n}\n")
            (ostest / "cond.c").write_text("void cond_test(void)\n{\n}\n")
            (ostest / "ostest.h").write_text("void cancel_test(void);\n")
            changed = ["testing/ostest/cancel.c", "testing/ostest/cond.c",
                       "testing/ostest/ostest.h", "testing/ostest/ostest_main.c"]
            self.assertEqual(render.ostest_banners(ostest, changed),
                             ["user_main: cancel test", "user_main: condition variable test"])
            with self.assertRaisesRegex(ValueError, "banner"):
                render.ostest_banners(ostest, ["testing/ostest/ostest.h"])

    def test_banner_before_a_config_skip_does_not_prove_the_test_ran(self):
        with tempfile.TemporaryDirectory() as tmp:
            ostest = Path(tmp)
            (ostest / "ostest_main.c").write_text(
                '  printf("\\nuser_main: condition variable test\\n");\n'
                '#ifdef CONFIG_PRIORITY_INHERITANCE\n  printf("Skipping\\n");\n'
                '#else\n  cond_test();\n#endif\n')
            (ostest / "cond.c").write_text("void cond_test(void)\n{\n}\n")
            with self.assertRaisesRegex(ValueError, "cond_test"):
                render.ostest_banners(ostest, ["testing/ostest/cond.c"])

    def test_empty_tag_suffix_gives_the_standard_image_tag(self):
        meta = {
            "instance_id": "nuttx__nuttx-11", "project": "nuttx",
            "kernel_repo": "https://github.com/apache/nuttx",
            "apps_repo": "https://github.com/apache/nuttx-apps",
            "fail_type": "runtime_assert", "kernel_base_commit": "1" * 40,
            "kernel_merge_commit": "2" * 40, "apps_base_commit": "3" * 40,
            "files_changed_by_fix": ["sched/example.c"],
            "files_changed_by_test_patch": ["testing/ostest/example.c"],
            "test_banners": ["user_main: example test"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            patch_file = Path(tmp) / "test.diff"
            patch_file.write_text("diff --git a/testing/ostest/example.c b/testing/ostest/example.c\n")
            inst = render.render(meta, patch_file, Path(tmp) / "out", tag_suffix="")
            self.assertEqual(json.loads((inst / "metadata.json").read_text())["docker_image"],
                             "embedeval:nuttx-11")
            with self.assertRaisesRegex(ValueError, "tag_suffix"):
                render.render(meta, patch_file, Path(tmp) / "out2", tag_suffix="gen")

    def test_runner_lists_each_banner_as_one_shell_word(self):
        runner = render.render_runner(["user_main: cancel test", "user_main: POSIX timer test"])
        self.assertIn("BANNERS=('user_main: cancel test' 'user_main: POSIX timer test')", runner)
        self.assertNotIn("@@", runner)

    def test_generator_cli_uses_pair_in_triage_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "verdict.json"
            verdict.write_text(json.dumps({"case": "apps-7-kernel-11",
                                           "kernel_pr": 11, "verdict": "accept"}))
            with patch.object(sys, "argv", ["generate_nuttx_instance.py",
                                            "--triage", str(verdict)]), \
                 patch.object(generator, "generate", return_value=Path(tmp) / "instance") as run:
                generator.main()
            self.assertEqual(run.call_args.args[:2], (7, 11))

    def test_pair_only_run_cannot_be_scored_as_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "manifest.json").write_text(
                json.dumps({"cases": ["apps-7-kernel-11"]}))
            (run_dir / "apps-7-kernel-11.json").write_text(
                json.dumps({"case": "apps-7-kernel-11", "verdict": "accept"}))
            with self.assertRaisesRegex(ValueError, "no gold cases"):
                triage.eval_run(run_dir)

    def test_gold_scorecard_needs_only_gold_labels_and_kernel_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gold = root / "gold.json"
            gold.write_text(json.dumps({"accept": {"apps-7": {"kernel_pr": 11}},
                                        "reject": {"apps-8": "no detecting test"}}))
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text(
                json.dumps({"cases": ["apps-7", "apps-8"]}))
            (run_dir / "apps-7.json").write_text(
                json.dumps({"verdict": "accept", "kernel_pr": 11,
                            "test_paths": ["testing/ostest/example.c"]}))
            (run_dir / "apps-8.json").write_text(json.dumps({"verdict": "reject"}))
            with patch.object(triage, "GOLD", gold), patch.object(triage, "ROOT", root):
                result = triage.eval_run(run_dir)

            self.assertEqual((result["correct"], result["scored"]), (2, 2))
            self.assertEqual(result["rows"][0]["expected_kernel_pr"], 11)
            self.assertTrue(result["rows"][0]["kernel_match"])
            self.assertNotIn("test_paths_overlap", result["rows"][0])
            self.assertNotIn("expected_platform", result["rows"][0])
            scorecard = (run_dir / "scorecard.md").read_text()
            self.assertIn("Kernel PR (expected)", scorecard)
            self.assertNotIn("Test paths", scorecard)
            self.assertNotIn("Platform (expected)", scorecard)


class QemuLeadTests(unittest.TestCase):
    """Only sim:nsh pairs are accepted; QEMU-only pairs are rejected as leads."""

    def test_accept_must_run_on_sim(self):
        record = pair_record()
        verdict = {"case": "apps-7", "verdict": "accept", "reason": "r", "kernel_pr": 11,
                   "platform": "qemu-armv8a:nsh_smp"}
        issues = triage.check_verdict(verdict, "apps-7", record)
        self.assertTrue(any("qemu_lead" in issue for issue in issues))

    def test_reject_may_carry_a_checked_qemu_lead(self):
        record = pair_record()
        lead = {"platform": "qemu-armv8a:nsh_smp", "kernel_pr": 11,
                "confidence": "medium", "why": "the needed arch hook is arm64-only"}
        verdict = {"case": "apps-7", "verdict": "reject", "reason": "needs QEMU",
                   "qemu_lead": lead}
        self.assertEqual(triage.check_verdict(verdict, "apps-7", record), [])
        verdict["qemu_lead"] = {**lead, "platform": "sim:nsh"}
        self.assertTrue(triage.check_verdict(verdict, "apps-7", record))
        verdict["qemu_lead"] = {**lead, "kernel_pr": 99}
        self.assertTrue(triage.check_verdict(verdict, "apps-7", record))
        verdict["qemu_lead"] = None
        self.assertEqual(triage.check_verdict(verdict, "apps-7", record), [])

    def test_unchanged_test_paths_are_named_for_removal_with_the_allowed_files(self):
        record = pair_record()
        record["apps"]["url"] = "https://example.test/apps"
        verdict = {"case": "apps-7", "verdict": "accept", "reason": "r", "kernel_pr": 11,
                   "platform": "sim:nsh",
                   "test_paths": ["testing/ostest/example.c", "testing/ostest/Makefile"],
                   "fix_paths": ["sched/example.c"]}
        issue = next(i for i in triage.check_verdict(verdict, "apps-7", record)
                     if i.startswith("test_paths"))
        self.assertIn("remove ['testing/ostest/Makefile']", issue)
        self.assertIn("Choose only from the files the apps PR changed: "
                      "['testing/ostest/example.c']", issue)

    def test_leads_collect_the_latest_run_per_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            for run, why in (("2026-01-01_000000_m", "old"), ("2026-01-02_000000_m", "new")):
                (runs / run).mkdir(parents=True)
                (runs / run / "qemu_leads.jsonl").write_text(json.dumps(
                    {"case": "apps-7", "platform": "qemu-armv8a:nsh", "kernel_pr": 11,
                     "confidence": "high", "why": why}) + "\n")
            out = Path(tmp) / "leads.jsonl"
            with patch.object(triage, "RUNS", runs), patch.object(triage, "QEMU_LEADS", out), \
                 patch.object(triage, "ROOT", Path(tmp)), patch("builtins.print"):
                leads = triage.collect_leads()
            self.assertEqual([lead["why"] for lead in leads], ["new"])
            self.assertEqual(json.loads(out.read_text())["run"], "2026-01-02_000000_m")


class RepositoryToolTests(unittest.TestCase):
    """read_file, list_dir and grep read a local clone at the PR's commits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = Path(self.tmp.name) / "kernel"
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        repo.mkdir()
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (repo / "sched").mkdir()
        (repo / "sched" / "Kconfig").write_text("config SCHED_EVENTS\n\tbool \"Events\"\n")
        git("add", "-A"); git("commit", "-qm", "base")
        self.base = git("rev-parse", "HEAD")
        (repo / "sched" / "Kconfig").write_text("config SCHED_EVENTS\n\tbool \"Events\"\n\tdefault n\n")
        git("commit", "-qam", "head")
        self.head = git("rev-parse", "HEAD")
        self.record = {"apps": {"base_commit": self.base, "head_commit": self.head},
                       "kernel_candidates": [{"number": 11, "base_commit": self.base,
                                              "head_commit": self.head}]}
        self.clones = patch.dict(triage.CLONES, {"kernel": repo, "apps": repo})
        self.clones.start()

    def tearDown(self):
        self.clones.stop()
        self.tmp.cleanup()

    def tool(self, name, **args):
        return triage.run_tool(name, {"repo": "kernel", **args}, self.record)

    def test_read_file_numbers_lines_at_the_pinned_commit(self):
        self.assertNotIn("default n", self.tool("read_file", path="sched/Kconfig", ref="base"))
        head = self.tool("read_file", path="sched/Kconfig", ref="head")
        self.assertIn("    3  \tdefault n", head)
        self.assertIn("lines in total", self.tool("read_file", path="sched/Kconfig",
                                                  ref="head", max_lines=1))

    def test_list_dir_and_grep_search_the_tree(self):
        self.assertIn("tree  sched", self.tool("list_dir", path="", ref="head"))
        self.assertIn("sched/Kconfig:3:", self.tool("grep", pattern="default n", path="", ref="head"))
        self.assertEqual(self.tool("grep", pattern="default n", path="sched", ref="base"),
                         "(no matches)")

    def test_bad_requests_return_errors_instead_of_raising(self):
        self.assertIn("invalid", self.tool("read_file", path="../etc/passwd", ref="head"))
        self.assertIn("ref must be", self.tool("read_file", path="sched/Kconfig", ref="merge"))
        self.assertIn("unknown tool", self.tool("rm", path="sched", ref="head"))
        self.record["kernel_candidates"][0]["head_commit"] = "0" * 40
        with patch.object(triage, "git", wraps=triage.git) as git:
            self.assertIn("unavailable", self.tool("read_file", path="sched/Kconfig", ref="head"))
            self.assertTrue(any(c.args[1] == "fetch" for c in git.call_args_list))


if __name__ == "__main__":
    unittest.main()
