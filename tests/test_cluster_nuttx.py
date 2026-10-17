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
    def test_later_followups_rank_prs_by_how_many_fix_files_they_touch(self):
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
                return {"prs": [{"number": n, "merged_at": "2026"}],
                        "files": [{"filename": f} for f in shown]}
            nc = types.SimpleNamespace(
                KERNEL="apache/nuttx", commit_record=commit_record,
                kernel_record=lambda n: {"merged_at": "2025-12-18T10:00:00Z",
                                         "files": [{"filename": f} for f in files]})
            self.assertEqual(node_worker.later_followups(nc, 1, tmp, set(), repo / "log"), [3, 2])
            self.assertEqual(node_worker.later_followups(nc, 1, tmp, {3}, repo / "log"), [2])

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
