"""The generated hiding scripts of Mynewt, Apptainer NuttX and Contiki-NG, run
against temporary directories shaped like the images (the absolute paths of the
script text are substituted).

    python3 -m unittest discover -s scripts/tests -p 'test_hidden_ports_scripts.py'
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "scripts"), str(REPO / "harness")]

import hidden_tests  # noqa: E402
import patch_checks  # noqa: E402

# harness/run.py's one-commit check (BSD wc pads its count: stripped here), and
# its Contiki-NG tests commit.
ONE_COMMIT = ('cd /testbed && n=$(git rev-list --all | wc -l) && [ "$n" = 1 ] '
              '|| { echo "git history has $n commits, expected 1"; exit 1; }'
              ).replace("wc -l)", "wc -l | tr -d ' ')")
COMMIT_TESTS = ("cd /testbed && "
                "if [ -d tests ]; then git add -A -- tests/; fi; "
                "if [ -d regression-tests ]; then git add -A -- regression-tests/; fi; "
                "git -c user.name=EmbedEval -c user.email=embedeval@localhost "
                "commit -q -m 'Protected upstream tests'")


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-c", "user.name=U", "-c", "user.email=u@u",
                        "-c", "init.defaultBranch=master", "-c", "commit.gpgsign=false",
                        "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class Image:
    """A temporary root with /testbed, /opt, /usr/local/bin and /project in it."""

    def __init__(self, tmp):
        self.root = Path(tmp)
        self.tb = self.root / "testbed"
        self.tb.mkdir()
        (self.root / "bin").mkdir()
        (self.root / "project").mkdir()

    def sub(self, script):
        for old, new in (("/usr/local/bin/run_tests", self.root / "bin" / "run_tests"),
                         ("/tmp/pr-test-files", self.root / "pr-test-files"),
                         ("/opt/benchmark", self.root / "opt" / "benchmark"),
                         ("/opt/embedeval", self.root / "opt" / "embedeval"),
                         ("/testbed", self.tb), ("/project", self.root / "project")):
            script = script.replace(old, str(new))
        return script

    def run(self, script, check=True):
        r = subprocess.run(["bash", "-c", self.sub(script)], capture_output=True, text=True)
        if check and r.returncode:
            raise AssertionError(r.stdout + r.stderr)
        return r

    def diff_of(self, repo):
        git(repo, "add", "-A")
        patch = subprocess.run(["git", "-C", str(repo), "diff", "--cached"],
                               capture_output=True, text=True, check=True).stdout
        git(repo, "reset", "-q")
        return patch


class HistoryChecks:
    def assert_one_root(self, repo, tree=None):
        repo = Path(repo)
        self.assertEqual(git(repo, "rev-list", "--all").count("\n") + 1, 1)
        types = git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objecttype)").split()
        self.assertEqual(types.count("commit"), 1)
        reach = {l.split()[0] for l in git(repo, "rev-list", "--objects", "--all").splitlines()}
        every = set(git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").split())
        self.assertEqual(reach, every)
        self.assertEqual(git(repo, "for-each-ref", "--format=%(refname)"), "refs/heads/main")
        self.assertEqual(git(repo, "remote"), "")
        self.assertFalse((repo / ".git" / "shallow").exists())
        self.assertFalse((repo / ".git" / "logs").exists())
        if tree:
            self.assertEqual(git(repo, "rev-parse", "HEAD^{tree}"), tree)

    def has_blob(self, repo, content):
        sha = subprocess.run(["git", "-C", str(repo), "hash-object", "--stdin"],
                             input=content.encode(), capture_output=True).stdout.decode().strip()
        return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", sha],
                              capture_output=True).returncode == 0


NEW_TEST = "int unique_new_test_content_4711;\n"


class MynewtScriptTest(unittest.TestCase, HistoryChecks):
    META = {"instance_id": "mynewt__mynewt-1", "project": "mynewt",
            "protected_paths": ["kernel/os/selftest/"],
            "protected_line_ranges": {"kernel/os/src/os_msys.c": [[8, 10]]}}

    def make_image(self, tmp):
        im = Image(tmp)
        tb = im.tb
        git(tb, "init", "-q")
        write(tb / "kernel/os/selftest/src/old.c", "int old;\n")
        write(tb / "kernel/os/src/os_msys.c", "".join("line%d\n" % i for i in range(1, 13)))
        git(tb, "add", "-A")
        git(tb, "commit", "-qm", "base")
        base = git(tb, "rev-parse", "HEAD")
        git(tb, "remote", "add", "origin", "https://example.invalid/x.git")
        write(tb / ".git" / "shallow", base + "\n")
        write(tb / "kernel/os/selftest/src/new_test.c", NEW_TEST)
        write(tb / "kernel/os/selftest/src/old.c", "int old;\nint older;\n")
        write(tb / "kernel/os/src/os_msys.c",
              "".join("line%d\n" % i for i in range(1, 13)).replace("line11\n", "line11 SELFTEST\n"))
        patch = im.diff_of(tb)
        git(tb, "add", "-A")
        git(tb, "commit", "-qm", "recipe")
        write(im.root / "opt/benchmark/test_patch.diff", patch)
        write(im.root / "opt/benchmark/metadata.json", json.dumps(
            {"test_path": "kernel/os/selftest", "fail_to_pass": ["a/b"], "mcumgr_commit": "0" * 40,
             "test_observer": "testutil-gdb"}))
        write(im.root / "bin/run_tests", "#!/bin/sh\necho base runner\n")
        write(im.root / "project/bin/leftover", "x")
        return im

    def test_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            im = self.make_image(tmp)
            tb = im.tb
            image_tree = git(tb, "rev-parse", "HEAD^{tree}")
            image_copy = Path(tmp) / "image_copy"
            shutil.copytree(tb, image_copy, symlinks=True)
            im.run(hidden_tests.history_command(self.META, True))
            im.run(ONE_COMMIT)
            im.run(hidden_tests.setup_command(self.META))
            self.assert_one_root(tb)
            self.assertFalse(self.has_blob(tb, NEW_TEST))
            self.assertFalse((tb / "kernel/os/selftest/src/new_test.c").exists())
            self.assertEqual((tb / "kernel/os/selftest/src/old.c").read_text(), "int old;\n")
            self.assertIn("SELFTEST", (tb / "kernel/os/src/os_msys.c").read_text())
            self.assertEqual(git(tb, "status", "--porcelain"), "")
            self.assertEqual(json.loads((im.root / "opt/benchmark/metadata.json").read_text()),
                             {"mcumgr_commit": "0" * 40, "test_observer": "testutil-gdb"})
            self.assertEqual((im.root / "bin/run_tests").read_bytes(),
                             (hidden_tests.RUNNERS / "mynewt.py").read_bytes())
            self.assertIn("base runner", (im.root / "opt/embedeval/run_tests_base.sh").read_text())
            self.assertFalse((im.root / "opt/benchmark/test_patch.diff").exists())
            self.assertFalse((im.root / "project/bin").exists())
            self.assertNotEqual(git(tb, "rev-parse", "HEAD^{tree}"), image_tree)

    def test_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            im = self.make_image(tmp)
            tree = git(im.tb, "rev-parse", "HEAD^{tree}")
            im.run(hidden_tests.history_command(self.META, False))
            im.run(ONE_COMMIT)
            self.assert_one_root(im.tb, tree)
            self.assertTrue((im.tb / "kernel/os/selftest/src/new_test.c").exists())

    def test_setup_fails_if_the_tests_were_not_taken_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            im = self.make_image(tmp)
            r = im.run(hidden_tests.setup_command(self.META), check=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("still in the tree", r.stdout)

    def test_fix_outside_the_protected_paths_end_to_end(self):
        import run
        with tempfile.TemporaryDirectory() as tmp:
            im = self.make_image(tmp)
            image_copy = Path(tmp) / "image_copy"
            shutil.copytree(im.tb, image_copy, symlinks=True)
            im.run(hidden_tests.history_command(self.META, True))
            im.run(ONE_COMMIT)
            im.run(hidden_tests.setup_command(self.META))
            tb = im.tb

            class Env:
                def execute(self, action):
                    cmd = action["command"].replace("/testbed", str(tb))
                    cmd = cmd.replace("base64 -w0", "base64 | tr -d '\\n'")   # BSD base64: no -w0
                    r = subprocess.run(["bash", "-c", cmd],
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    return {"output": r.stdout, "returncode": r.returncode}

            def fix(os_msys_line, text):
                git(tb, "checkout", "-q", "--", ".")
                lines = (tb / "kernel/os/src/os_msys.c").read_text().splitlines(True)
                lines[os_msys_line - 1] = text
                (tb / "kernel/os/src/os_msys.c").write_text("".join(lines))
                write(tb / "kernel/os/selftest/src/old.c", "int tampered;\n")
                return run.capture_patch(Env(), self.META)

            patch, touched = fix(2, "line2 fixed\n")
            self.assertNotIn("selftest", patch.replace("SELFTEST", ""))
            self.assertIn("kernel/os/selftest/src/old.c", touched)
            check = subprocess.run(["git", "-C", str(image_copy), "apply", "--check", "-"],
                                   input=patch.encode(), capture_output=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(patch_checks.protected_ranges_touched(patch, self.META), [])
            patch, _ = fix(9, "line9 fixed\n")
            self.assertEqual(patch_checks.protected_ranges_touched(patch, self.META),
                             ["kernel/os/src/os_msys.c"])


class NuttxScriptTest(unittest.TestCase, HistoryChecks):
    META = {"instance_id": "nuttx__nuttx-1-a2", "project": "nuttx", "runtime": "apptainer"}

    def make_image(self, tmp):
        im = Image(tmp)
        tb, apps = im.tb, im.tb / "apps"
        git(tb, "init", "-q")
        write(tb / ".gitignore", ".config\n.config.old\n")
        write(tb / "Makefile", "all:\n")
        git(tb, "add", "-A")
        git(tb, "commit", "-qm", "base")
        write(tb / ".git" / "shallow", git(tb, "rev-parse", "HEAD") + "\n")
        for i in (1, 2):
            write(tb / ("recipe%d.txt" % i), "r\n")
            git(tb, "add", "-A")
            git(tb, "commit", "-qm", "recipe %d" % i)
        write(tb / ".git/info/exclude", "/apps/\n")
        for f in (".config", ".config.old"):
            write(tb / f, "CONFIG_FOO=y\nCONFIG_TESTING_OSTEST_WQUEUE=y\n"
                  "# CONFIG_TESTING_OSTEST_WQUEUE is not set\nCONFIG_TESTING_OSTEST_WQUEUE_X=y\n")
        git(apps, "init", "-q") if apps.mkdir() is None else None
        write(apps / "testing/ostest/Kconfig", "config TESTING_OSTEST\n\tbool\n")
        write(apps / "testing/ostest/getopt.c", "int getopt_old;\n")
        git(apps, "add", "-A")
        git(apps, "commit", "-qm", "apps base")
        write(apps / ".git" / "shallow", git(apps, "rev-parse", "HEAD") + "\n")
        write(apps / "testing/ostest/Kconfig",
              "config TESTING_OSTEST\n\tbool\n\nconfig TESTING_OSTEST_WQUEUE\n\tbool\n")
        write(apps / "testing/ostest/getopt.c", "int getopt_new;\n")
        patch = im.diff_of(apps)
        write(im.root / "opt/embedeval/test_patch.diff", patch)
        write(im.root / "bin/run_tests", "#!/usr/bin/env bash\nTIMEOUT=300\n"
              "BANNERS=('user_main: getopt() test')\necho base\n")
        return im

    def test_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            im = self.make_image(tmp)
            tb, apps = im.tb, im.tb / "apps"
            tree = git(tb, "rev-parse", "HEAD^{tree}")
            im.run(hidden_tests.history_command(self.META, True))
            im.run(ONE_COMMIT)
            im.run(hidden_tests.setup_command(self.META))
            self.assert_one_root(tb, tree)
            self.assert_one_root(apps)
            self.assertEqual(git(tb, "status", "--porcelain"), "")
            self.assertEqual(git(apps, "status", "--porcelain"), "")
            self.assertEqual((apps / "testing/ostest/getopt.c").read_text(), "int getopt_old;\n")
            for f in (".config", ".config.old"):
                text = (tb / f).read_text()
                self.assertNotIn("CONFIG_TESTING_OSTEST_WQUEUE=", text)
                self.assertNotIn("CONFIG_TESTING_OSTEST_WQUEUE is not set", text)
                self.assertIn("CONFIG_FOO=y", text)
                self.assertIn("CONFIG_TESTING_OSTEST_WQUEUE_X=y", text)
            base = (im.root / "opt/embedeval/run_tests_base.sh").read_text()
            self.assertIn("BANNERS=()\n", base)
            self.assertNotIn("getopt", base)
            self.assertEqual((im.root / "bin/run_tests").read_bytes(),
                             (hidden_tests.RUNNERS / "nuttx.py").read_bytes())
            self.assertFalse((im.root / "opt/embedeval/test_patch.diff").exists())

    def test_a_remaining_mention_of_the_symbol_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            im = self.make_image(tmp)
            write(im.tb / "include/config.h", "#define CONFIG_TESTING_OSTEST_WQUEUE 1\n")
            im.run(hidden_tests.history_command(self.META, True))
            r = im.run(hidden_tests.setup_command(self.META), check=False)
            self.assertNotEqual(r.returncode, 0)

    def test_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            im = self.make_image(tmp)
            im.run(hidden_tests.history_command(self.META, False))
            im.run(ONE_COMMIT)
            self.assert_one_root(im.tb)
            self.assert_one_root(im.tb / "apps")
            self.assertEqual((im.tb / "apps/testing/ostest/getopt.c").read_text(), "int getopt_new;\n")


class ContikiScriptTest(unittest.TestCase, HistoryChecks):
    META = {"instance_id": "contiki-ng__contiki-ng-1", "project": "contiki-ng"}

    def make_image(self, tmp):
        im = Image(tmp)
        tb = im.tb
        git(tb, "init", "-q")
        write(tb / ".gitignore", "build/\n")
        write(tb / "os/x.c", "int x;\n")
        write(tb / "tests/01-a/test.c", "int old;\n")
        git(tb, "add", "-A")
        git(tb, "commit", "-qm", "base")
        write(tb / ".git" / "shallow", git(tb, "rev-parse", "HEAD") + "\n")
        write(tb / ".git/info/exclude", ".embedeval/\ntags\n")
        write(tb / "tests/01-a/test.c", "int new;\n")
        write(tb / "tests/02-new/test.c", "int brand_new;\n")
        patch = im.diff_of(tb)
        write(tb / "tests/01-a/build/native/x.o", "obj")
        write(tb / ".embedeval/logs/x.log", "unit tests/02-new\n")
        write(tb / ".embedeval/gradle/keep", "cache")
        write(tb / "tags", "t\t./tests/02-new/test.c\t/^x$/;\"\tf\nt\t./tests/01-a/test.c\t/^x$/\n"
              "main\tos/x.c\t/^y$/;\"\tf\n")
        write(im.root / "opt/embedeval/test_patch.diff", patch)
        write(im.root / "opt/embedeval/bench.json", json.dumps(
            {"units": [{"id": "tests/02-new"}], "submodules": ["tools/cooja"]}))
        write(im.root / "bin/run_tests", "#!/usr/bin/env python3\nprint('base')\n")
        return im

    def test_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            im = self.make_image(tmp)
            tb = im.tb
            tree = git(tb, "rev-parse", "HEAD^{tree}")
            im.run(hidden_tests.history_command(self.META, True))
            im.run(ONE_COMMIT)
            im.run(hidden_tests.setup_command(self.META))
            self.assert_one_root(tb, tree)
            self.assertEqual(git(tb, "status", "--porcelain", "--ignored"), "!! .embedeval/\n!! tags")
            self.assertEqual((tb / "tests/01-a/test.c").read_text(), "int old;\n")
            self.assertFalse((tb / "tests/01-a/build").exists())
            self.assertEqual(list((tb / ".embedeval/logs").iterdir()), [])
            self.assertTrue((tb / ".embedeval/gradle/keep").exists())
            tags = (tb / "tags").read_text()
            self.assertNotIn("tests/", tags)
            self.assertIn("os/x.c", tags)
            self.assertEqual(json.loads((im.root / "opt/embedeval/bench.json").read_text()),
                             {"submodules": ["tools/cooja"]})
            self.assertEqual((im.root / "bin/run_tests").read_bytes(),
                             (hidden_tests.RUNNERS / "contiki_ng.py").read_bytes())
            self.assertIn("base", (im.root / "opt/embedeval/run_tests_base.py").read_text())
            self.assertFalse((im.root / "opt/embedeval/test_patch.diff").exists())
            self.assertFalse((im.root / "pr-test-files").exists())

    def test_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            im = self.make_image(tmp)
            im.run(hidden_tests.history_command(self.META, False))
            im.run(ONE_COMMIT)
            im.run(COMMIT_TESTS)
            self.assertEqual(git(im.tb, "rev-list", "--all").count("\n") + 1, 2)
            self.assertEqual((im.tb / "tests/01-a/test.c").read_text(), "int new;\n")
            self.assertEqual(git(im.tb, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
