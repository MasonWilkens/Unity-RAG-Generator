import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.evaluation import evaluate


class EvaluationTests(unittest.TestCase):
    def test_relationship_requires_correct_target_and_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            dataset = Path(temp)/'questions.json'
            dataset.write_text(json.dumps({'schema_version': 1, 'split': 'held-out',
                'questions': [{'id': 'link', 'query': 'component', 'mode': 'references',
                    'relevant_entities': ['component'], 'expected_relationship': {
                        'kind': 'script_type', 'target': 'Expected', 'resolution': 'inferred_filename'}}]}))
            wrong = {'source': 'component', 'kind': 'script_type', 'target': 'Wrong',
                     'resolution': 'inferred_filename'}
            right = {**wrong, 'target': 'Expected'}
            with patch('unity_rag.evaluation.retrieve', return_value={'results': [wrong], 'context': [right]}):
                report = evaluate(temp, dataset, expand=1)
            self.assertEqual(report['recall_at_k'], 0)
            self.assertEqual(report['results'][0]['context_recall_at_k'], 1)
            self.assertEqual(report['split'], 'held-out')
            self.assertEqual(len(report['dataset_sha256']), 64)
