import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    "mynewt_runner", Path(__file__).resolve().parents[1] / "docker/shared/mynewt_runner.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ResultTests(unittest.TestCase):
    def test_missing_test_is_not_success(self):
        self.assertEqual(runner.classify("[pass] suite/a\n", 0, ["suite/a", "suite/b"])[0], 3)

    def test_process_failure_is_not_success(self):
        self.assertEqual(runner.classify("[pass] suite/a\n", -11, ["suite/a"])[0], 3)

    def test_multiple_assertions_count_as_one_failed_case(self):
        code, result = runner.classify(
            "[pass] suite/a\n[FAIL] suite/b [file:1] assertion\n"
            "[FAIL] suite/b [file:2] assertion\n", 1, ["suite/a", "suite/b"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(result["failed"], ["suite/b"])

    def test_complete_success(self):
        self.assertEqual(runner.classify("[pass] suite/a\n", 0, ["suite/a"])[0], 0)

    def test_failure_output_cannot_pass_with_zero_exit(self):
        self.assertNotEqual(runner.classify("[FAIL] suite/a [file:1]\n", 0, ["suite/a"])[0], 0)


if __name__ == "__main__":
    unittest.main()
