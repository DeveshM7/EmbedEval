"""Behavior checks for the per-apps-PR NuttX triage: records, tools, checks,
scoring, gold promotion, the conversation budget and the generator."""

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nuttx_candidates as candidates
import triage_nuttx_prs as triage
import generate_nuttx_instance as generator


def apps_record():
    return {
        "project": "nuttx",
        "apps": {"repo": "apache/nuttx-apps", "number": 7, "url": "https://example.test/apps/7",
                 "title": "ostest: check example", "merged_at": "2024-01-02",
                 "base_commit": "a0", "head_commit": "a1", "body": "",
                 "files": [{"filename": "testing/ostest/example.c", "status": "modified"}],
                 "commits": [], "comments": [], "review_comments": [], "reviews": [],
                 "timeline": []},
        "test_contents": {"testing/ostest/example.c": "void example_test(void) {}"},
    }


def kernel_pr(number=11):
    return {"repo": "apache/nuttx", "number": number, "url": f"https://example.test/k/{number}",
            "title": "sched: fix example", "merged_at": "2024-01-01", "base_commit": "k0",
            "head_commit": "k1", "files": [{"filename": "sched/example.c", "status": "modified"}]}


def full_pair(**changes):
    pair = {"kernel_pr": 11, "confidence": "high", "evidence": ["apps body links #11"],
            "tag": "bug_test", "platform": "sim:nsh",
            "test_paths": ["testing/ostest/example.c"], "fix_paths": ["sched/example.c"],
            "fail_type": "runtime_assert", "config_overrides": {},
            "fail_to_pass": ["example_test"], "problem_statement": "Wrong result.",
            "readiness": "ready_to_generate"}
    pair.update(changes)
    return pair


def verdict(*pairs, kind="kernel_test", search=None):
    sent = any(p.get("confidence") in ("high", "medium") for p in pairs)
    return {"case": "apps-7", "kind": kind, "reason": "The test checks the fix.",
            "kernel_search": search or ("found" if sent else "nothing_found"),
            "searched": ["apps body"], "pairs": list(pairs)}


