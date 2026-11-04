"""Fresh grading must reject protected-test edits before starting a container."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "harness"))
import evaluate_patches as grading


class ContikiGradingTest(unittest.TestCase):
    def test_protected_patch_never_reaches_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "patch.diff"
            artifact.write_text("diff --git a/tests/check.c b/tests/check.c\n")
            with (patch.object(grading.paths, "patch_path", return_value=artifact),
                  patch.object(grading.paths, "resolve_model", return_value="control"),
                  patch.object(grading.subprocess, "run") as run):
                result = grading.evaluate_apptainer(
                    "contiki-ng__contiki-ng-1", "control", {"project": "contiki-ng"}, 30)
            self.assertEqual(result["error"], "patch modifies protected upstream tests")
            self.assertFalse(result["patch_applied"])
            run.assert_not_called()
