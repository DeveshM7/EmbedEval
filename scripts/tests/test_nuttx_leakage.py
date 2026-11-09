"""Local checks that a NuttX agent gets the kernel and apps at their base
commits and the problem statement, and nothing that leads to the fix.

    python3 -m unittest discover -s scripts/tests -p 'test_nuttx_*.py'
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "scripts"), str(REPO / "harness")]

import apptainer_util as au  # noqa: E402
import nuttx_leakcheck as lc  # noqa: E402
import nuttx_statement as ns  # noqa: E402

GIT = ["git", "-c", "user.email=t@t", "-c", "init.defaultBranch=master", "-c", "gc.auto=0"]
TEMPLATE = (REPO / "templates" / "nuttx" / "apptainer" / "instance.def.tmpl").read_text()


def git(repo, *args, author="Upstream"):
    return subprocess.run([*GIT, "-c", f"user.name={author}", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def recipe_functions() -> str:
    """The shallow() and forget() shell functions of the instance recipe."""
    post = TEMPLATE.split("%post", 1)[1]
    retry = re.search(r"^    retry\(\).*$", post, re.M)[0]
    body = re.search(r"^    shallow\(\).*?^    }\n    forget\(\).*?^    }\n", post, re.M | re.S)[0]
    return retry + "\n" + body


class Upstream:
    """A tiny kernel (base, then a fix of sched/lib.c) and apps (base, then
    the test PR)."""

    def __init__(self, root: Path):
        self.root = root
        self.kernel = root / "kernel"
        self.kernel.mkdir()
        git(self.kernel, "init", "-q")
        (self.kernel / "sched").mkdir()
        (self.kernel / "sched" / "lib.c").write_text("int lib(int x)\n{\n    return x;\n}\n")
        git(self.kernel, "add", "-A")
        git(self.kernel, "commit", "-qm", "base")
        self.base = git(self.kernel, "rev-parse", "HEAD")
        (self.kernel / "sched" / "lib.c").write_text(
            "int lib(int x)\n{\n    if (x > g_lib_limit_for_fix) { return g_lib_limit_for_fix; }\n"
            "    return x;\n}\n")
        git(self.kernel, "commit", "-qam", "fix")
        git(self.kernel, "tag", "v1")
        self.fix = git(self.kernel, "rev-parse", "HEAD")
        self.patch = git(self.kernel, "diff", f"{self.base}..{self.fix}")

        self.apps = root / "apps"
        self.apps.mkdir()
        git(self.apps, "init", "-q")
        (self.apps / "testing").mkdir()
        (self.apps / "testing" / "ostest.c").write_text("int main(void) { return 0; }\n")
        git(self.apps, "add", "-A")
        git(self.apps, "commit", "-qm", "apps base")
        self.apps_base = git(self.apps, "rev-parse", "HEAD")
        (self.apps / "testing" / "lib_test.c").write_text("void lib_test(void) { lib(1); }\n")
        git(self.apps, "add", "-A")
        git(self.apps, "commit", "-qm", "the test")
        self.apps_merge = git(self.apps, "rev-parse", "HEAD")
        self.test_patch = git(self.apps, "diff", f"{self.apps_base}..{self.apps_merge}")
        self.meta = {"kernel_base_commit": self.base, "kernel_merge_commit": self.fix,
                     "apps_base_commit": self.apps_base, "apps_merge_commit": self.apps_merge,
                     "problem_statement": "lib() returns values above the limit the test expects."}

    def facts(self, **meta) -> dict:
        return lc.fix_facts(self.kernel, {**self.meta, **meta}, [("primary", self.fix, self.patch)],
                            self.test_patch)

    def image(self) -> Path:
        """An unpacked image as the recipe makes it, with the recipe's own
        shell functions: both repositories at depth 1, the test applied."""
        root = self.root / "image"
        (root / "embedeval-src" / "nuttx.git").mkdir(parents=True)
        script = recipe_functions() + f"""