class AppsRecordTests(unittest.TestCase):
    def test_generator_uses_node_local_apps_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "apps.git").mkdir()
            with patch.dict(os.environ, {"EMBEDEVAL_NUTTX_DATA": str(root)}), \
                 patch.object(generator, "APPS_CLONE", root / "unused"):
                self.assertEqual(generator.apps_clone(), root / "apps.git")

    def test_generator_uses_cached_apps_commits_without_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "apps"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
                           check=True)
            (repo / "a.c").write_text("a\n")
            subprocess.run(["git", "-C", str(repo), "add", "a.c"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "a"], check=True,
                           capture_output=True)
            sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                 capture_output=True, text=True).stdout.strip()
            generator.ensure_apps_commits(repo, sha)

    def test_ostest_population_comes_from_changed_paths(self):
        scan = {"prs": {
            "5": {"files": {"nodes": [{"path": "testing/ostest/sem.c"}]}},
            "6": {"files": {"nodes": [{"path": "testing/drivers/x.c"}]}},
            "4": {"files": {"nodes": [{"path": "README"}, {"path": "testing/ostest/a.c"}]}}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.json"
            path.write_text(json.dumps(scan))
            self.assertEqual(candidates.ostest_apps_prs(path), [4, 5])

    def test_apps_record_has_no_partner_and_keeps_timeline(self):
        pr = {"number": 7, "head_commit": "a1", "repo": candidates.APPS,
              "files": [{"filename": "testing/ostest/example.c", "status": "modified"}]}
        timeline = [{"event": "cross-referenced", "created_at": "2024-01-03",
                     "source": {"issue": {"number": 11, "title": "fix",
                                          "repository_url": "https://api.github.com/repos/apache/nuttx",
                                          "pull_request": {"merged_at": "2024-01-04"},
                                          "html_url": "https://github.com/apache/nuttx/pull/11"}}},
                    {"event": "labeled"}]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(candidates, "APPS_RECORDS", Path(tmp) / "apps"), \
             patch.object(candidates, "GITHUB_CACHE", Path(tmp) / "github"), \
             patch.object(candidates, "full_pr", return_value=pr) as full, \
             patch.object(candidates, "paged", return_value=timeline), \
             patch.object(candidates, "source_at", return_value="source"):
            record = candidates.apps_record(7)
            self.assertNotIn("kernel_candidates", record)
            self.assertEqual(record["apps"]["timeline"][0]["repo"], "apache/nuttx")
            self.assertEqual(record["apps"]["timeline"][0]["merged_at"], "2024-01-04")
            self.assertEqual(len(record["apps"]["timeline"]), 1)
            self.assertEqual(record["test_contents"]["testing/ostest/example.c"], "source")
            candidates.apps_record(7)   # cached: no second fetch
            self.assertEqual(full.call_count, 1)

    def test_rendered_apps_record_shrinks_to_its_limit(self):
        record = apps_record()
        record["apps"]["files"] = [{"filename": f"apps/f{i}.c", "status": "modified",
                                    "patch": "x" * 5000} for i in range(100)]
        with patch.object(triage, "RECORD_LIMIT", 60000):
            text = triage.render_apps_record("apps-7", record)
        self.assertLessEqual(len(text), 60000)
        self.assertIn("apps/f99.c", text)
        self.assertNotIn("kernel_candidates", text)


class AppsVerdictTests(unittest.TestCase):
    def record(self):
        return {**apps_record(), "kernel_candidates": [kernel_pr()]}

    def test_pair_needs_a_dated_followup_search_on_its_fix_path(self):
        record = self.record()
        record["followup_searches"] = []
        reply = verdict(full_pair())
        self.assertTrue(any("follow-up" in issue for issue in
                            triage.check_apps_verdict(reply, "apps-7", record)))
        record["followup_searches"] = [
            {"repo": "kernel", "since": "2024-01-01", "until": "2024-01-02",
             "paths": ["sched/unrelated.c"]}]
        self.assertTrue(any("follow-up" in issue for issue in
                            triage.check_apps_verdict(reply, "apps-7", record)))
        record["followup_searches"][0]["paths"] = ["sched/example.c"]
        self.assertEqual(triage.check_apps_verdict(reply, "apps-7", record), [])

    def test_complete_pair_passes(self):
        self.assertEqual(triage.check_apps_verdict(verdict(full_pair()), "apps-7", self.record()), [])

    def test_low_pair_needs_only_number_and_evidence(self):
        low = {"kernel_pr": 99, "confidence": "low", "evidence": ["only a CI mention"]}
        self.assertEqual(triage.check_apps_verdict(verdict(low, kind="test_only"),
                                                   "apps-7", self.record()), [])
        del low["evidence"]
        self.assertTrue(any("evidence" in i for i in triage.check_apps_verdict(
            verdict(low, kind="test_only"), "apps-7", self.record())))

    def test_kind_and_search_must_agree_with_the_pairs(self):
        issues = triage.check_apps_verdict(verdict(full_pair(confidence="medium"), kind="apps_fix"),
                                           "apps-7", self.record())
        self.assertTrue(any("kind apps_fix" in i for i in issues))
        issues = triage.check_apps_verdict(verdict(search="found"), "apps-7", self.record())
        self.assertTrue(any("kernel_search found" in i for i in issues))
        issues = triage.check_apps_verdict(verdict(full_pair(), search="nothing_found"),
                                           "apps-7", self.record())
        self.assertTrue(any("nothing_found" in i for i in issues))
        reply = verdict(full_pair())
        reply["searched"] = []
        self.assertTrue(any("searched" in i for i in triage.check_apps_verdict(
            reply, "apps-7", self.record())))

    def test_proposed_pair_is_checked_against_the_kernel_pr(self):
        issues = triage.check_apps_verdict(verdict(full_pair(kernel_pr=12)), "apps-7", self.record())
        self.assertTrue(any("not a pull request" in i for i in issues))
        issues = triage.check_apps_verdict(verdict(full_pair(fix_paths=["sched/other.c"])),
                                           "apps-7", self.record())
        self.assertTrue(any("fix_paths" in i and "kernel #11" in i for i in issues))
        issues = triage.check_apps_verdict(verdict(full_pair(tag="fix")), "apps-7", self.record())
        self.assertTrue(any("tag" in i for i in issues))
        issues = triage.check_apps_verdict(verdict(full_pair(), full_pair()), "apps-7", self.record())
        self.assertTrue(any("once" in i for i in issues))

    def test_qemu_pair_is_a_lead_needing_a_fixture(self):
        qemu = full_pair(platform="qemu-armv8a:nsh_smp", readiness="needs_fixture")
        self.assertEqual(triage.check_apps_verdict(verdict(qemu, kind="qemu_only"),
                                                   "apps-7", self.record()), [])
        issues = triage.check_apps_verdict(verdict(full_pair(), kind="qemu_only"),
                                           "apps-7", self.record())
        self.assertTrue(any("qemu_only" in i for i in issues))


class ScoreTests(unittest.TestCase):
    gold = {
        "apps-1": {"kind": "kernel_test",
                   "must_find": [{"kernel_pr": 10, "platform": "sim:nsh", "tag": "bug_test"}],
                   "must_not_propose": [{"kernel_pr": 13, "why": "CI note"}]},
        "apps-2": {"kind": ["test_only", "apps_fix"], "must_find": [],
                   "must_not_propose": [{"kernel_pr": 20, "why": "exposed only"}]},
        "apps-3": {"kind": "qemu_only", "must_find": [{"kernel_pr": 30, "platform": "qemu"}]},
    }

    def test_partial_gold_scores_recall_and_violations_but_not_extras(self):
        verdicts = {
            "apps-1": {"kind": "kernel_test", "pairs": [
                # The gold says bug_test: tags are recorded, not scored.
                {"kernel_pr": 10, "confidence": "medium", "platform": "sim:nsh",
                 "tag": "behavior_change"},
                {"kernel_pr": 11, "confidence": "high", "platform": "sim:nsh"},
                {"kernel_pr": 13, "confidence": "low"}]},
            "apps-2": {"kind": "apps_fix", "pairs": [{"kernel_pr": 20, "confidence": "low"}]},
            "apps-3": {"kind": "qemu_only", "pairs": [
                {"kernel_pr": 30, "confidence": "high", "platform": "qemu-armv8a:nsh_smp"}]},
        }
        result = triage.score_apps(self.gold, verdicts)
        self.assertEqual((result["correct"], result["scored"]), (3, 3))
        self.assertEqual((result["must_find_found"], result["must_find_total"]), (2, 2))
        self.assertEqual(result["violations"], 0)
        self.assertEqual([(e["case"], e["kernel_pr"]) for e in result["unknown_extras"]],
                         [("apps-1", 11)])

    def test_missing_wrong_platform_and_forbidden_pairs_fail(self):
        verdicts = {
            "apps-1": {"kind": "kernel_test", "pairs": [
                {"kernel_pr": 10, "confidence": "low"},
                {"kernel_pr": 13, "confidence": "medium"}]},
            "apps-2": {"kind": "kernel_test", "pairs": []},
            "apps-3": {"kind": "qemu_only", "pairs": [
                {"kernel_pr": 30, "confidence": "high", "platform": "sim:nsh"}]},
        }
        rows = {row["case"]: row for row in triage.score_apps(self.gold, verdicts)["rows"]}
        self.assertEqual((rows["apps-1"]["missing"], rows["apps-1"]["violations"]), ([10], [13]))
        self.assertFalse(rows["apps-2"]["kind_ok"])
        self.assertEqual(rows["apps-3"]["missing"], [30])
        self.assertFalse(any(row["correct"] for row in rows.values()))

    def test_eval_writes_a_scorecard_for_the_run_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, gold = Path(tmp) / "run", Path(tmp) / "gold.json"
            run_dir.mkdir()
            gold.write_text(json.dumps({"cases": self.gold}))
            (run_dir / "manifest.json").write_text(json.dumps(
                {"mode": "apps", "blind": True, "cases": ["apps-2", "apps-9"]}))
            (run_dir / "apps-2.json").write_text(json.dumps(
                {"kind": "budget_exhausted", "pairs": [],
                 "budget": {"tool_calls": 40, "seconds": 1800.0, "exhausted": "time"}}))
            with patch("builtins.print"):
                result = triage.eval_apps_run(run_dir, gold)
            self.assertEqual(result["scored"], 1)
            card = (run_dir / "scorecard.md").read_text()
            self.assertIn("blind", card)
            self.assertIn("Budget exhausted: 1", card)


class PromoteTests(unittest.TestCase):
    def instance(self, root, kernel, valid, reason):
        inst = root / f"nuttx__nuttx-{kernel}"
        inst.mkdir()
        (inst / "metadata.json").write_text(json.dumps(
            {"apps_pr": 7, "kernel_pr": kernel, "sim_config": "sim:nsh"}))
        (inst / "validation.json").write_text(json.dumps({"valid": valid, "reason": reason}))
        return inst

    def test_valid_pairs_become_must_find_and_failures_wait_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gold = root / "gold.json"
            gold.write_text(json.dumps({"cases": {"apps-7": {
                "kind": "kernel_test", "must_find": [],
                "must_not_propose": [{"kernel_pr": 13, "why": "CI note"}]}}}))
            dirs = [self.instance(root, 11, True, "fails before the fix, passes after"),
                    self.instance(root, 12, False, "tests still fail with the fix"),
                    self.instance(root, 13, True, "fails before the fix, passes after")]
            with patch("builtins.print"):
                triage.promote(dirs, gold)
                triage.promote(dirs[1:2], gold)   # promoting twice adds nothing
            data = json.loads(gold.read_text())
            case = data["cases"]["apps-7"]
            self.assertEqual([item["kernel_pr"] for item in case["must_find"]], [11])
            self.assertEqual([item["kernel_pr"] for item in case["must_not_propose"]], [13])
            self.assertEqual([item["kernel_pr"] for item in data["needs_review"]], [12, 13])


class BlindTests(unittest.TestCase):
    def test_links_hashes_and_partner_numbers_are_hidden(self):
        record = apps_record()
        record["apps"].update({
            "title": "Changes needed by nuttx PR 8885",
            "body": "See https://github.com/apache/nuttx/pull/8885 and apache/nuttx#8886. "
                    "Since commit 5a209a853e the call returns -ENOENT. apps#12 stays.",
            "commits": [{"sha": "a" * 40, "message": "cites 5a209a853ec0dac623"}],
            "timeline": [{"event": "cross-referenced", "repo": "apache/nuttx", "number": 9},
                         {"event": "cross-referenced", "repo": "apache/nuttx-apps", "number": 12},
                         {"event": "referenced", "commit_id": "b" * 40}]})
        blind = triage.blind_record(record, [8885])
        apps = blind["apps"]
        self.assertEqual(apps["title"], "Changes needed by nuttx PR [hidden]")
        for text in (apps["body"], apps["commits"][0]["message"]):
            self.assertNotIn("8885", text)
            self.assertNotIn("8886", text)
            self.assertNotIn("5a209a853e", text)
        self.assertIn("-ENOENT", apps["body"])
        self.assertIn("apps#12", apps["body"])
        self.assertEqual([e["number"] for e in apps["timeline"]], [12])
        self.assertEqual(blind["blind"], [8885])
        self.assertIn("5a209a853e", record["apps"]["body"])   # the original is untouched


class HistoryToolTests(unittest.TestCase):
    """git_log, open_ref and reads at any revision, on a local repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = Path(self.tmp.name) / "kernel"

        def git(*args, date="2024-01-01T12:00:00+0000"):
            env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
                   "HOME": self.tmp.name, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
            return subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                                  capture_output=True, text=True).stdout.strip()
        self.git = git
        repo.mkdir()
        git("init", "-q", "-b", "master")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (repo / "sched").mkdir()
        (repo / "sched" / "wq.c").write_text("int work_cancel(void) { return 0; }\n")
        git("add", "-A")
        git("commit", "-qm", "sched: add work_cancel")
        self.first = git("rev-parse", "HEAD")
        (repo / "sched" / "wq.c").write_text("int work_cancel(void) { return -ENOENT; }\n")
        (repo / "README").write_text("docs\n")
        git("add", "-A")
        git("commit", "-qm", "sched/wqueue: restore -ENOENT", date="2024-03-01T12:00:00+0000")
        self.second = git("rev-parse", "HEAD")
        self.record = {**apps_record(), "kernel_candidates": []}
        self.clones = patch.dict(triage.CLONES, {"kernel": repo, "apps": repo})
        self.clones.start()

    def tearDown(self):
        self.clones.stop()
        self.tmp.cleanup()

    def tool(self, name, **args):
        return triage.run_tool(name, args, self.record, free=True)

    def test_git_log_filters_by_message_symbol_dates_and_paths(self):
        self.assertIn("restore -ENOENT", self.tool("git_log", message="enoent"))
        self.assertNotIn("add work_cancel", self.tool("git_log", message="enoent"))
        hits = self.tool("git_log", symbol="-ENOENT")
        self.assertIn(self.second[:7], hits)
        self.assertIn("\n    sched/wq.c", hits)   # -S lists only the files that match
        self.assertIn("add work_cancel", self.tool("git_log", until="2024-02-01"))
        self.assertNotIn("add work_cancel", self.tool("git_log", since="2024-02-01"))
        self.assertIn("restore", self.tool("git_log", paths=["README"]))
        self.assertIn("stopped at 1", self.tool("git_log", paths=["sched"], max_results=1))
        self.assertIn("at least one", self.tool("git_log"))
        self.assertIn("YYYY-MM-DD", self.tool("git_log", since="last week"))
        self.assertIn("relative", self.tool("git_log", paths=["../x"]))

    def test_followup_review_names_later_commits_on_the_fix_paths(self):
        record = {**self.record, "kernel_candidates": [
            {**kernel_pr(), "merged_at": "2024-01-01T13:00:00Z", "commits": [{"sha": self.first}],
             "merge_commit_sha": None}]}
        record["apps"] = {**record["apps"], "merged_at": "2024-04-01T00:00:00Z"}
        reply = verdict(full_pair(fix_paths=["sched/wq.c"]))
        issues = triage.followup_review_issues(reply, record)
        self.assertEqual(len(issues), 1)
        self.assertIn(self.second[:7], issues[0])
        self.assertIn("restore -ENOENT", issues[0])
        reply["pairs"][0]["evidence"].append(f"{self.second[:7]} only changes a return code")
        self.assertEqual(triage.followup_review_issues(reply, record), [])

    def test_git_log_path_search_includes_merged_branch_changes(self):
        repo = triage.CLONES["kernel"]
        self.git("checkout", "-qb", "followup")
        (repo / "sched" / "wq.c").write_text("int work_cancel(void) { return -EAGAIN; }\n")
        self.git("add", "sched/wq.c")
        self.git("commit", "-qm", "follow-up fix", date="2024-03-02T12:00:00+0000")
        followup = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "master")
        (repo / "sched" / "wq.c").write_text("int work_cancel(void) { return -EAGAIN; }\n")
        self.git("add", "sched/wq.c")
        self.git("commit", "-qm", "parallel fix", date="2024-03-03T12:00:00+0000")
        self.git("merge", "--no-ff", "-q", "-m", "merge follow-up", "followup",
                 date="2024-03-04T12:00:00+0000")
        hits = self.tool("git_log", since="2024-03-02", until="2024-03-04",
                         paths=["sched/wq.c"])
        self.assertIn(followup[:7], hits)

    def test_reads_resolve_dates_and_shas(self):
        self.assertIn("return 0", self.tool("read_file", repo="kernel", path="sched/wq.c",
                                            ref="2024-02-01"))
        self.assertIn("-ENOENT", self.tool("read_file", repo="kernel", path="sched/wq.c",
                                           ref="2024-03-01"))
        self.assertIn("return 0", self.tool("read_file", repo="kernel", path="sched/wq.c",
                                            ref=self.first[:10]))
        self.assertIn("no kernel commit", self.tool("list_dir", repo="kernel", path="",
                                                    ref="2023-01-01"))
        self.assertIn("needs kernel_pr", self.tool("list_dir", repo="kernel", path="", ref="head"))
        self.assertIn("ref must be", self.tool("list_dir", repo="kernel", path="", ref="main"))

    def test_kernel_pr_base_and_head_are_loaded_on_demand(self):
        kernel = {**kernel_pr(11), "base_commit": self.first, "head_commit": self.second}
        with patch.object(candidates, "kernel_record", return_value=kernel) as load:
            self.assertIn("-ENOENT", self.tool("read_file", repo="kernel", path="sched/wq.c",
                                               ref="head", kernel_pr=11))
            self.tool("read_file", repo="kernel", path="sched/wq.c", ref="base", kernel_pr=11)
        self.assertEqual(load.call_count, 1)
        with patch.object(candidates, "kernel_record",
                          side_effect=RuntimeError("gh api x: HTTP 404")):
            self.assertIn("not a pull request", self.tool("list_dir", repo="kernel", path="",
                                                          ref="head", kernel_pr=12))

    def test_open_ref_resolves_shas_locally_and_hides_apps_text_when_blind(self):
        commit = {"repo": "apache/nuttx", "sha": self.second, "url": None, "date": "2024-03-01",
                  "parents": [self.first], "message": "sched/wqueue: restore -ENOENT",
                  "files": [{"filename": "sched/wq.c", "status": "modified", "patch": "@@ -1 +1 @@"}],
                  "prs": [{"number": 11, "title": "restore", "merged_at": "2024-03-02",
                           "url": "u"}]}
        # The test repository stands in for both clones: a short SHA is then
        # in both, so give repo.
        with patch.object(candidates, "commit_record", return_value=commit) as fetch:
            self.assertIn("both repositories", self.tool("open_ref", ref=self.second[:9]))
            out = self.tool("open_ref", ref=self.second[:9], repo="kernel")
        self.assertEqual(fetch.call_args.args, ("apache/nuttx", self.second))
        self.assertIn('apache/nuttx#11 "restore"', out)
        item = {"kind": "pr", **apps_record()["apps"], "body": "needs apache/nuttx#8885 at 5a209a853e",
                "commits": [], "timeline": [{"event": "cross-referenced", "repo": "apache/nuttx",
                                             "number": 8885, "title": "t", "is_pr": True,
                                             "merged_at": "x", "created_at": "2024"}]}
        with patch.object(candidates, "issue_or_pr", return_value=item) as fetch:
            seen = self.tool("open_ref", ref="https://github.com/apache/nuttx-apps/pull/7")
            self.record["blind"] = [8885]
            hidden = self.tool("open_ref", ref="apache/nuttx-apps#7")
        self.assertEqual(fetch.call_args.args, ("apache/nuttx-apps", 7))
        self.assertIn("Links mentioned: apache/nuttx#8885, commit 5a209a853e", seen)
        self.assertNotIn("8885", hidden)
        self.assertNotIn("5a209a853e", hidden)
        self.assertIn("needs repo", self.tool("open_ref", ref="12"))


def fake_litellm(replies):
    """A litellm stand-in that returns the scripted replies in order."""
    def message(reply):
        calls = [types.SimpleNamespace(
            id=f"c{i}", function=types.SimpleNamespace(name=name, arguments=json.dumps(args)),
            model_dump=lambda name=name, args=args, i=i: {"id": f"c{i}", "type": "function",
                                                          "function": {"name": name,
                                                                       "arguments": json.dumps(args)}})
            for i, (name, args) in enumerate(reply.get("tools", []))]
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content=reply.get("content"), tool_calls=calls,
                                              reasoning_content=None),
                finish_reason="stop")],
            usage=types.SimpleNamespace(prompt_tokens=100, completion_tokens=10))
    queue = list(replies)
    module = types.ModuleType("litellm")
    module.completion = lambda **kwargs: (
        module.calls.append({**kwargs, "messages": list(kwargs["messages"])}),
        message(queue.pop(0)))[1]
    module.calls = []
    return module


class AppsConversationTests(unittest.TestCase):
    def run_case(self, replies, **budget):
        llm = fake_litellm(replies)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = Path(tmp.name)
        record = apps_record()
        with patch.dict(sys.modules, {"litellm": llm}), \
             patch.object(triage, "run_tool", return_value="tool output") as tool, \
             patch.object(candidates, "kernel_record", return_value=kernel_pr()):
            result = triage.triage_apps_one("apps-7", record, "m", {}, "http://x", run_dir,
                                            budget.get("max_calls", 40),
                                            budget.get("max_minutes", 30))
        output = json.loads((run_dir / "apps-7.json").read_text())
        return result, output, llm, tool, run_dir

    def test_search_then_verdict_writes_output_and_qemu_leads(self):
        reply = verdict(full_pair(), {"kernel_pr": 12, "confidence": "low",
                                      "platform": "qemu-armv8a:nsh", "evidence": ["smp"]})
        result, output, llm, tool, run_dir = self.run_case([
            {"tools": [("git_log", {"message": "example"}),
                       ("open_ref", {"ref": "apache/nuttx#11"}),
                       ("git_log", {"repo": "kernel", "since": "2024-01-01",
                                    "until": "2024-01-02", "paths": ["sched/example.c"]})]},
            {"content": json.dumps(reply)}])
        self.assertEqual(result["issues"], [])
        self.assertEqual(tool.call_count, 3)
        self.assertTrue(all(call.kwargs.get("free") for call in tool.call_args_list))
        self.assertEqual(output["kind"], "kernel_test")
        self.assertEqual(output["budget"], {"tool_calls": 3, "seconds": output["budget"]["seconds"],
                                            "exhausted": None})
        lead = json.loads((run_dir / "qemu_leads.jsonl").read_text())
        self.assertEqual((lead["case"], lead["kernel_pr"]), ("apps-7", 12))

    def test_tool_budget_withdraws_tools_and_is_recorded(self):
        result, output, llm, tool, _ = self.run_case([
            {"tools": [("git_log", {"message": "a"}), ("git_log", {"message": "b"})]},
            {"content": json.dumps(verdict())}], max_calls=2)
        self.assertNotIn("tools", llm.calls[1])
        self.assertIn("Tool budget reached", llm.calls[1]["messages"][-1]["content"])
        self.assertEqual(output["budget"]["exhausted"], "tool")
        self.assertEqual(output["kernel_search"], "nothing_found")

    def test_no_valid_answer_after_the_budget_is_budget_exhausted(self):
        _, output, _, _, _ = self.run_case(
            [{"tools": [("git_log", {"message": "a"})]}] + [{"content": "no json"}] * 3,
            max_minutes=0)
        self.assertEqual(output["kind"], "budget_exhausted")
        self.assertEqual(output["budget"]["exhausted"], "time")
        self.assertEqual(output["pairs"], [])


class GeneratorTests(unittest.TestCase):
    def test_kernel_pr_found_by_search_is_enriched_on_demand(self):
        record = apps_record()   # an apps-only record has no kernel_candidates
        reply = {**full_pair(), "verdict": "accept", "case": "apps-7-kernel-11"}
        with patch.object(candidates, "kernel_record", return_value=kernel_pr()) as load:
            meta = generator.metadata(record, 11, reply)
        load.assert_called_once_with(11)
        self.assertEqual(meta["files_changed_by_fix"], ["sched/example.c"])
        with patch.object(candidates, "kernel_record",
                          return_value={**kernel_pr(), "merged_at": None}), \
             self.assertRaisesRegex(ValueError, "not merged"):
            generator.metadata(record, 11, reply)

    def test_linked_kernel_candidate_is_used_without_fetching(self):
        record = {**apps_record(), "kernel_candidates": [kernel_pr()]}
        reply = {**full_pair(), "verdict": "accept", "case": "apps-7-kernel-11"}
        with patch.object(candidates, "kernel_record", side_effect=AssertionError("fetched")):
            self.assertEqual(generator.metadata(record, 11, reply)["kernel_pr"], 11)

    def test_pair_verdict_selects_one_high_or_medium_pair(self):
        reply = verdict(full_pair(), full_pair(kernel_pr=12, confidence="medium"),
                        {"kernel_pr": 13, "confidence": "low", "evidence": ["x"]})
        with self.assertRaisesRegex(ValueError, "--kernel-pr"):
            generator.pair_verdict(reply, None)
        apps, kernel, ready = generator.pair_verdict(reply, 12)
        self.assertEqual((apps, kernel, ready["case"], ready["verdict"]),
                         (7, 12, "apps-7-kernel-12", "accept"))
        with self.assertRaisesRegex(ValueError, "low confidence"):
            generator.pair_verdict(reply, 13)
        with self.assertRaisesRegex(ValueError, "no pair"):
            generator.pair_verdict(reply, 14)

    def test_cli_generates_from_an_apps_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "apps-7.json"
            path.write_text(json.dumps(verdict(full_pair())))
            with patch.object(sys, "argv", ["generate_nuttx_instance.py", "--triage", str(path)]), \
                 patch.object(generator, "generate", return_value=Path(tmp) / "i") as run, \
                 patch("builtins.print"):
                generator.main()
            self.assertEqual(run.call_args.args[:2], (7, 11))
            self.assertEqual(run.call_args.args[2]["case"], "apps-7-kernel-11")


class AlsoNeedsAndConfigTests(unittest.TestCase):
    def record(self):
        return {**apps_record(), "kernel_candidates": [kernel_pr(), kernel_pr(12)]}

    def test_also_needs_entries_are_merged_prs_with_evidence(self):
        ok = full_pair(also_needs=[{"kernel_pr": 12, "evidence": ["fixes a hang in #11"]}])
        self.assertEqual(triage.check_apps_verdict(verdict(ok), "apps-7", self.record()), [])
        for bad, words in (([{"kernel_pr": 11, "evidence": ["x"]}], "another kernel_pr"),
                           ([{"kernel_pr": 12}], "evidence"),
                           ([{"kernel_pr": 13, "evidence": ["x"]}], "merged"),
                           ({"kernel_pr": 12}, "list")):
            issues = triage.check_apps_verdict(verdict(full_pair(also_needs=bad)),
                                               "apps-7", self.record())
            self.assertTrue(any(words in i for i in issues), (bad, issues))

    def test_reserved_overrides_are_refused(self):
        issues = triage.check_apps_verdict(
            verdict(full_pair(config_overrides={"CONFIG_NDEBUG": "n"})), "apps-7", self.record())
        self.assertTrue(any("reserved" in i for i in issues))

    def test_scoring_needs_also_needs_and_a_working_config(self):
        gold = {"apps-7": {"kind": "kernel_test", "must_find": [
            {"kernel_pr": 11, "platform": "qemu", "config_platform": "qemu-armv8a:nsh_smp",
             "config_required": {"CONFIG_SMP_CALL": "y"},
             "config_one_of": [{"CONFIG_A": "y", "CONFIG_B": "y"}], "also_needs": [12]}]}}
        pair = {"kernel_pr": 11, "confidence": "high", "platform": "qemu-armv8a:nsh_smp",
                "also_needs": [{"kernel_pr": 12, "evidence": ["x"]},
                               {"kernel_pr": 14, "evidence": ["extra is fine"]}]}
        seen = []

        def config_problems(case, got, required, one_of, platform):
            seen.append((case, got["kernel_pr"], required, one_of, platform))
            return []
        result = triage.score_apps(gold, {"apps-7": {"kind": "kernel_test", "pairs": [pair]}},
                                   config_problems)
        self.assertEqual(result["correct"], 1)
        self.assertEqual(seen, [("apps-7", 11, {"CONFIG_SMP_CALL": "y"},
                                 [{"CONFIG_A": "y", "CONFIG_B": "y"}], "qemu-armv8a:nsh_smp")])
        pair["also_needs"] = []
        row = triage.score_apps(gold, {"apps-7": {"kind": "kernel_test", "pairs": [pair]}},
                                config_problems)["rows"][0]
        self.assertEqual(row["config"], {11: ["also_needs lacks #12"]})
        self.assertFalse(row["correct"])
        pair["also_needs"] = [{"kernel_pr": 12, "evidence": ["x"]}]
        row = triage.score_apps(gold, {"apps-7": {"kind": "kernel_test", "pairs": [pair]}},
                                lambda *args: ["testing/ostest/t.c is not built"])["rows"][0]
        self.assertEqual(row["config"], {11: ["testing/ostest/t.c is not built"]})
        self.assertFalse(row["correct"])

    def test_eval_checks_the_fixed_side_once_and_keeps_it_with_the_run(self):
        gold = {"cases": {"apps-7": {"kind": "kernel_test", "must_find": [
            {"kernel_pr": 11, "platform": "sim:nsh",
             "config_required": {"CONFIG_SCHED_WORKQUEUE": "y"}}]}}}
        reply = verdict(full_pair(config_overrides={"CONFIG_SCHED_HPWORK": "y"}))
        fixed = {"ostest": True, "settings": {"CONFIG_SCHED_WORKQUEUE": "y"},
                 "tests": [{"path": "testing/ostest/example.c", "built": True, "runs": True}]}
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, gold_path = Path(tmp) / "run", Path(tmp) / "gold.json"
            run_dir.mkdir()
            gold_path.write_text(json.dumps(gold))
            (run_dir / "manifest.json").write_text(json.dumps({"cases": ["apps-7"]}))
            (run_dir / "apps-7.json").write_text(json.dumps(reply))
            with patch.object(candidates, "apps_record", return_value=apps_record()), \
                 patch.object(candidates, "kernel_record", return_value=kernel_pr()), \
                 patch.object(triage.nuttx_config, "check_pair",
                              return_value={"fixed": fixed}) as check, \
                 patch("builtins.print"):
                first = triage.eval_apps_run(run_dir, gold_path)
                second = triage.eval_apps_run(run_dir, gold_path)
            self.assertEqual((first["correct"], second["correct"]), (1, 1))
            self.assertEqual(check.call_count, 1)
            self.assertEqual(check.call_args.args[2], {"CONFIG_SCHED_HPWORK": "y"})
            self.assertEqual(check.call_args.kwargs["sides"], ("fixed",))
            self.assertIn("apps-7#11", json.loads((run_dir / "config_checks.json").read_text()))

    def test_generator_records_extra_fix_prs_in_merge_order(self):
        record = {**apps_record(), "kernel_candidates": [kernel_pr()]}
        later = {**kernel_pr(13), "merged_at": "2024-02-01"}
        earlier = {**kernel_pr(12), "merged_at": "2023-12-01"}
        reply = {**full_pair(also_needs=[{"kernel_pr": 13, "evidence": ["x"]},
                                         {"kernel_pr": 12, "evidence": ["y"]}]),
                 "verdict": "accept", "case": "apps-7-kernel-11"}
        with patch.object(candidates, "kernel_record",
                          side_effect=lambda n: {12: earlier, 13: later}[n]):
            meta = generator.metadata(record, 11, reply)
        self.assertEqual([e["kernel_pr"] for e in meta["extra_fix_prs"]], [12, 13])
        self.assertEqual(meta["extra_fix_prs"][0]["head_commit"], "k1")
        self.assertEqual(meta["kernel_merged_at"], "2024-01-01")
        self.assertNotIn("extra_fix_prs", generator.metadata(
            record, 11, {**full_pair(), "verdict": "accept", "case": "apps-7-kernel-11"}))

    def test_generator_accepts_qemu_armv8a_pair_for_new_fixture(self):
        record = {**apps_record(), "kernel_candidates": [kernel_pr()]}
        reply = {**full_pair(platform="qemu-armv8a:nsh_smp", readiness="needs_fixture"),
                 "verdict": "accept", "case": "apps-7-kernel-11"}
        self.assertEqual(generator.metadata(record, 11, reply)["sim_config"],
                         "qemu-armv8a:nsh_smp")


if __name__ == "__main__":
    unittest.main()
