import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.answer_evaluation import evaluate_answers


class AnswerEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dataset = Path(self.temp.name)/'questions.json'
        self.dataset.write_text(json.dumps({'schema_version': 1, 'questions': [
            {'id': 'case', 'question': 'Question', 'mode': 'exact', 'expected_entities': ['wanted']}]}))

    def tearDown(self):
        self.temp.cleanup()

    def test_settings_propagate_and_retrieval_is_separate_from_citation(self):
        answer = {'claims': [], 'evidence': {'E1': {'entity_id': 'wanted'}},
                  'grounding_review': {'removed_claims': 0}, 'model_calls': 1,
                  'context_budget': {'used_chars': 123}, 'status': 'insufficient_evidence',
                  'runtime_required': False}
        with patch('unity_rag.answer_evaluation.ask', return_value=answer) as ask:
            report = evaluate_answers('workspace', self.dataset, mode='semantic',
                                      limit=3, expand=0, context_chars=4000, followups=0)
        options = ask.call_args.kwargs
        self.assertEqual(options['mode'], 'exact')
        self.assertEqual(options['limit'], 3)
        self.assertEqual(options['expand'], 0)
        self.assertEqual(options['context_chars'], 4000)
        self.assertEqual(options['followups'], 0)
        result = report['results'][0]
        self.assertEqual(result['settings']['mode'], 'exact')
        self.assertEqual(report['settings']['mode'], 'semantic')
        self.assertEqual(result['checks']['retrieved_expected_entity_coverage'], 1)
        self.assertEqual(result['checks']['expected_entity_coverage'], 0)
        self.assertEqual(result['checks']['evidence_chars'], 123)
        self.assertEqual(len(report['draft_prompt_sha256']), 64)

    def test_failure_retains_effective_settings(self):
        with patch('unity_rag.answer_evaluation.ask', side_effect=ValueError('Invalid range')):
            report = evaluate_answers('workspace', self.dataset, limit=2, expand=0)
        self.assertEqual(report['valid_outputs'], 0)
        self.assertEqual(report['results'][0]['settings']['limit'], 2)
        self.assertEqual(report['results'][0]['error'], 'Invalid range')
