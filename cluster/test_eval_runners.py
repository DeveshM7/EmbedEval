"""Local checks of the evaluation runners of Mynewt, NuttX and Contiki-NG: the
worker's queue and results table for every kind of instance id, the image
build before a run (mocked), the choice of instances by architecture, and
that each runner keeps main's Zephyr and RIOT settings.

    python3 -m unittest cluster/test_eval_runners.py
"""

import argparse
import ast
import contextlib
import io
import json
import re
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
sys.path[:0] = [str(CLUSTER), str(REPO / "harness"), str(REPO / "scripts")]

import claims  # noqa: E402
import paths  # noqa: E402
import run  # noqa: E402
import eval_images  # noqa: E402
import eval_set  # noqa: E402
import eval_worker  # noqa: E402
import results  # noqa: E402

MODEL = "qwen38-nvfp4"
FULL = "openai/qwen38-nvfp4"


def instance(root: Path, iid: str, **meta) -> None:
    d = root / iid
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(json.dumps({"instance_id": iid, "project": iid.split("__")[0], **meta}))


class Scratch(unittest.TestCase):
    """Instances and an eval folder of their own, in place of the repository's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.instances, self.eval = self.root / "instances", self.root / "eval"
        self.instances.mkdir()
        for patcher in (mock.patch.object(paths, "INSTANCE_ROOTS", [self.instances]),
                        mock.patch.object(paths, "OUTPUTS_DIR", self.eval)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def grade(self, iid: str, grade: str = "pass") -> None:
        paths.write_json(paths.grade_path(iid, MODEL), {"instance_id": iid, "grade": grade})


class IdKeys(unittest.TestCase):
    def test_a_plain_pr_is_its_number_and_a_suffixed_one_is_text(self):
        self.assertEqual(claims.key_of("65697"), 65697)
        for text in ("3299-a651", "123-t45", "123-c1a2b3c4"):
            self.assertEqual(claims.key_of(text), text)

    def test_numbers_keep_their_numeric_order_and_suffixed_ones_follow_their_number(self):
        keys = [100, 20, "3299-a651", 3299, "123-t45", 123, "123-c1a2b3c4"]
        self.assertEqual(sorted(keys, key=claims.order),
                         [20, 100, 123, "123-c1a2b3c4", "123-t45", 3299, "3299-a651"])
        ints = [65697, 9, 100, 12345]
        self.assertEqual(sorted(ints, key=claims.order), sorted(ints))

    def test_the_queue_takes_suffixed_ids_and_survives_a_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            todo = ["1236-a293", "11889-a2327", "14152-a2725", "14152-a2704"]
            todo.sort(key=claims.order)
            q = claims.Queue(Path(tmp), "nuttx", "job1-node0", needed=lambda pr: True)
            first = q.take(todo)
            self.assertEqual(first, "1236-a293")
            second = claims.Queue(Path(tmp), "nuttx", "job2-node0", needed=lambda pr: True)
            alive = Path(tmp) / "claims" / "alive"
            (alive / "job1-node0").touch()
            (alive / "job2-node0").touch()
            self.assertEqual(second.take(todo), "11889-a2327")     # the first is held, not taken twice
            q.release(first, done=True)
            self.assertEqual(json.loads((Path(tmp) / "claims" / "nuttx.json").read_text())["1236-a293"]["state"],
                             "done")
            self.assertTrue(second.busy_elsewhere(todo) is False)  # nothing held by another worker now
            second.release("11889-a2327", done=False)               # freed: gone from the table
            self.assertNotIn("11889-a2327", json.loads((Path(tmp) / "claims" / "nuttx.json").read_text()))

    def test_integer_claims_are_written_as_they_always_were(self):
        """Zephyr's and RIOT's table: the same file, the same order."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "claims").mkdir()
            old = {"65697": {"owner": "x-node0", "state": "done", "since": "2026-11-01T00:00:00"},
                   "9": {"owner": "x-node0", "state": "done", "since": "2026-11-01T00:00:00"}}
            f = Path(tmp) / "claims" / "zephyr.json"
            f.write_text(json.dumps(old))
            q = claims.Queue(Path(tmp), "zephyr", "y-node0", needed=lambda pr: True)
            (Path(tmp) / "claims" / "alive" / "y-node0").touch()
            self.assertEqual(q.take([9, 100, 65697]), 9)            # done by another job: taken again
            written = json.loads(f.read_text())
            self.assertEqual(list(written), ["9", "65697"])
            q.take([100])
            expected = {str(k): v for k, v in sorted({int(k): v for k, v in json.loads(f.read_text()).items()}.items())}
            self.assertEqual(f.read_text(), json.dumps(expected))   # as the integer-only code wrote it


