"""Local checks that a Mynewt agent gets the repository at the base commit and
the problem statement, and nothing that leads to the fix.

    python3 -m unittest discover -s scripts/tests -p 'test_mynewt_*.py'
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "scripts"), str(REPO / "harness")]

import mynewt_leakcheck as lc  # noqa: E402
import mynewt_network  # noqa: E402

GIT = ["git", "-c", "user.email=t@t", "-c", "init.defaultBranch=master"]


def git(repo, *args, author="Upstream"):
    return subprocess.run([*GIT, "-c", f"user.name={author}", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class Upstream:
    """A tiny upstream: a base commit, then a fix of src/lib.c."""

    def __init__(self, root: Path):
        self.clone = root / "upstream"
        self.clone.mkdir()
        git(self.clone, "init", "-q")
        (self.clone / "src").mkdir()
        (self.clone / "src" / "lib.c").write_text("int lib(int x)\n{\n    return x;\n}\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-qm", "base")
        self.base = git(self.clone, "rev-parse", "HEAD")
        (self.clone / "src" / "lib.c").write_text(
            "int lib(int x)\n{\n    if (x > LIB_LIMIT_FOR_THE_FIX) { return LIB_LIMIT_FOR_THE_FIX; }\n"
            "    return x;\n}\n")
        git(self.clone, "commit", "-qam", "fix")
        self.fix = git(self.clone, "rev-parse", "HEAD")
        self.patch = git(self.clone, "diff", f"{self.base}..{self.fix}")
        self.meta = {"base_commit": self.base, "fix_commit": self.fix}

    def image(self, root: Path) -> Path:
        """An unpacked image as the recipe makes it: the base, depth 1, plus
        the benchmark commit."""
        tb = root / "testbed"
        tb.mkdir(parents=True)
        git(tb, "init", "-q")
        subprocess.run(["git", "-C", str(tb), "fetch", "-q", "--depth", "1", str(self.clone), self.base],
                       check=True)
        git(tb, "checkout", "-q", "--detach", "FETCH_HEAD")
        (tb / "test.c").write_text("void test(void) { lib(1); }\n")
        git(tb, "add", "-A")
        git(tb, "commit", "-qm", "Benchmark starting snapshot", author=lc.RECIPE_AUTHOR)
        (tb / ".git" / "FETCH_HEAD").unlink()
        (root / "project" / "repos").mkdir(parents=True)
        return root


class LeakCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.up = Upstream(self.root)
        self.facts = lc.fix_facts(self.up.clone, self.up.meta, self.up.patch, "")

    def tearDown(self):
        self.tmp.cleanup()

    def test_facts_name_the_fix(self):
        self.assertEqual(self.facts["fix_commit"], self.up.fix)
        self.assertEqual(list(self.facts["fix_blobs"].values()), ["src/lib.c"])
        self.assertTrue(any("LIB_LIMIT_FOR_THE_FIX" in l for l in self.facts["fix_lines"]))

    def test_the_recipes_image_is_clean(self):
        found = lc.scan(self.up.image(self.root / "img"), self.facts)
        self.assertTrue(found["ok"], found["findings"])
        self.assertEqual(found["commits"], 2)

    def test_the_fix_commit_is_found(self):
        img = self.up.image(self.root / "img")
        subprocess.run(["git", "-C", str(img / "testbed"), "fetch", "-q", "--depth", "1",
                        str(self.up.clone), self.up.fix], check=True)
        found = lc.scan(img, self.facts)
        self.assertFalse(found["ok"])
        self.assertTrue(any(f.startswith("fix: the fix commit") for f in found["findings"]))
        self.assertTrue(any(f.startswith("history:") for f in found["findings"]))

    def test_full_history_is_found(self):
        img = self.up.image(self.root / "img")
        (img / "testbed" / ".git" / "shallow").unlink()
        self.assertIn("history: the base commit is not a shallow boundary (its history is present)",
                      lc.scan(img, self.facts)["findings"])

    def test_a_fix_line_anywhere_is_found_but_not_in_a_pinned_dependency(self):
        img = self.up.image(self.root / "img")
        line = next(l for l in self.facts["fix_lines"] if "LIB_LIMIT" in l)
        (img / "project" / "repos" / "dep").mkdir()
        (img / "project" / "repos" / "dep" / "copy.c").write_text(line + "\n")
        found = lc.scan(img, self.facts)
        self.assertTrue(found["ok"], found["findings"])
        self.assertEqual(found["fix_lines_in_pinned_dependencies"], ["/project/repos/dep/copy.c"])
        (img / "opt").mkdir()
        (img / "opt" / "notes.txt").write_text(line + "\n")
        self.assertIn("lines: files hold lines the fix adds: ['/opt/notes.txt']",
                      lc.scan(img, self.facts)["findings"])

    def test_newt_sources_and_other_repositories_are_found(self):
        img = self.up.image(self.root / "img")
        (img / "opt" / "newt-src-1.15.0").mkdir(parents=True)
        (img / "srv" / "core.git").mkdir(parents=True)
        (img / "project" / "repos" / "mbedtls" / ".git").mkdir(parents=True)     # a pinned dependency
        found = lc.scan(img, self.facts)
        self.assertIn("copies: other repositories or newt sources: ['/opt/newt-src-1.15.0', '/srv/core.git']",
                      found["findings"])

    def test_the_empty_build_mount_point_is_allowed(self):
        img = self.up.image(self.root / "img")
        (img / "embedeval-src" / "mynewt-core.git").mkdir(parents=True)
        self.assertTrue(lc.scan(img, self.facts)["ok"])
        (img / "embedeval-src" / "mynewt-core.git" / "objects").mkdir()
        self.assertFalse(lc.scan(img, self.facts)["ok"])

    def test_a_statement_naming_the_fixs_new_name_is_a_leak(self):
        meta = {**self.up.meta, "problem_statement": "lib must clamp to LIB_LIMIT_FOR_THE_FIX."}
        facts = lc.fix_facts(self.up.clone, meta, self.up.patch, "")
        self.assertEqual(facts["statement_fix_identifiers"], ["LIB_LIMIT_FOR_THE_FIX"])
        found = lc.scan(self.up.image(self.root / "img"), facts)
        self.assertIn("statement: names what only the fix introduces: ['LIB_LIMIT_FOR_THE_FIX']",
                      found["findings"])
        # the tests use the name: then it is the interface asked for, not a clue
        facts = lc.fix_facts(self.up.clone, meta, self.up.patch, "+  x = LIB_LIMIT_FOR_THE_FIX;\n")
        self.assertEqual(facts["statement_fix_identifiers"], [])

    def test_lines_already_at_the_base_or_in_the_tests_are_no_clue(self):
        facts = lc.fix_facts(self.up.clone, self.up.meta, self.up.patch,
                             "+    if (x > LIB_LIMIT_FOR_THE_FIX) { return LIB_LIMIT_FOR_THE_FIX; }\n")
        self.assertEqual(facts["fix_lines"], [])
        # part of a longer line the tests add (808: a continuation line of a call)
        facts = lc.fix_facts(self.up.clone, self.up.meta, self.up.patch,
                             "+  y = 1; if (x > LIB_LIMIT_FOR_THE_FIX) { return LIB_LIMIT_FOR_THE_FIX; } z();\n")
        self.assertEqual(facts["fix_lines"], [])


class GoldPatchTest(unittest.TestCase):
    def test_a_crlf_file_keeps_its_line_endings(self):
        import validate_instance as vi
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            git(repo, "init", "-q")
            (repo / "a.c").write_bytes(b"int a;\r\nint b;\r\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "a.c").write_bytes(b"int a;\r\nint c;\r\n")
            git(repo, "commit", "-qam", "fix")
            meta = {"project": "mynewt", "repo": "x", "base_commit": base,
                    "fix_commit": git(repo, "rev-parse", "HEAD"), "files_changed_by_fix": ["a.c"]}
            with patch.object(vi.au, "mynewt_clone", return_value=repo):
                self.assertIn("+int c;\r\n", vi.gold_patch(meta, Path(tmp)))

    def test_the_validator_defines_its_mynewt_step_once(self):
        text = (REPO / "scripts" / "validate_instance.py").read_text()
        self.assertEqual(text.count("def validate_mynewt_apptainer("), 1)


class NetworkTest(unittest.TestCase):
    def test_every_command_gets_its_own_network_namespace(self):
        cmd = mynewt_network.isolated_command(["apptainer", "exec", "img", "run_tests"], None)
        self.assertEqual(cmd[:4], ["unshare", "--user", "--map-root-user", "--net"])
        self.assertIn("ip link set lo up", cmd[-1])
        self.assertNotIn("veth", cmd[-1])
        self.assertTrue(cmd[-1].endswith("exec apptainer exec img run_tests"))

    def test_the_fixture_adds_a_private_veth_pair(self):
        self.assertIn("type veth", mynewt_network.isolated_command(["x"], "mynewt-veth")[-1])
        with self.assertRaises(ValueError):
            mynewt_network.isolated_command(["x"], "host")

    @unittest.skipUnless(importlib.util.find_spec("minisweagent"), "needs mini-swe-agent")
    def test_the_agent_environment_is_isolated(self):
        import apptainer_env
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "i.sif"
            img.write_text("")
            with patch.dict("os.environ", {"EMBEDEVAL_WORK": tmp}):
                env = apptainer_env.ApptainerEnvironment(image=img, name="t", isolate_network=True)
                with patch.object(apptainer_env.subprocess, "run") as run:
                    run.return_value.stdout, run.return_value.returncode = "", 0
                    env.execute({"command": "git fetch origin"})
                    self.assertEqual(run.call_args.args[0][:4], ["unshare", "--user", "--map-root-user", "--net"])
                env.cleanup()


class PromptTest(unittest.TestCase):
    META = {"project": "mynewt", "instance_id": "mynewt__mynewt-1", "problem_statement": "P",
            "fail_to_pass": ["s/a"], "pass_to_pass": ["s/b"],
            "files_changed_by_fix": ["kernel/os/src/secret_fix_file.c"],
            "protected_paths": ["kernel/os/selftest/"],
            "extra_warnings": "Do not modify get_x in kernel/os/src/os.c; it is protected test support."}

    def test_no_oracle_files_in_the_task(self):
        import run
        task = run.build_task(self.META)
        self.assertNotIn("secret_fix_file", task)
        self.assertNotIn("Relevant Source File", task)
        self.assertIn("s/a", task)

    def test_the_instances_own_warnings_reach_the_agent(self):
        import run
        _, instance = run.build_prompts(self.META)
        self.assertIn("protected test support", instance)


class RecipeTest(unittest.TestCase):
    def test_the_instance_recipe_drops_post_base_patches(self):
        text = (REPO / "templates" / "mynewt" / "apptainer" / "instance.def.tmpl").read_text()
        cleanup = text.index("rm -f .git/FETCH_HEAD /opt/benchmark/sim_arm64.patch /opt/benchmark/simulator_compat.diff")
        self.assertGreater(cleanup, text.index("@@ARM_STEP@@"))
        self.assertGreater(cleanup, text.index("@@SIMULATOR_STEP@@"))
        self.assertIn("--depth 1", text)

    def test_the_base_image_keeps_no_newt_sources(self):
        text = (REPO / "templates" / "mynewt" / "apptainer" / "base.def").read_text()
        self.assertIn("rm -rf /opt/newt-src*", text)
        self.assertGreater(text.index("rm -rf /opt/newt-src*"), text.rindex("go build"))


class PromoteTest(unittest.TestCase):
    def promote(self, report: dict, mode: str = "runtime"):
        import apptainer_util as au
        import promote_instance
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "mynewt__mynewt-1"
            src.mkdir()
            for f in au.MYNEWT_RECIPE_FILES:
                (src / f).write_text(f)
            (src / "metadata.json").write_text(json.dumps({"failure_mode": mode, "problem_statement": "P"}))
            report = {"valid": True, "recipe_sha256": au.recipe_fingerprint(src, au.MYNEWT_RECIPE_FILES), **report}
            (src / "validation.json").write_text(json.dumps(report))
            with patch.object(promote_instance, "DEST", Path(tmp) / "dest"):
                return promote_instance.promote("mynewt__mynewt-1", Path(tmp))

    def test_promotion_needs_a_clean_leak_check(self):
        clean = {"ok": True, "findings": [], "network": "isolated"}
        det = {"runs": 5, "ok": True}
        self.assertEqual(self.promote({})[1], "no leak check record; validate it again")
        self.assertFalse(self.promote({"leak_check": {**clean, "ok": False}, "determinism": det})[0])
        self.assertFalse(self.promote({"leak_check": {**clean, "network": "reachable"}, "determinism": det})[0])
        self.assertTrue(self.promote({"leak_check": clean, "determinism": det})[0])

    def test_every_instance_needs_a_deterministic_before_state(self):
        clean = {"ok": True, "findings": [], "network": "isolated"}
        self.assertFalse(self.promote({"leak_check": clean})[0])
        self.assertFalse(self.promote({"leak_check": clean, "determinism": {"runs": 3, "ok": True}})[0])
        self.assertFalse(self.promote({"leak_check": clean}, mode="compile")[0])
        self.assertTrue(self.promote({"leak_check": clean, "determinism": {"runs": 5, "ok": True}},
                                     mode="compile")[0])


if __name__ == "__main__":
    unittest.main()
