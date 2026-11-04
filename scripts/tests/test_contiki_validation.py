"""A declared pass-to-pass case must pass before the production fix too."""
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
            with (patch.object(pipeline.instances, 'instance_dir', return_value=root),
                  patch.object(pipeline.instances, 'load_metadata', return_value={}),
                  patch.object(pipeline.au, 'instance_image', return_value=image),
                  patch.object(pipeline.au, 'work_root', return_value=root / 'work'),
                  patch.object(pipeline.au, 'recipe_fingerprint', return_value='control'),
                  patch.object(pipeline.au, 'exec_args', return_value=[]),
                  patch.object(pipeline.subprocess, 'run', side_effect=results),
                  patch.object(validate_instance, 'compile_attribution', return_value='fix_api'),
                  patch.object(pipeline, 'config_attribution', return_value='config error'),
                  patch.object(validate_instance, '_finish', side_effect=lambda iid, report: report)):
                result = pipeline.validate('contiki-ng__contiki-ng-1', patch_override=fix)
            self.assertFalse(result['valid'])
            self.assertEqual(result['reason'], 'oracle or required named case evidence missing')
