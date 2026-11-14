"""Local checks of the NuttX phase jobs: prepare --instances, phase
submission against a fake scheduler, per-architecture work, and the
evaluate phase's records.

    python3 -m unittest cluster/test_nuttx_cluster.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

CLUSTER = Path(__file__).resolve().parent
REPO = CLUSTER.parent
sys.path[:0] = [str(CLUSTER), str(REPO / "scripts")]

import node_worker  # noqa: E402
import report  # noqa: E402
import status  # noqa: E402

INSTANCES = REPO / "apptainer" / "instances" / "nuttx"
ARM, X86 = "nuttx__nuttx-11889-a2327", "nuttx__nuttx-14152-a2725"

FAKE = {
    "sbatch": """#!/bin/bash
echo "$@" >> "$FAKE_LOG/sbatch.log"
for a in "$@"; do case "$a" in --test-only) echo "sbatch: Job would start"; exit 0;; esac; done
n=$(( $(wc -l < "$FAKE_LOG/sbatch.log") + 1000 )); echo "$n"
""",
    "squeue": "#!/bin/bash\nexit 0\n",
    "scontrol": "#!/bin/bash\necho \"$@\" >> \"$FAKE_LOG/scontrol.log\"\n",
    "scancel": "#!/bin/bash\necho \"$@\" >> \"$FAKE_LOG/scancel.log\"\n",
    "sinfo": "#!/bin/bash\necho 3\n",
}


class Scholar:
    """A scratch root, a config, and fake sbatch/squeue/sinfo on PATH."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in FAKE.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.log = self.root / "log"
        self.log.mkdir()
        self.config = self.root / "config.yaml"
        self.config.write_text(f"""project: nuttx
scratch_root: {self.root / 'scratch'}
slurm: {{partition: spark-interactive, nodes_min: 1, nodes_max: 4, time_limit: "04:00:00",
        mem: 60250M, build_partition: cpu, build_path: /opt/x86/bin}}
caps: {{triage_phase: 120, validate_phase: 220}}
nuttx: {{validate: true}}
""")
        self.instances = self.root / "promoted"
        self.instances.mkdir()
        for iid in (ARM, X86):
            shutil.copytree(INSTANCES / iid, self.instances / iid)
            meta = json.loads((self.instances / iid / "metadata.json").read_text())
            meta.setdefault("needs_version_file", False)      # no kernel clone here
            (self.instances / iid / "metadata.json").write_text(json.dumps(meta, indent=4) + "\n")

    def run(self, script: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log)}
        r = subprocess.run([sys.executable, str(CLUSTER / script), *args, "--config", str(self.config)],
                           capture_output=True, text=True, env=env, cwd=REPO)
        if check and r.returncode:
            raise AssertionError(f"{script} {args}: rc={r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def prepared(self, name="reval") -> Path:
        self.run("prepare.py", "--name", name, "--instances", str(self.instances))
        return self.root / "scratch" / "runs" / name

    def sbatch_calls(self) -> list[str]:
        f = self.log / "sbatch.log"
        return [l for l in f.read_text().splitlines() if "--test-only" not in l] if f.exists() else []


class Prepare(unittest.TestCase):
    def setUp(self):
        self.s = Scholar()

    def tearDown(self):
        self.s.tmp.cleanup()

    def test_instances_round_trip(self):
        run = self.s.prepared()
        m = status.manifest(run)
        self.assertEqual({e["pr"]: (e["instance"], e["arch"], e["stage"]) for e in m["prs"]},
                         {2327: (ARM, "aarch64", "to validate"), 2725: (X86, "x86_64", "to validate")})
        for iid in (ARM, X86):
            d = run / "instances" / iid
            self.assertIn("--depth 1", (d / "instance.def").read_text())
            self.assertTrue((d / "previous-validation.json").exists())
            self.assertEqual((d / "test_patch.diff").read_bytes(),
                             (INSTANCES / iid / "test_patch.diff").read_bytes())
        self.assertEqual(sorted(status.validate_todo(run)), [2327, 2725])
        self.assertEqual(sorted(status.statement_todo(run)), [2327, 2725])
        self.assertEqual(status.evaluate_todo(run, "qwen38-nvfp4-50steps-40min"), [])

    def test_id_must_match_metadata(self):
        (self.s.instances / ARM).rename(self.s.instances / "nuttx__nuttx-11889-a9999")
        r = self.s.run("prepare.py", "--name", "bad", "--instances", str(self.s.instances), check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("matching its metadata", r.stderr)


class Submit(unittest.TestCase):
    def setUp(self):
        self.s = Scholar()
        self.run = self.s.prepared()

    def tearDown(self):
        self.s.tmp.cleanup()

    def script(self, mode: str, arch: str) -> str:
        return next((self.run / "jobs").glob(f"{mode}-{arch}-*.sbatch")).read_text()

    def test_validate_per_arch(self):
        self.s.run("submit.py", "validate", "--run", self.run.name, "--arch", "aarch64")
        self.s.run("submit.py", "validate", "--run", self.run.name, "--arch", "x86_64")
        arm, x86 = self.script("validate", "aarch64"), self.script("validate", "x86_64")
        self.assertIn("#SBATCH -p spark-interactive", arm)
        self.assertIn("#SBATCH --gres=gpu:1", arm)
        self.assertIn("#SBATCH -p cpu", x86)
        self.assertIn('PATH="/opt/x86/bin:$HOME/.local/bin:', x86)
        self.assertIn('PATH="$HOME/.local/bin:', arm)
        self.assertNotIn("--gres", x86)
        for text in (arm, x86):
            self.assertIn("env -i PATH=", text)
            self.assertIn("sha256sum --quiet -c source-sha256.txt", text)
        sub = [json.loads(l) for l in (self.run / "jobs" / "submitted.jsonl").read_text().splitlines()]
        self.assertEqual([(x["arch"], x["todo"]) for x in sub], [("aarch64", 1), ("x86_64", 1)])

    def test_frozen_source_checks(self):
        self.s.run("submit.py", "statement", "--run", self.run.name)
        source = next((self.run / "jobs").glob("source-*"))
        r = subprocess.run(["sha256sum", "--quiet", "-c", "source-sha256.txt"], cwd=source,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(any(p.name == "config.yaml" for p in source.rglob("*")))
        self.assertTrue((source / "cluster" / "config.example.yaml").exists())
        (source / "scripts" / "nuttx_leakcheck.py").write_text("changed")
        r = subprocess.run(["sha256sum", "--quiet", "-c", "source-sha256.txt"], cwd=source,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_after_chains(self):
        self.s.run("submit.py", "validate", "--run", self.run.name, "--arch", "aarch64", "--after", "77")
        self.assertIn("--dependency=afterany:77", self.s.sbatch_calls()[-1])

    def test_evaluate_x86_owns_its_server(self):
        self.s.run("submit.py", "evaluate", "--run", self.run.name, "--arch", "x86_64",
                   "--model", "qwen38-nvfp4", "--after", "77")
        job, server = self.s.sbatch_calls()[-2:]
        self.assertIn("--hold", job)                       # held until it is chained to its server
        self.assertIn("--dependency=afterany:77", job)
        self.assertIn("--dependency=afterany:77", server)
        self.assertTrue(server.rstrip().endswith(".sbatch"))
        export = server.split("--export=", 1)[1].split()[0]
        self.assertNotIn("ALL", export)
        self.assertIn("PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin,", export)
        scontrol = (self.s.log / "scontrol.log").read_text().splitlines()
        self.assertEqual(scontrol, ["update JobId=1001 Dependency=afterany:77,after:1002", "release 1001"])
        server_script = next((self.run / "jobs").glob("server-x86_64-*.sbatch")).read_text()
        self.assertIn("squeue -h -j 1001", server_script)
        self.assertIn("#SBATCH --gres=gpu:1", server_script)
        self.assertIn("serve_triage_model.sh qwen38-nvfp4", server_script)
        self.assertIn("#SBATCH -N 1\n", server_script)            # one server per evaluate node
        self.assertIn("#SBATCH -t 04:00:00\n", server_script)    # as long as its evaluate job
        self.assertIn('node$SLURM_PROCID', server_script)
        self.assertIn("server-of-$SLURM_JOB_ID", self.script("evaluate", "x86_64"))
        self.assertEqual((self.run / "jobs" / "server-of-1001").read_text().strip(), "1002")
        cfg = json.loads(next((self.run / "jobs").glob("config-*.json")).read_text())
        self.assertEqual((cfg["evaluate"]["model"], cfg["evaluate_server_job"]), ("qwen38-nvfp4", "1002"))

    def test_evaluate_arm_serves_itself(self):
        r = self.s.run("submit.py", "evaluate", "--run", self.run.name, "--arch", "aarch64")
        self.assertIn("nothing left to evaluate", r.stdout)       # nothing VALID yet
        status.write_stage(self.run, 2327, "validate", {"state": "done", "outcome": "VALID"})
        self.s.run("submit.py", "evaluate", "--run", self.run.name, "--arch", "aarch64", "--model", "nemotron")
        self.assertEqual(len(self.s.sbatch_calls()), 1)
        cfg = json.loads(next((self.run / "jobs").glob("config-*.json")).read_text())
        self.assertEqual(cfg["evaluate"]["model"], "nemotron")
        self.assertNotIn("evaluate_server_job", cfg)
        self.assertNotIn("scancel", self.script("evaluate", "aarch64"))
        # the whole job: evaluate.job_time_limit, the phase all of it but the report
        script = self.script("evaluate", "aarch64")
        self.assertIn("#SBATCH -t 04:00:00\n", script)
        self.assertIn("timeout -k 2m 230m srun", script)
        self.assertIn('EMBEDEVAL_PHASE_END="$PHASE_END"', script)

    def test_unknown_model(self):
        r = self.s.run("submit.py", "evaluate", "--run", self.run.name, "--arch", "aarch64",
                       "--model", "nobody", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown model", r.stderr)

    def test_arch_needed(self):
        r = self.s.run("submit.py", "validate", "--run", self.run.name, check=False)
        self.assertIn("needs --arch", r.stderr)


class ServerWatchdog(unittest.TestCase):
    """server.sbatch ends when its evaluate job is gone, or cannot start."""

    def run_server(self, squeue: str, grace: int) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "bin").mkdir()
            for name, body in (("squeue", squeue), ("curl", "exit 0"), ("sleep", "exit 0"),
                               ("srun", 'exec "${@: -3}"')):
                (tmp / "bin" / name).write_text(f"#!/bin/bash\n{body}\n")
                (tmp / "bin" / name).chmod(0o755)
            (tmp / "scripts").mkdir()
            (tmp / "scripts" / "serve_triage_model.sh").write_text("exec tail -f /dev/null\n")
            text = (CLUSTER / "templates" / "server.sbatch").read_text().format(
                partition="p", cpus=1, gres="gpu:1", mem="1G", time_limit="1:00", name="n", jobs=tmp,
                model="m", eval_job="42", stamp="s", source=tmp, grace=grace, server_nodes=1,
                warm_cache=tmp / "warm")
            (tmp / "server.sh").write_text(text)
            r = subprocess.run(["bash", str(tmp / "server.sh")], capture_output=True, text=True, timeout=60,
                               env={**os.environ, "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}"})
            return r.stdout

    def test_gone(self):
        self.assertIn("evaluate job 42 is gone", self.run_server("exit 0", 30))

    def test_cannot_start(self):
        self.assertIn("still PENDING", self.run_server("echo PENDING", -1))


class Worker(unittest.TestCase):
    def setUp(self):
        self.s = Scholar()
        self.run = self.s.prepared()
        node_worker.N, node_worker.NN, node_worker.LOGS = 0, 1, self.s.root / "logs"
        node_worker.LOGS.mkdir()

    def tearDown(self):
        self.s.tmp.cleanup()

    def test_arch_filter(self):
        with mock.patch.object(node_worker, "MACHINE", "x86_64"):
            self.assertEqual(node_worker.assigned_work(self.run, "validate"), [2725])
        with mock.patch.object(node_worker, "MACHINE", "aarch64"):
            self.assertEqual(node_worker.assigned_work(self.run, "validate"), [2327])
            self.assertEqual(node_worker.assigned_work(self.run, "statement"), [2327, 2725])

    def test_evaluate_records_timeouts_apart(self):
        status.write_stage(self.run, 2327, "validate", {"state": "done", "outcome": "VALID"})
        cfg = json.loads((self.run / "manifest.json").read_text())["config"]
        cfg["derived"] = {"vllm_cache": str(self.s.root / "vc")}
        cfg["nuttx"]["data"] = str(self.s.root / "data")
        (self.s.root / "data").mkdir()
        cfg["evaluate"]["time_limit"] = 40
        label = "qwen38-nvfp4-250steps-40min"
        out = self.run / "outputs" / label / "nuttx" / "qwen38-nvfp4" / "11889-a2327"
        graded = []

        def fake_step(cmd, logfile, minutes, env, cwd=REPO):
            graded.extend(c for c in cmd if c == "harness/evaluate_patches.py")
            if "harness/run.py" in cmd:
                out.mkdir(parents=True, exist_ok=True)
                (out / "result.json").write_text(json.dumps(
                    {"exit_status": "TimeExceeded", "steps": 31, "elapsed_seconds": 2400}))
            if "harness/evaluate_patches.py" in cmd:
                Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(
                    {"results": [{"grade": "fail", "error": "tests failed", "network": "isolated"}]}))
            return 0

        server = mock.Mock(pid=4242)
        with mock.patch.object(node_worker, "MACHINE", "aarch64"), \
                mock.patch.object(node_worker, "step", fake_step), \
                mock.patch.object(node_worker, "start_server", return_value=server) as start, \
                mock.patch.object(node_worker, "server_ready", return_value=True), \
                mock.patch.object(node_worker, "server_alive", return_value=True), \
                mock.patch.object(node_worker, "kill_group") as kill:
            node_worker.evaluate(cfg, self.run, {}, self.s.root / "node")
        self.assertEqual(start.call_args[0][3], "qwen38-nvfp4")
        kill.assert_called_with(4242)
        ev = status.read(self.run, 2327)[f"evaluate:{label}"]
        # not submitted (the time limit): a fail, never graded (as main)
        self.assertEqual((ev["state"], ev["grade"], ev["timed_out"], ev["steps"], ev["tests_run"]),
                         ("done", "fail", True, 31, False))
        self.assertEqual(ev["error"], "not submitted (run ended: TimeExceeded)")
        self.assertEqual(graded, [])
        self.assertEqual(ev["limits"], {"steps": 250, "minutes": 40})
        self.assertEqual(status.evaluate_todo(self.run, label), [])
        self.assertEqual(status.evaluate_todo(self.run, "qwen38-nvfp4-50steps-80min"), [2327])
        # a git-generated diff that git apply calls corrupt is the harness's fault: run again
        status.write_stage(self.run, 2327, f"evaluate:{label}", {**ev, "grade": "fail",
                                                                  "error": "git apply failed: error: corrupt patch"})
        self.assertEqual(status.evaluate_todo(self.run, label), [2327])

    def test_unlabeled_results_count_for_their_limits(self):
        """Results of the first evaluate jobs (stage "evaluate") keep counting."""
        status.write_stage(self.run, 2327, "validate", {"state": "done", "outcome": "VALID"})
        status.write_stage(self.run, 2327, "evaluate", {"state": "done", "grade": "pass", "model": "qwen38-nvfp4",
                                                        "limits": {"steps": 50, "minutes": 40}})
        self.assertEqual(status.evaluate_todo(self.run, "qwen38-nvfp4-50steps-40min"), [])
        self.assertEqual(status.evaluate_todo(self.run, "qwen38-nvfp4-50steps-80min"), [2327])

    def test_lost_server_runs_again(self):
        status.write_stage(self.run, 2327, "validate", {"state": "done", "outcome": "VALID"})
        cfg = json.loads((self.run / "manifest.json").read_text())["config"]
        cfg["derived"] = {"vllm_cache": str(self.s.root / "vc")}
        cfg["nuttx"]["data"] = str(self.s.root / "data")
        (self.s.root / "data").mkdir()
        cfg["evaluate"]["time_limit"] = 40
        with mock.patch.object(node_worker, "MACHINE", "aarch64"), \
                mock.patch.object(node_worker, "step", return_value=0), \
                mock.patch.object(node_worker, "start_server", return_value=mock.Mock(pid=1)), \
                mock.patch.object(node_worker, "server_ready", return_value=True), \
                mock.patch.object(node_worker, "server_alive", return_value=False), \
                mock.patch.object(node_worker, "kill_group"):
            node_worker.evaluate(cfg, self.run, {}, self.s.root / "node")
        label = "qwen38-nvfp4-250steps-40min"
        self.assertEqual(status.read(self.run, 2327)[f"evaluate:{label}"]["state"], "failed")
        self.assertEqual(status.evaluate_todo(self.run, label), [2327])


    def run_evaluate(self, minutes, left=170, agent_rc=0, ending="Submitted"):
        """evaluate() on instance 2327 with evaluate.time_limit `minutes` and `left`
        minutes of the job (validate_phase = left + 3) left; returns the commands run."""
        status.write_stage(self.run, 2327, "validate", {"state": "done", "outcome": "VALID"})
        cfg = json.loads((self.run / "manifest.json").read_text())["config"]
        cfg["derived"] = {"vllm_cache": str(self.s.root / "vc")}
        cfg["nuttx"]["data"] = str(self.s.root / "data")
        (self.s.root / "data").mkdir(exist_ok=True)
        cfg["evaluate"]["time_limit"] = minutes
        cfg["caps"]["validate_phase"] = left + 3
        calls = []

        label = status.eval_label(cfg["evaluate"])
        out = self.run / "outputs" / label / "nuttx" / "qwen38-nvfp4" / "11889-a2327"

        def fake_step(cmd, logfile, mins, env, cwd=REPO):
            calls.append((cmd, mins))
            if "harness/run.py" in cmd:
                if ending is not None:
                    out.mkdir(parents=True, exist_ok=True)
                    (out / "result.json").write_text(json.dumps({"exit_status": ending, "steps": 3}))
                return agent_rc
            if "harness/evaluate_patches.py" in cmd:
                Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(
                    {"results": [{"grade": "fail", "error": "tests failed"}]}))
            return 0

        with mock.patch.object(node_worker, "MACHINE", "aarch64"), \
                mock.patch.object(node_worker, "step", fake_step), \
                mock.patch.object(node_worker, "start_server", return_value=mock.Mock(pid=1)), \
                mock.patch.object(node_worker, "server_ready", return_value=True), \
                mock.patch.object(node_worker, "server_alive", return_value=True), \
                mock.patch.object(node_worker, "kill_group"):
            node_worker.evaluate(cfg, self.run, {}, self.s.root / "node")
        return calls

    def test_label_with_and_without_time_limit(self):
        ev = {"model": "m", "step_limit": 50, "time_limit": 0}
        self.assertEqual(status.eval_label(ev), "m-50steps")
        self.assertEqual(status.eval_label({**ev, "time_limit": 40}), "m-50steps-40min")

    def test_old_positive_limit_result_keeps_its_label(self):
        status.write_stage(self.run, 2327, "validate", {"state": "done", "outcome": "VALID"})
        status.write_stage(self.run, 2327, "evaluate", {"state": "done", "grade": "fail", "model": "m",
                                                        "limits": {"steps": 50, "minutes": 80}})
        self.assertEqual(status.evaluate_todo(self.run, "m-50steps-80min"), [])
        self.assertEqual(status.evaluate_todo(self.run, "m-50steps"), [2327])

    def test_no_limit_cap_is_the_time_left(self):
        calls = self.run_evaluate(0, left=170)
        cmd, cap = next(c for c in calls if "harness/run.py" in c[0])
        self.assertEqual(cmd[cmd.index("--time-limit") + 1], "0")
        self.assertAlmostEqual(cap, 170 - 30, delta=1)       # less the grading margin, not 20
        self.assertEqual(status.read(self.run, 2327)["evaluate:qwen38-nvfp4-250steps"]["grade"], "fail")

    def test_positive_limit_cap_unchanged(self):
        cmd, cap = next(c for c in self.run_evaluate(40) if "harness/run.py" in c[0])
        self.assertEqual((cmd[cmd.index("--time-limit") + 1], cap), ("40", 60))

    def test_no_new_run_with_less_than_min_minutes_left(self):
        self.assertFalse([c for c in self.run_evaluate(0, left=50) if "harness/run.py" in c[0]])
        self.assertEqual(status.evaluate_todo(self.run, "qwen38-nvfp4-250steps"), [2327])

    def test_time_limited_run_starts_until_the_last_hour(self):
        # as main's EVAL_MARGIN: a 120-min run starts with 61 min left, not with 59
        self.assertFalse([c for c in self.run_evaluate(120, left=59) if "harness/run.py" in c[0]])
        self.assertEqual(status.evaluate_todo(self.run, "qwen38-nvfp4-250steps-120min"), [2327])
        cmd, cap = next(c for c in self.run_evaluate(120, left=100) if "harness/run.py" in c[0])
        self.assertEqual(cmd[cmd.index("--time-limit") + 1], "120")
        self.assertAlmostEqual(cap, 100 - 30, delta=1)       # the time left, less grading

    def test_only_a_submitted_run_is_graded(self):
        label = "qwen38-nvfp4-250steps-120min"
        for ending in ("LimitsExceeded", "RepeatedFormatError", "ContextWindowExceededError"):
            status.write_stage(self.run, 2327, f"evaluate:{label}", {"state": "failed"})   # to do again
            calls = self.run_evaluate(120, ending=ending)
            self.assertFalse([c for c in calls if "harness/evaluate_patches.py" in c[0]], ending)
            ev = status.read(self.run, 2327)[f"evaluate:{label}"]
            self.assertEqual((ev["state"], ev["grade"], ev["tests_run"]), ("done", "fail", False), ending)
        status.write_stage(self.run, 2327, f"evaluate:{label}", {"state": "failed"})
        calls = self.run_evaluate(120, ending="Submitted")
        self.assertTrue([c for c in calls if "harness/evaluate_patches.py" in c[0]])
        self.assertTrue(status.read(self.run, 2327)[f"evaluate:{label}"]["tests_run"])

    def test_run_with_no_ending_runs_again(self):
        # no exit_status (setup or harness error, e.g. hiding the tests): not the agent's
        label = "qwen38-nvfp4-250steps-120min"
        calls = self.run_evaluate(120, ending=None)
        self.assertFalse([c for c in calls if "harness/evaluate_patches.py" in c[0]])
        self.assertEqual(status.read(self.run, 2327)[f"evaluate:{label}"]["state"], "failed")
        self.assertEqual(status.evaluate_todo(self.run, label), [2327])

    def test_deadline_is_the_phase_end(self):
        # phase.sbatch's EMBEDEVAL_PHASE_END, not caps.validate_phase (set to 1000 here)
        with mock.patch.dict(os.environ, {"EMBEDEVAL_PHASE_END": str(time.time() + 103 * 60)}):
            cmd, cap = next(c for c in self.run_evaluate(120, left=1000) if "harness/run.py" in c[0])
        self.assertAlmostEqual(cap, 100 - 30, delta=1)

    def test_time_limited_run_cut_by_the_job_runs_again(self):
        label = "qwen38-nvfp4-250steps-120min"
        calls = self.run_evaluate(120, left=100, agent_rc=124)
        self.assertFalse([c for c in calls if "harness/evaluate_patches.py" in c[0]])   # never graded
        ev = status.read(self.run, 2327)[f"evaluate:{label}"]
        self.assertEqual((ev["cut_short"], ev["cuts"]), (True, 1))
        self.assertEqual(status.evaluate_todo(self.run, label), [2327])

    def test_cut_short_runs_again_then_is_final(self):
        label = "qwen38-nvfp4-250steps"
        calls = self.run_evaluate(0, agent_rc=124)
        self.assertFalse([c for c in calls if "harness/evaluate_patches.py" in c[0]])   # never graded
        ev = status.read(self.run, 2327)[f"evaluate:{label}"]
        self.assertEqual((ev["cut_short"], ev["cuts"], report.evaluation(ev)), (True, 1, "cut short"))
        self.assertEqual(status.evaluate_todo(self.run, label), [2327])
        self.run_evaluate(0, agent_rc=124)
        ev = status.read(self.run, 2327)[f"evaluate:{label}"]
        self.assertEqual((ev["cuts"], report.evaluation(ev)), (2, "unfinished"))
        self.assertEqual(status.evaluate_todo(self.run, label), [])


if __name__ == "__main__":
    unittest.main()
