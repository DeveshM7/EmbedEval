"""Local checks for parsing upstream Contiki-NG test evidence."""

import importlib.util
from pathlib import Path
import unittest
import json
import tempfile
from unittest.mock import patch


RUNNER = (Path(__file__).resolve().parents[2] / 'templates' / 'contiki-ng'
          / 'apptainer' / 'run_tests.py')
spec = importlib.util.spec_from_file_location('contiki_runner', RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class NativeParserTests(unittest.TestCase):
    def test_unselected_failure_is_not_hidden_by_empty_p2p_list(self):
        unit={'id':'unit','kind':'native'}
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'instance.json'
            config.write_text(json.dumps({'units':[unit],'observe':False,
                'fail_to_pass':['unit::fix'],'pass_to_pass':[],
                'after_required':['unit::fix','unit::existing']}))
            with (patch.object(runner,'CONFIG',config),patch.object(runner,'LOGS',Path(tmp)),
                  patch.object(runner,'build_native',return_value=True),
                  patch.object(runner,'run_native',return_value=({'unit::fix':'passed','unit::existing':'failed'},True)),
                  patch('builtins.print')):
                self.assertEqual(runner.main(),1)

    def test_current_format_maps_only_completed_named_cases(self):
        output = ('Unit test: first case\nResult: success\n'
                  'Unit test: second case\nResult: failure\n'
                  'Unit test: interrupted case\n=check-me= DONE\n')
        self.assertEqual(runner.parse_native(output),
                         ({'first case': 'passed', 'second case': 'failed'}, True))

    def test_legacy_format_and_missing_completion(self):
        output = ('=check-me= SUCCEEDED - old pass\n'
                  '=check-me= FAILED - old fail\n'
                  'exit point: test.c:42\n')
        self.assertEqual(runner.parse_native(output),
                         ({'old pass': 'passed', 'old fail': 'failed'}, False))

    def test_legacy_failed_line_uses_registered_description(self):
        output = ('=check-me= FAILED   - Singly-linked list: exit at L157\n'
                  '=check-me= SUCCEEDED - Stack Push/Pop\n=check-me= DONE\n')
        self.assertEqual(runner.parse_native(output),
                         ({'Singly-linked list': 'failed', 'Stack Push/Pop': 'passed'},
                          True))

    def test_no_result_is_not_a_fabricated_failure(self):
        self.assertEqual(runner.parse_native('Unit test: pending\ncrashed\n'), ({}, False))

    def test_framer_parenthesized_exit_keeps_case_identity(self):
        self.assertEqual(runner.parse_native('=check-me= FAILED   - frame parse (exit at 203)\n=check-me= DONE\n'),
                         ({'frame parse':'failed'},True))

    def test_ignores_output_after_completion(self):
        self.assertEqual(runner.parse_native('=check-me= DONE\n'
                                             'Unit test: spurious\nResult: failure\n'),
                         ({}, True))


class OtherParserTests(unittest.TestCase):
    def test_cooja_requires_verdict(self):
        self.assertEqual(runner.parse_cooja('INFO - TEST OK\n'), 'passed')
        self.assertEqual(runner.parse_cooja('TEST OK\nTEST FAILED\n'), 'failed')
        self.assertEqual(runner.parse_cooja('BUILD SUCCESSFUL'), 'not_run')

    def test_cooja_distinguishes_compile_failure_without_log(self):
        self.assertTrue(runner.cooja_build_failed(2, 'not_run',
                        'Failed to load simulation: Compilation failed'))
        self.assertFalse(runner.cooja_build_failed(2, 'not_run',
                         'TEST FAILED: assertion failed'))
        self.assertFalse(runner.cooja_build_failed(2, 'failed',
                         'Failed to load simulation: Compilation failed'))

    def test_script_requires_named_build_and_clean_summary(self):
        entry = 'code-test-lc/:test-lc-switch:native'
        output = 'Building: code-test-lc/ test-lc-switch for target native\ntest passed\nAll tests OK\n'
        self.assertEqual(runner.parse_script(output, entry), 'passed')
        self.assertEqual(runner.parse_script('All tests OK\n', entry), 'not_run')
        self.assertEqual(runner.parse_script(output, entry, 'code-test-lc/ TEST FAIL'), 'failed')


if __name__ == '__main__':
    unittest.main()
