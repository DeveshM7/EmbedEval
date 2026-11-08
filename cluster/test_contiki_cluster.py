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
    def test_scorecard_uses_contiki_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'triage').mkdir()
            rep = {'run':'gold','prs':0,'funnel':{},'observations':{},'valid':[],
                   'project':'contiki-ng'}
            with (patch.object(sys,'argv',['report.py','--run',str(run),'--scorecard']),
                  patch.object(report,'build',return_value=rep),
                  patch.object(report,'markdown',return_value=''),
                  patch.object(report,'to_ledger',return_value=(0,run/'ledger')),
                  patch.object(report.subprocess,'run') as score,
                  contextlib.redirect_stdout(io.StringIO())):
                report.main()
            self.assertEqual(score.call_args.args[0][-2:],['--project','contiki-ng'])

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

    def test_manifest_observe_profile_reaches_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'manifest.json').write_text(json.dumps({'prs': [
                {'pr': 52, 'stage': 'to triage', 'observe_profile': 'native'},
                {'pr': 53, 'stage': 'to triage'}]}))
            calls = []
            with (patch.object(node_worker, 'LOGS', run / 'logs', create=True),
                  patch.object(node_worker, 'ME', 'test-node0', create=True),
                  patch.object(node_worker, 'N', 0, create=True),
                  patch.object(node_worker, 'step', side_effect=lambda cmd, *a: calls.append(cmd) or 1),
                  patch.object(node_worker, 'sweep'),
                  patch.object(node_worker.report, 'to_ledger')):
                node_worker.observe({'caps': {'validate_pr': 30, 'build': 10},
                                     'parallel': {'validate_per_node': 1}}, run,
                                    {'EMBEDEVAL_WORK': str(run / 'work')}, 30)
            generate = {c[c.index('--pr') + 1]: c for c in calls if 'scripts/generate_instance.py' in c}
            self.assertEqual(generate['52'][-2:], ['--profile', 'native'])
            self.assertNotIn('--profile', generate['53'])

    def test_observed_valid_pr_is_finalized_and_then_needs_a_statement(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'manifest.json').write_text(json.dumps({'prs': [{'pr': 7, 'stage': 'to triage'}]}))
            iid = 'contiki-ng__contiki-ng-7'
            calls = []
            def fake_step(cmd, log, cap, env):
                calls.append((cmd, env.get('EMBEDEVAL_INSTANCES_DIR')))
                script = cmd[1]
                if script == 'scripts/validate_instance.py' and not env.get('EMBEDEVAL_INSTANCES_DIR'):
                    (run / 'instances' / iid).mkdir(parents=True)
                    (run / 'instances' / iid / 'observed.json').write_text('{"valid": true}')
                if script == 'scripts/validate_instance.py' and env.get('EMBEDEVAL_INSTANCES_DIR'):
                    d = run / 'finalized' / iid
                    d.mkdir(parents=True)
                    (d / 'validation.json').write_text('{"valid": true}')
                    (d / 'metadata.json').write_text('{"problem_statement": null}')
                return 0
            with (patch.object(node_worker, 'LOGS', run / 'logs', create=True),
                  patch.object(node_worker, 'ME', 'test-node0', create=True),
                  patch.object(node_worker, 'N', 0, create=True),
                  patch.object(node_worker, 'step', side_effect=fake_step),
                  patch.object(node_worker, 'sweep'),
                  patch.object(node_worker.report, 'to_ledger')):
                node_worker.observe({'caps': {'validate_pr': 30, 'build': 10},
                                     'parallel': {'validate_per_node': 1}}, run,
                                    {'EMBEDEVAL_WORK': str(run / 'work')}, 60)
            fromobs = [c for c, e in calls if '--from-observed' in c]
            self.assertEqual(len(fromobs), 1)
            self.assertEqual(fromobs[0][fromobs[0].index('--out') + 1], str(run / 'finalized'))
            self.assertEqual(status.read(run, 7)['finalize']['outcome'], 'VALID')
            self.assertEqual(status.finalize_todo(run), [])
            self.assertEqual(status.statement_todo(run), [7])

    def test_statement_recheck_covers_triage_instances_once_per_check_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'manifest.json').write_text(json.dumps({'prs': [{'pr': p, 'stage': 'to triage'}
                                                                    for p in (1, 2, 3, 4)]}))
            def put(pr, valid, meta):
                d = run / 'instances' / f'contiki-ng__contiki-ng-{pr}'
                d.mkdir(parents=True)
                (d / 'validation.json').write_text(json.dumps({'valid': valid}))
                (d / 'metadata.json').write_text(json.dumps(meta))
            put(1, True, {'problem_statement': 'old wording'})
            put(2, True, {'problem_statement': 'checked',
                          'statement_check': {'version': status.STATEMENT_CHECK}})
            put(3, False, {'problem_statement': ''})
            put(4, True, {'problem_statement': ''})
            self.assertEqual(status.instance_folder(run, 1), 'instances')
            self.assertEqual(status.statement_todo(run), [4])
            self.assertEqual(status.statement_todo(run, recheck=True), [1, 4])

    def test_not_observable_generation_is_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'manifest.json').write_text(json.dumps({'observe_only': True, 'prs': [
                {'pr': 5, 'stage': 'to triage', 'observe_profile': 'cooja'}]}))
            with (patch.object(node_worker, 'LOGS', run / 'logs', create=True),
                  patch.object(node_worker, 'ME', 'test-node0', create=True),
                  patch.object(node_worker, 'N', 0, create=True),
                  patch.object(node_worker, 'step', side_effect=lambda cmd, *a: 3 if 'generate' in cmd[1] else 0),
                  patch.object(node_worker, 'sweep'),
                  patch.object(node_worker.report, 'to_ledger')):
                node_worker.observe({'caps': {'validate_pr': 30, 'build': 10},
                                     'parallel': {'validate_per_node': 1}}, run,
                                    {'EMBEDEVAL_WORK': str(run / 'work')}, 30)
            self.assertEqual(status.read(run, 5)['observe']['state'], 'done')
            self.assertEqual(status.observe_todo(run), [])
            self.assertEqual(status.triage_todo(run), [])   # observation runs hold nothing for triage

    def test_report_follows_observation_runs_to_promotable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            prs = list(range(1, 8))
            (run / 'manifest.json').write_text(json.dumps({'name': 'obs', 'project': 'contiki-ng',
                'observe_only': True, 'code': {'commit': 'x'},
                'environment': {'EMBEDEVAL_SHARED': str(run / 'shared')},
                'prs': [{'pr': p, 'stage': 'to triage'} for p in prs]}))
            def put(folder, pr, name, data):
                d = run / folder / f'contiki-ng__contiki-ng-{pr}'
                d.mkdir(parents=True, exist_ok=True)
                (d / name).write_text(json.dumps(data))
            status.write_stage(run, 2, 'observe', {'state': 'done', 'note': 'not observable'})
            for pr in (3, 4, 5, 6, 7):
                status.write_stage(run, pr, 'observe', {'state': 'done'})
            put('instances', 3, 'observed.json', {'valid': False, 'reason': 'no attributable runtime F2P'})
            for pr in (4, 5, 6, 7):
                put('instances', pr, 'observed.json', {'valid': True})
            status.write_stage(run, 5, 'finalize', {'state': 'done', 'outcome': 'INVALID: x'})
            for pr in (6, 7):
                status.write_stage(run, pr, 'finalize', {'state': 'done', 'outcome': 'VALID'})
            put('finalized', 6, 'metadata.json', {'problem_statement': ''})
            put('finalized', 7, 'metadata.json', {'problem_statement': 'Ping fails.',
                                                   'statement_provenance': {'model': 'qwen38-nvfp4'}})
            rep = report.build(run)
            got = {r['pr']: r['outcome'] for r in rep['rows']}
            self.assertEqual(got, {1: 'observation: not yet', 2: 'observation: not observable',
                                   3: 'observation: no fail-to-pass', 4: 'observed, finalize not yet',
                                   5: 'finalize: INVALID', 6: 'VALID, statement not yet', 7: 'VALID'})
            self.assertEqual(rep['valid'], [7])
            self.assertTrue(rep['rows'][6]['instance'].endswith('finalized/contiki-ng__contiki-ng-7'))
            n, _ = report.to_ledger(run)
            self.assertEqual(n, 1)       # only the promotable instance settles its PR

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
                        "docs/contiki_ng_triage.md", "triage/models.json", "triage/gold-contiki-ng.json", "requirements.txt")
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
                      validate_min=30, stamp="now", name="run", gres="", phase="observe",
                      end_server="")
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


