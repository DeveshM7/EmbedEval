"""A declared pass-to-pass case must pass before the production fix too; a
leak finding stops validation before any test; a VALID instance has five
identical before runs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import contiki_ng_pipeline as pipeline
import validate_instance


class ContikiValidationTest(unittest.TestCase):
    def test_failing_pass_to_pass_case_cannot_validate(self):
        self.check_invalid_pass_to_pass('runtime', 1, '  failed unit::fix\n  failed unit::stable\n')

    def test_compile_oracle_cannot_claim_unexecuted_pass_to_pass(self):
        self.check_invalid_pass_to_pass('compile', 2, '')

    def test_config_oracle_cannot_claim_unexecuted_pass_to_pass(self):
        self.check_invalid_pass_to_pass('config', 2, '')

    def test_after_only_guard_is_required_without_claiming_p2p(self):
        self.check_invalid_pass_to_pass('runtime',1,'  failed unit::fix\n',
                                        p2p=[],after_status='  passed unit::fix\n  failed unit::stable\n')

    def check_invalid_pass_to_pass(self, oracle, before_rc, before_status, p2p=None, after_status=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'instance.json').write_text(json.dumps({
                'profile': 'native', 'oracle': oracle,
                'fail_to_pass': ['unit::fix'], 'pass_to_pass': ['unit::stable'] if p2p is None else p2p,
                'after_required':['unit::fix','unit::stable']}))
            (root / 'test_patch.diff').write_text('')
            image = root / 'image.sif'
            image.touch()
            fix = root / 'fix.diff'
            fix.write_text('diff --git a/os/lib/foo.c b/os/lib/foo.c\n')
            def output(status):
                return '--- contiki-ng: results\n' + status + '--- end\n'
            before = output(before_status)
            after = output(after_status if after_status is not None else '  passed unit::fix\n  passed unit::stable\n')
            results = [SimpleNamespace(returncode=rc, stdout=out, stderr='')
                       for rc, out in ((before_rc, before), (0, ''), (0, after))]
            current_details = None
            def exec_args(image, binds, overlay, network=True):
                self.assertFalse(network)    # no step of a validation has a network
                nonlocal current_details
                current_details = next((p for p, target in binds.items()
                                        if target == '/testbed/.embedeval/logs'), None)
                return []
            outputs = iter(results)
            def run(*args, **kwargs):
                result = next(outputs)
                if current_details is not None and args[0] == ['run_tests']:
                    (current_details / 'unit.run.log').write_text(result.stdout)
                return result
            with (patch.object(pipeline.instances, 'instance_dir', return_value=root),
                  patch.object(pipeline.instances, 'load_metadata', return_value={}),
                  patch.object(pipeline.au, 'instance_image', return_value=image),
                  patch.object(pipeline.au, 'work_root', return_value=root / 'work'),
                  patch.object(pipeline.au, 'recipe_fingerprint', return_value='control'),
                  patch.object(pipeline.au, 'exec_args', side_effect=exec_args),
                  patch.object(pipeline.subprocess, 'run', side_effect=run),
                  patch.object(pipeline, 'leak_check', return_value={'ok': True, 'findings': [], 'network': 'isolated'}),
                  patch.object(pipeline, 'compile_attestation', return_value=({'unit': {'symbols': ['fix_api']}}, None)),
                  patch.object(pipeline, 'config_attribution', return_value='config error'),
                  patch.object(validate_instance, '_finish', side_effect=lambda iid, report: report)):
                result = pipeline.validate('contiki-ng__contiki-ng-1', patch_override=fix)
            self.assertFalse(result['valid'])
            self.assertIn(result['reason'], ("pass_to_pass did not pass before: ['unit::stable']",
                                             'required named cases did not pass with the fix'))
            # Raw logs survive overlay cleanup and each state has its own file.
            self.assertNotEqual(result['before']['details'], result['after']['details'])
            self.assertEqual((Path(result['before']['details']) / 'unit.run.log').read_text(), before)
            self.assertEqual((Path(result['after']['details']) / 'unit.run.log').read_text(), after)


class LeakAndDeterminismTest(unittest.TestCase):
    def validate(self, before_outputs, leak=None):
        """pipeline.validate on a runtime instance whose before runs print
        before_outputs in turn (the first is the initial before run)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'instance.json').write_text(json.dumps({
                'profile': 'native', 'oracle': 'runtime', 'units': [],
                'fail_to_pass': ['unit::fix'], 'pass_to_pass': [], 'after_required': ['unit::fix']}))
            (root / 'test_patch.diff').write_text('')
            image = root / 'image.sif'
            image.touch()
            fix = root / 'fix.diff'
            fix.write_text('diff --git a/os/lib/foo.c b/os/lib/foo.c\n')
            out = lambda status: '--- contiki-ng: results\n  ' + status + ' unit::fix\n--- end\n'
            runs = [(1, out(before_outputs[0])), (0, ''), (0, out('passed')), (0, '')]
            runs += [(1, out(b)) for b in before_outputs[1:]]
            results = iter(SimpleNamespace(returncode=rc, stdout=o, stderr='') for rc, o in runs)
            commands = []
            def run(cmd, **kw):
                commands.append(cmd)
                return next(results)
            with (patch.object(pipeline.instances, 'instance_dir', return_value=root),
                  patch.object(pipeline.instances, 'load_metadata', return_value={}),
                  patch.object(pipeline.au, 'instance_image', return_value=image),
                  patch.object(pipeline.au, 'work_root', return_value=root / 'work'),
                  patch.object(pipeline.au, 'recipe_fingerprint', return_value='control'),
                  patch.object(pipeline.au, 'exec_args', return_value=[]),
                  patch.object(pipeline, 'leak_check', return_value=leak or {'ok': True, 'findings': [], 'network': 'isolated'}),
                  patch.object(pipeline.subprocess, 'run', side_effect=run),
                  patch.object(validate_instance, '_finish', side_effect=lambda iid, report: report)):
                return pipeline.validate('contiki-ng__contiki-ng-1', patch_override=fix), commands

    def test_leak_finding_runs_no_test(self):
        result, commands = self.validate(['failed'], leak={'ok': False, 'findings': ['history: x'], 'network': 'isolated'})
        self.assertFalse(result['valid'])
        self.assertTrue(result['reason'].startswith('leak check: history: x'))
        self.assertEqual(commands, [])

    def test_five_identical_before_runs_validate(self):
        result, _ = self.validate(['failed'] * 5)
        self.assertTrue(result['valid'], result['reason'])
        self.assertEqual(result['determinism'], {'runs': 5, 'ok': True, 'differences': []})

    def test_a_before_run_that_differs_is_not_deterministic(self):
        result, _ = self.validate(['failed', 'failed', 'error', 'failed', 'failed'])
        self.assertFalse(result['valid'])
        self.assertEqual(result['determinism']['differences'][0]['run'], 3)

