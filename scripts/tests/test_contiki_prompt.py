"""Control answers in reporting must never become model instructions."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import triage_prs


class ContikiPromptTest(unittest.TestCase):
    def test_instructions_exclude_control_results(self):
        doc = Path(__file__).resolve().parents[2] / 'docs/contiki_ng_triage.md'
        with (patch.object(triage_prs, 'PROJECT', 'contiki-ng'),
              patch.object(triage_prs, 'INSTRUCTIONS', doc)):
            prompt = triage_prs.instruction_text()
        self.assertIn('compile_symbols', prompt)
        self.assertIn('fail_to_pass', prompt)
        self.assertNotIn('## Integration proof', prompt)
        for pr in ('#3246', '#3020', '#3195', '#1183', '#1170', '#255',
                   '#3202', '#3261', '#2830', '#384', '#958', '#1769',
                   '#2587', '#3251', '#3057', '#422'):
            self.assertNotIn(pr, prompt)

    def test_missing_boundary_fails_closed(self):
        with (patch.object(triage_prs, 'PROJECT', 'contiki-ng'),
              patch.object(Path, 'read_text', return_value='unbounded report')):
            with self.assertRaisesRegex(ValueError, 'reporting boundary'):
                triage_prs.instruction_text()
