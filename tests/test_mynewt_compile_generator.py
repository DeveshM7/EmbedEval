from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_instance


TEST_SOURCE = "kernel/os/selftest/src/testcases/os_msys_test_cases.c"
UNCHANGED_SOURCE = "kernel/os/selftest/src/testcases/other.c"


class MynewtCompileGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        source = self.root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
        test_file = source / TEST_SOURCE
        test_file.parent.mkdir(parents=True)
        test_file.write_text("int old_test;\n")
        (source / UNCHANGED_SOURCE).write_text("int unchanged;\n")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)
        base = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        test_file.write_text("void test(void) { os_msys_get_free(); }\n")
        subprocess.run(["git", "-C", str(source), "commit", "-qam", "compile test"], check=True)
        test_commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        clone = self.root / "mynewt.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(source), str(clone)], check=True)
        project_patch = patch.dict(generate_instance.PROJECTS["mynewt"], {"clone": clone})
        project_patch.start()
        self.addCleanup(project_patch.stop)

        self.enriched = self.root / "enriched"
        self.enriched.mkdir()
        record = {
            "url": "https://github.com/apache/mynewt-core/pull/3299",
            "base_commit": base,
            "head_commit": test_commit,
            "linked_issues": [{"number": 3234}],
            "files": [{"filename": TEST_SOURCE}],
            "test_suites": {"kernel/os/selftest": {}},
        }
        (self.enriched / "3299.json").write_text(json.dumps(record))
        self.triage = {
            "platform": "native",
            "problem_statement": "Expose the free-buffer count.",
            "fail_to_pass": ["os_msys_suite/os_msys_get_free"],
            "pass_to_pass": [],
            "baseline_tests": [],
            "failure_mode": "compile",
            "compile_test_source": TEST_SOURCE,
            "missing_api": "os_msys_get_free",
        }

    def generate(self, **changes: object) -> Path:
        return generate_instance.generate(
            3299, {**self.triage, **changes}, self.root / "generated",
            project="mynewt", enriched_root=self.enriched,
        )

    def test_compile_verdict_survives_metadata_and_dockerfile_minimization(self) -> None:
        instance = self.generate()
        metadata_path = instance / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        self.assertEqual(metadata["failure_mode"], "compile")
        self.assertEqual(metadata["compile_test_source"], TEST_SOURCE)
        self.assertEqual(metadata["missing_api"], "os_msys_get_free")
        dockerfile = (instance / "Dockerfile").read_text()
        command = re.search(r"&& python3 -c ('[^']+')", dockerfile)
        self.assertIsNotNone(command)
        code = (shlex.split(command.group(1))[0]
                .replace("/opt/benchmark/metadata.json", str(metadata_path))
                .replace("/project/targets/unittest/pkg.yml", str(self.root / "pkg.yml")))
        subprocess.run([sys.executable, "-c", code], check=True)
        minimized = json.loads(metadata_path.read_text())
        self.assertEqual(minimized["failure_mode"], "compile")
        self.assertEqual(minimized["compile_test_source"], TEST_SOURCE)
        self.assertEqual(minimized["missing_api"], "os_msys_get_free")

    def test_omitted_mode_defaults_to_runtime(self) -> None:
        triage = {key: value for key, value in self.triage.items()
                  if key not in ("failure_mode", "compile_test_source", "missing_api")}
        instance = generate_instance.generate(
            3299, triage, self.root / "generated",
            project="mynewt", enriched_root=self.enriched,
        )
        self.assertEqual(json.loads((instance / "metadata.json").read_text())["failure_mode"], "runtime")

    def test_rejects_unknown_mode(self) -> None:
        with self.assertRaises(SystemExit):
            self.generate(failure_mode="link")

    def test_rejects_absent_compile_source(self) -> None:
        with self.assertRaises(SystemExit):
            self.generate(compile_test_source="")

    def test_rejects_absent_missing_api(self) -> None:
        with self.assertRaises(SystemExit):
            self.generate(missing_api="")

    def test_rejects_source_outside_test_patch(self) -> None:
        with self.assertRaises(SystemExit):
            self.generate(compile_test_source=UNCHANGED_SOURCE)

    def test_rejects_token_absent_from_test_source(self) -> None:
        with self.assertRaises(SystemExit):
            self.generate(missing_api="not_in_test_source")


if __name__ == "__main__":
    unittest.main()
