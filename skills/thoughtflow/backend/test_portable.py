"""Portable host sync, local outlines and stable summary hand-off using synthetic data."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.imports import write_turns, write_turn, ImportedReader
from backend.organize import Organizer, source_key, _run_codex

ROOT = Path(__file__).resolve().parent.parent

class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'THOUGHT_FLOW_DATA_DIR': self.temp.name})
        self.env.start()
        self.turns = [{'turnId':'t%d'%i,'question':'Question %d'%i,'answer':'Example answer. '*300,'state':'completed'} for i in range(58)]
        self.batch = {'source':'WorkBuddy','session':'synthetic-58','title':'Example with 58 turns','turns':self.turns}

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def test_58_turn_batch_and_outline_without_codex_or_model(self):
        start = time.perf_counter()
        result = write_turns(self.batch)
        ident = result['conversationId']
        path = Path(self.temp.name)/'imports'/(ident+'.json')
        _, nodes = ImportedReader(path).snapshot()
        with patch('backend.organize.find_codex', side_effect=AssertionError('No CLI needed')):
            organizer = Organizer(Path(self.temp.name)/'organized', runner=lambda _:self.fail('No model needed'))
            job = organizer.start({'id':ident,'title':self.batch['title']}, nodes, source_key(nodes), mode='outline')
            self.assertEqual(organizer.start({'id':ident}, nodes, source_key(nodes), mode='outline')['id'], job['id'])
        self.assertEqual(job['engine'], 'local-outline')
        self.assertEqual(job['turnCount'], 58)
        self.assertEqual([i for s in job['sections'] for i in s['sourceTurnIds']], [t['id'] for t in nodes])
        self.assertIn('[Excerpt', job['markdown'])
        self.assertEqual(json.loads(path.read_text())['nodes'][-1]['answer'], self.turns[-1]['answer'])
        elapsed = time.perf_counter()-start
        print('\n58-turn synthetic batch + local outline: %.3fs (%d input characters)' % (elapsed,sum(len(n['answer']) for n in nodes)))
        self.assertLess(elapsed, 5, 'Local outline should not wait for a model')

    def test_bad_batch_is_atomic_and_sessions_remain_separate(self):
        first=write_turns(self.batch); path=Path(self.temp.name)/'imports'/(first['conversationId']+'.json'); original=path.read_bytes()
        broken=copy.deepcopy(self.batch); broken['turns'][0]['answer']='Changed'; broken['turns'][-1]['state']='running'
        with self.assertRaises(ValueError): write_turns(broken)
        self.assertEqual(path.read_bytes(), original)
        other=copy.deepcopy(self.batch);other['session']='another-session'
        self.assertNotEqual(write_turns(other)['conversationId'], first['conversationId'])
        single={k:v for k,v in self.batch.items() if k!='turns'} | self.turns[0]
        self.assertEqual(write_turn(single)['conversationId'], first['conversationId'])

    def test_host_summary_requires_current_snapshot_when_supplied(self):
        small=copy.deepcopy(self.batch)
        for turn in small['turns']: turn['answer']='Example answer.'
        result=write_turns(small); ident=result['conversationId']
        prepared=subprocess.run([sys.executable,str(ROOT/'scripts/prepare_summary.py'),'--thread',ident],capture_output=True,text=True,check=True)
        data=json.loads(prepared.stdout)
        self.assertEqual(len(data['input']['sources']),58)
        summary={'conversationId':ident,'sourceKey':data['sourceKey'],'result':{'title':'Notes','overview':'Example','sections':[{'heading':'Topic','body':'Example answer.','sources':['T0001']}],'openQuestions':[]}}
        command=[sys.executable,str(ROOT/'scripts/publish_summary.py')]
        good=subprocess.run(command,input=json.dumps(summary),capture_output=True,text=True)
        self.assertEqual(good.returncode,0,good.stderr)
        changed=copy.deepcopy(self.batch);changed['turns'][0]['answer']='Updated';write_turns(changed)
        stale=subprocess.run(command,input=json.dumps(summary),capture_output=True,text=True)
        self.assertNotEqual(stale.returncode,0)
        self.assertIn('Conversation changed',stale.stderr)

    def test_ai_uses_fast_preset_and_bounded_timeout(self):
        def fake_run(command, **kwargs):
            self.assertIn('model_reasoning_effort="low"',command)
            self.assertEqual(kwargs['timeout'],120)
            output=Path(command[command.index('--output-last-message')+1]);output.write_text('{}')
            return subprocess.CompletedProcess(command,0,stderr='')
        with patch('backend.organize.find_codex',return_value='codex'), patch('backend.organize.subprocess.run',side_effect=fake_run):
            self.assertEqual(_run_codex('{}'),{})

if __name__=='__main__':unittest.main()
