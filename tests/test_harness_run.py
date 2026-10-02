from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import run


class RecordingEnvironment:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def execute(self, action: dict) -> dict:
        self.commands.append(action["command"])
        return {"output": ""}


class PerInstanceProtectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.meta = {
            "project": "mynewt",
            "protected_paths": ["new/component/selftest/"],
        }

    def test_prompt_names_generated_instance_selftest_as_protected(self) -> None:
        _, instance_prompt = run.build_prompts(self.meta)

        self.assertIn("`new/component/selftest/`", instance_prompt)
        self.assertNotIn("`fs/fcb2/selftest/`", instance_prompt)

    def test_patch_capture_excludes_generated_instance_selftest(self) -> None:
        environment = RecordingEnvironment()

        run.capture_patch(environment, self.meta)

        self.assertEqual(len(environment.commands), 2)
        self.assertIn("':(exclude)new/component/selftest/'", environment.commands[0])
        self.assertIn("'new/component/selftest/'", environment.commands[1])


if __name__ == "__main__":
    unittest.main()
