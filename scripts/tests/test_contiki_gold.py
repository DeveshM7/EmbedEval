"""Contiki gold labels stay separate from Zephyr and from model inputs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import triage_prs as triage


class ContikiGoldTests(unittest.TestCase):
    def test_gold_has_seven_accepts_and_eight_rejects_without_overlap(self):
        with patch.object(triage,'PROJECT','contiki-ng'):
            gold=triage.gold_file();labels=triage.load_gold()
        self.assertEqual(len(gold['accept']),7)
        self.assertEqual(len(gold['reject']),8)
        self.assertEqual(len(labels),15)
        self.assertFalse(set(gold['accept']) & set(gold['reject']))
        with patch.object(triage,'PROJECT','zephyr'):
            self.assertIn('33690',triage.gold_file()['accept'])

    def test_pending_gold_cannot_start_model_calls(self):
        with (patch.object(triage,'PROJECT','contiki-ng'),
              patch.object(triage,'INSTRUCTIONS',triage.INSTRUCTIONS),
              patch.object(triage,'ENRICHED',triage.ENRICHED),
              patch.object(triage,'gold_file',return_value={'_status':'pending'})):
            with self.assertRaisesRegex(SystemExit,'not finalized'):
                triage.cmd_run(SimpleNamespace(project='contiki-ng',profile='qwen38-nvfp4',gold=True,pr=None))

    def test_holds_do_not_score_as_correct_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=root/'score';run.mkdir()
            for pr,verdict in [(255,'accept'),(384,'hold')]:
                (run/f'{pr}.json').write_text(json.dumps({'pr':pr,'verdict':verdict,'profile':'native','oracle':'runtime'}))
                (run/f'{pr}.trace.json').write_text(json.dumps({'seconds':1,'turns':[]}))
            with (patch.object(triage,'PROJECT',triage.PROJECT),
                  patch.object(triage,'INSTRUCTIONS',triage.INSTRUCTIONS),
                  patch.object(triage,'ENRICHED',triage.ENRICHED),
                  patch.object(triage,'RUNS',root)):
                triage.cmd_eval(SimpleNamespace(run_dir=str(run),project='contiki-ng'))
            result=json.loads((run/'scorecard.json').read_text())
            self.assertEqual((result['correct'],result['scored']),(1,2))
            self.assertEqual(result['project'],'contiki-ng')
            self.assertEqual(result['abstentions'],[384])
            row=next(r for r in result['rows'] if r['pr']==384)
            self.assertEqual(row['ok'],'✗')
            self.assertIn('plan validation required',result['rows'][0]['secondary'])