class RevalidationTest(unittest.TestCase):
    """prepare.py --instances: existing instances, rendered again and
    validated under the current runner."""

    def instance(self, root, pr, iid=None):
        d = root / (iid or f'contiki-ng__contiki-ng-{pr}')
        d.mkdir(parents=True)
        for n in prepare.SOURCE_FILES:
            (d / n).write_text(json.dumps({'instance_id': iid or f'contiki-ng__contiki-ng-{pr}'}) if n == 'metadata.json' else '{}')
        return d

    def test_sources_round_trip_ids_and_copy_only_the_recipe_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = self.instance(root / 'a', 3246)
            self.instance(root / 'b', 255)
            (one / 'validation.json').write_text('{}')
            run = root / 'run'
            got = prepare.instance_sources([one, root / 'b'], run)
            self.assertEqual(sorted(got), [255, 3246])
            self.assertEqual(sorted(p.name for p in got[3246].iterdir()), sorted(prepare.SOURCE_FILES))
            self.assertEqual(got[3246], run / 'sources' / 'contiki-ng__contiki-ng-3246')

    def test_malformed_or_duplicate_ids_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.instance(root / 'a', 1, iid='contiki-ng__contiki-ng-01')
            with self.assertRaises(SystemExit):
                prepare.instance_sources([root / 'a'], root / 'run')
            self.instance(root / 'b', 7)
            self.instance(root / 'c', 7)
            with self.assertRaises(SystemExit):
                prepare.instance_sources([root / 'b', root / 'c'], root / 'run2')

    def test_revalidation_prs_are_validated_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'manifest.json').write_text(json.dumps({'project': 'contiki-ng', 'revalidation': True, 'prs': [
                {'pr': 1, 'stage': 'to validate', 'instance': 'contiki-ng__contiki-ng-1'},
                {'pr': 2, 'stage': 'to validate', 'instance': 'contiki-ng__contiki-ng-2'}]}))
            self.assertEqual(status.validate_todo(run), [1, 2])
            status.write_stage(run, 1, 'validate', {'state': 'done', 'outcome': 'VALID'})
            self.assertEqual(status.validate_todo(run), [2])
            self.assertEqual(status.valid(run), [1])
            self.assertEqual(report.outcome(status.entry(run, 1), status.read(run, 1), run), ('VALID', ''))
            self.assertEqual(report.outcome(status.entry(run, 2), status.read(run, 2), run)[0],
                             'accepted, validation not yet')

    def test_worker_renders_a_revalidation_instead_of_generating(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'manifest.json').write_text(json.dumps({'project': 'contiki-ng', 'prs': [
                {'pr': 5, 'stage': 'to validate', 'instance': 'contiki-ng__contiki-ng-5'}]}))
            calls = []
            def fake_step(cmd, *args):
                calls.append(cmd)
                if 'validate_instance.py' in cmd[1]:
                    d = run / 'instances' / 'contiki-ng__contiki-ng-5'
                    d.mkdir(parents=True, exist_ok=True)
                    (d / 'validation.json').write_text(json.dumps({'valid': True}))
                return 0
            with (patch.object(node_worker, 'LOGS', run / 'logs', create=True),
                  patch.object(node_worker, 'ME', 'test-node0', create=True),
                  patch.object(node_worker, 'N', 0, create=True),
                  patch.object(node_worker, 'step', side_effect=fake_step),
                  patch.object(node_worker, 'sweep'),
                  patch.object(node_worker.report, 'to_ledger')):
                node_worker.validate({'project': 'contiki-ng', 'caps': {'validate_pr': 60, 'build': 10},
                                      'parallel': {'validate_per_node': 1}, 'disk_guard_gb': 250}, run,
                                     {'EMBEDEVAL_WORK': str(run / 'work')}, 60)
            generate = next(c for c in calls if 'generate_instance.py' in c[1])
            self.assertEqual(generate[generate.index('--render') + 1],
                             str(run / 'sources' / 'contiki-ng__contiki-ng-5'))
            self.assertNotIn('--triage', generate)
            self.assertEqual(status.read(run, 5)['validate']['outcome'], 'VALID')


