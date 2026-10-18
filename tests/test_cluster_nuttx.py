import sys
import tempfile
import unittest
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "cluster"), str(ROOT / "scripts")]
import config as cluster_config  # noqa: E402
import node_worker  # noqa: E402
import prepare  # noqa: E402


class NuttxClusterTests(unittest.TestCase):
    def test_later_followups_rank_prs_on_the_fix_files_earliest_first(self):
        import os
        import subprocess
        import types
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            def commit(name: str, date: str, *paths: str) -> str:
                for path in paths:
                    (repo / path).parent.mkdir(parents=True, exist_ok=True)
                    (repo / path).write_text(name)
                env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
                for args in (["add", *paths], ["commit", "-qm", name]):
                    subprocess.run(["git", "-C", tmp, *args], check=True, env=env)
                return subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], check=True,
                                      capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
            for key, value in (("user.name", "T"), ("user.email", "t@example.invalid")):
                subprocess.run(["git", "-C", tmp, "config", key, value], check=True)
            commit("fix", "2025-12-18T10:00:00Z", "sched/a.c", "sched/b.c", "include/x.h")
            prs = {commit("one file", "2025-12-20T10:00:00Z", "include/x.h"): 2,
                   commit("two files", "2026-01-08T10:00:00Z", "sched/a.c", "sched/b.c"): 3,
                   commit("unrelated", "2026-01-09T10:00:00Z", "drivers/y.c"): 4,
                   commit("too late", "2026-03-01T10:00:00Z", "sched/a.c"): 5}
            files = {"sched/a.c", "sched/b.c", "include/x.h", "Documentation/z.rst"}
            def commit_record(repo_name, sha):
                n = prs[sha]
                shown = subprocess.run(["git", "-C", tmp, "show", "--name-only", "--format=", sha],
                                       capture_output=True, text=True).stdout.split()
                merged = {2: "2025-12-20T11:00:00Z", 3: "2026-01-08T11:00:00Z"}.get(n, "2026")
                return {"prs": [{"number": n, "merged_at": merged}],
                        "files": [{"filename": f} for f in shown]}
            nc = types.SimpleNamespace(
                KERNEL="apache/nuttx", commit_record=commit_record,
                kernel_record=lambda n: {"merged_at": "2025-12-18T10:00:00Z",
                                         "files": [{"filename": f} for f in files]})
            # The one-file fix-up two days after the fix comes before the
            # two-file change three weeks later.
            self.assertEqual(node_worker.later_followups(nc, 1, tmp, set(), repo / "log"),
                             [(2, []), (3, [])])
            self.assertEqual(node_worker.later_followups(nc, 1, tmp, {3}, repo / "log"), [(2, [])])

    def test_later_followups_put_build_wiring_of_new_code_first(self):
        import os
        import subprocess
        import types
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            def commit(name: str, date: str, *paths: str) -> str:
                for path in paths:
                    (repo / path).parent.mkdir(parents=True, exist_ok=True)
                    (repo / path).write_text(name)
                env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
                for args in (["add", *paths], ["commit", "-qm", name]):
                    subprocess.run(["git", "-C", tmp, *args], check=True, env=env)
                return subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], check=True,
                                      capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
            for key, value in (("user.name", "T"), ("user.email", "t@example.invalid")):
                subprocess.run(["git", "-C", tmp, "config", key, value], check=True)
            commit("base", "2025-12-01T10:00:00Z", "sched/Makefile", "sched/old.c")
            commit("fix", "2025-12-18T10:00:00Z", "sched/new/a.c", "sched/new/b.c", "sched/old.c")
            prs = {commit("two files", "2025-12-20T10:00:00Z", "sched/new/a.c", "sched/new/b.c"): 2,
                   commit("wiring", "2026-01-08T10:00:00Z", "sched/Makefile", "sched/new/a.c"): 3,
                   commit("build only", "2026-01-09T10:00:00Z", "sched/Makefile"): 4}
            def commit_record(repo_name, sha):
                shown = subprocess.run(["git", "-C", tmp, "show", "--name-only", "--format=", sha],
                                       capture_output=True, text=True).stdout.split()
                return {"prs": [{"number": prs[sha], "merged_at": "2026"}],
                        "files": [{"filename": f} for f in shown]}
            def nc(added: bool):
                return types.SimpleNamespace(
                    KERNEL="apache/nuttx", commit_record=commit_record,
                    kernel_record=lambda n: {"merged_at": "2025-12-18T10:00:00Z", "files": [
                        {"filename": f, "status": "added" if added and "/new/" in f else "modified"}
                        for f in ("sched/new/a.c", "sched/new/b.c", "sched/old.c")]})
            self.assertEqual(node_worker.later_followups(nc(True), 1, tmp, set(), repo / "log"),
                             [(3, ["sched/Makefile"]), (4, ["sched/Makefile"]), (2, [])])
            # A fix that only modifies files has no new code to wire in.
            self.assertEqual(node_worker.later_followups(nc(False), 1, tmp, set(), repo / "log"),
                             [(2, []), (3, [])])

    def test_a_follow_up_retry_regenerates_the_pair(self):
        # The generator refuses to overwrite a recipe, as render does; the
        # first try fails to build with the fix, the retry with #3's build
        # file passes.
        import types
        with tempfile.TemporaryDirectory() as tmp:
            run, node_dir = Path(tmp) / "run", Path(tmp) / "node"
            (run / "triage").mkdir(parents=True)
            (run / "triage" / "7.json").write_text(json.dumps({"case": "apps-7", "pairs": [
                {"kernel_pr": 1, "confidence": "high"}]}))

            def step(cmd, logfile, minutes, env, cwd=None):
                if "generate_nuttx_instance.py" in cmd[1]:
                    out = Path(cmd[cmd.index("--out") + 1]) / "nuttx__nuttx-1-a7"
                    if out.exists():
                        return 1
                    out.mkdir(parents=True)
                    (out / "triage.json").write_text(Path(cmd[cmd.index("--triage") + 1]).read_text())
                if "validate_instance.py" in cmd[1]:
                    inst = Path(env["EMBEDEVAL_INSTANCES_DIR"]) / "nuttx__nuttx-1-a7"
                    needs = json.loads((inst / "triage.json").read_text())["pairs"][0]
                    ok = any(e["kernel_pr"] == 3 and e.get("files") == ["sched/Makefile"]
                             for e in needs.get("also_needs") or [])
                    (inst / "validation.json").write_text(json.dumps(
                        {"reason": None if ok else "the build failed with the fix (rc=2)"}))
                    return 0 if ok else 1
                return 0

            nc = types.SimpleNamespace(
                kernel_record=lambda n: {"merged_at": "2025-12-18T10:00:00Z"},
                apps_record=lambda n: {"apps": {"merged_at": "2025-12-24T10:00:00Z"}})
            outcome = {}
            with patch.dict(sys.modules, {"nuttx_candidates": nc}), \
                    patch.object(node_worker, "nuttx_data", return_value=node_dir / "data"), \
                    patch.object(node_worker, "later_followups",
                                 return_value=[(2, []), (3, ["sched/Makefile"])]), \
                    patch.object(node_worker, "step", side_effect=step), \
                    patch.object(node_worker, "LOGS", Path(tmp), create=True):
                node_worker.validate_nuttx({}, run, 7, {"EMBEDEVAL_BUILD_TIMEOUT_MIN": "5"},
                                           node_dir, lambda: 100,
                                           lambda state, note, **kw: outcome.update(
                                               state=state, note=note, **kw))
            self.assertEqual(outcome.get("outcome"), "VALID", outcome)
            self.assertIn("+#3 (sched/Makefile only)", outcome["note"])
            # The run keeps the VALID recipe, for promote_instance.py.
            self.assertTrue((run / "instances" / "nuttx__nuttx-1-a7" / "triage.json").exists())

    def test_a_pre_fix_build_break_outside_the_test_retries_with_apps_of_the_kernel_date(self):
        # The apps PR's base does not build on the kernel base; on the apps of
        # the kernel's date it builds, still fails with the fix, and passes with
        # follow-up #3, which keeps the apps date.
        import types
        with tempfile.TemporaryDirectory() as tmp:
            run, node_dir = Path(tmp) / "run", Path(tmp) / "node"
            (run / "triage").mkdir(parents=True)
            (run / "triage" / "7.json").write_text(json.dumps({"case": "apps-7", "pairs": [
                {"kernel_pr": 1, "confidence": "high"}]}))

            def step(cmd, logfile, minutes, env, cwd=None):
                if "generate_nuttx_instance.py" in cmd[1]:
                    out = Path(cmd[cmd.index("--out") + 1]) / "nuttx__nuttx-1-a7"
                    out.mkdir(parents=True)
                    (out / "triage.json").write_text(Path(cmd[cmd.index("--triage") + 1]).read_text())
                if "validate_instance.py" in cmd[1]:
                    inst = Path(env["EMBEDEVAL_INSTANCES_DIR"]) / "nuttx__nuttx-1-a7"
                    pair = json.loads((inst / "triage.json").read_text())["pairs"][0]
                    reason = ("the runtime test did not build (rc=2)"
                              if not pair.get("apps_at_kernel_date") else
                              None if pair.get("also_needs") else "tests still fail with the fix")
                    (inst / "validation.json").write_text(json.dumps({"reason": reason}))
                    return 1 if reason else 0
                return 0

            nc = types.SimpleNamespace(
                kernel_record=lambda n: {"merged_at": "2025-12-18T10:00:00Z"},
                apps_record=lambda n: {"apps": {"merged_at": "2025-12-24T10:00:00Z"}})
            outcome = {}
            with patch.dict(sys.modules, {"nuttx_candidates": nc}), \
                    patch.object(node_worker, "nuttx_data", return_value=node_dir / "data"), \
                    patch.object(node_worker, "later_followups", return_value=[(3, [])]), \
                    patch.object(node_worker, "step", side_effect=step), \
                    patch.object(node_worker, "LOGS", Path(tmp), create=True):
                node_worker.validate_nuttx({}, run, 7, {"EMBEDEVAL_BUILD_TIMEOUT_MIN": "5"},
                                           node_dir, lambda: 100,
                                           lambda state, note, **kw: outcome.update(
                                               state=state, note=note, **kw))
            self.assertEqual(outcome.get("outcome"), "VALID", outcome)
            self.assertIn("apps at the kernel base's date", outcome["note"])
            self.assertIn("+#3", outcome["note"])

    def test_missing_symbols_come_from_the_post_fix_build_errors(self):
        log = ("x.c:3:(.text+0x1): undefined reference to `nxevent_clear'\n"
               "y.c:4:7: error: \u2018CONFIG_LOOPS\u2019 undeclared (first use in this function)\n"
               "z.c:5:3: error: implicit declaration of function \u2018fs_checkmode\u2019\n")
        self.assertEqual(node_worker.missing_symbols(log),
                         {"nxevent_clear", "CONFIG_LOOPS", "fs_checkmode"})

    def test_prerequisites_are_prs_adding_a_missing_symbol_after_the_fix_branched(self):
        import os
        import subprocess
        import types
        with tempfile.TemporaryDirectory() as tmp:
            def commit(name, date, path, text):
                (Path(tmp) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / path).write_text(text)
                env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
                for args in (["add", path], ["commit", "-qm", name]):
                    subprocess.run(["git", "-C", tmp, *args], check=True, env=env)
                return subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], check=True,
                                      capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
            for key, value in (("user.name", "T"), ("user.email", "t@example.invalid")):
                subprocess.run(["git", "-C", tmp, "config", key, value], check=True)
            base = commit("base", "2025-09-07T10:00:00Z", "sched/a.c", "a\n")
            prs = {base: [],
                   commit("old api", "2025-08-01T10:00:00Z", "include/o.h", "void old(void);\n"):
                       [{"number": 2, "merged_at": "2025-08-01T10:00:00Z"}],
                   commit("add api", "2025-09-08T10:00:00Z", "include/e.h",
                          "void nxevent_clear(void);\n"):
                       [{"number": 16920, "merged_at": "2025-09-08T11:00:00Z"}],
                   commit("fix", "2025-09-09T07:00:00Z", "sched/a.c", "b\n"):
                       [{"number": 1, "merged_at": "2025-09-09T07:00:00Z"}]}
            nc = types.SimpleNamespace(
                KERNEL="apache/nuttx",
                commit_record=lambda repo, sha: {"date": "2025-09-07T10:00:00Z", "prs": prs[sha]},
                kernel_record=lambda n: {"base_commit": base, "merged_at": "2025-09-09T07:00:00Z"})
            log = Path(tmp) / "log"
            self.assertEqual(node_worker.prerequisites(nc, 1, tmp, {"nxevent_clear", "old"},
                                                       set(), log), [16920])
            self.assertEqual(node_worker.prerequisites(nc, 1, tmp, {"nxevent_clear"},
                                                       {16920}, log), [])

    def run_pair(self, tmp, reason_for, **patches):
        """validate_nuttx on one pair whose validation reason is reason_for(pair)."""
        import types
        run, node_dir = Path(tmp) / "run", Path(tmp) / "node"
        (run / "triage").mkdir(parents=True)
        (run / "triage" / "7.json").write_text(json.dumps({"case": "apps-7", "pairs": [
            {"kernel_pr": 1, "confidence": "high"}]}))
        logs = Path(tmp) / "validate-logs"
        logs.mkdir()
        (logs / "build-after.log").write_text("undefined reference to `nxevent_clear'\n")

        def step(cmd, logfile, minutes, env, cwd=None):
            if "generate_nuttx_instance.py" in cmd[1]:
                out = Path(cmd[cmd.index("--out") + 1]) / "nuttx__nuttx-1-a7"
                out.mkdir(parents=True)
                (out / "triage.json").write_text(Path(cmd[cmd.index("--triage") + 1]).read_text())
            if "validate_instance.py" in cmd[1]:
                inst = Path(env["EMBEDEVAL_INSTANCES_DIR"]) / "nuttx__nuttx-1-a7"
                reason = reason_for(json.loads((inst / "triage.json").read_text())["pairs"][0])
                (inst / "validation.json").write_text(json.dumps({"reason": reason,
                                                                  "logs": str(logs)}))
                return 1 if reason else 0
            return 0

        nc = types.SimpleNamespace(
            kernel_record=lambda n: {"merged_at": "2025-12-18T10:00:00Z"},
            apps_record=lambda n: {"apps": {"merged_at": "2025-12-24T10:00:00Z"}})
        outcome = {}
        with patch.dict(sys.modules, {"nuttx_candidates": nc}), \
                patch.object(node_worker, "nuttx_data", return_value=node_dir / "data"), \
                patch.object(node_worker, "step", side_effect=step), \
                patch.object(node_worker, "LOGS", Path(tmp), create=True), \
                patch.multiple(node_worker, **patches):
            node_worker.validate_nuttx({}, run, 7, {"EMBEDEVAL_BUILD_TIMEOUT_MIN": "5"},
                                       node_dir, lambda: 100,
                                       lambda state, note, **kw: outcome.update(
                                           state=state, note=note, **kw))
        return outcome

    def test_a_fix_lacking_a_symbol_is_retried_with_the_pr_that_adds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = []
            outcome = self.run_pair(
                tmp, lambda pair: None if any(e["kernel_pr"] == 5 for e in pair.get("also_needs") or [])
                else "the build failed with the fix (rc=2)",
                prerequisites=lambda nc, k, clone, symbols, skip, lg: found.append(symbols) or [5],
                later_followups=lambda *a: [])
        self.assertEqual(found, [{"nxevent_clear"}])
        self.assertEqual(outcome.get("outcome"), "VALID", outcome)
        self.assertIn("prerequisite: +#5", outcome["note"])

    def test_a_hang_with_the_fix_tries_later_follow_ups(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = self.run_pair(
                tmp, lambda pair: None if pair.get("also_needs") else "run_tests timed out with the fix",
                later_followups=lambda *a: [(3, [])])
        self.assertEqual(outcome.get("outcome"), "VALID", outcome)
        self.assertIn("+#3", outcome["note"])

    def test_phase_assignment_stays_fixed_as_other_nodes_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "manifest.json").write_text(json.dumps({"prs": [
                {"pr": pr, "stage": "to triage"} for pr in range(1, 7)]}))
            node_worker.status.write_stage(run, 1, "triage",
                                           {"state": "done", "verdict": "accept"})
            with patch.object(node_worker, "N", 1, create=True), \
                 patch.object(node_worker, "NN", 3, create=True):
                self.assertEqual(node_worker.assigned_work(run, "triage"), [2, 5])
                for pr in range(2, 7):
                    node_worker.status.write_stage(run, pr, "triage",
                                                   {"state": "done", "verdict": "accept"})
                node_worker.status.write_stage(run, 1, "validate",
                                               {"state": "done", "outcome": "VALID"})
                self.assertEqual(node_worker.assigned_work(run, "validate"), [2, 5])

    def test_parallel_triage_copies_node_data_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            shared.mkdir()
            (shared / "record.json").write_text("data")
            node = root / "node"
            cfg = {"nuttx": {"data": str(shared)}}
            start = threading.Barrier(2)
            copies = []

            def copy(_cmd, check):
                copies.append(1)
                time.sleep(0.05)
                shutil.copy(shared / "record.json", node / "work" / "nuttx-data" / "record.json")

            def read():
                start.wait()
                return node_worker.nuttx_data(cfg, node)

            with patch.object(node_worker.subprocess, "run", side_effect=copy), \
                 ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: read(), range(2)))
            self.assertEqual(len(copies), 1)
            self.assertEqual(results[0], results[1])
            self.assertEqual((results[0] / "record.json").read_text(), "data")

    def test_verdict_info_accepts_on_a_high_or_medium_pair(self):
        accept = node_worker.nuttx_verdict_info({"kind": "qemu_only", "pairs": [
            {"kernel_pr": 5, "confidence": "high", "also_needs": [{"kernel_pr": 6}]},
            {"kernel_pr": 7, "confidence": "low"}]})
        self.assertEqual(accept["verdict"], "accept")
        self.assertEqual(accept["pair"], "qemu_only: #5 high +[6]")
        reject = node_worker.nuttx_verdict_info({"kind": "test_only", "pairs": [
            {"kernel_pr": 7, "confidence": "low"}]})
        self.assertEqual((reject["verdict"], reject["pair"]), ("reject", "test_only"))
        self.assertEqual(node_worker.nuttx_verdict_info({"kind": "error", "pairs": []})["verdict"], "error")

    def test_project_selects_ledger_and_data_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            path.write_text("project: nuttx\nnuttx:\n  data: /d\nscratch_root: /s\n")
            cfg = cluster_config.load(path)
            self.assertTrue(cfg["derived"]["ledger"].endswith("ledger-nuttx.jsonl"))
            self.assertEqual(cluster_config.env_for(cfg)["EMBEDEVAL_NUTTX_DATA"], "/d")
            path.write_text("project: other\n")
            with self.assertRaises(cluster_config.ConfigError):
                cluster_config.load(path)
        self.assertTrue(cluster_config.load()["derived"]["ledger"].endswith("/ledger.jsonl"))

    def test_seeded_verdicts_skip_triage(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, run = Path(tmp) / "old", Path(tmp) / "new"
            (src / "triage").mkdir(parents=True)
            run.mkdir()
            (src / "triage" / "5.json").write_text('{"case": "apps-5"}')
            (src / "triage" / "5.trace.json").write_text("[]")
            node_worker.status.write_stage(src, 5, "triage", {"state": "done", "verdict": "accept"})
            node_worker.status.write_stage(src, 6, "triage", {"state": "cap"})
            stage = {5: "to triage", 6: "to triage", 7: "in ledger"}
            self.assertEqual(prepare.seed_verdicts(src, run, [5, 6, 7], stage), 1)
            self.assertEqual((run / "triage" / "5.json").read_text(), '{"case": "apps-5"}')
            self.assertTrue((run / "triage" / "5.trace.json").exists())
            got = node_worker.status.read(run, 5)["triage"]
            self.assertEqual((got["verdict"], got["seeded_from"]), ("accept", "old"))
            self.assertNotIn("triage", node_worker.status.read(run, 6))


if __name__ == "__main__":
    unittest.main()
