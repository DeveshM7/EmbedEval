from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_instance


class MynewtInstanceGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.enriched = self.root / "candidates" / "mynewt" / "enriched"
        self.enriched.mkdir(parents=True)
        record = {
            "number": 3299,
            "url": "https://github.com/apache/mynewt-core/pull/3299",
            "base_commit": "base123",
            "head_commit": "head456",
            "linked_issues": [
                {
                    "number": 3234,
                    "title": "msys ignores free buffers",
                    "body": "Allocation can fail while a larger pool has space.",
                    "state": "closed",
                    "labels": [],
                    "comments": [],
                }
            ],
            "files": [
                {
                    "filename": "kernel/os/src/os_msys.c",
                    "status": "modified",
                    "additions": 2,
                    "deletions": 1,
                    "patch": "@@ -1 +1 @@",
                },
                {
                    "filename": "kernel/os/selftest/src/testcases/os_msys_test_cases.c",
                    "status": "modified",
                    "additions": 20,
                    "deletions": 0,
                    "patch": "@@ -1 +1,2 @@",
                },
            ],
            "test_suites": {
                "kernel/os/selftest": {
                    "config": "pkg.yml",
                    "files": {
                        "pkg.yml": {
                            "size": 50,
                            "content": "pkg.name: kernel/os/selftest\npkg.type: unittest\n",
                        }
                    },
                }
            },
        }
        (self.enriched / "3299.json").write_text(json.dumps(record))
        self.triage = {
            "verdict": "accept",
            "confidence": "high",
            "change_type": "fix",
            "platform": "native",
            "problem_statement": "Allocation must continue into a larger pool with free buffers.",
            "fail_to_pass": ["os_msys_test_suite/os_msys_test_alloc1"],
            "pass_to_pass": ["os_msys_test_suite/os_msys_test_limit1"],
            "baseline_tests": [],
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_generates_native_only_mynewt_instance(self) -> None:
        patch_text = (
            "diff --git a/kernel/os/selftest/test.c b/kernel/os/selftest/test.c\n"
            "--- a/kernel/os/selftest/test.c\n"
            "+++ b/kernel/os/selftest/test.c\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        with (
            patch.object(generate_instance, "build_test_patch", return_value=patch_text),
            patch.object(generate_instance, "check_applies"),
        ):
            instance = generate_instance.generate(
                3299,
                self.triage,
                self.root / "generated",
                project="mynewt",
                enriched_root=self.enriched,
            )

        self.assertEqual(instance.name, "mynewt__mynewt-3299")
        self.assertEqual(
            {path.name for path in instance.iterdir()},
            {"Dockerfile", "metadata.json", "run_tests.sh", "test_patch.diff"},
        )
        metadata = json.loads((instance / "metadata.json").read_text())
        self.assertEqual(metadata["platform"], "native")
        self.assertEqual(metadata["docker_platform"], "linux/amd64")
        self.assertEqual(metadata["test_path"], "kernel/os/selftest")
        self.assertEqual(metadata["protected_paths"], ["kernel/os/selftest/"])
        self.assertEqual(metadata["structured_test_results"], "/tmp/mynewt-result.json")
        self.assertEqual(metadata["issue_url"], "https://github.com/apache/mynewt-core/issues/3234")
        self.assertNotIn("sdk_fallback_version", metadata)
        self.assertNotIn("extra_configs", metadata)
        dockerfile = (instance / "Dockerfile").read_text()
        self.assertIn("hw/bsp/native", dockerfile)
        self.assertIn("compiler/sim", dockerfile)
        self.assertNotIn("qemu_x86", dockerfile)
        self.assertNotIn("qemu-system", dockerfile)

    def test_rejects_non_native_mynewt_platform(self) -> None:
        triage = {**self.triage, "platform": "qemu_x86"}

        with self.assertRaisesRegex(SystemExit, "platform must be 'native'"):
            generate_instance.generate(
                3299,
                triage,
                self.root / "generated",
                project="mynewt",
                enriched_root=self.enriched,
            )

    def test_protects_every_changed_selftest_suite(self) -> None:
        record_path = self.enriched / "3299.json"
        record = json.loads(record_path.read_text())
        record["files"].append(
            {
                "filename": "encoding/json/selftest/src/json_test.c",
                "status": "modified",
                "additions": 3,
                "deletions": 1,
                "patch": "@@ -1 +1 @@",
            }
        )
        record["test_suites"]["encoding/json/selftest"] = {
            "config": "pkg.yml",
            "files": {
                "pkg.yml": {
                    "size": 52,
                    "content": "pkg.name: encoding/json/selftest\npkg.type: unittest\n",
                }
            },
        }
        record_path.write_text(json.dumps(record))
        patch_text = (
            "diff --git a/kernel/os/selftest/test.c b/kernel/os/selftest/test.c\n"
            "--- a/kernel/os/selftest/test.c\n"
            "+++ b/kernel/os/selftest/test.c\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )

        with (
            patch.object(generate_instance, "build_test_patch", return_value=patch_text),
            patch.object(generate_instance, "check_applies"),
        ):
            instance = generate_instance.generate(
                3299,
                self.triage,
                self.root / "generated",
                project="mynewt",
                enriched_root=self.enriched,
            )

        metadata = json.loads((instance / "metadata.json").read_text())
        self.assertEqual(
            metadata["protected_paths"],
            ["kernel/os/selftest/", "encoding/json/selftest/"],
        )

    def test_rejects_missing_issue_before_writing_instance(self) -> None:
        record_path = self.enriched / "3299.json"
        record = json.loads(record_path.read_text())
        record["linked_issues"] = []
        record_path.write_text(json.dumps(record))
        out = self.root / "generated"

        with (
            patch.object(generate_instance, "build_test_patch", return_value="patch\n"),
            patch.object(generate_instance, "check_applies"),
            self.assertRaisesRegex(SystemExit, "no linked issue"),
        ):
            generate_instance.generate(
                3299,
                self.triage,
                out,
                project="mynewt",
                enriched_root=self.enriched,
            )

        self.assertFalse((out / "mynewt__mynewt-3299").exists())

    def test_test_patch_paths_include_both_sides_of_selftest_rename(self) -> None:
        record = {
            "files": [
                {
                    "filename": "encoding/base64/selftest/src/base64_test.c",
                    "previous_filename": "encoding/base64/selftest/src/encoding_test.c",
                    "status": "renamed",
                },
                {
                    "filename": "encoding/base64/src/base64.c",
                    "status": "modified",
                },
            ]
        }

        self.assertEqual(
            generate_instance.changed_test_paths(record, "mynewt"),
            [
                "encoding/base64/selftest/src/encoding_test.c",
                "encoding/base64/selftest/src/base64_test.c",
            ],
        )

    def test_separates_external_test_support_from_later_production_fix(self) -> None:
        source = self.root / "upstream"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(
            ["git", "-C", str(source), "config", "user.name", "EmbedEval Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(source), "config", "user.email", "test@example.com"],
            check=True,
        )
        test_file = source / "kernel/os/selftest/src/testcases/msys_test.c"
        support_file = source / "kernel/os/src/os_msys.c"
        test_file.parent.mkdir(parents=True)
        support_file.parent.mkdir(parents=True)
        test_file.write_text("old test\n")
        support_file.write_text("int existing;\n")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)
        base = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()

        test_file.write_text("new regression test\n")
        support_file.write_text("int existing;\nint test_support;\n")
        subprocess.run(["git", "-C", str(source), "commit", "-qam", "add tests"], check=True)
        test_commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()

        support_file.write_text("int existing;\nint test_support;\nint production_fix;\n")
        subprocess.run(["git", "-C", str(source), "commit", "-qam", "fix bug"], check=True)
        fix_commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()

        clone = self.root / "mynewt.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(source), str(clone)], check=True)

        record_path = self.enriched / "3299.json"
        record = json.loads(record_path.read_text())
        record["base_commit"] = base
        record["head_commit"] = fix_commit
        record["commits"] = [
            {"sha": test_commit, "message": "add tests"},
            {"sha": fix_commit, "message": "fix bug"},
        ]
        record["files"][1]["filename"] = "kernel/os/selftest/src/testcases/msys_test.c"
        record_path.write_text(json.dumps(record))
        triage = {
            **self.triage,
            "test_commit": test_commit,
            "test_support_paths": ["kernel/os/src/os_msys.c"],
        }

        with patch.dict(generate_instance.PROJECTS["mynewt"], {"clone": clone}):
            instance = generate_instance.generate(
                3299,
                triage,
                self.root / "generated",
                project="mynewt",
                enriched_root=self.enriched,
            )

        patch_text = (instance / "test_patch.diff").read_text()
        self.assertIn("+int test_support;", patch_text)
        self.assertNotIn("+int production_fix;", patch_text)
        metadata = json.loads((instance / "metadata.json").read_text())
        self.assertEqual(metadata["test_commit"], test_commit)
        self.assertEqual(metadata["gold_base_commit"], test_commit)


if __name__ == "__main__":
    unittest.main()