class Worker(Scratch):
    """eval_worker.main against fake instances, with the run and the image build mocked."""

    def work(self, project, ids, *extra, ensure=None, runs=None):
        calls = []

        def fake_run(iid, model, args):
            calls.append(iid)
            self.grade(iid)
            return {"grade": "pass"}
        argv = ["eval_worker.py", "--project", project, "--model", MODEL, "--name", "t-node0",
                "--stop-at", str(time.time() + 10 ** 6), "--margin", "0", *extra]
        out = io.StringIO()
        old = eval_worker.signal.getsignal(eval_worker.signal.SIGTERM)
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(run, "run_and_grade", side_effect=runs or fake_run), \
                mock.patch.object(eval_images, "ensure", side_effect=ensure or (lambda iid: None)) as ens, \
                mock.patch.object(eval_images, "sweep_sandboxes"), \
                contextlib.redirect_stdout(out):
            try:
                eval_worker.main()
            finally:
                eval_worker.signal.signal(eval_worker.signal.SIGTERM, old)
        return calls, ens, out.getvalue()

    def test_nuttx_ids_with_an_apps_pr_are_worked_in_numeric_order_under_their_own_key(self):
        for iid in ("nuttx__nuttx-14152-a2725", "nuttx__nuttx-1236-a293", "nuttx__nuttx-14152-a2704"):
            instance(self.instances, iid, runtime="apptainer", host_arch="x86_64")
        calls, ens, out = self.work("nuttx", None, "--arch", "x86_64")
        self.assertEqual(calls, ["nuttx__nuttx-1236-a293", "nuttx__nuttx-14152-a2704", "nuttx__nuttx-14152-a2725"])
        self.assertEqual(sorted(json.loads((self.eval / FULL.split("/")[1] / "claims" / "nuttx.json").read_text())),
                         ["1236-a293", "14152-a2704", "14152-a2725"])
        self.assertEqual(ens.call_count, 3)                     # each image built, if need be, before its run

    def test_mynewt_test_pr_and_fix_commit_ids_and_hyphenated_contiki_ids(self):
        for iid in ("mynewt__mynewt-1030", "mynewt__mynewt-1030-t5", "mynewt__mynewt-77-c0123abcd"):
            instance(self.instances, iid, runtime="apptainer", host_arch="x86_64")
        calls, _, _ = self.work("mynewt", None, "--arch", "x86_64")
        self.assertEqual(calls, ["mynewt__mynewt-77-c0123abcd", "mynewt__mynewt-1030", "mynewt__mynewt-1030-t5"])
        instance(self.instances, "contiki-ng__contiki-ng-1205", pipeline="contiki-ng", architecture="x86_64")
        instance(self.instances, "contiki-ng__contiki-ng-226", pipeline="contiki-ng", architecture="x86_64")
        calls, _, _ = self.work("contiki-ng", None, "--arch", "x86_64")
        self.assertEqual(calls, ["contiki-ng__contiki-ng-226", "contiki-ng__contiki-ng-1205"])

    def test_an_instance_of_the_other_architecture_is_left_and_said_to_be(self):
        instance(self.instances, "nuttx__nuttx-1-a1", runtime="apptainer", host_arch="aarch64")
        instance(self.instances, "nuttx__nuttx-2-a2", runtime="apptainer", host_arch="x86_64")
        instance(self.instances, "nuttx__nuttx-3", runtime=None)        # an old Docker instance
        calls, _, out = self.work("nuttx", None, "--arch", "x86_64")
        self.assertEqual(calls, ["nuttx__nuttx-2-a2"])
        self.assertIn("1 nuttx PRs need aarch64, not taken here", out)
        self.assertIn("1 instances cannot run on the cluster, not taken (runtime docker, not an Apptainer image): nuttx__nuttx-3", out)
        self.assertFalse(paths.grade_path("nuttx__nuttx-1-a1", MODEL).exists())     # still to do
        calls, _, _ = self.work("nuttx", None, "--arch", "aarch64")
        self.assertEqual(calls, ["nuttx__nuttx-1-a1"])

    def test_a_listed_instance_that_is_not_there_is_not_taken_silently(self):
        instance(self.instances, "nuttx__nuttx-2-a2", runtime="apptainer", host_arch="x86_64")
        listed = self.root / "trial.txt"
        listed.write_text("nuttx__nuttx-2-a2\nnuttx__nuttx-9-a9\n")
        calls, _, out = self.work("nuttx", None, "--arch", "x86_64", "--instances", str(listed))
        self.assertEqual(calls, ["nuttx__nuttx-2-a2"])
        self.assertIn("1 instances cannot run on the cluster, not taken (no metadata.json): nuttx__nuttx-9-a9", out)

    def test_zephyr_and_riot_are_worked_as_before(self):
        for iid in ("riot__riot-100", "riot__riot-20", "riot__riot-3000"):
            instance(self.instances, iid, pipeline="riot-layer")
        calls, ens, out = self.work("riot", None)
        self.assertEqual(calls, ["riot__riot-20", "riot__riot-100", "riot__riot-3000"])
        ens.assert_not_called()                                 # published: nothing is built here
        self.assertEqual(sorted(json.loads((self.eval / "qwen38-nvfp4" / "claims" / "riot.json").read_text()), key=int),
                         ["20", "100", "3000"])
        self.assertNotIn("not taken", out)

    def test_a_failed_build_is_an_infrastructure_error_retried_then_not_run(self):
        iid = "nuttx__nuttx-2-a2"
        instance(self.instances, iid, runtime="apptainer", host_arch="x86_64")
        calls, ens, out = self.work("nuttx", None, "--arch", "x86_64",
                                    ensure=lambda i: "image build failed (exit 1): no space left")
        self.assertEqual(calls, [])                             # the agent never ran: not the model's failure
        self.assertEqual(ens.call_count, 2)                     # built again once, as run.py retries once
        rec = json.loads(paths.run_record_path(iid, MODEL).read_text())
        self.assertEqual((rec["attempt"], rec["infra_error"]), (2, "image build failed (exit 1): no space left"))
        g = json.loads(paths.grade_path(iid, MODEL).read_text())
        self.assertEqual(g["grade"], "not_run")
        self.assertIn("infrastructure error in 2 attempts", g["error"])
        self.assertIn(f"{iid}: not_run", out)

    def test_a_build_failure_after_an_earlier_infrastructure_error_uses_up_the_attempts(self):
        iid = "nuttx__nuttx-2-a2"
        instance(self.instances, iid, runtime="apptainer", host_arch="x86_64")
        paths.write_json(paths.run_record_path(iid, MODEL), {"attempt": 1, "infra_error": "model server gone"})
        args = argparse.Namespace(step_limit=250, time_limit=120)
        g = eval_images.record_failure(iid, MODEL, args, "image build failed (exit 1)")
        self.assertEqual(g["grade"], "not_run")
        self.assertEqual(json.loads(paths.run_record_path(iid, MODEL).read_text())["attempt"], 2)


