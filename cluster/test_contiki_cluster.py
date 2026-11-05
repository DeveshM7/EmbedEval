"""Local checks for project isolation and Contiki's split Slurm phases."""

import json
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import ledger
import prepare
import status
import submit
import report
import node_worker


class ContikiClusterTest(unittest.TestCase):
    def test_failed_observation_is_attempted_once_per_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'manifest.json').write_text(json.dumps({'prs': [{'pr': 52, 'stage': 'to triage'}]}))
            calls = []
            def failed_step(cmd, *args):
                calls.append(cmd)
                if len(calls) > 2:
                    self.fail('failed generation was immediately retried')
                return 1
            with (patch.object(node_worker, 'LOGS', run / 'logs', create=True),
                  patch.object(node_worker, 'ME', 'test-node0', create=True),
                  patch.object(node_worker, 'N', 0, create=True),
                  patch.object(node_worker, 'step', side_effect=failed_step),
                  patch.object(node_worker, 'sweep'),
                  patch.object(node_worker.report, 'to_ledger')):
                node_worker.observe({'caps': {'validate_pr': 30, 'build': 10},
                                     'parallel': {'validate_per_node': 1}}, run,
                                    {'EMBEDEVAL_WORK': str(run / 'work')}, 30)
            self.assertEqual(len(calls), 2)  # generation and cleanup
            self.assertEqual(status.read(run, 52)['observe']['state'], 'failed')
            self.assertEqual(status.observe_todo(run), [52])  # a later job can retry

    def test_observation_resume_retries_failures_but_keeps_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "manifest.json").write_text(json.dumps({"prs": [
                {"pr": pr, "stage": "to triage"} for pr in range(1, 5)]}))
            for pr, state in ((1, "done"), (2, "failed"), (3, "cap")):
                status.write_stage(run, pr, "observe", {"state": state})
            self.assertEqual(status.observe_todo(run), [2, 3, 4])

    def test_idle_nodes_excludes_maintenance_and_reserved_nodes(self):
        response = SimpleNamespace(returncode=0,
                                   stdout="idle 2\nmaint 14\nresv 8\ndown$ 4\nidle~ 3\n",
                                   stderr="")
        with patch.object(submit.subprocess, "run", return_value=response) as run:
            self.assertEqual(submit.idle_nodes("spark-batch"), 2)
        self.assertEqual(run.call_args.args[0][-2:], ["-o", "%t %D"])

    def test_archive_revision_is_recorded_without_clean_git_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".embedeval-revision").write_text("123abc\n")
            self.assertEqual(prepare.code_identity(root, "contiki-ng"),
                             {"commit": "123abc", "dirty": None, "archive": True})

    def test_freeze_copies_runtime_only_and_hashes_copied_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            required = ("cluster/node_worker.py", "cluster/report.py", "scripts/triage_prs.py",
                        "scripts/contiki_ng_pipeline.py", "harness/projects.py",
                        "templates/contiki-ng/apptainer/instance.def.tmpl",
                        "docs/contiki_ng_triage.md", "triage/models.json", "requirements.txt")
            for name in required:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            (root / "cluster/config.yaml").write_text("secret")
            (root / "scripts/__pycache__").mkdir()
            (root / "scripts/__pycache__/worker.pyc").write_bytes(b"cache")
            (root / "scripts/runs").mkdir()
            (root / "scripts/runs/result.json").write_text("result")
            (root / ".embedeval-revision").write_text("123abc\n")
            destination = root / "job-source"
            manifest = submit.freeze_source(root, destination)
            self.assertEqual(manifest["revision"], "123abc")
            for name in required:
                self.assertIn(name, manifest["files"])
                self.assertEqual((destination / name).read_text(), name)
            for name in ("cluster/config.yaml", "scripts/__pycache__/worker.pyc",
                         "scripts/runs/result.json"):
                self.assertFalse((destination / name).exists())
            self.assertEqual(json.loads((destination / "source-manifest.json").read_text()), manifest)
            (root / ".embedeval-revision").unlink()
            with patch.object(submit.subprocess, "run", return_value=SimpleNamespace(stdout="git-sha\n")):
                fallback = submit.freeze_source(root, root / "git-source")
            self.assertEqual((root / "git-source/.embedeval-revision").read_text(), "git-sha\n")
            self.assertIn(".embedeval-revision", fallback["files"])

    def test_infrastructure_hold_does_not_settle_as_invalid(self):
        entry={"pr":123,"stage":"to triage"}
        phases={"triage":{"state":"done","verdict":"accept"},
                "validate":{"state":"done","outcome":"HOLD","note":"missing fixture"}}
        outcome,detail=report.outcome(entry,phases)
        self.assertEqual(outcome,"validation: hold")
        self.assertFalse(ledger.settled(outcome,detail))

    def test_project_ledgers_and_unfinished_runs_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = root / "ledger.jsonl"
            rows = [{"pr": 123, "outcome": "VALID", "detail": ""}]
            ledger.record(book, "z", "sha", "now", rows)
            ledger.record(book, "c", "sha", "now", rows, project="contiki-ng")
            self.assertEqual(ledger.latest(book)[123]["run"], "z")
            self.assertEqual(ledger.latest(book, "contiki-ng")[123]["run"], "c")

            runs = root / "runs"
            for name, project in (("z", "zephyr"), ("c", "contiki-ng")):
                run = runs / name
                run.mkdir(parents=True)
                (run / "manifest.json").write_text(json.dumps({
                    "name": name, "project": project,
                    "prs": [{"pr": 123, "stage": "to triage"}]}))
            self.assertEqual(status.unfinished(runs), {123: "z"})
            self.assertEqual(status.unfinished(runs, project="contiki-ng"), {123: "c"})

    def test_config_exports_contiki_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(f"scratch_root: {tmp}\nproject: contiki-ng\n")
            cfg = config.load(path)
            env = config.env_for(cfg)
            self.assertEqual(env["EMBEDEVAL_CONTIKI_NG_CLONE"],
                             str(Path(tmp) / "shared" / "contiki-ng.git"))
            self.assertTrue(env["EMBEDEVAL_CONTIKI_NG_IMAGE"].endswith("x86_64.sif"))

    def test_phase_template_has_cpu_and_gpu_modes(self):
        template = (Path(__file__).parent / "templates" / "phase.sbatch").read_text()
        common = dict(partition="cpu", nodes=1, cpus=2, mem="2G", time_limit="01:00:00",
                      job_name="job", run="/tmp/run", repo="/tmp/repo", config="/tmp/config",
                      python="/usr/bin/python3", limit_min=60, report_min=8,
                      validate_min=30, stamp="now", name="run", gres="", phase="observe")
        cpu = template.format(**(common | {"gres_line": ""}))
        gpu = template.format(**(common | {"gres_line": "#SBATCH --gres=gpu:1",
                                          "phase": "triage"}))
        self.assertNotIn("--gres", cpu)
        self.assertIn("--gres=gpu:1", gpu)
        self.assertIn("--export=NONE", cpu)
        self.assertIn("/usr/bin/env -i", cpu)
        self.assertIn("/bin/bash -c", cpu)
        self.assertNotIn("    bash -c", cpu)
        self.assertIn("SLURM_EXPORT_ENV=ALL", cpu)
        self.assertIn("worker_rc=$?", cpu)
        self.assertIn('exit "$worker_rc"', cpu)
        self.assertIn("/usr/sbin", cpu)
        self.assertEqual(submit.plan_nodes({"slurm": {"nodes_min": 2, "nodes_max": 2}},
                                           100, 1)[:2], (1, 2))

    def test_submit_routes_cpu_and_spark_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_file = root / "config.yaml"
            cfg_file.write_text(f"scratch_root: {tmp}\nproject: contiki-ng\n"
                                "slurm:\n  build_partition: cpu-batch\n"
                                "  build_cpus_per_node: 4\n  build_mem: 12G\n"
                                "  build_python: /x86/venv/bin/python\n"
                                "  triage_python: /spark/venv/bin/python\n")
            cfg = config.load(cfg_file)
            run = Path(cfg["derived"]["runs"]) / "pilot"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({
                "name": "pilot", "project": "contiki-ng",
                "prs": [{"pr": 123, "stage": "to triage"}]}))
            response = SimpleNamespace(returncode=0, stdout="", stderr="")
            for phase in ("observe", "triage"):
                with (patch.object(sys, "argv", ["submit.py", phase, "--run", "pilot",
                                                "--config", str(cfg_file), "--dry-run", "--idle", "1"]),
                      patch.object(submit.subprocess, "run", return_value=response),
                      contextlib.redirect_stdout(io.StringIO())):
                    submit.main()
            cpu = next((run / "jobs").glob("observe-*.sbatch")).read_text()
            spark = next((run / "jobs").glob("triage-*.sbatch")).read_text()
            self.assertIn("#SBATCH -p cpu-batch", cpu)
            self.assertIn("#SBATCH --cpus-per-task=4", cpu)
            self.assertIn("#SBATCH --mem=12G", cpu)
            self.assertNotIn("#SBATCH --gres=", cpu)
            self.assertIn("/x86/venv/bin/python cluster/node_worker.py", cpu)
            self.assertIn("#SBATCH -p spark-batch", spark)
            self.assertIn("#SBATCH --cpus-per-task=20", spark)
            self.assertIn("#SBATCH --mem=120500M", spark)
            self.assertIn("#SBATCH --gres=gpu:1", spark)
            self.assertIn("/spark/venv/bin/python cluster/node_worker.py", spark)
            source = next((run / "jobs").glob("source-*/source-manifest.json"))
            self.assertIn("scripts/contiki_ng_pipeline.py", json.loads(source.read_text())["files"])
            self.assertIn(str(source.parent), cpu + spark)


if __name__ == "__main__":
    unittest.main()
