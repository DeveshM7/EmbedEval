"""Control answers in reporting must never become model instructions."""
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
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

    def test_instructions_are_only_for_the_model(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / 'docs/contiki_ng_triage.md').read_text()
        gold = json.loads((root / 'triage/gold-contiki-ng.json').read_text())
        numbers = set(gold['accept']) | set(gold['reject'])
        self.assertTrue(numbers)
        for n in numbers:
            self.assertNotRegex(text, rf'#{n}\b')
        for marker in ('/scratch/', 'zhan5136', 'results (', 'pilot', 'Status:'):
            self.assertNotIn(marker, text)


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


class StrictStatementTests(unittest.TestCase):
    REC = {'number': 9,
           'production_diff': '--- a/os/net/ip64.c\n+++ b/os/net/ip64.c\n'
                              '-  if(len > BUFSIZE) {\n+  if(payload_len + HDR_LEN > avail_len) {\n'
                              '+    return ip64_reject(pkt);\n',
           'test_patch': '+  r = ip64_reject(&short_pkt);\n+  CHECK(r == 0);\n'}

    def check(self, statement, review=None):
        return triage_prs.check_statement({'pr': 9, 'problem_statement': statement, 'causal_chain': 'c',
                                           'change_type': 'bugfix'}, self.REC, review)

    def test_fix_identifiers_the_tests_do_not_use_are_issues(self):
        self.assertEqual(triage_prs.fix_identifiers('It compares against BUFSIZE, not avail_len.', self.REC),
                         ['BUFSIZE', 'avail_len'])
        # an interface named as the tests use it is allowed, in any case
        self.assertEqual(triage_prs.fix_identifiers('IP64_REJECT and ip64_reject() accept it.', self.REC), [])
        self.assertTrue(any('names code of the fix' in i for i in self.check('Overflows BUFSIZE.')))

    def test_review_runs_last_and_its_phrases_become_the_issue(self):
        seen = []
        def review(s):
            seen.append(s)
            return ['must validate the length against the bytes received']
        self.assertTrue(any('names code' in i for i in self.check('Overflows BUFSIZE.', review)))
        self.assertEqual(seen, [])               # mechanical failures do not cost a review
        issues = self.check('It must validate the length against the bytes received.', review)
        self.assertEqual(len(issues), 1)
        self.assertIn('gives the fix away', issues[0])
        self.assertIn('bytes received', issues[0])
        self.assertEqual(self.check('A short packet makes the translator read past it.', lambda s: []), [])

    def test_review_reply_parsing(self):
        def reply(text):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
        replies = iter([reply('no json'), reply('{"gives_away_fix": true, "phrases": []}')])
        fake = SimpleNamespace(completion=lambda **kw: next(replies))
        with patch.dict(sys.modules, {'litellm': fake}):
            trace = {}
            self.assertEqual(triage_prs.review_statement('s', self.REC, {}, trace),
                             ['(the reviewer quoted no phrase)'])
            self.assertEqual(len(trace['reviews']), 2)
            fake.completion = lambda **kw: reply('nothing')
            with self.assertRaisesRegex(ValueError, 'no verdict'):
                triage_prs.review_statement('s', self.REC, {}, {})


class RecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.enriched, self.folder = root / 'enriched', root / 'contiki-ng__contiki-ng-9'
        self.enriched.mkdir()
        (self.enriched / '9.json').write_text(json.dumps(StrictStatementTests.REC))
        self.folder.mkdir()
        self.out = root / 'triage'

    def tearDown(self):
        self.tmp.cleanup()

    def run_one(self, statement, review, answer=None):
        (self.folder / 'metadata.json').write_text(json.dumps({
            'instance_id': 'contiki-ng__contiki-ng-9', 'problem_statement': statement,
            'fail_to_pass': ['u::a'], 'pass_to_pass': []}))
        def converse(messages, rec, check, kw, trace):
            issues = check(answer) if answer else ['no reply']
            return answer, issues
        with (patch.object(triage_prs, 'ENRICHED', self.enriched),
              patch.object(triage_prs, 'review_statement', side_effect=lambda s, *a: review(s)),
              patch.object(triage_prs, 'converse', side_effect=converse),
              patch.object(triage_prs, 'instruction_text', return_value='rules'),
              patch.dict(triage_prs.PROJECTS['contiki-ng'], {'render': lambda rec: 'record'}),
              patch.object(triage_prs, 'PROJECT', 'contiki-ng')):
            r = triage_prs.statement_one(self.folder, 'qwen', {}, 'http://x', self.out, recheck=True)
        return r, json.loads((self.folder / 'metadata.json').read_text())

    def test_passing_statement_is_kept_and_marked(self):
        r, meta = self.run_one('A short packet is read past its end.', lambda s: [])
        self.assertTrue(r['kept'])
        self.assertEqual(meta['problem_statement'], 'A short packet is read past its end.')
        self.assertEqual(meta['statement_check']['version'], triage_prs.STATEMENT_CHECK)
        self.assertEqual(meta['statement_check']['result'], 'kept')

    def test_failing_statement_is_replaced_and_superseded(self):
        good = {'pr': 9, 'problem_statement': 'A short packet is read past its end.',
                'causal_chain': 'c', 'change_type': 'bugfix'}
        r, meta = self.run_one('It ignores avail_len.', lambda s: [], good)
        self.assertTrue(r['ok'] and not r['kept'])
        self.assertEqual(meta['problem_statement'], good['problem_statement'])
        self.assertEqual(meta['superseded_statements'][0]['problem_statement'], 'It ignores avail_len.')
        self.assertEqual(meta['superseded_statements'][0]['provenance'], 'triage verdict')
        self.assertEqual(meta['statement_check']['result'], 'written')

    def test_no_acceptable_replacement_clears_the_statement(self):
        r, meta = self.run_one('It ignores avail_len.', lambda s: ['x'])
        self.assertFalse(r['ok'])
        self.assertEqual(meta['problem_statement'], '')
        self.assertEqual(meta['statement_check']['result'], 'failed')
        self.assertEqual(meta['superseded_statements'][0]['problem_statement'], 'It ignores avail_len.')

    def test_cluster_copy_of_the_check_version_matches(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'cluster'))
        import status
        self.assertEqual(status.STATEMENT_CHECK, triage_prs.STATEMENT_CHECK)

