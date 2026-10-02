from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_instance


class GoldPatchTests(unittest.TestCase):
    def test_uses_gold_base_commit_to_exclude_test_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "upstream"
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
            production = source / "kernel/os/src/os_msys.c"
            production.parent.mkdir(parents=True)
            production.write_text("int existing;\n")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)
            base = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()

            production.write_text("int existing;\nint test_support;\n")
            subprocess.run(
                ["git", "-C", str(source), "commit", "-qam", "add test support"],
                check=True,
            )
            test_commit = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()

            production.write_text(
                "int existing;\nint test_support;\nint production_fix;\n"
            )
            subprocess.run(["git", "-C", str(source), "commit", "-qam", "fix bug"], check=True)
            fix_commit = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()

            patch_text = validate_instance.gold_patch(
                {
                    "project": "mynewt",
                    "repo": str(source),
                    "base_commit": base,
                    "gold_base_commit": test_commit,
                    "fix_commit": fix_commit,
                    "files_changed_by_fix": ["kernel/os/src/os_msys.c"],
                },
                root / "work",
            )

        self.assertIn("+int production_fix;", patch_text)
        self.assertNotIn("+int test_support;", patch_text)


if __name__ == "__main__":
    unittest.main()
