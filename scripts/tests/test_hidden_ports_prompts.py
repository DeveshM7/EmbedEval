"""Hidden tests for Mynewt, Apptainer NuttX and Contiki-NG: what the prompts and
the generated scripts say, for every instance.

    python3 -m unittest discover -s scripts/tests -p 'test_hidden_ports_prompts.py'
"""

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "scripts"), str(REPO / "harness")]

import hidden_tests  # noqa: E402
import paths  # noqa: E402
import projects  # noqa: E402
import run  # noqa: E402

ORACLE = "supplied tests as the acceptance oracle"


def instances(project):
    for iid in paths.discover_instances(project):
        meta = paths.load_metadata(iid)
        if project == "nuttx" and meta.get("runtime") != "apptainer":
            continue
        yield iid, meta


def diff_files(iid):
    """The files of an instance's test diff."""
    for root in paths.INSTANCE_ROOTS:
        p = root / paths.split_instance(iid)[0] / iid / "test_patch.diff"
        if not p.exists():
            p = root / iid / "test_patch.diff"
        if p.exists():
            return re.findall(r"^diff --git a/(\S+) b/", p.read_text(errors="replace"), re.M)
    raise FileNotFoundError(iid)


def protected_files(meta, files):
    """The diff files the hiding reverses (hidden_tests._includes's rule)."""
    apps = meta["project"] == "nuttx"
    out = []
    for pat in projects.setting(meta, "protected_paths"):
        if apps:
            if not pat.startswith("apps/"):
                continue
            pat = pat[len("apps/"):]
        out += [f for f in files if (f.startswith(pat) if pat.endswith("/") else f == pat)]
    return out


def secrets(meta, project):
    names = list(meta.get("fail_to_pass", [])) + list(meta.get("pass_to_pass", []))
    names += list(meta.get("test_banners", []))
    if project == "contiki-ng":
        for root in paths.INSTANCE_ROOTS:
            b = root / "contiki-ng" / meta["instance_id"] / "bench.json"
            if b.exists():
                for u in json.loads(b.read_text())["units"]:
                    for c in u.get("cases", []):
                        names += [c["id"], c["description"]]
    # A name the project's own hidden prompt text contains (a bare banner prefix)
    # tells nothing.
    static = " ".join(hidden_tests.RUNNING_TESTS[project] + hidden_tests.ORIENTATION[project]
                      for _ in [0])
    return [n for n in names if isinstance(n, str) and len(n) > 8 and n not in static]


class PromptsTest(unittest.TestCase):
    def check_project(self, project):
        count = 0
        for iid, meta in instances(project):
            with self.subTest(iid):
                count += 1
                self.assertTrue(run._hide_tests(meta))
                system, instance = run.build_prompts(meta)
                run.build_task(meta)
                text = system + instance
                self.assertIn(hidden_tests.ORIENTATION[project], system)
                self.assertIn(hidden_tests.RUNNING_TESTS[project], instance)
                self.assertNotIn(ORACLE, text)
                for s in secrets(meta, project):
                    self.assertNotIn(s, text)
                with patch.object(hidden_tests, "ENABLED", False):
                    self.assertFalse(run._hide_tests(meta))
                    run.build_prompts(meta)
        self.assertGreater(count, 0)

    def test_mynewt(self):
        self.check_project("mynewt")

    def test_nuttx(self):
        self.check_project("nuttx")

    def test_contiki(self):
        self.check_project("contiki-ng")

    def test_mynewt_3299_keeps_its_test_support_warning(self):
        meta = paths.load_metadata("mynewt__mynewt-3299")
        text = "".join(run.build_prompts(meta))
        self.assertIn("get_msys_pool_list", text)
        self.assertNotIn(ORACLE, text)
        with patch.object(hidden_tests, "ENABLED", False):
            self.assertIn(ORACLE, "".join(run.build_prompts(meta)))

    def test_kept_warnings_are_protected_test_support(self):
        for iid, meta in instances("mynewt"):
            kept = [l for l in (hidden_tests.instance_warnings(meta) or "").split("\n") if l]
            if not kept:
                continue
            with self.subTest(iid):
                self.assertTrue(set(kept) <= set(meta["extra_warnings"].split("\n")))
                if any("protected upstream test support" in l for l in kept):
                    protected = projects.setting(meta, "protected_paths")
                    outside = [f for f in meta["protected_line_ranges"]
                               if not any(f.startswith(p) for p in protected)]
                    self.assertTrue(outside)

    def test_zephyr_riot_warnings_unchanged(self):
        self.assertEqual(hidden_tests.instance_warnings({}), None)

    def test_unsupported_keep_the_tests(self):
        for project in ("openthread", "nuttx"):
            for iid in paths.discover_instances(project):
                meta = paths.load_metadata(iid)
                if project == "nuttx" and meta.get("runtime") == "apptainer":
                    continue
                with self.subTest(iid):
                    self.assertFalse(run._hide_tests(meta))
                    self.assertFalse(hidden_tests.supported(meta))

    def test_scripts_parse_and_tests_are_in_protected_paths(self):
        for project in ("mynewt", "nuttx", "contiki-ng"):
            for iid, meta in instances(project):
                with self.subTest(iid):
                    for script in (hidden_tests.setup_command(meta),
                                   hidden_tests.history_command(meta, True),
                                   hidden_tests.history_command(meta, False)):
                        r = subprocess.run(["bash", "-n"], input=script.encode(),
                                           capture_output=True)
                        self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertTrue(protected_files(meta, diff_files(iid)),
                                    "no test file under the protected paths")


class CommandTimeoutTest(unittest.TestCase):
    """Main's per-command limit is 900 s for every project; Contiki-NG had
    drifted to 1800 (copied from OpenThread). The ports evaluated against
    main's rules must apply 900 s, via the lookups harness/run.py uses."""

    def test_contiki_and_mynewt_use_mains_900s(self):
        for project in ("contiki-ng", "mynewt"):
            iids = list(paths.discover_instances(project))
            self.assertTrue(iids, project)
            meta = paths.load_metadata(iids[0])
            with self.subTest(project):
                self.assertEqual(projects.config_for(meta).get("command_timeout", 900), 900)
                self.assertEqual(projects.setting(meta, "command_timeout", 900), 900)


if __name__ == "__main__":
    unittest.main()
