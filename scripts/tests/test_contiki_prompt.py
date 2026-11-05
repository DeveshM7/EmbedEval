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


class StatementCheckTests(unittest.TestCase):
    REC = {'number': 9, 'production_diff': '+++ b/os/lib/x.c\n+    if(len > sizeof(buf)) { return -1; }\n'}

    def check(self, **v):
        import triage_prs
        return triage_prs.check_statement({'pr': 9, 'problem_statement': 'Long input overflows the buffer.',
                                           'causal_chain': 'c', 'change_type': 'bugfix', **v}, self.REC)

    def test_complete_behavioral_statement_passes(self):
        self.assertEqual(self.check(), [])

    def test_missing_fields_wrong_pr_and_quoted_fix_are_issues(self):
        self.assertTrue(any('causal_chain' in i for i in self.check(causal_chain='')))
        self.assertTrue(any('`pr`' in i for i in self.check(pr=8)))
        self.assertTrue(any('quotes the production fix' in i for i in
                            self.check(problem_statement='Add if(len > sizeof(buf)) { return -1; } to x.c')))