set -e
shallow {self.kernel} {self.base} {root}/testbed
cd {root}/testbed
git -c user.name=EmbedEval -c user.email=e@e commit -q --allow-empty -m compat
shallow {self.apps} {self.apps_base} apps
echo /apps/ >> .git/info/exclude
forget {root}/testbed
forget {root}/testbed/apps
"""
        subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True)
        (root / "testbed" / "apps" / "testing" / "lib_test.c").write_text("void lib_test(void) { lib(1); }\n")
        return root


class LeakCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.up = Upstream(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, image=None, **meta):
        return lc.scan(image or self.up.image(), self.up.facts(**meta))

    def test_recipe_image_is_clean(self):
        r = self.scan()
        self.assertTrue(r["ok"], r["findings"])
        self.assertEqual(r["commits"], {"kernel": 2, "apps": 1})
        self.assertEqual(r["fix_blobs_checked"], 1)
        self.assertEqual(r["fix_lines_checked"], 1)

    def test_full_clone_is_history(self):
        image = self.up.image()
        tb = image / "testbed"
        subprocess.run(["rm", "-rf", str(tb / ".git")], check=True)
        subprocess.run(["git", "clone", "-q", "--no-checkout", str(self.up.kernel), str(tb / "full")], check=True)
        (tb / "full" / ".git").rename(tb / ".git")
        findings = " ".join(self.scan(image)["findings"])
        self.assertIn("history: kernel commits other than", findings)
        self.assertIn("shallow boundary", findings)
        self.assertIn("refs:", findings)
        self.assertIn("fix: the kernel commit", findings)

    def test_fetch_head_and_remote_are_refs(self):
        image = self.up.image()
        tb = image / "testbed"
        (tb / ".git" / "FETCH_HEAD").write_text(f"{self.up.fix}\t\tbranch 'master'\n")
        git(tb, "remote", "add", "origin", "https://github.com/apache/nuttx.git")
        findings = " ".join(self.scan(image)["findings"])
        self.assertIn("FETCH_HEAD", findings)
        self.assertIn("remote config remote.origin.url", findings)

    def test_fixed_blob_in_object_store(self):
        image = self.up.image()
        blob = subprocess.run(["git", "-C", str(image / "testbed"), "hash-object", "-w", "--stdin"],
                              input=(self.up.kernel / "sched" / "lib.c").read_text(),
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertIn(blob, self.up.facts()["fix_blobs"])
        findings = " ".join(self.scan(image)["findings"])
        self.assertIn("fixed versions of ['sched/lib.c']", findings)

    def test_apps_history_after_base(self):
        image = self.up.image()
        apps = image / "testbed" / "apps"
        subprocess.run(["git", "-C", str(apps), "fetch", "-q", str(self.up.apps), self.up.apps_merge],
                       check=True, capture_output=True)
        findings = " ".join(self.scan(image)["findings"])
        self.assertIn("history: apps commits other than", findings)
        self.assertIn("fix: the apps commit", findings)

    def test_fix_line_in_a_file(self):
        image = self.up.image()
        (image / "opt").mkdir()
        (image / "opt" / "notes.txt").write_text(
            "    if (x > g_lib_limit_for_fix) { return g_lib_limit_for_fix; }\n")
        findings = " ".join(self.scan(image)["findings"])
        self.assertIn("lines: files hold lines the fix adds: ['/opt/notes.txt']", findings)

    def test_second_repository(self):
        image = self.up.image()
        subprocess.run(["git", "clone", "-q", "--bare", str(self.up.kernel), str(image / "root" / "nuttx.git")],
                       check=True)
        findings = " ".join(self.scan(image)["findings"])
        self.assertIn("copies: other repositories: ['/root/nuttx.git']", findings)

    def test_non_empty_mount_point(self):
        image = self.up.image()
        (image / "embedeval-src" / "nuttx.git" / "HEAD").write_text("ref: refs/heads/master\n")
        self.assertIn("copies", " ".join(self.scan(image)["findings"]))

    def test_statement_naming_fix_identifier(self):
        r = self.scan(problem_statement="lib() ignores g_lib_limit_for_fix.")
        self.assertTrue(r["ok"], r["findings"])            # the image is clean
        self.assertFalse(r["statement_ok"])
        self.assertEqual(r["statement_fix_identifiers"], ["g_lib_limit_for_fix"])

    def test_false_positive_fix_line_inside_test_line(self):
        """A fix line that is part of a line the tests add is shown by design."""
        test_patch = self.up.test_patch + (
            "\n+/* expect: if (x > g_lib_limit_for_fix) { return g_lib_limit_for_fix; } */\n")
        facts = lc.fix_facts(self.up.kernel, self.up.meta, [("primary", self.up.fix, self.up.patch)], test_patch)
        self.assertEqual(facts["fix_lines"], [])

    def test_false_positive_line_already_at_base(self):
        (self.up.kernel / "sched" / "other.c").write_text(
            "    if (x > g_lib_limit_for_fix) { return g_lib_limit_for_fix; }\n")
        git(self.up.kernel, "add", "-A")
        git(self.up.kernel, "commit", "-qm", "unrelated")
        facts = lc.fix_facts(self.up.kernel, {**self.up.meta, "kernel_base_commit": git(self.up.kernel, "rev-parse", "HEAD")},
                             [("primary", self.up.fix, self.up.patch)], self.up.test_patch)
        self.assertEqual(facts["fix_lines"], [])

    def test_false_positive_statement_names_test_identifier(self):
        r = self.scan(problem_statement="lib_test() fails: lib() returns too large values.")
        self.assertTrue(r["ok"] and r["statement_ok"], r)

    def test_false_positive_prerequisite_line(self):
        """A fix line the base patch (prerequisites, committed onto the base) adds is in the starting tree."""
        base_patch = "+    if (x > g_lib_limit_for_fix) { return g_lib_limit_for_fix; }\n"
        facts = lc.fix_facts(self.up.kernel, self.up.meta, [("primary", self.up.fix, self.up.patch)],
                             self.up.test_patch, base_patch)
        self.assertEqual(facts["fix_lines"], [])


class VersionFile(unittest.TestCase):
    """Kernels whose build needs a release tag get a version file, decided
    from the clone once and kept in the metadata."""

    def test_render(self):
        import render_nuttx_instance as rn
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            kernel = tmp / "k"
            kernel.mkdir()
            git(kernel, "init", "-q")
            (kernel / "tools").mkdir()
            (kernel / "tools" / "version.sh").write_text('echo "Missing versioning information"\nexit 1\n')
            git(kernel, "add", "-A")
            git(kernel, "commit", "-qm", "old")
            old = git(kernel, "rev-parse", "HEAD")
            (kernel / "tools" / "version.sh").write_text('echo "Missing versioning information. Using the dummy value. (0.0.0)"\n')
            git(kernel, "commit", "-qam", "cb9ff5ff99")
            new = git(kernel, "rev-parse", "HEAD")
            self.assertTrue(rn.needs_version_file(kernel / ".git", old))
            self.assertFalse(rn.needs_version_file(kernel / ".git", new))
            src = REPO / "apptainer" / "instances" / "nuttx" / "nuttx__nuttx-11889-a2327"
            meta = json.loads((src / "metadata.json").read_text())
            for flag in (True, False):
                out = tmp / f"out-{flag}"
                inst = rn.render({**meta, "needs_version_file": flag}, src / "test_patch.diff", out,
                                 runtime="apptainer")
                recipe = (inst / "instance.def").read_text()
                self.assertEqual("backport of cb9ff5ff99" in recipe, flag)
                self.assertIsNone(re.search(r"@@[A-Z_]+@@", recipe))     # every placeholder rendered
                self.assertIs(json.loads((inst / "metadata.json").read_text())["needs_version_file"], flag)


    def test_backport_applies_to_the_old_script(self):
        import re as _re
        import render_nuttx_instance as rn
        patch = _re.search(r"<<'EOF_CB9FF5FF99'\n(.*?)EOF_CB9FF5FF99", rn.VERSION_FILE, _re.S)[1]
        old = ("#!/bin/bash\n" + "#\n" * 82 + "# Make sure we know what is going on\n\n"
               "if [ -z ${VERSION} ] ; then\n  echo \"Missing versioning information\"\n  echo $USAGE\n"
               "  echo $ADVICE\n  exit 1\nfi\n\nif [ -z ${OUTFILE} ] ; then\n  exit 1\nfi\n")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "tools").mkdir()
            (Path(tmp) / "tools" / "version.sh").write_text(old)
            r = subprocess.run(["git", "apply", "--unsafe-paths", "--directory=", "-"], input=patch, text=True,
                               cwd=tmp, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            new = (Path(tmp) / "tools" / "version.sh").read_text()
            self.assertIn('VERSION="0.0.0"', new)
            self.assertNotIn("exit 1\nfi\n\nif [ -z ${OUTFILE}", new.split("Using the dummy value")[0])
        self.assertIn("commit -q -a \\\n", rn.VERSION_FILE)     # a line continuation, not a literal backslash


class Network(unittest.TestCase):
    def test_exec_args_isolates_when_asked(self):
        self.assertIn("--network", au.exec_args(Path("i.sif"), network=False))
        self.assertNotIn("--network", au.exec_args(Path("i.sif")))

    def test_probe_reading(self):
        self.assertTrue(au.network_isolated("interfaces: lo\n"))
        self.assertFalse(au.network_isolated("interfaces: eth0 lo\n"))
        self.assertFalse(au.network_isolated("interfaces: lo\nREACHABLE\n"))
        self.assertFalse(au.network_isolated("FATAL: no network\n"))


class Prompt(unittest.TestCase):
    def test_no_fix_files_in_the_task(self):
        import run
        meta = json.loads((REPO / "apptainer" / "instances" / "nuttx" / "nuttx__nuttx-11889-a2327"
                           / "metadata.json").read_text())
        task = run.build_task(meta)
        self.assertNotIn("Relevant Source", task)
        for f in meta["files_changed_by_fix"]:
            self.assertNotIn(f, task)

    def test_the_task_is_the_problem_statement_without_test_names(self):
        import run
        meta = {"instance_id": "nuttx__nuttx-1", "problem_statement": "P",
                "fail_to_pass": ["memmem_test"], "pass_to_pass": ["other_test"]}
        task = run.build_task(meta)
        self.assertIn("\n\nP\n", task)
        for name in meta["fail_to_pass"] + meta["pass_to_pass"]:
            self.assertNotIn(name, task)

    def test_the_writer_prompt_does_not_suggest_naming_tests(self):
        self.assertNotIn("{f2p}", ns.WRITE_PROMPT)
        self.assertNotIn("test programs", ns.WRITE_PROMPT)


class PatchCapture(unittest.TestCase):
    """The agent's patch reaches the grader byte for byte."""

    def test_carriage_returns_survive(self):
        import run
        with tempfile.TemporaryDirectory() as tmp:
            tb = Path(tmp) / "testbed"
            tb.mkdir()
            git(tb, "init", "-q")
            (tb / "a.c").write_text("int a;\n")
            git(tb, "add", "-A")
            git(tb, "commit", "-qm", "base")
            (tb / "a.c").write_text("int a = 1;\n")
            (tb / "ostest_full.log").write_bytes(b"user_main: test\r\n  Total nested    0\r\rmore\n")

            class Env:      # runs the command as ApptainerEnvironment does: text, stderr merged
                def execute(self, action):
                    r = subprocess.run(["bash", "-c", action["command"].replace("/testbed", str(tb))],
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    return {"output": "WARNING: a container notice\n" + r.stdout, "returncode": r.returncode}

            patch, touched = run.capture_patch(Env(), {"project": "nuttx"})
            self.assertIn("\r\rmore", patch)
            self.assertNotIn("WARNING", patch)
            check = subprocess.run(["git", "-C", str(tb), "apply", "--check", "-R", "--cached", "-"],
                                   input=patch.encode(), capture_output=True)
            self.assertEqual(check.returncode, 0, check.stderr)


class Statement(unittest.TestCase):
    FIX = "+++ b/sched/lib.c\n-    return x;\n+    if (x > g_lib_limit) return g_lib_limit;\n+    return x;\n"
    TEST = "+void lib_test(void) { ASSERT(lib(9) <= 4); }\n"

    def test_names_fix_identifier(self):
        issues = ns.check("lib() returns values above g_lib_limit when called with large inputs.",
                          self.FIX, self.TEST)
        self.assertTrue(any("g_lib_limit" in i for i in issues), issues)

    def test_test_names_allowed_and_review_consulted(self):
        seen = []
        ok = ns.check("lib_test fails: lib() returns a value above the documented limit for large inputs.",
                      self.FIX, self.TEST, review=lambda s: seen.append(s) or [])
        self.assertEqual(ok, [])
        self.assertEqual(len(seen), 1)

    def test_review_phrases_fail(self):
        issues = ns.check("lib_test fails: lib() returns a value above the documented limit for large inputs.",
                          self.FIX, self.TEST, review=lambda s: ["a value above the documented limit"])
        self.assertIn("gives the fix away", issues[0])


class PromoteGate(unittest.TestCase):
    def setUp(self):
        import hashlib
        import promote_instance
        self.gate = promote_instance.nuttx_gate
        self.meta = {"problem_statement": "lib_test fails for large inputs.",
                     "statement_check": {"version": ns.STATEMENT_CHECK, "result": "written"}}
        self.report = {"leak_check": {"ok": True, "network": "isolated", "statement_ok": True,
                                      "statement_sha256": hashlib.sha256(b"lib_test fails for large inputs.").hexdigest()},
                       "determinism": {"runs": 5, "ok": True}}

    def test_passes(self):
        self.assertIsNone(self.gate(self.report, self.meta))

    def test_refusals(self):
        import copy
        cases = {"no leak check record": lambda r, m: r.pop("leak_check"),
                 "leak check failed": lambda r, m: r["leak_check"].update(ok=False),
                 "network": lambda r, m: r["leak_check"].update(network="reachable"),
                 "names the fix": lambda r, m: r["leak_check"].update(statement_ok=False),
                 "changed after its leak check": lambda r, m: m.update(problem_statement="other"),
                 "deterministic": lambda r, m: r["determinism"].update(runs=1),
                 "statement check": lambda r, m: m["statement_check"].update(version=0)}
        for why, change in cases.items():
            r, m = copy.deepcopy(self.report), copy.deepcopy(self.meta)
            change(r, m)
            if why == "statement check":
                self.assertIn("current statement check", self.gate(r, m))
            elif why == "network":
                self.assertIn("leak check failed", self.gate(r, m))
            else:
                self.assertIn(why, self.gate(r, m))


class StatementRefresh(unittest.TestCase):
    """validate_instance --statement-only: only the statement fields change."""

    def setUp(self):
        import copy
        import validate_instance
        self.vi = validate_instance
        self.tmp = tempfile.TemporaryDirectory()
        self.up = Upstream(Path(self.tmp.name))
        self.folder = Path(self.tmp.name) / "instance"
        self.folder.mkdir()
        for name in au.NUTTX_RECIPE_FILES:
            (self.folder / name).write_text(self.up.test_patch if name == "test_patch.diff" else name)
        self.meta = {**self.up.meta, "problem_statement": "lib() returns too much."}
        self.old = {"valid": True, "validated_at": "2026-11-01T00:00:00",
                    "recipe_sha256": au.recipe_fingerprint(self.folder, au.nuttx_recipe_files(self.folder)),
                    "leak_check": {"ok": True, "findings": [], "network": "isolated", "commits": 2,
                                   "statement_ok": True, "statement_fix_identifiers": [],
                                   "statement_sha256": "stale"},
                    "determinism": {"runs": 5, "ok": True}}
        self.record = self.folder / "validation.json"
        self.record.write_text(json.dumps(self.old))
        self.copy = copy

    def tearDown(self):
        self.tmp.cleanup()

    def refresh(self) -> bool:
        from unittest import mock
        vi = self.vi
        with mock.patch.object(vi.instances, "load_metadata", return_value=self.meta), \
                mock.patch.object(vi.instances, "instance_dir", return_value=self.folder), \
                mock.patch.object(vi, "nuttx_fix_patches", return_value=[("primary", self.up.patch)]), \
                mock.patch.object(vi, "kernel_clone", return_value=self.up.kernel):
            return vi.refresh_nuttx_statement("nuttx__x")

    def test_refresh_matches_the_statement_and_changes_nothing_else(self):
        import promote_instance
        self.assertTrue(self.refresh())
        new = json.loads(self.record.read_text())
        self.assertIn("T", new["leak_check"].pop("statement_refreshed_at"))
        self.assertEqual(new["leak_check"].pop("statement_sha256"), self.vi.statement_sha256(self.meta))
        self.old["leak_check"].pop("statement_sha256")
        self.assertEqual(new, self.old)                     # incl. validated_at, ok, findings
        gate_meta = {**self.meta, "statement_check": {"version": ns.STATEMENT_CHECK, "result": "written"}}
        self.assertIsNone(promote_instance.nuttx_gate(json.loads(self.record.read_text()), gate_meta))

    def test_statement_naming_fix_identifier(self):
        self.meta["problem_statement"] = "lib() ignores g_lib_limit_for_fix."
        self.assertFalse(self.refresh())
        leak = json.loads(self.record.read_text())["leak_check"]
        self.assertFalse(leak["statement_ok"])
        self.assertEqual(leak["statement_fix_identifiers"], ["g_lib_limit_for_fix"])
        self.assertTrue(leak["ok"])                         # the image's verdict is unchanged

    def test_refusals_change_nothing(self):
        cases = {"invalid": lambda r: r.update(valid=False),
                 "recipe changed": lambda r: r.update(recipe_sha256="other"),
                 "leak found": lambda r: r["leak_check"].update(ok=False),
                 "network": lambda r: r["leak_check"].update(network="reachable")}
        for why, change in cases.items():
            report = self.copy.deepcopy(self.old)
            change(report)
            text = json.dumps(report)
            self.record.write_text(text)
            self.assertFalse(self.refresh(), why)
            self.assertEqual(self.record.read_text(), text, why)
        self.record.unlink()
        self.assertFalse(self.refresh(), "no record")


class CompileAttestation(unittest.TestCase):
    """A build-failure before state is attested from the fix alone."""

    def setUp(self):
        import validate_instance as vi
        self.vi = vi
        self.tmp = tempfile.TemporaryDirectory()
        self.up = Upstream(Path(self.tmp.name))
        self.meta = {**self.up.meta, "files_changed_by_test_patch": ["testing/lib_test.c", "testing/ostest.h"]}
        self.fix = self.up.patch + "\n" + (
            "diff --git a/include/nuttx/limit.h b/include/nuttx/limit.h\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/include/nuttx/limit.h\n@@ -0,0 +1 @@\n+extern int g_lib_limit_for_fix;\n")

    def tearDown(self):
        self.tmp.cleanup()

    def attest(self, output: str):
        d = self.vi.test_diagnostics(self.meta, output)
        return d, self.vi.attest_build_failure(self.up.kernel, self.meta, [("primary", self.fix)], d)

    def test_every_test_error_listed_once_without_lines(self):
        d, _ = self.attest("CC: x\n/apps/testing/lib_test.c:3:7: error: 'g_lib_limit_for_fix' undeclared\n"
                           "lib_test.c:9:1: error: 'g_lib_limit_for_fix' undeclared\n"
                           "sched/other.c:2:1: error: unrelated\n")
        self.assertEqual(d, ["lib_test.c: error: 'g_lib_limit_for_fix' undeclared"])

    def test_name_the_fix_adds(self):
        _, proofs = self.attest("lib_test.c:3:7: error: implicit declaration of function ‘g_lib_limit_for_fix’\n")
        self.assertEqual(proofs[0]["name"], "g_lib_limit_for_fix")

    def test_header_the_fix_adds(self):
        _, proofs = self.attest("lib_test.c:1:10: fatal error: nuttx/limit.h: No such file or directory\n")
        self.assertIn("include/nuttx/limit.h", proofs[0]["proof"])

    def test_link_error(self):
        _, proofs = self.attest("ld: lib_test.c:5:(.text+0x10): undefined reference to `g_lib_limit_for_fix'\n")
        self.assertEqual(len(proofs), 1)

    def test_declaration_the_fix_changes(self):
        """A test calling an interface with the signature the fix gives it."""
        (self.up.kernel / "include").mkdir()
        (self.up.kernel / "include" / "lib.h").write_text("int lib(int x);\n")
        git(self.up.kernel, "add", "-A")
        git(self.up.kernel, "commit", "-qm", "header")
        base = git(self.up.kernel, "rev-parse", "HEAD")
        (self.up.kernel / "include" / "lib.h").write_text("int lib(int x, int limit);\n")
        git(self.up.kernel, "commit", "-qam", "new signature")
        fix = git(self.up.kernel, "diff", f"{base}..HEAD") + "\n"
        meta = {**self.meta, "kernel_base_commit": base}
        d = self.vi.test_diagnostics(meta, "lib_test.c:3:7: error: too many arguments to function ‘lib’\n")
        proofs = self.vi.attest_build_failure(self.up.kernel, meta, [("primary", fix)], d)
        self.assertEqual(proofs[0]["proof"], "the fix changes its declaration in include/lib.h")

    def test_host_header_in_place_of_one_the_fix_adds(self):
        fix = self.fix.replace("include/nuttx/limit.h", "include/malloc.h")
        out = ("/usr/include/malloc.h:82:8: error: redefinition of ‘struct mallinfo’\n"
               "ostest_main.c:113:40: error: ‘struct mallinfo’ has no member named ‘lib’\n")
        d = self.vi.test_diagnostics(self.meta, out.replace("ostest_main.c", "lib_test.c"))
        proofs = self.vi.attest_build_failure(self.up.kernel, self.meta, [("primary", fix)], d, "", out)
        self.assertIn("the fix adds include/malloc.h", proofs[0]["proof"])
        # a host header the fix does not add explains nothing
        self.assertEqual(self.vi.attest_build_failure(self.up.kernel, self.meta, [("primary", self.fix)],
                                                      d, "", out), [])

    def test_name_at_base_is_not_attested(self):
        _, proofs = self.attest("lib_test.c:3:7: error: too few arguments to function 'lib'\n")
        self.assertEqual(proofs, [])

    def test_name_in_prerequisites_is_not_attested(self):
        d = self.vi.test_diagnostics(self.meta, "lib_test.c:3:7: error: 'g_lib_limit_for_fix' undeclared\n")
        self.assertEqual(self.vi.attest_build_failure(self.up.kernel, self.meta, [("primary", self.fix)], d,
                                                      "+int g_lib_limit_for_fix;\n"), [])


if __name__ == "__main__":
    unittest.main()
