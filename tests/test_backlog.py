import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from unity_rag import backlog


class BacklogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve(); self.game=self.base/'Game'; self.workspace=self.base/'Knowledge'; self.workspace.mkdir()
        self.path=self.game/'Assets'/'A.cs';self.path.parent.mkdir(parents=True);self.path.write_text('const int cap = 8;\n')
        raw=self.path.read_bytes();self.entry={'path':'Assets/A.cs','status':'supported','sha256':hashlib.sha256(raw).hexdigest()}
        self.manifest={'version':1,'task_version':'v1','items':[{'id':'case','kind':'rag_eval','question':'What is cap?',
           'expected_sources':['Assets/A.cs'],'sources':[{'path':'Assets/A.cs','start_line':1,'end_line':1}],
           'required_facts':[{'id':'f','statement':'Cap is 8.','source_path':'Assets/A.cs','quote':'const int cap = 8;','exact_values':['8']}],
           'setup_requirements':[],'behavior_assertions':[]}]}
        self.file=self.base/'manifest.json';self.file.write_text(json.dumps(self.manifest))

    def prepare(self):
        with patch.object(backlog,'status',return_value={'project':str(self.game),'entries':[self.entry]}):
            return backlog.prepare(self.workspace,'batch',self.file,budget_usd='1',credit_stop='2099-01-01T00:00:00Z',key_stop='2099-01-01T00:00:00Z')

    def provider(self):
        p=Mock();p.last_usage={'input_tokens':1,'output_tokens':1}
        draft={'id':'case','kind':'rag_eval','question':'What is cap?','answer':'Cap is 8.',
               'facts':[{'id':'f','citation':{'path':'Assets/A.cs','start_line':1,'end_line':1}}],
               'failure_classification':{'setup':'','behavior':''},'limitations':['static']}
        review={'id':'case','verdict':'supported','issues':[],'checked_fact_ids':['f']}
        p.generate.side_effect=[{'items':[draft]},{'items':[review]}]
        return p

    def test_explicit_remote_and_human_review_and_resume(self):
        self.prepare(); p=self.provider(); factory=Mock(return_value=p)
        with self.assertRaisesRegex(ValueError,'authorization'):
            backlog.run(self.workspace,'batch',api_key_file='unused',provider_factory=factory)
        result=backlog.run(self.workspace,'batch',api_key_file='unused',allow_remote=True,provider_factory=factory)
        self.assertEqual(result['items']['case']['status'],'needs_human_review')
        self.assertEqual(result['items']['case']['local_errors'],[])
        backlog.run(self.workspace,'batch',api_key_file='unused',allow_remote=True,provider_factory=factory)
        self.assertEqual(p.generate.call_count,2)
        self.assertEqual(factory.call_count,1)

    def test_missing_ledger_expiry_and_uncertain_response_fail_closed(self):
        self.prepare(); root=backlog._root(self.workspace,'batch'); (root.parent/'spend.sqlite3').unlink()
        with self.assertRaisesRegex(ValueError,'ledger'):
            backlog.run(self.workspace,'batch',api_key_file='unused',allow_remote=True,provider_factory=Mock())
        with self.assertRaisesRegex(ValueError,'deadline'):
            backlog.prepare(self.workspace,'other',self.file,budget_usd='1',credit_stop='2000-01-01T00:00:00Z',key_stop='2099-01-01T00:00:00Z')

    def test_other_batch_cannot_choose_a_new_budget_or_replace_ledger(self):
        self.prepare()
        with patch.object(backlog,'status',return_value={'project':str(self.game),'entries':[self.entry]}):
            with self.assertRaisesRegex(ValueError,'fixed'):
                backlog.prepare(self.workspace,'other',self.file,budget_usd='2',credit_stop='2099-01-01T00:00:00Z',key_stop='2099-01-01T00:00:00Z')
        ledger=backlog._root(self.workspace,'batch').parent/'spend.sqlite3'
        ledger.write_bytes(b'')
        with self.assertRaisesRegex(ValueError,'corrupt'):
            backlog.run(self.workspace,'batch',api_key_file='unused',allow_remote=True,provider_factory=Mock())

    def test_changed_source_stops_before_provider(self):
        self.prepare();self.path.write_text('changed')
        factory=Mock()
        with self.assertRaisesRegex(ValueError,'Source changed'):
            backlog.run(self.workspace,'batch',api_key_file='unused',allow_remote=True,provider_factory=factory)
        factory.assert_not_called()

    def test_uncertain_generation_never_retries(self):
        self.prepare()
        provider=Mock();provider.generate.side_effect=TimeoutError('uncertain')
        factory=Mock(return_value=provider)
        with self.assertRaises(TimeoutError):
            backlog.run(self.workspace,'batch',api_key_file='unused',allow_remote=True,provider_factory=factory)
        with self.assertRaisesRegex(ValueError,'no automatic retry'):
            backlog.run(self.workspace,'batch',api_key_file='unused',allow_remote=True,provider_factory=factory)
        self.assertEqual(provider.generate.call_count,1)
