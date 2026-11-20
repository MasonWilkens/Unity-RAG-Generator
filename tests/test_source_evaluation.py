import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unity_rag.source_evaluation import evaluate_sources


class SourceEvaluationTests(unittest.TestCase):
    def test_coverage_does_not_claim_factual_accuracy_and_reports_stale_hits(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); dataset=root/'cases.json'
            dataset.write_text(json.dumps({'schema_version':1,'split':'heldout','cases':[{'id':'x','question':'why?',
                'expected_sources':['A.cs','B.cs'],'required_facts':['a separately reviewed fact']}]}))
            result={'results':[{'sources':[{'path':'A.cs'}]}], 'context':[], 'source_freshness':[{'path':'A.cs','status':'stale'}]}
            with patch('unity_rag.source_evaluation.retrieve',return_value=result):
                report=evaluate_sources(root,dataset)
            self.assertEqual(report['any_expected_hits'],1)
            self.assertEqual(report['all_expected_hits'],0)
            self.assertEqual(report['cases'][0]['noncurrent_expected_hits'],['A.cs'])
            self.assertIn('not factual correctness',report['limitation'])

    def test_reviewed_hash_mismatch_is_reported_even_when_index_is_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); dataset=root/'cases.json'
            dataset.write_text(json.dumps({'schema_version':1,'cases':[{'id':'x','question':'why?',
                'expected_sources':['A.cs'],'reviewed_source_sha256':{'A.cs':'old'}}]}))
            result={'results':[{'sources':[{'path':'A.cs'}]}], 'context':[],
                    'source_freshness':[{'path':'A.cs','status':'current','sha256':'new'}]}
            with patch('unity_rag.source_evaluation.retrieve',return_value=result):
                row=evaluate_sources(root,dataset)['cases'][0]
            self.assertEqual(row['reviewed_hash_mismatch_hits'],['A.cs'])
            self.assertEqual(row['unversioned_expected_sources'],[])