class Results(Scratch):
    def test_the_table_counts_every_kind_of_id_and_leaves_the_docker_ones_out(self):
        ids = ["nuttx__nuttx-1236-a293", "nuttx__nuttx-14152-a2725", "nuttx__nuttx-14152-a2704"]
        for iid in ids:
            instance(self.instances, iid, runtime="apptainer", host_arch="x86_64")
        instance(self.instances, "nuttx__nuttx-8832", runtime=None)
        self.assertEqual(eval_set.instances("nuttx"), sorted(ids))
        self.grade(ids[0], "pass")
        self.grade(ids[1], "fail")
        paths.write_json(paths.run_record_path(ids[1], MODEL), {"exit_status": "Submitted", "steps": 7,
                                                               "elapsed_seconds": 120})
        rows = results.runs_of(self.eval / "qwen38-nvfp4", "nuttx", eval_set.instances("nuttx"))
        self.assertEqual({r["instance_id"]: r["grade"] for r in rows},
                         {ids[0]: "pass", ids[1]: "fail", ids[2]: "to do"})
        s = results.summary(rows)
        self.assertEqual((s["instances"], s["graded"], s["pass"], s["fail"], s["to do"]), (3, 2, 1, 1, 1))
        # the folders are <eval>/<model>/nuttx/<kernel PR>-a<apps PR>
        self.assertTrue(paths.grade_path(ids[1], MODEL).parent.name == "14152-a2725")

    def test_zephyr_and_riot_tables_still_list_every_discovered_instance(self):
        for iid in ("riot__riot-100", "riot__riot-20"):
            instance(self.instances, iid, pipeline="riot-layer")
        self.assertEqual(eval_set.instances("riot"), paths.discover_instances("riot"))


