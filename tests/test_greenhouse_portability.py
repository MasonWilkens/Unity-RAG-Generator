"""Synthetic second project: local indexing and mock backlog, no real-project claim."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock

from unity_rag import backlog
from unity_rag.source_evaluation import evaluate_sources
from unity_rag.workspace import refresh


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class SyntheticGreenhouseTests(unittest.TestCase):
    def test_reviewed_sources_and_mock_backlog_on_distinct_project(self):
        here = Path(__file__).parent
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); project = root/'Greenhouse'; workspace = root/'Index'
            shutil.copytree(here/'fixtures'/'greenhouse_project', project)
            refresh(workspace, project, semantic=False)
            report = evaluate_sources(workspace, here/'evaluation'/'greenhouse-synthetic-source-v1.json')
            self.assertEqual(report['all_expected_hits'], 2)
            for case in report['cases']:
                self.assertEqual(case['noncurrent_expected_hits'], [])
                self.assertEqual(case['reviewed_hash_mismatch_hits'], [])
                self.assertEqual(case['unversioned_expected_sources'], [])
            manifest = here/'evaluation'/'greenhouse-synthetic-backlog-v1.json'
            prepared = backlog.prepare(workspace, 'synthetic', manifest, budget_usd='1',
                                       credit_stop='2099-01-01T00:00:00Z', key_stop='2099-01-01T00:00:00Z')
            self.assertEqual(prepared['api_calls'], 0)
            provider = Mock(); provider.last_usage = {'input_tokens':1,'output_tokens':1}
            draft = {'id':'minimum','kind':'rag_eval','question':'What is GreenhouseHeat SafeMinimum?',
                     'answer':'SafeMinimum is 12.',
                     'facts':[{'id':'minimum','citation':{'path':'Assets/Heat/GreenhouseHeat.cs','start_line':5,'end_line':5}}],
                     'failure_classification':{'setup':'','behavior':''},'limitations':['synthetic fixture']}
            review = {'id':'minimum','verdict':'supported','issues':[],'checked_fact_ids':['minimum']}
            provider.generate.side_effect = [{'items':[draft]},{'items':[review]}]
            result = backlog.run(workspace,'synthetic',api_key_file='unused',allow_remote=True,
                                 provider_factory=Mock(return_value=provider))
            self.assertEqual(result['items']['minimum']['local_errors'], [])
            self.assertEqual(result['items']['minimum']['status'], 'needs_human_review')
            self.assertEqual(provider.generate.call_count, 2)
