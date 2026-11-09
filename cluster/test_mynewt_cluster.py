"""Local checks for Mynewt runs of the cluster pipeline.

    python3 -m unittest cluster/test_mynewt_cluster.py
"""

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

CLUSTER = Path(__file__).resolve().parent
REPO = CLUSTER.parent
sys.path[:0] = [str(CLUSTER), str(REPO / "scripts")]
import config  # noqa: E402
import ledger  # noqa: E402
import node_worker  # noqa: E402
import prepare  # noqa: E402
import report  # noqa: E402
import status  # noqa: E402
import submit  # noqa: E402


LABEL = "qwen38-nvfp4-50steps-40min"


def valid_run(tmp: Path) -> Path:
    return write_run(tmp / "r", [{"pr": 1, "stage": "to validate", "instance": "mynewt__mynewt-1"}],
                     {1: {"validate": {"state": "done", "outcome": "VALID", "instance": "mynewt__mynewt-1"}}})


def write_run(run: Path, prs: list[dict], states: dict | None = None) -> Path:
    run.mkdir(parents=True, exist_ok=True)
    (run / "manifest.json").write_text(json.dumps({
        "name": run.name, "project": "mynewt", "prs": prs, "code": {"commit": "c"},
        "environment": {"EMBEDEVAL_SHARED": str(run / "shared")}}))
    for pr, st in (states or {}).items():
        for stage, info in st.items():
            status.write_stage(run, pr, stage, info)
    return run


class ConfigTest(unittest.TestCase):
    def test_a_mynewt_run_gets_its_clone_and_records(self):
        cfg = config.load()
        env = config.env_for(cfg, Path("/r"), "mynewt")
        self.assertEqual(env["EMBEDEVAL_MYNEWT_CLONE"], cfg["derived"]["mynewt_clone"])
        self.assertEqual(env["MYNEWT_CLONE"], env["EMBEDEVAL_MYNEWT_CLONE"])
        self.assertEqual(env["MYNEWT_ENRICHED"], "/r/candidates/mynewt/enriched")
        self.assertNotIn("MYNEWT_CLONE", config.env_for(cfg, Path("/r"), "zephyr"))

    def test_an_unknown_project_is_an_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
            f.write("project: nuttx\n")
            f.flush()
            with self.assertRaises(config.ConfigError):
                config.load(f.name)


