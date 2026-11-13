"""Local checks of the NuttX phase jobs: prepare --instances, phase
submission against a fake scheduler, and per-architecture work.

    python3 -m unittest cluster/test_nuttx_cluster.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLUSTER = Path(__file__).resolve().parent
REPO = CLUSTER.parent
sys.path[:0] = [str(CLUSTER), str(REPO / "scripts")]

import node_worker  # noqa: E402
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

    def test_arch_needed(self):
        r = self.s.run("submit.py", "validate", "--run", self.run.name, check=False)
        self.assertIn("needs --arch", r.stderr)


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


if __name__ == "__main__":
    unittest.main()
