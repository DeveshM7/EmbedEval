"""A Contiki-NG agent gets the repository at the base commit, the upstream
tests and the problem statement, and nothing that leads to the fix:
scripts/contiki_ng_leakcheck.py on a tiny synthetic upstream and image, one
case per finding type and per known false positive; the image's bench file;
the prompt.

    cd scripts/tests && python3 -m unittest test_contiki_leakage
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "scripts"), str(REPO / "harness")]

import contiki_ng_leakcheck as lc  # noqa: E402

GIT = ["git", "-c", "user.email=t@t", "-c", "init.defaultBranch=master", "-c", "protocol.file.allow=always"]


def git(repo, *args, author="Upstream"):
    return subprocess.run([*GIT, "-c", f"user.name={author}", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class Upstream:
    """A base commit, then a fix of os/lib/lib.c; the clone is bare, as the
    pipeline's is."""

    def __init__(self, root: Path):
        work = root / "upstream-work"
        work.mkdir()
        git(work, "init", "-q")
        (work / "os" / "lib").mkdir(parents=True)
        (work / "os" / "lib" / "lib.c").write_text("int lib(int x)\n{\n    return x;\n}\n")
        (work / "os" / "contiki.h").write_text("/* contiki */\nstatic const char lib_name[] = \"contiki lib\";\n")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "base")
        self.base = git(work, "rev-parse", "HEAD")
        (work / "os" / "lib" / "lib.c").write_text(
            "int lib(int x)\n{\n    if (x > LIB_LIMIT_FOR_THE_FIX) { return LIB_LIMIT_FOR_THE_FIX; }\n"
            "    return x;\n}\n")
        git(work, "commit", "-qam", "fix")
        self.fix = git(work, "rev-parse", "HEAD")
        self.patch = git(work, "diff", f"{self.base}..{self.fix}")
        self.clone = root / "contiki-ng.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(work), str(self.clone)], check=True)
        self.work = work
        self.meta = {"base_commit": self.base, "fix_commit": self.fix, "problem_statement": "lib() returns too much"}

    def image(self, root: Path) -> Path:
        """An unpacked image as the recipe makes it: the base fetched by URL
        at depth 1, the tests applied, FETCH_HEAD and reflog removed."""
        tb = root / "testbed"
        tb.mkdir(parents=True)
        git(tb, "init", "-q")
        subprocess.run(["git", "-C", str(tb), "fetch", "-q", "--depth", "1", f"file://{self.work}", self.base],
                       check=True, capture_output=True)
        git(tb, "checkout", "-q", "--detach", self.base)
        (tb / ".git" / "FETCH_HEAD").unlink()
        subprocess.run(["rm", "-rf", str(tb / ".git" / "logs")], check=True)
        (tb / "tests").mkdir()
        (tb / "tests" / "test.c").write_text("void test(void) { lib(1); }\n")
        return root


class LeakCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.up = Upstream(self.root)
        self.facts = lc.fix_facts(self.up.clone, self.up.meta, self.up.patch, "")
        self.img = self.up.image(self.root / "img")

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        return lc.scan(self.img, self.facts)

    def test_facts_name_the_fix(self):
        self.assertEqual(self.facts["fix_commit"], self.up.fix)
        self.assertEqual(list(self.facts["fix_blobs"].values()), ["os/lib/lib.c"])
        self.assertTrue(any("LIB_LIMIT_FOR_THE_FIX" in l for l in self.facts["fix_lines"]))

    def test_the_recipes_image_is_clean(self):
        found = self.scan()
        self.assertTrue(found["ok"], found["findings"])
        self.assertEqual(found["commits"], 1)

    def test_the_fix_commit_and_its_blob_are_found(self):
        subprocess.run(["git", "-C", str(self.img / "testbed"), "fetch", "-q", "--depth", "1",
                        f"file://{self.up.work}", self.up.fix], check=True, capture_output=True)
        findings = self.scan()["findings"]
        self.assertTrue(any(f.startswith("fix: the fix commit") for f in findings), findings)
        self.assertTrue(any(f.startswith("fix: fixed versions of ['os/lib/lib.c']") for f in findings), findings)
        self.assertTrue(any(f.startswith("history: commits other") for f in findings), findings)
        self.assertIn("refs: /testbed has FETCH_HEAD", findings)

    def test_full_history_is_found(self):
        (self.img / "testbed" / ".git" / "shallow").unlink()
        self.assertIn("history: the base commit is not a shallow boundary (its history is present)",
                      self.scan()["findings"])

    def test_remotes_tags_and_reflogs_are_found(self):
        tb = self.img / "testbed"
        git(tb, "remote", "add", "origin", "https://github.com/contiki-ng/contiki-ng.git")
        git(tb, "tag", "v1")
        (tb / ".git" / "logs").mkdir()
        (tb / ".git" / "logs" / "HEAD").write_text("x\n")
        findings = self.scan()["findings"]
        self.assertIn("refs: /testbed has remotes ['origin']", findings)
        self.assertIn("refs: /testbed has refs ['refs/tags/v1']", findings)
        self.assertIn("refs: /testbed has a reflog", findings)

    def test_a_submodule_with_its_branch_is_found_and_its_files_are_a_dependency(self):
        dep = self.root / "dep"
        dep.mkdir()
        git(dep, "init", "-q")
        line = next(l for l in self.facts["fix_lines"] if "LIB_LIMIT" in l)
        (dep / "copy.c").write_text(line + "\n")
        git(dep, "add", "-A")
        git(dep, "commit", "-qm", "dep")
        tb = self.img / "testbed"
        git(tb, "submodule", "add", "-q", f"file://{dep}", "tools/cooja")
        found = self.scan()
        self.assertIn("/testbed/.git/modules/tools/cooja", found["submodule_repositories"])
        self.assertTrue(any(f.startswith("refs: /testbed/.git/modules/tools/cooja has") for f in found["findings"]),
                        found["findings"])
        self.assertEqual(found["fix_lines_in_pinned_submodules"], ["/testbed/tools/cooja/copy.c"])
        self.assertFalse(any(f.startswith("lines:") for f in found["findings"]))

    def test_a_fix_line_anywhere_else_is_found(self):
        line = next(l for l in self.facts["fix_lines"] if "LIB_LIMIT" in l)
        (self.img / "opt").mkdir()
        (self.img / "opt" / "notes.txt").write_text(line + "\n")
        self.assertIn("lines: files hold lines the fix adds: ['/opt/notes.txt']", self.scan()["findings"])

    def test_other_repositories_and_contiki_trees_are_found(self):
        (self.img / "home" / "user" / "renode" / ".git").mkdir(parents=True)
        (self.img / "srv" / "contiki-ng.git").mkdir(parents=True)
        (self.img / "opt" / "contiki-ng" / "os").mkdir(parents=True)
        (self.img / "opt" / "contiki-ng" / "os" / "contiki.h").write_text("/* later */\n")
        findings = " ".join(self.scan()["findings"])
        for p in ("/home/user/renode/.git", "/srv/contiki-ng.git", "/opt/contiki-ng (Contiki tree)"):
            self.assertIn(p, findings)

    def test_statement_naming_a_fix_only_identifier_is_found(self):
        meta = {**self.up.meta, "problem_statement": "lib() ignores LIB_LIMIT_FOR_THE_FIX"}
        facts = lc.fix_facts(self.up.clone, meta, self.up.patch, "")
        self.assertEqual(facts["statement_fix_identifiers"], ["LIB_LIMIT_FOR_THE_FIX"])
        self.assertIn("statement: names what only the fix introduces: ['LIB_LIMIT_FOR_THE_FIX']",
                      lc.scan(self.img, facts)["findings"])

    # known false positives

    def test_fix_line_inside_a_line_the_tests_add_is_not_a_clue(self):
        line = next(l for l in self.facts["fix_lines"] if "LIB_LIMIT" in l)
        tests = f"+++ b/tests/test.c\n+    /* expect: */ {line} /* as before */\n"
        facts = lc.fix_facts(self.up.clone, self.up.meta, self.up.patch, tests)
        self.assertNotIn(line, facts["fix_lines"])

    def test_statement_may_name_what_the_tests_use_or_the_base_has(self):
        meta = {**self.up.meta, "problem_statement": "LIB_LIMIT_FOR_THE_FIX and lib_contiki_h"}
        tests = "+++ b/tests/test.c\n+  check(LIB_LIMIT_FOR_THE_FIX);\n"
        self.assertEqual(lc.fix_facts(self.up.clone, meta, self.up.patch, tests)["statement_fix_identifiers"], [])

    def test_fix_line_already_in_the_base_tree_is_not_a_clue(self):
        patch = self.up.patch + ("diff --git a/os/x.c b/os/x.c\n--- a/os/x.c\n+++ b/os/x.c\n"
                                 "@@ -0,0 +1 @@\n+static const char lib_name[] = \"contiki lib\";\n")
        facts = lc.fix_facts(self.up.clone, self.up.meta, patch, "")
        self.assertFalse(any("lib_name" in l for l in facts["fix_lines"]))
        self.assertTrue(any("LIB_LIMIT" in l for l in facts["fix_lines"]))


class BenchFileTest(unittest.TestCase):
    def test_the_image_gets_no_oracle(self):
        import contiki_ng_pipeline as pipeline
        cfg = {"units": [{"id": "tests/u/t", "kind": "native", "directory": "tests/u", "binary": "t",
                          "make_options": [], "cases": [{"id": "c", "description": "C", "extra": 1}],
                          "sources": ["tests/u/t.c"], "profile_evidence": {}, "before_compile": {
                              "required_interfaces": ["fix_api"]}, "submodules": []}],
               "fail_to_pass": ["tests/u/t::c"], "pass_to_pass": [], "after_required": ["tests/u/t::c"],
               "oracle": "compile", "compile_symbols": ["fix_api"], "oracle_attribution": "the fix adds fix_api",
               "compile_expectations": {"tests/u/t": {"symbols": ["fix_api"], "headers": []}},
               "submodules": ["tools/cooja"], "ci_image": "x"}
        bench = pipeline.bench(cfg)
        self.assertNotIn("fix_api", json.dumps(bench))
        self.assertEqual(bench, {"units": [{"id": "tests/u/t", "kind": "native", "directory": "tests/u",
                                            "binary": "t", "make_options": [], "submodules": [],
                                            "cases": [{"id": "c", "description": "C"}]}],
                                 "submodules": ["tools/cooja"]})


class PromptTest(unittest.TestCase):
    def test_the_task_names_no_fix_file_and_no_test_list(self):
        import run
        meta = {"instance_id": "contiki-ng__contiki-ng-1", "project": "contiki-ng", "pipeline": "contiki-ng",
                "problem_statement": "Parsing a truncated value reads past the input.",
                "fail_to_pass": ["tests/u/t::secret_case"], "pass_to_pass": ["tests/u/t::other_case"],
                "files_changed_by_fix": ["os/lib/json.c"]}
        task = run.build_task(meta)
        system, instance = run.build_prompts(meta)
        for text in (task, system, instance):
            for leak in ("os/lib/json.c", "secret_case", "other_case", "Relevant Source"):
                self.assertNotIn(leak, text)
        self.assertIn("Parsing a truncated value reads past the input.", task)


if __name__ == "__main__":
    unittest.main()