class StatusTest(unittest.TestCase):
    def test_revalidation_and_accepted_prs_are_validated_and_valid_ones_evaluated(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = write_run(Path(tmp) / "r", [
                {"pr": 1, "stage": "to validate", "instance": "mynewt__mynewt-1"},
                {"pr": 2, "stage": "to triage"}, {"pr": 3, "stage": "to triage"},
                {"pr": 4, "stage": "hard filter"}],
                {2: {"triage": {"state": "done", "verdict": "accept"}},
                 3: {"triage": {"state": "done", "verdict": "reject"}}})
            self.assertEqual(status.triage_todo(run), [])
            self.assertEqual(status.validate_todo(run), [1, 2])
            status.write_stage(run, 1, "validate", {"state": "done", "outcome": "VALID", "instance": "mynewt__mynewt-1"})
            status.write_stage(run, 2, "validate", {"state": "done", "outcome": "INVALID: x"})
            self.assertEqual(status.evaluate_todo(run, LABEL), [1])
            status.write_stage(run, 1, f"evaluate:{LABEL}", {"state": "failed", "note": "build hit the cap"})
            self.assertEqual(status.evaluate_todo(run, LABEL), [1])
            status.write_stage(run, 1, f"evaluate:{LABEL}", {"state": "done", "grade": "fail"})
            self.assertEqual(status.evaluate_todo(run, LABEL), [])

    def test_evaluations_are_kept_apart_by_model_and_limits(self):
        cfg = {"model": "qwen38-nvfp4", "step_limit": 50, "time_limit": 40}
        self.assertEqual(status.eval_label(cfg), LABEL)
        self.assertEqual(status.eval_label({**cfg, "model": "nemotron", "time_limit": 80.5}),
                         "nemotron-50steps-80.5min")
        with tempfile.TemporaryDirectory() as tmp:
            run = valid_run(Path(tmp))
            status.write_stage(run, 1, f"evaluate:{LABEL}", {"state": "done", "grade": "pass"})
            # a second model, or the same one under other limits, has not been run
            self.assertEqual(status.evaluate_todo(run, LABEL), [])
            self.assertEqual(status.evaluate_todo(run, "nemotron-50steps-40min"), [1])
            self.assertEqual(status.evaluate_todo(run, "qwen38-nvfp4-50steps-80min"), [1])

    def test_unlabeled_results_count_for_their_own_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = valid_run(Path(tmp))
            status.write_stage(run, 1, "evaluate", {"state": "done", "grade": "pass", "model": "qwen38-nvfp4",
                                                    "limits": {"steps": 50, "minutes": 40}})
            self.assertEqual(status.evaluate_todo(run, LABEL), [])
            self.assertEqual(status.evaluate_todo(run, "qwen38-nvfp4-50steps-80min"), [1])
            self.assertEqual(status.evaluate_todo(run, "nemotron-50steps-40min"), [1])
            # one that recorded no limits belongs to no label
            status.write_stage(run, 1, "evaluate", {"state": "done", "grade": "pass", "model": "qwen38-nvfp4"})
            self.assertEqual(status.evaluate_todo(run, LABEL), [1])

    def test_cut_short_evaluations_run_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = valid_run(Path(tmp))
            for info in ({"state": "failed", "note": "model server gone during the run"},
                         {"state": "failed", "note": "build hit the cap"},
                         {"state": "failed", "note": "no time left"},
                         {"state": "done", "grade": "fail", "error": "git apply failed: error: corrupt patch at line 9"}):
                status.write_stage(run, 1, f"evaluate:{LABEL}", info)
                self.assertEqual(status.evaluate_todo(run, LABEL), [1], info)
            status.write_stage(run, 1, f"evaluate:{LABEL}", {"state": "done", "grade": "fail",
                                                            "error": "git apply failed: patch does not apply"})
            self.assertEqual(status.evaluate_todo(run, LABEL), [])

    def test_runs_of_other_projects_do_not_hold_prs(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(Path(tmp) / "a", [{"pr": 7, "stage": "to validate", "instance": "mynewt__mynewt-7"}])
            self.assertEqual(status.unfinished(Path(tmp), of="mynewt"), {7: "a"})
            self.assertEqual(status.unfinished(Path(tmp), of="zephyr"), {})


class ReportTest(unittest.TestCase):
    def test_outcomes_of_a_revalidation(self):
        e = {"pr": 1, "stage": "to validate", "instance": "mynewt__mynewt-1"}
        self.assertEqual(report.outcome(e, {})[0], "accepted, validation not yet")
        self.assertEqual(report.outcome(e, {"validate": {"state": "done", "outcome": "VALID"}})[0], "VALID")
        self.assertEqual(report.outcome({"pr": 2, "stage": "to triage"},
                                        {"triage": {"state": "done", "verdict": "reject"}})[0], "triage: reject")

    def test_the_report_counts_agent_grades(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = write_run(Path(tmp) / "r", [{"pr": 1, "stage": "to validate", "instance": "mynewt__mynewt-1"}],
                            {1: {"validate": {"state": "done", "outcome": "VALID", "instance": "mynewt__mynewt-1",
                                              "failure_mode": "runtime"},
                                 f"evaluate:{LABEL}": {"state": "done", "grade": "pass"},
                                 "evaluate:nemotron-50steps-40min": {"state": "done", "grade": "fail"},
                                 # an unlabeled one is counted for its own limits
                                 "evaluate": {"state": "done", "grade": "fail", "model": "qwen38-nvfp4",
                                              "limits": {"steps": 50, "minutes": 80}}}})
            rep = report.build(run)
            self.assertEqual(rep["project"], "mynewt")
            self.assertEqual(rep["agents"], {LABEL: {"pass": 1}, "nemotron-50steps-40min": {"fail": 1},
                                             "qwen38-nvfp4-50steps-80min": {"fail": 1}})
            self.assertEqual(rep["rows"][0]["pair"], "mynewt__mynewt-1 runtime")
            self.assertIn(f"Agent grades, {LABEL}:** pass 1", report.markdown(rep))

    def test_the_ledger_keeps_projects_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / "ledger.jsonl"
            ledger.record(book, "z", "c", "d", [{"pr": 5, "outcome": "VALID", "detail": ""}])
            ledger.record(book, "m", "c", "d", [{"pr": 5, "outcome": "INVALID", "detail": "x"}], "mynewt")
            self.assertEqual(ledger.latest(book)[5]["outcome"], "VALID")
            self.assertEqual(ledger.latest(book, "mynewt")[5]["outcome"], "INVALID")


class PrepareTest(unittest.TestCase):
    def test_instances_are_copied_and_go_straight_to_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "src" / "mynewt__mynewt-42"
            src.mkdir(parents=True)
            for f in ("metadata.json", "test_patch.diff", "observed_before.log"):
                (src / f).write_text("{}")
            clone = tmp / "clone.git"
            (clone / "objects").mkdir(parents=True)
            cfg = {"derived": {"mynewt_clone": str(clone)}, "parallel": {"enrich": 1}}
            args = SimpleNamespace(instances=str(tmp / "src"), redo=False, pr=None, all=False)
            stage, notes, timings = {}, {}, {}
            with contextlib.redirect_stdout(io.StringIO()):
                prs, source, extra = prepare.plan_mynewt(args, cfg, tmp / "run", {}, tmp / "logs",
                                                         lambda prs: None, stage, notes, timings)
            self.assertEqual(prs, [42])
            self.assertEqual(stage, {42: "to validate"})
            self.assertEqual(extra, {42: {"instance": "mynewt__mynewt-42"}})
            self.assertTrue((tmp / "run" / "sources" / "mynewt__mynewt-42" / "observed_before.log").exists())

    def test_a_cross_pr_instance_is_keyed_by_its_tests_pr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for iid in ("mynewt__mynewt-42-t50", "zephyr__zephyr-7"):
                (tmp / iid / "src" / iid).mkdir(parents=True)
                (tmp / iid / "src" / iid / "metadata.json").write_text("{}")
            (tmp / "clone.git" / "objects").mkdir(parents=True)
            cfg = {"derived": {"mynewt_clone": str(tmp / "clone.git")}}
            stage, extra = {}, None
            with contextlib.redirect_stdout(io.StringIO()):
                args = SimpleNamespace(instances=str(tmp / "mynewt__mynewt-42-t50" / "src"), redo=False)
                prs, _, extra = prepare.plan_mynewt(args, cfg, tmp / "run", {}, tmp / "logs",
                                                    lambda p: None, stage, {}, {})
            self.assertEqual(prs, [50])
            self.assertEqual(extra, {50: {"instance": "mynewt__mynewt-42-t50"}})
            args = SimpleNamespace(instances=str(tmp / "zephyr__zephyr-7" / "src"), redo=False)
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                prepare.plan_mynewt(args, cfg, tmp / "run2", {}, tmp / "logs", lambda p: None, {}, {}, {})


class WorkerTest(unittest.TestCase):
    def test_triage_verdicts_become_accept_or_reject(self):
        with patch.object(node_worker, "PROJECT", "mynewt"):
            self.assertEqual(node_worker.verdict_fields({"kind": "fix_with_test", "confidence": "high",
                                                         "failure_mode": "compile"}),
                             {"verdict": "accept", "kind": "fix_with_test", "failure_mode": "compile"})
            self.assertEqual(node_worker.verdict_fields({"kind": "fix_with_test", "confidence": "low"})["verdict"],
                             "reject")
            self.assertEqual(node_worker.verdict_fields({"kind": "error"})["verdict"], "error")

    def run_validate(self, tmp: Path, results: dict):
        run = write_run(tmp / "r", [{"pr": 9, "stage": "to validate", "instance": "mynewt__mynewt-9"}])
        calls = []

        def step(cmd, logfile, minutes, env):
            calls.append(cmd[1])
            logfile.parent.mkdir(parents=True, exist_ok=True)
            inst = run / "instances" / "mynewt__mynewt-9"
            if cmd[1].endswith("render_mynewt_instance.py"):
                if results.get("generate"):
                    logfile.write_text("ERROR: no clone\n")
                    return 1
                inst.mkdir(parents=True)
                (inst / "metadata.json").write_text(json.dumps({"failure_mode": "runtime"}))
            if cmd[1].endswith("validate_instance.py"):
                self.assertIn("--determinism", cmd)
                (inst / "validation.json").write_text(json.dumps(results["report"]))
                return 0 if results["report"].get("valid") else 1
            return 0
        with patch.object(node_worker, "step", side_effect=step):
            res = node_worker.mynewt_validate(run, 9, tmp / "logs" / "v.log", lambda: 60,
                                              {"EMBEDEVAL_BUILD_TIMEOUT_MIN": "10"})
        return res, calls

    def test_a_revalidation_renders_builds_validates_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, calls = self.run_validate(Path(tmp), {"report": {"valid": True, "leak_check": {"ok": True}}})
            self.assertEqual(res, ("done", "VALID", {"instance": "mynewt__mynewt-9", "failure_mode": "runtime",
                                                     "leak_check": "clean"}))
            self.assertEqual([Path(c).name for c in calls],
                             ["cleanup_instance.py", "render_mynewt_instance.py", "build_instances.py",
                              "validate_instance.py", "cleanup_instance.py"])

    def test_a_leak_is_invalid_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, _ = self.run_validate(Path(tmp), {"report": {
                "valid": False, "reason": "leak: copies: /opt/newt-src",
                "leak_check": {"ok": False, "findings": ["copies: /opt/newt-src"]}}})
            self.assertEqual(res[:2], ("done", "INVALID: leak: copies: /opt/newt-src"))
            self.assertEqual(res[2]["leak_check"], "copies: /opt/newt-src")

    def test_a_refused_recipe_is_invalid_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, calls = self.run_validate(Path(tmp), {"generate": True})
            self.assertEqual(res[:2], ("done", "INVALID: generate refused: ERROR: no clone"))
            self.assertNotIn("build_instances.py", [Path(c).name for c in calls])


class EvaluateTest(unittest.TestCase):
    def test_an_agent_that_lost_its_server_is_run_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = write_run(Path(tmp) / "r", [{"pr": 1, "stage": "to validate", "instance": "mynewt__mynewt-1"}],
                            {1: {"validate": {"state": "done", "outcome": "VALID", "instance": "mynewt__mynewt-1"}}})
            cfg = {"evaluate": {"model": "m", "step_limit": 1, "time_limit": 1, "per_node": 1},
                   "caps": {"build": 1, "server_start": 1}, "evaluate_server_job": "7"}
            tunnel = SimpleNamespace(pid=1, poll=lambda: None)
            alive = iter([True, False])
            with patch.object(node_worker, "LOGS", run / "logs", create=True), \
                    patch.object(node_worker, "ME", "t-node0", create=True), \
                    patch.object(node_worker, "model_tunnel", return_value=(tunnel, "http://x/v1")), \
                    patch.object(node_worker, "server_alive", side_effect=lambda *a: next(alive)), \
                    patch.object(node_worker, "step", return_value=0), \
                    patch.object(node_worker, "kill_group"), patch.object(node_worker, "sweep"), \
                    patch.object(node_worker.report, "to_ledger"):
                node_worker.evaluate(cfg, run, {"EMBEDEVAL_WORK": str(run / "work")}, 120)
            label = "m-1steps-1min"
            self.assertEqual(status.read(run, 1)[f"evaluate:{label}"]["note"], "model server gone during the run")
            self.assertEqual(status.evaluate_todo(run, label), [1])
            self.assertEqual(status.evaluate_todo(run, "n-1steps-1min"), [1])

    def test_an_evaluation_keeps_its_outputs_and_status_to_its_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = valid_run(Path(tmp))
            cfg = {"evaluate": {"model": "nemotron", "step_limit": 30, "time_limit": 25, "per_node": 1},
                   "caps": {"build": 1, "server_start": 1}}
            label = "nemotron-30steps-25min"
            seen = []

            def step(cmd, logfile, minutes, env):
                seen.append((Path(cmd[1]).name, env.get("EMBEDEVAL_OUTPUTS_DIR")))
                if cmd[1].endswith("evaluate_patches.py"):
                    out = Path(cmd[cmd.index("--output") + 1])
                    out.write_text(json.dumps({"results": [{"grade": "pass"}]}))
                return 0
            with patch.object(node_worker, "LOGS", run / "logs", create=True), \
                    patch.object(node_worker, "ME", "t-node0", create=True), \
                    patch.object(node_worker, "step", side_effect=step), \
                    patch.object(node_worker, "sweep"), patch.object(node_worker.report, "to_ledger"):
                node_worker.evaluate(cfg, run, {"EMBEDEVAL_WORK": str(run / "work")}, 120)
            outputs = str(run / "outputs" / label)
            self.assertIn(("run.py", outputs), seen)
            self.assertTrue((run / "outputs" / label / "grades" / "mynewt__mynewt-1.json").exists())
            ev = status.read(run, 1)[f"evaluate:{label}"]
            self.assertEqual((ev["state"], ev["grade"], ev["limits"]), ("done", "pass", {"steps": 30, "minutes": 25}))
            self.assertNotIn("evaluate", status.read(run, 1))
            self.assertEqual(status.evaluate_todo(run, label), [])


class SubmitTest(unittest.TestCase):
    def test_the_frozen_source_is_the_code_with_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = submit.freeze_source(REPO, Path(tmp) / "src")
            self.assertIn("cluster/node_worker.py", m["files"])
            self.assertIn("triage/models.json", m["files"])
            self.assertNotIn("cluster/config.yaml", m["files"])
            self.assertTrue((Path(tmp) / "src" / "scripts" / "mynewt_leakcheck.py").exists())

    def test_mynewt_phases_go_to_their_partitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = write_run(Path(tmp) / "r", [{"pr": 1, "stage": "to validate", "instance": "mynewt__mynewt-1"}])
            cfg = config.load()
            cfg["slurm"].update(build_partition="cpu", partition="spark-interactive")
            q = SimpleNamespace(stdout="", returncode=0)
            for mode, part in (("validate", "cpu"),):
                args = SimpleNamespace(mode=mode, after=None, dry_run=True, idle=2, server_job=None, model=None)
                with patch.object(submit.subprocess, "run", return_value=q), \
                        contextlib.redirect_stdout(io.StringIO()):
                    submit.submit_phase(args, cfg, run)
                script = next((run / "jobs").glob(f"{mode}-*.sbatch")).read_text()
                self.assertIn(f"#SBATCH -p {part}", script)
                self.assertNotIn("--gres", script)
                self.assertIn("--phase validate", script)
            with self.assertRaises(SystemExit):
                submit.submit_phase(SimpleNamespace(mode="pipeline", after=None), cfg, run)

    def test_evaluate_takes_any_profile_of_models_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = valid_run(Path(tmp))
            cfg = config.load()
            cfg["slurm"].update(build_partition="cpu", partition="spark-interactive")
            q = SimpleNamespace(stdout="", returncode=0)
            args = SimpleNamespace(mode="evaluate", after=None, dry_run=True, idle=2, server_job=None,
                                   model="nemotron")
            with patch.object(submit.subprocess, "run", return_value=q), \
                    contextlib.redirect_stdout(io.StringIO()):
                submit.submit_phase(args, cfg, run)
            frozen = json.loads(next((run / "jobs").glob("config-*.json")).read_text())
            self.assertEqual(frozen["evaluate"]["model"], "nemotron")
            args.model = "no-such-model"
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                submit.submit_phase(args, cfg, run)

    def submit_evaluate(self, run, cfg, **given):
        """submit_phase for an evaluate job with sbatch and scontrol faked:
        the commands it ran."""
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            out = f"{75 + sum(1 for c in calls if c[0] == 'sbatch')};scholar\n" if cmd[0] == "sbatch" else ""
            return SimpleNamespace(stdout=out, stderr="", returncode=0)
        args = SimpleNamespace(mode="evaluate", after="5", dry_run=False, idle=2, server_job=None, model=None)
        args.__dict__.update(given)
        with patch.object(submit.subprocess, "run", side_effect=fake), \
                patch.dict(submit.os.environ, {"PATH": "/apps/cent7/anaconda/bin:/usr/bin"}), \
                contextlib.redirect_stdout(io.StringIO()):
            submit.submit_phase(args, cfg, run)
        return calls

    def test_the_model_server_job_gets_a_clean_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = valid_run(Path(tmp))
            cfg = config.load()
            cfg["slurm"].update(build_partition="cpu", partition="spark-interactive")
            calls = self.submit_evaluate(run, cfg)
            server = next(c for c in calls if c[0] == "sbatch" and any(a.startswith("--export") for a in c))
            export = next(a for a in server if a.startswith("--export="))
            self.assertNotIn("anaconda", export)
            self.assertIn("PATH=/usr/local/bin:/usr/bin", export)
            self.assertIn("EMBEDEVAL_MODELS=", export)
            self.assertIn("EMBEDEVAL_VLLM_CACHE=" + str(run / "jobs" / "vllm-cache-"), export)
            self.assertIn("EMBEDEVAL_SCRATCH=" + str(run / "jobs" / "server-scr-"), export)

    def test_an_evaluate_job_gets_one_model_server_per_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            prs = [{"pr": i, "stage": "to validate", "instance": f"mynewt__mynewt-{i}"} for i in range(1, 8)]
            run = write_run(Path(tmp) / "r", prs, {i: {"validate": {"state": "done", "outcome": "VALID"}}
                                                    for i in range(1, 8)})
            cfg = config.load()
            cfg["slurm"].update(build_partition="cpu", partition="spark-interactive", nodes_min=1)
            cfg["evaluate"]["per_node"] = 3                    # 7 instances: 3 nodes
            calls = self.submit_evaluate(run, cfg)
            sbatch = [c for c in calls if c[0] == "sbatch"]
            evaluate_job, server_job = sbatch
            # the evaluate job is held until it names the server, on exactly the nodes of its servers
            self.assertIn("--hold", evaluate_job)
            script = next((run / "jobs").glob("evaluate-*.sbatch")).read_text()
            self.assertIn("#SBATCH -N 3\n", script)
            server_script = next((run / "jobs").glob("server-*.sbatch")).read_text()
            self.assertIn("#SBATCH -N 3\n", server_script)
            self.assertIn("#SBATCH --gres=", server_script)
            self.assertIn("freeing the slot", server_script)       # it does not hold a slot it cannot use
            self.assertIn("squeue -h -j 76", server_script)        # it ends with its evaluate job
            self.assertIn("--dependency=afterany:5", server_job)
            self.assertIn(["scontrol", "update", "JobId=76", "Dependency=afterany:5,after:77"], calls)
            self.assertIn(["scontrol", "release", "76"], calls)
            frozen = json.loads(next((run / "jobs").glob("config-*.json")).read_text())
            self.assertEqual(frozen["evaluate_server_job"], "77")
            # the job ends its servers once its workers are done
            self.assertIn(f"scancel $(cat {run}/jobs/server-of-$SLURM_JOB_ID", script)
            self.assertLess(script.index("srun"), script.index("scancel"))
            self.assertEqual((run / "jobs" / "server-of-76").read_text().strip(), "77")

    def test_a_server_job_given_or_a_hosted_model_starts_no_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = valid_run(Path(tmp))
            cfg = config.load()
            cfg["slurm"].update(build_partition="cpu", partition="spark-interactive")
            calls = self.submit_evaluate(run, cfg, server_job="9")
            self.assertEqual(len([c for c in calls if c[0] == "sbatch"]), 1)
            self.assertNotIn("--hold", calls[-1])
            self.assertIn("--dependency=afterany:5,after:9", calls[-1])
            self.assertNotIn("scancel", next((run / "jobs").glob("evaluate-*.sbatch")).read_text())
            calls = self.submit_evaluate(run, cfg, model="claude-opus-4-6")
            self.assertEqual(len([c for c in calls if c[0] == "sbatch"]), 1)


class ChainTest(unittest.TestCase):
    """Several models queued at once: one evaluation each, after the one before."""
    MODELS = ["qwen38-nvfp4", "nemotron", "qwen38"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = valid_run(Path(self.tmp.name))
        self.cfg = config.load()
        self.cfg["slurm"].update(build_partition="cpu", partition="spark-interactive")

    def submit(self, dry_run, models=None, after=None, queue=""):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            n = sum(1 for c in calls if c[0] == "sbatch")
            return SimpleNamespace(stdout=f"{100 + n};scholar\n" if cmd[0] == "sbatch" else queue,
                                   stderr="", returncode=0)
        args = SimpleNamespace(mode="evaluate", after=after, dry_run=dry_run, idle=2, server_job=None,
                               model=models or self.MODELS)
        out = io.StringIO()
        with patch.object(submit.subprocess, "run", side_effect=fake), \
                patch.dict(submit.os.environ, {"PATH": "/usr/bin"}), contextlib.redirect_stdout(out):
            submit.submit_evaluations(args, self.cfg, self.run)
        return calls, out.getvalue()

    def test_a_dry_run_plans_the_whole_chain_without_sbatch(self):
        calls, out = self.submit(True)
        self.assertEqual([c[0] for c in calls if c[0] in ("sbatch", "scontrol", "scancel")], [])
        scripts = sorted((self.run / "jobs").glob("evaluate-*.sbatch"))
        self.assertEqual(len(scripts), 3)
        names = [re.search(r"#SBATCH -J (\S+)", p.read_text())[1] for p in scripts]
        self.assertEqual(sorted(names), sorted(f"embedeval-r-evaluate-{m}-50steps-40min" for m in self.MODELS))
        self.assertEqual(len(set(names)), 3)           # so the one-job guard never meets another model's job
        first, second, third = (f"embedeval-r-evaluate-{m}-50steps-40min" for m in self.MODELS)
        self.assertIn(f"dependency: afterany:<{first}>", out)
        self.assertIn(f"dependency: afterany:<{second}>", out)
        self.assertNotIn("afterany:<" + third, out)
        self.assertIn(f"evaluations, in order: {self.MODELS[0]} (job <{first}>) -> {self.MODELS[1]}", out)

    def test_each_evaluation_is_chained_after_the_one_before(self):
        calls, out = self.submit(False, after="5")
        sbatch = [c for c in calls if c[0] == "sbatch"]
        # per model: the held evaluate job (ids 101, 103, 105), then its server job (102, 104, 106)
        evaluate_jobs = [c for c in sbatch if "--hold" in c]
        servers = [c for c in sbatch if any(a.startswith("--export") for a in c)]
        self.assertEqual([next(a for a in c if a.startswith("--dependency")) for c in evaluate_jobs],
                         ["--dependency=afterany:5", "--dependency=afterany:101", "--dependency=afterany:103"])
        self.assertEqual([next(a for a in c if a.startswith("--dependency")) for c in servers],
                         ["--dependency=afterany:5", "--dependency=afterany:101", "--dependency=afterany:103"])
        self.assertIn(["scontrol", "update", "JobId=103", "Dependency=afterany:101,after:104"], calls)
        self.assertIn("evaluations, in order: qwen38-nvfp4 (job 101) -> nemotron (job 103) -> qwen38 (job 105)", out)
        self.assertEqual(len(list((self.run / "jobs").glob("server-of-*"))), 3)

    def test_a_model_already_evaluated_is_left_out_and_unknown_ones_refuse_everything(self):
        status.write_stage(self.run, 1, "evaluate:nemotron-50steps-40min", {"state": "done", "grade": "pass"})
        calls, out = self.submit(False)
        self.assertIn("nothing left to evaluate as nemotron-50steps-40min", out)
        evaluate_jobs = [c for c in calls if c[0] == "sbatch" and "--hold" in c]
        self.assertEqual([next(a for a in c if a.startswith("--dependency")) for c in evaluate_jobs[1:]],
                         ["--dependency=afterany:101"])
        with tempfile.TemporaryDirectory() as tmp:
            self.run = valid_run(Path(tmp))
            before = []
            with self.assertRaises(SystemExit):
                self.submit(False, models=["nemotron", "no-such-model"])
            self.assertEqual(list((self.run / "jobs").glob("*")), before)
            with self.assertRaises(SystemExit):
                self.submit(False, models=["nemotron", "nemotron"])


if __name__ == "__main__":
    unittest.main()
