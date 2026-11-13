"""Statement verification (scripts/verify_statements.py) for the ports: the fix of a
Mynewt, NuttX and Contiki-NG instance from a clone, where the instances and clones are
looked for, and `prepare --project` end to end on temporary repositories. Zephyr and RIOT
keep what they had.

    python3 -m unittest discover -s scripts/tests -p 'test_verify_ports.py'
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "scripts")]

import verify_ports  # noqa: E402
import verify_statements as vs  # noqa: E402


def git(repo, *args):
    r = subprocess.run(["git", "-c", "user.name=U", "-c", "user.email=u@u", "-c", "commit.gpgsign=false",
                        "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


def commit(repo, files, message="c"):
    for name, text in files.items():
        p = Path(repo) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.clone = self.tmp / "clone"
        self.clone.mkdir()
        git(self.clone, "init", "-q")
        self.shared = self.tmp / "shared"
        self.shared.mkdir()
        # a clean environment: no clone variable of the host decides these tests
        env = {k: v for k, v in os.environ.items() if not k.startswith("EMBEDEVAL_")}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)


class FixDiffTest(Env):
    def test_mynewt_uses_the_gold_base_and_only_the_fix_files(self):
        a = commit(self.clone, {"kernel/os/src/x.c": "int x;\n", "kernel/os/selftest/t.c": "t1\n"})
        b = commit(self.clone, {"kernel/os/selftest/t.c": "t2\n"})          # the tests, the gold base
        c = commit(self.clone, {"kernel/os/src/x.c": "int x = 1;\n", "kernel/os/selftest/t.c": "t3\n"})
        os.environ["EMBEDEVAL_MYNEWT_CLONE"] = str(self.clone)
        meta = {"project": "mynewt", "base_commit": a, "gold_base_commit": b, "fix_commit": c,
                "files_changed_by_fix": ["kernel/os/src/x.c"]}
        d = verify_ports.fix_diff(meta, self.shared)
        self.assertIn("+int x = 1;", d)
        self.assertNotIn("selftest", d)
        meta.pop("gold_base_commit")
        self.assertIn("+int x = 1;", verify_ports.fix_diff(meta, self.shared))

    def test_contiki_ng_from_the_shared_folder(self):
        a = commit(self.clone, {"os/a.c": "a\n", "tests/t.c": "t\n"})
        b = commit(self.clone, {"os/a.c": "b\n", "tests/t.c": "u\n"})
        os.rename(self.clone, self.shared / "contiki-ng.git")
        meta = {"project": "contiki-ng", "base_commit": a, "fix_commit": b,
                "files_changed_by_fix": ["os/a.c"]}
        d = verify_ports.fix_diff(meta, self.shared)
        self.assertIn("+b", d)
        self.assertNotIn("tests/t.c", d)

    def test_nuttx_adds_the_follow_up_prs_in_merge_order(self):
        a = commit(self.clone, {"sched/a.c": "0\n", "sched/b.c": "0\n"})
        b = commit(self.clone, {"sched/a.c": "1\n", "sched/other.c": "x\n"})      # the primary PR
        c = commit(self.clone, {"sched/b.c": "2\n"})                              # a follow-up
        os.environ["EMBEDEVAL_NUTTX_CLONE"] = str(self.clone)
        meta = {"project": "nuttx", "kernel_base_commit": a, "kernel_merge_commit": b,
                "files_changed_by_fix": ["sched/a.c"], "kernel_merged_at": "2023-02-01T00:00:00Z",
                "extra_fix_prs": [{"kernel_pr": 7, "merged_at": "2023-01-01T00:00:00Z",
                                   "base_commit": b, "head_commit": c, "files_changed": ["sched/b.c"]}]}
        d = verify_ports.fix_diff(meta, self.shared)
        self.assertIn("# follow-up PR #7", d)
        self.assertLess(d.index("sched/b.c"), d.index("sched/a.c"))              # merged first
        self.assertNotIn("sched/other.c", d)
        del meta["extra_fix_prs"], meta["kernel_merged_at"]
        d = verify_ports.fix_diff(meta, self.shared)
        self.assertIn("sched/a.c", d)
        self.assertNotIn("follow-up", d)

    def test_a_cut_file_list_takes_the_whole_diff(self):
        a = commit(self.clone, {"a.c": "0\n", "b.c": "0\n"})
        b = commit(self.clone, {"a.c": "1\n", "b.c": "1\n"})
        os.environ["EMBEDEVAL_CONTIKI_NG_CLONE"] = str(self.clone)
        meta = {"project": "contiki-ng", "base_commit": a, "fix_commit": b,
                "files_changed_by_fix": [f"f{i}" for i in range(verify_ports.FILES_CAP)]}
        d = verify_ports.fix_diff(meta, self.shared)
        self.assertIn("a.c", d)
        self.assertIn("b.c", d)

    def test_a_missing_commit_or_clone_is_an_error_that_says_what_to_do(self):
        commit(self.clone, {"a.c": "0\n"})
        os.environ["EMBEDEVAL_MYNEWT_CLONE"] = str(self.clone)
        meta = {"project": "mynewt", "base_commit": "1" * 40, "fix_commit": "2" * 40,
                "files_changed_by_fix": ["a.c"]}
        with self.assertRaisesRegex(RuntimeError, "not in .*fetch origin"):
            verify_ports.fix_diff(meta, self.shared)
        with self.assertRaisesRegex(RuntimeError, "EMBEDEVAL_NUTTX_CLONE"):
            verify_ports.fix_diff({"project": "nuttx"}, self.shared)
        with self.assertRaisesRegex(RuntimeError, "EMBEDEVAL_CONTIKI_NG_CLONE"):
            verify_ports.fix_diff({"project": "contiki-ng"}, self.shared)

    def test_nuttx_data_folder_names_the_kernel_clone(self):
        (self.tmp / "data").mkdir()
        os.rename(self.clone, self.tmp / "data" / "kernel.git")
        os.environ["EMBEDEVAL_NUTTX_DATA"] = str(self.tmp / "data")
        self.assertEqual(verify_ports.clone_for("nuttx", self.shared), self.tmp / "data" / "kernel.git")


class InstanceLayoutTest(unittest.TestCase):
    def test_every_port_instance_has_what_prepare_reads(self):
        for project, root in verify_ports.instance_roots().items():
            dirs = [d for d in root.iterdir() if (d / "metadata.json").exists()]
            self.assertTrue(dirs, project)
            for d in dirs:
                meta = json.loads((d / "metadata.json").read_text())
                self.assertEqual(meta["project"], project)
                self.assertEqual(d.name.split("__")[0], project)
                self.assertTrue((d / "test_patch.diff").exists(), d.name)
                self.assertTrue(meta["problem_statement"], d.name)
                self.assertTrue(meta["files_changed_by_fix"], d.name)
                if project == "nuttx":
                    keys = ["kernel_base_commit", "kernel_merge_commit"]
                    for e in meta.get("extra_fix_prs") or []:
                        keys_e = {"base_commit", "head_commit", "files_changed", "kernel_pr"}
                        self.assertTrue(keys_e <= set(e), d.name)
                else:
                    keys = ["base_commit", "fix_commit"]
                self.assertTrue(all(meta.get(k) for k in keys), d.name)

    def test_the_ports_are_not_in_the_default_selection(self):
        self.assertEqual(set(vs.CLONES), {"zephyr", "riot"})
        self.assertFalse(set(verify_ports.PORTS) & set(vs.CLONES))


class PrepareTest(Env):
    def setUp(self):
        super().setUp()
        self.inst = self.tmp / "instances"
        self.work = self.tmp / "work"
        a = commit(self.clone, {"os/a.c": "int a;\n"})
        b = commit(self.clone, {"os/a.c": "int a;\nint fresh_name(void);\n"})
        os.environ["EMBEDEVAL_CONTIKI_NG_CLONE"] = str(self.clone)
        for project, iid in (("contiki-ng", "contiki-ng__contiki-ng-1"), ("zephyr", "zephyr__zephyr-2")):
            d = (self.inst / "contiki-ng" if project == "contiki-ng" else self.inst) / iid
            d.mkdir(parents=True)
            (d / "metadata.json").write_text(json.dumps(
                {"project": project, "problem_statement": "Fix it.", "base_commit": a, "fix_commit": b,
                 "files_changed_by_fix": ["os/a.c"]}, indent=2) + "\n")
            (d / "test_patch.diff").write_text(
                "diff --git a/tests/t.c b/tests/t.c\n--- a/tests/t.c\n+++ b/tests/t.c\n"
                "@@ -0,0 +1 @@\n+fresh_name();\n")
        for name, value in (("INSTANCES", self.inst), ("WORK", self.work)):
            p = mock.patch.object(vs, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(verify_ports, "INSTANCES", self.inst)
        p.start()
        self.addCleanup(p.stop)

    def test_prepare_a_port_names_the_missing_name_and_apply_writes_the_port_file(self):
        vs.prepare(None, ("contiki-ng",))
        self.assertEqual((self.work / "tasks.txt").read_text(), "contiki-ng__contiki-ng-1\n")
        task = json.loads((self.work / "tasks" / "contiki-ng__contiki-ng-1.json").read_text())
        self.assertEqual(task["project"], "contiki-ng")
        self.assertEqual([m["name"] for m in task["missing_names"]], ["fresh_name"])
        self.assertIn("+int fresh_name(void);", task["fix"])
        self.assertEqual(vs.r_orig("contiki-ng__contiki-ng-1"), "Fix it.")
        self.assertEqual(vs.instance_dir("contiki-ng__contiki-ng-1"),
                         self.inst / "contiki-ng" / "contiki-ng__contiki-ng-1")

    def test_the_default_selection_is_zephyr_and_riot_only(self):
        with mock.patch.object(vs, "fix_diff", return_value=""):
            vs.prepare(None)
        self.assertEqual((self.work / "tasks.txt").read_text(), "zephyr__zephyr-2\n")

    def test_the_report_has_a_row_per_project(self):
        final = {"contiki-ng__contiki-ng-1": {
            "instance_id": "contiki-ng__contiki-ng-1", "project": "contiki-ng", "votes": ["fine"] * 3,
            "missing_names": [], "verdict": "fine", "status": "fine", "reason": "r", "problems": [],
            "new_statement": None, "leak_check": None}}
        self.work.mkdir()
        vs.report(final)
        self.assertIn("| contiki-ng | 1 | 0 | 0 | 0 |", (self.work / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