class Arch(Scratch):
    def test_each_instance_goes_to_exactly_one_architecture_or_is_named_unrunnable(self):
        instance(self.instances, "nuttx__nuttx-1-a1", runtime="apptainer", host_arch="aarch64")
        instance(self.instances, "nuttx__nuttx-2-a2", runtime="apptainer", host_arch="x86_64")
        instance(self.instances, "nuttx__nuttx-3", runtime=None)
        ids = paths.discover_instances("nuttx")
        arm, x86 = eval_set.classify(ids, "nuttx", "aarch64"), eval_set.classify(ids, "nuttx", "x86_64")
        self.assertEqual((arm["mine"], arm["other"]), (["nuttx__nuttx-1-a1"], {"x86_64": ["nuttx__nuttx-2-a2"]}))
        self.assertEqual((x86["mine"], x86["other"]), (["nuttx__nuttx-2-a2"], {"aarch64": ["nuttx__nuttx-1-a1"]}))
        self.assertEqual(set(arm["unrunnable"]), {"nuttx__nuttx-3"})

    def test_what_is_left_is_counted_per_architecture(self):
        instance(self.instances, "nuttx__nuttx-1-a1", runtime="apptainer", host_arch="aarch64")
        instance(self.instances, "nuttx__nuttx-2-a2", runtime="apptainer", host_arch="x86_64")
        instance(self.instances, "nuttx__nuttx-4-a4", runtime="apptainer", host_arch="x86_64")
        self.grade("nuttx__nuttx-2-a2")
        left = eval_set.left("nuttx", MODEL, "x86_64")
        self.assertEqual((left["mine"], left["other"]), (["nuttx__nuttx-4-a4"], {"aarch64": ["nuttx__nuttx-1-a1"]}))
        self.assertEqual(eval_set.describe("nuttx", MODEL, "aarch64"),
                         "1 to grade on aarch64; 1 to grade on x86_64 (not taken by a job of this architecture)")
        r = subprocess.run([sys.executable, str(REPO / "harness" / "eval_set.py"), "left", "--project", "nuttx",
                            "--model", MODEL, "--arch", "x86_64"], capture_output=True, text=True,
                           env={"EMBEDEVAL_INSTANCES_DIR": str(self.instances), "EMBEDEVAL_EVAL_DIR": str(self.eval),
                                "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.stdout.strip(), "1", r.stderr)

    def test_an_arch_less_instance_is_taken_by_any_job(self):
        instance(self.instances, "mynewt__mynewt-5", runtime="apptainer")
        for arch in eval_set.ARCHS:
            self.assertEqual(eval_set.classify(paths.discover_instances("mynewt"), "mynewt", arch)["mine"],
                             ["mynewt__mynewt-5"])

    def test_the_committed_instances_are_all_covered_by_a_runner_of_their_architecture(self):
        """A project that gets an instance of an architecture no runner serves would
        leave it to do for ever: this fails first."""
        runners = {"mynewt": {"x86_64"}, "contiki-ng": {"x86_64"}, "nuttx": {"aarch64", "x86_64"}}
        with mock.patch.object(paths, "INSTANCE_ROOTS", [REPO / "apptainer" / "instances" / d
                                                         for d in ("mynewt", "nuttx", "contiki-ng")]
                               + [REPO / "docker" / "instances"]):
            for project, archs in runners.items():
                c = eval_set.classify(paths.discover_instances(project), project)
                everything = set(eval_set.instances(project))
                served = set()
                for arch in archs:
                    served |= set(eval_set.classify(sorted(everything), project, arch)["mine"])
                self.assertTrue(everything, project)
                self.assertEqual(served, everything, project)
                self.assertTrue(set(c["unrunnable"]) <= {i for i in paths.discover_instances(project)
                                                         if i not in everything})


class Images(Scratch):
    def setUp(self):
        super().setUp()
        self.work = self.root / "work"
        patcher = mock.patch.dict("os.environ", {"EMBEDEVAL_WORK": str(self.work),
                                                "EMBEDEVAL_SHARED": str(self.root / "shared")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.iid = "mynewt__mynewt-1030"
        instance(self.instances, self.iid, runtime="apptainer", host_arch="x86_64")
        import apptainer_util as au
        self.au = au
        self.image = au.instance_image(self.iid)

    def ensure(self, rc=0, published=None, builds=True):
        import registry

        def build(iid, minutes):
            if rc == 0 and builds:
                self.image.parent.mkdir(parents=True, exist_ok=True)
                self.image.write_text("sif")
            return rc
        with mock.patch.object(registry, "published", return_value=published), \
                mock.patch.object(registry, "pull") as pull, \
                mock.patch.object(eval_images, "base_image") as base, \
                mock.patch.object(eval_images, "_build", side_effect=build) as built, \
                mock.patch.object(self.au, "require_host_arch"), \
                contextlib.redirect_stdout(io.StringIO()):
            return eval_images.ensure(self.iid), built, base, pull

    def test_a_missing_image_is_built_once_after_its_base(self):
        error, built, base, pull = self.ensure()
        self.assertIsNone(error)
        base.assert_called_once()
        self.assertEqual(built.call_args[0][0], self.iid)
        pull.assert_not_called()
        self.assertTrue(self.image.exists())

    def test_an_image_already_there_is_not_built(self):
        self.image.write_text("sif")
        error, built, base, _ = self.ensure()
        self.assertIsNone(error)
        built.assert_not_called()
        base.assert_not_called()

    def test_a_published_image_is_pulled_not_built(self):
        error, built, _, pull = self.ensure(published={"ref": "oras://x", "sha256": "0"})
        self.assertIsNone(error)
        pull.assert_called_once_with(self.iid)
        built.assert_not_called()

    def test_a_failed_build_is_reported_with_its_exit_code(self):
        error, built, _, _ = self.ensure(rc=1)
        self.assertTrue(error.startswith("image build failed (exit 1)"), error)
        error, _, _, _ = self.ensure(rc=124)
        self.assertIn("exit 124", error)
        error, _, _, _ = self.ensure(rc=0, builds=False)         # exit 0 but no image
        self.assertTrue(error.startswith("image build failed"), error)

    def test_a_base_image_that_cannot_be_built_is_reported_not_raised(self):
        import registry
        with mock.patch.object(registry, "published", return_value=None), \
                mock.patch.object(eval_images, "base_image", side_effect=RuntimeError("could not build base")), \
                mock.patch.object(self.au, "require_host_arch"):
            self.assertEqual(eval_images.ensure(self.iid), "RuntimeError: could not build base")

    def test_cleanup_leaves_no_image_overlay_or_sandbox_behind(self):
        au = self.au
        self.image.write_text("sif")
        overlay = au.path("runs", f"agent-{self.iid}-20261113-010101", "x").parent
        (overlay / "overlay").mkdir()
        sandboxes = self.root / "sandboxes"
        (sandboxes / "embedeval-sbx-abc" / "root").mkdir(parents=True)
        (sandboxes / "embedeval-sbx-abc" / "root" / "file").write_text("x")
        with mock.patch.dict("os.environ", {"EMBEDEVAL_SANDBOX_DIR": str(sandboxes)}):
            eval_images.cleanup(self.iid)
        self.assertFalse(self.image.exists())
        self.assertFalse(overlay.exists())
        self.assertEqual(list(sandboxes.iterdir()), [])

    def test_a_build_over_its_cap_is_killed_whole(self):
        proc = mock.Mock(pid=123)
        proc.wait.side_effect = [subprocess.TimeoutExpired("b", 1), 0]
        with mock.patch.object(eval_images.subprocess, "Popen", return_value=proc), \
                mock.patch.object(eval_images.os, "killpg") as killpg:
            self.assertEqual(eval_images._build(self.iid, 0.01), 124)    # the cap: the group is killed
        self.assertEqual(killpg.call_args_list[0][0], (123, eval_images.signal.SIGTERM))


# --------------------------------------------------------------------------
# The runners and main's: same settings, same defaults.

# (new file, the main file it is a copy of)
PAIRS = [
    ("cluster/templates/eval_nuttx.sbatch", "cluster/templates/eval_zephyr.sbatch"),
    ("scripts/eval_nuttx_node.sh", "scripts/eval_zephyr_node.sh"),
    *[(f"cluster/templates/eval_{p}.sbatch", "cluster/templates/eval_riot.sbatch")
      for p in ("mynewt", "contiki_ng", "nuttx_x86")],
    *[(f"scripts/eval_{p}_{k}.sh", f"scripts/eval_riot_{k}.sh")
      for p in ("mynewt", "contiki_ng", "nuttx_x86") for k in ("server", "agents")],
]
# settings a project's runner adds to main's
EXTRA = {"EMBEDEVAL_NUTTX_CLONE", "EMBEDEVAL_NUTTX_APPS_CLONE", "EMBEDEVAL_MYNEWT_CLONE",
         "EMBEDEVAL_CONTIKI_NG_IMAGE", "EMBEDEVAL_WORK", "EMBEDEVAL_SANDBOX_DIR", "TMPDIR"}
DEFAULT = re.compile(r"\$\{(\w+):-([^}]*)\}")
PROJECT_NAMES = re.compile(r"zephyr|riot|nuttx_x86|nuttx|mynewt|contiki_ng|contiki-ng")


def defaults(text: str) -> dict:
    return {m[1]: PROJECT_NAMES.sub("P", m[2]) for m in DEFAULT.finditer(text)
            if m[1].startswith(("EVAL_", "EMBEDEVAL_"))}


def worker_defaults() -> dict:
    tree = ast.parse((REPO / "harness" / "eval_worker.py").read_text())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            name = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
            for kw in node.keywords:
                if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                    out[name] = kw.value.value
    return out


class RunnersFollowMain(unittest.TestCase):
    def read(self, rel):
        return (REPO / rel).read_text()

    def test_every_setting_has_main_s_default(self):
        for new, main in PAIRS:
            theirs, mine = defaults(self.read(main)), defaults(self.read(new))
            for name, value in theirs.items():
                if name == "EMBEDEVAL_WORK":      # each project's own folder, in the same place
                    self.assertRegex(mine[name], r"^/scratch/scholar/\$USER/embedeval/eval-work/[\w-]+\$I$", new)
                elif name in mine:
                    self.assertEqual(mine[name], value, f"{new}: {name}")
            for name in set(mine) - set(theirs):
                self.assertIn(name, EXTRA, f"{new} has a setting main's {main} lacks: {name}")

    def test_margin_time_limit_and_step_limit_are_main_s_everywhere(self):
        w = worker_defaults()
        self.assertEqual((w["--margin"], w["--time-limit"], w["--step-limit"]), (60, 120, 250))
        for new in [n for n, _ in PAIRS if "agents" in n or "node" in n]:
            text = self.read(new)
            self.assertIn('--margin "${EVAL_MARGIN:-60}" --time-limit "${EVAL_TIME_LIMIT:-120}"', text, new)
            self.assertNotIn("--step-limit", text, new)          # the worker's own default, 250
        for new in [n for n, _ in PAIRS if n.endswith(".sbatch")]:
            text = self.read(new)
            self.assertIn("EVAL_MAX_JOBS:-6", text, new)
            for setting in ("EVAL_MODEL", "EVAL_AGENTS", "EVAL_INSTANCES", "EVAL_MARGIN", "EVAL_TIME_LIMIT",
                            "EVAL_MAX_JOBS", "EVAL_KEEP_IMAGES", "EMBEDEVAL_EVAL_DIR"):
                self.assertIn(setting, text.split("set -u")[0], f"{new} does not document {setting}")

    def test_the_stop_resume_and_resubmit_lines_are_main_s(self):
        norm = lambda s: PROJECT_NAMES.sub("P", s)   # noqa: E731
        for new, main in PAIRS:
            if not new.endswith(".sbatch"):
                continue
            text = norm(self.read(new))
            text = text.replace(' -p "${SLURM_JOB_PARTITION_HET_GROUP_0:-$SLURM_JOB_PARTITION}"', "")   # kept Spark partition (main's Zephyr has a -p too)
            for line in norm(self.read(main)).splitlines():
                if line.strip().startswith('echo "[$(date +%T)] job $ATTEMPT for $MODEL:'):
                    continue   # a log line only; our runners name servers of several nodes
                if any(k in line for k in ("srun", "timeout -s TERM", "kill-on-bad-exit", "squeue", "scancel",
                                           "END=", "LIMIT=", "sbatch \\", "-N \"$", "attempt", "ATTEMPT",
                                           "--export=NONE,EVAL_MODEL")):
                    self.assertIn(line.strip(), text, f"{new} lacks {main}'s line: {line.strip()}")

    def test_the_architecture_each_runner_names_is_the_one_its_node_has(self):
        arch = {"cluster/templates/eval_nuttx.sbatch": "aarch64", "scripts/eval_nuttx_node.sh": "aarch64",
                **{f"{k}/eval_{p}{s}": "x86_64"
                   for p in ("mynewt", "contiki_ng", "nuttx_x86")
                   for k, s in (("cluster/templates", ".sbatch"), ("scripts", "_agents.sh"))}}
        for rel, want in arch.items():
            text = self.read(rel)
            found = set(re.findall(r"--arch (\w+)", text))
            if rel.endswith(".sbatch") or "node" in rel:
                self.assertEqual(found, {want}, rel)
            if "agents" in rel:
                self.assertIn('[ "$(uname -m)" = x86_64 ]', text, rel)
        node = self.read("scripts/eval_nuttx_node.sh")
        self.assertIn("--project nuttx --arch aarch64", node)

    def test_every_script_a_template_copies_exists_and_parses(self):
        bash = shutil.which("bash")
        for rel in sorted({n for n, _ in PAIRS}):
            self.assertTrue((REPO / rel).exists(), rel)
            if rel.endswith(".sbatch"):
                for script in re.findall(r"scripts/eval_\w+\.sh", self.read(rel)):
                    self.assertTrue((REPO / script).exists(), f"{rel} names {script}")
            if bash:
                r = subprocess.run([bash, "-n", str(REPO / rel)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{rel}: {r.stderr}")

    def test_the_worker_takes_the_project_and_architecture_its_script_names(self):
        for rel in [n for n, _ in PAIRS if "agents" in n or "node" in n]:
            m = re.search(r"eval_worker\.py --project ([\w-]+)(?: --arch (\w+))?", self.read(rel))
            self.assertIsNotNone(m, rel)
            self.assertIn(m[1], paths.PROJECTS)
            if m[1] in eval_set.BUILT_HERE:
                self.assertIn(m[2], eval_set.ARCHS, rel)


if __name__ == "__main__":
    unittest.main()
