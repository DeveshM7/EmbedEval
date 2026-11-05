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

    def test_ant_era_verdict_spelling(self):
        self.assertEqual(runner.parse_cooja('22-stack-guard-sky  TEST FAIL  0/1'), 'failed')
        self.assertEqual(runner.parse_cooja('22-stack-guard-sky  TEST OK  1/1'), 'passed')

    def run_cooja_with(self, testlog, output, rc):
        unit = {'id': 'tests/07-simulation-base/22-x.csc', 'suite_directory': 'tests/07-simulation-base',
                'scenario': '22-x.csc'}
        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp) / 'tests/07-simulation-base'
            suite.mkdir(parents=True)
            (suite / '22-x.testlog').write_text('stale TEST OK')       # from an earlier run: removed
            def fake(cmd, path, **kw):
                if testlog:
                    (suite / '22-x.testlog').write_text(testlog)
                return rc, output
            with patch.object(runner, 'TESTBED', Path(tmp)), patch.object(runner, 'LOGS', Path(tmp)), \
                 patch.object(runner, 'run_logged', side_effect=fake):
                return runner.run_cooja(unit)

    def test_ant_era_testlog_is_read_and_stale_logs_cleared(self):
        self.assertEqual(self.run_cooja_with('22-x  TEST OK  1/1', '', 0)[0],
                         {'tests/07-simulation-base/22-x.csc': 'passed'})
        self.assertEqual(self.run_cooja_with('', '', 2)[0],
                         {'tests/07-simulation-base/22-x.csc': 'not_run'})

    def test_simulation_that_never_loaded_is_a_build_failure_not_a_test_failure(self):
        out = ('FATAL [main] (Cooja.java:1337) - Exception when loading simulation:\n'
               'Mote type creation failed: Compilation error: Cannot run program "make"')
        statuses, complete, compile_failed = self.run_cooja_with('22-x  TEST FAIL  0/1', out, 2)
        self.assertEqual(statuses, {'tests/07-simulation-base/22-x.csc': 'not_run'})
        self.assertTrue(compile_failed)

    def test_script_requires_named_build_and_clean_summary(self):
        entry = 'code-test-lc/:test-lc-switch:native'
        output = 'Building: code-test-lc/ test-lc-switch for target native\ntest passed\nAll tests OK\n'
        self.assertEqual(runner.parse_script(output, entry), 'passed')
        self.assertEqual(runner.parse_script('All tests OK\n', entry), 'not_run')
        self.assertEqual(runner.parse_script(output, entry, 'code-test-lc/ TEST FAIL'), 'failed')


class CrashAttributionTests(unittest.TestCase):
    GDB = ('Running dbg-io unit tests\n\nProgram received signal SIGSEGV, Segmentation fault.\n'
           '0x00007ffff7e2a in __strlen_avx2 () from /lib/libc.so.6\n'
           '#0  0x00007ffff7e2a in __strlen_avx2 () from /lib/libc.so.6\n'
           '#1  0x0000555555559 in dbg_vprintf (fmt=0x0) at dbg-io.c:88\n'
           '#2  0x000055555555a in unit_test_function_test_puts_null (unit_test_ptr=0x1) at test.c:40\n'
           '#3  0x000055555555b in unit_test_function_outer (unit_test_ptr=0x2) at test.c:90\n')

    def test_innermost_unit_test_frame_names_the_crashed_case(self):
        self.assertEqual(runner.crashed_case(self.GDB), 'test_puts_null')

    def test_frame_zero_without_address_and_no_signal(self):
        self.assertEqual(runner.crashed_case('received signal SIGSEGV\n'
                         '#0  unit_test_function_direct (unit_test_ptr=0x1) at t.c:3\n'), 'direct')
        self.assertIsNone(runner.crashed_case(self.GDB.replace('received signal', 'exited')))

    def run_with(self, rc, name):
        unit = {'id': 'u', 'directory': 'd', 'binary': 'b',
                'cases': [{'id': 'first', 'description': 'First'},
                          {'id': 'test_puts_null', 'description': 'Puts'},
                          {'id': 'later', 'description': 'Later'}]}
        out = 'Unit test: First\nResult: success\n'
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(runner, 'LOGS', Path(tmp)), \
                patch.object(runner, 'native_binary', return_value=Path('/x/b.native')), \
                patch.object(runner, 'run_native_binary', return_value=(out, False, False, rc)), \
                patch.object(runner, 'attribute_crash', return_value=name) as gdb, \
                patch('builtins.print'):
            return runner.run_native(unit), gdb.called

    def test_signal_marks_only_the_attributed_case(self):
        (statuses, complete), _ = self.run_with(-11, 'test_puts_null')
        self.assertEqual(statuses, {'u::first': 'passed', 'u::test_puts_null': 'error',
                                    'u::later': 'not_run'})
        self.assertFalse(complete)

    def test_unattributed_signal_and_plain_exit_fabricate_nothing(self):
        (statuses, _), _ = self.run_with(-11, 'not_registered')
        self.assertEqual(statuses['u::test_puts_null'], 'not_run')
        (statuses, _), called = self.run_with(1, None)
        self.assertFalse(called)
        self.assertNotIn('error', statuses.values())


if __name__ == '__main__':
    unittest.main()