class EvaluateTest(unittest.TestCase):
    """submit.py evaluate --model M and its worker, against a fake scheduler."""

    def run_dir(self, tmp):
        root = Path(tmp)
        cfg_file = root / "config.yaml"
        cfg_file.write_text(f"scratch_root: {tmp}\nproject: contiki-ng\n"
                            "slurm:\n  build_partition: cpu\n  partition: spark-interactive\n"
                            "  build_python: /x86/python\n"
                            "evaluate:\n  python: /x86/agent-venv/python\n")
        cfg = config.load(cfg_file)
        run = Path(cfg["derived"]["runs"]) / "reval"
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(json.dumps({"name": "reval", "project": "contiki-ng", "prs": [
            {"pr": 1, "stage": "to validate", "instance": "contiki-ng__contiki-ng-1"},
            {"pr": 2, "stage": "to validate", "instance": "contiki-ng__contiki-ng-2"}]}))
        status.write_stage(run, 1, "validate", {"state": "done", "outcome": "VALID"})
        status.write_stage(run, 2, "validate", {"state": "done", "outcome": "INVALID: x"})
        return cfg_file, run

    def test_model_server_gets_a_clean_environment_and_starts_after_its_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file, run = self.run_dir(tmp)
            calls, ids = [], iter(["900", "901"])
            def fake(cmd, **kw):
                calls.append(cmd)
                out = next(ids) + ";cluster\n" if cmd[0] == "sbatch" and "--parsable" in cmd else ""
                return SimpleNamespace(returncode=0, stdout=out, stderr="")
            with (patch.object(sys, "argv", ["submit.py", "evaluate", "--run", "reval", "--config", str(cfg_file),
                                             "--model", "qwen38-nvfp4", "--idle", "1"]),
                  patch.dict(submit.os.environ, {"HOME": "/home/u", "USER": "u", "PATH": "/opt/conda/bin:/usr/bin"}),
                  patch.object(submit.subprocess, "run", side_effect=fake),
                  contextlib.redirect_stdout(io.StringIO())):
                submit.main()
            sbatch = [c for c in calls if c[0] == "sbatch" and "--parsable" in c]
            job, server = sbatch
            # the agents' job is submitted held, the server after it and only
            # once it runs, then the agents' job is released
            self.assertIn("--hold", job)
            self.assertIn("--dependency=after:900", server)
            self.assertEqual(calls[-1], ["scontrol", "release", "900"])
            export = next(a for a in server if a.startswith("--export="))
            self.assertIn("PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin,", export)
            self.assertNotIn("conda", export)
            self.assertEqual(server[-1], "qwen38-nvfp4")
            self.assertIn("spark-interactive", server)
            script = Path(job[-1]).read_text()
            self.assertIn("/usr/bin/scancel \"$S\"", script)
            self.assertEqual(next(run.glob("jobs/server-*.job")).read_text(), "901\n")
            self.assertIn("/x86/agent-venv/python cluster/node_worker.py", script)
            self.assertIn("#SBATCH -p cpu", script)
            frozen = json.loads(next(run.glob("jobs/config-*.json")).read_text())
            self.assertEqual((frozen["evaluate_model"], frozen["evaluate_server_job"]), ("qwen38-nvfp4", "901"))

    def test_hosted_models_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file, run = self.run_dir(tmp)
            with (patch.object(sys, "argv", ["submit.py", "evaluate", "--run", "reval", "--config", str(cfg_file),
                                             "--model", "gpt-5.4"]),
                  self.assertRaises(SystemExit)):
                submit.main()

    def evaluate(self, run, cfg, capacity=4.0, waiting=(), exit_status="TimeExceeded", job="test"):
        """Run the worker's evaluate phase with a fake server: `capacity`
        full-length requests held, `waiting` the queue samples during a run."""
        calls = []
        def fake_step(cmd, *args):
            calls.append(cmd)
            if "evaluate_patches.py" in cmd[1]:
                out = Path(cmd[cmd.index("--output") + 1])
                out.write_text(json.dumps({"results": [{"grade": "fail", "error": "tests failed"}]}))
            return 0
        def fake_watch(base, stop, samples, every=30):
            samples.extend(waiting)
        lanes = []
        real_drain = node_worker.drain
        def spy_drain(q, todo, one, slots, *a, **kw):
            lanes.append(slots)
            return real_drain(q, todo, one, slots, *a, **kw)
        tunnel = SimpleNamespace(pid=1, poll=lambda: None)
        with (patch.object(node_worker, "LOGS", run / "logs", create=True),
              patch.object(node_worker, "ME", f"{job}-node0", create=True),
              patch.object(node_worker, "N", 0, create=True),
              patch.object(node_worker, "step", side_effect=fake_step),
              patch.object(node_worker, "sweep"),
              patch.object(node_worker, "kill_group"),
              patch.object(node_worker, "model_tunnel", return_value=(tunnel, "http://127.0.0.1:8000/v1")),
              patch.object(node_worker, "server_alive", return_value=True),
              patch.object(node_worker, "server_capacity", return_value=capacity),
              patch.object(node_worker, "watch_queue", side_effect=fake_watch),
              patch.object(node_worker, "drain", side_effect=spy_drain),
              patch.object(node_worker, "agent_record", return_value={"exit_status": exit_status, "steps": 40}),
              patch.object(node_worker.report, "to_ledger")):
            node_worker.evaluate(cfg, run, {"EMBEDEVAL_WORK": str(run / "work")}, 240)
        return calls, lanes

    def test_worker_grades_only_valid_instances_and_keys_by_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file, run = self.run_dir(tmp)
            cfg = {**config.load(cfg_file), "evaluate_model": "qwen38-nvfp4", "evaluate_server_job": "900"}
            calls, _ = self.evaluate(run, cfg, waiting=[0, 0])
            agent = next(c for c in calls if "harness/run.py" in c[1])
            self.assertEqual(agent[agent.index("--instance") + 1], "contiki-ng__contiki-ng-1")
            self.assertEqual(agent[agent.index("--model") + 1], "qwen38-nvfp4")
            self.assertFalse(any("contiki-ng__contiki-ng-2" in " ".join(c) for c in calls))
            e = status.read(run, 1)["evaluate:qwen38-nvfp4"]
            self.assertEqual((e["state"], e["grade"], e["timed_out"], e["steps"]), ("done", "fail", True, 40))
            self.assertEqual(e["limits"], {"step_limit": cfg["evaluate"]["step_limit"],
                                           "time_limit": cfg["evaluate"]["time_limit"]})
            self.assertEqual(status.evaluate_todo(run, "qwen38-nvfp4"), [])
            self.assertEqual(status.evaluate_todo(run, "nemotron"), [1])
            self.assertEqual(e["server_queued"], 0.0)

    def test_agents_are_limited_to_what_the_server_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file, run = self.run_dir(tmp)
            cfg = {**config.load(cfg_file), "evaluate_model": "qwen38-nvfp4", "evaluate_server_job": "900"}
            cfg["evaluate"]["per_node"] = 4
            _, lanes = self.evaluate(run, cfg, capacity=1.56, waiting=[0])
            self.assertEqual(lanes, [1])
            calls, lanes = self.evaluate(run, cfg, capacity=0.8)     # holds no full request
            self.assertEqual((lanes, calls), ([], []))

    def test_a_time_out_behind_a_queueing_server_is_run_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file, run = self.run_dir(tmp)
            cfg = {**config.load(cfg_file), "evaluate_model": "qwen38-nvfp4", "evaluate_server_job": "900"}
            calls, _ = self.evaluate(run, cfg, waiting=[0, 2, 1, 0])
            e = status.read(run, 1)["evaluate:qwen38-nvfp4"]
            self.assertEqual((e["state"], e["retry"], e["server_queued"]), ("failed", True, 0.5))
            self.assertEqual(status.evaluate_todo(run, "qwen38-nvfp4"), [1])
            self.assertEqual(sum("harness/run.py" in c[1] for c in calls), 1)   # not again on this server
            # the next job runs it; a run that ends in time is the model's result however the server did
            self.evaluate(run, cfg, waiting=[3], exit_status="Submitted", job="next")
            self.assertEqual(status.evaluate_todo(run, "qwen38-nvfp4"), [])

    def test_a_time_out_from_before_the_queue_was_watched_is_run_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, run = self.run_dir(tmp)
            status.write_stage(run, 1, "evaluate:m", {"state": "done", "grade": "fail", "timed_out": True})
            self.assertEqual(status.evaluate_todo(run, "m"), [1])
            status.write_stage(run, 1, "evaluate:m", {"state": "done", "grade": "fail", "timed_out": False})
            self.assertEqual(status.evaluate_todo(run, "m"), [])

    def test_server_capacity_and_queue_are_read_from_vllm(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "server-901.log"
            log.write_text("INFO GPU KV cache size: 178,366 tokens\n"
                           "INFO Maximum concurrency for 131,072 tokens per request: 1.36x\n")
            shown = SimpleNamespace(stdout=f"JobId=901 StdOut={tmp}/server-%j.log\n", returncode=0)
            with patch.object(node_worker.subprocess, "run", return_value=shown):
                self.assertEqual(node_worker.server_capacity("901"), 1.36)
            log.write_text("starting\n")
            with patch.object(node_worker.subprocess, "run", return_value=shown):
                self.assertIsNone(node_worker.server_capacity("901"))
        metrics = (b'# HELP vllm:num_requests_waiting\n'
                   b'vllm:num_requests_waiting{engine="0",model_name="q"} 2.0\n'
                   b'vllm:num_requests_running{engine="0",model_name="q"} 4.0\n')
        stop, samples = SimpleNamespace(n=0), []
        def wait(_):
            stop.n += 1
            return stop.n > 2
        with patch.object(node_worker.urllib.request, "urlopen",
                          return_value=SimpleNamespace(read=lambda: metrics)) as urlopen:
            node_worker.watch_queue("http://127.0.0.1:8000/v1", SimpleNamespace(wait=wait), samples)
        self.assertEqual(samples, [2.0, 2.0])
        self.assertEqual(urlopen.call_args[0][0], "http://127.0.0.1:8000/metrics")
