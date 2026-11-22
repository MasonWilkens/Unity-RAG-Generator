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
        self.snapshot = {'inputs': {'discovery': 'frozen', 'code': 'a' * 64, 'assets': 'b' * 64}}
        self.addCleanup(patch.stopall)
        self.snapshot_mock = patch('unity_rag.answer_evaluation.retrieval_status', return_value=self.snapshot).start()
        patch('unity_rag.answer_evaluation.status', return_value={}).start()
        self.source_check = patch('unity_rag.answer_evaluation.verify_sources').start()

    def tearDown(self):
        self.temp.cleanup()

    def test_settings_propagate_and_retrieval_is_separate_from_citation(self):
        answer = {'claims': [], 'evidence': {'E1': {'entity_id': 'wanted'}},
                  'snapshot': self.snapshot['inputs'],
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
        self.assertEqual(len(report['implementation_sha256']), 64)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['snapshot'], self.snapshot['inputs'])
        self.source_check.assert_called_once()

    def test_failure_retains_effective_settings(self):
        with patch('unity_rag.answer_evaluation.ask', side_effect=ValueError('Invalid range')):
            report = evaluate_answers('workspace', self.dataset, limit=2, expand=0)
        self.assertEqual(report['valid_outputs'], 0)
        self.assertEqual(report['results'][0]['settings']['limit'], 2)
        self.assertEqual(report['results'][0]['error'], 'Invalid range')

    def answer(self):
        return {'claims': [], 'evidence': {'E1': {'entity_id': 'wanted'}},
                'snapshot': self.snapshot['inputs'], 'grounding_review': {'removed_claims': 0},
                'model_calls': 1, 'context_budget': {'used_chars': 100},
                'status': 'insufficient_evidence', 'runtime_required': False}

    def test_invalid_or_duplicate_question_ids_fail_before_generation(self):
        for questions in ([{'id': 'same', 'question': 'A'}, {'id': 'same', 'question': 'B'}],
                          [{'id': '', 'question': 'A'}], [{'id': 'a', 'question': ''}], [None]):
            self.dataset.write_text(json.dumps({'schema_version': 1, 'questions': questions}))
            with patch('unity_rag.answer_evaluation.ask') as ask, self.assertRaises(ValueError):
                evaluate_answers('workspace', self.dataset)
            ask.assert_not_called()

    def test_changed_dataset_retains_outputs_but_invalidates_run(self):
        def mutate(*args, **kwargs):
            self.dataset.write_text(self.dataset.read_text() + '\n')
            return self.answer()
        with patch('unity_rag.answer_evaluation.ask', side_effect=mutate):
            report = evaluate_answers('workspace', self.dataset)
        self.assertEqual(report['status'], 'invalid')
        self.assertEqual(report['valid_outputs'], 1)
        self.assertIn('Dataset changed during evaluation.', report['validation_errors'])

    def test_changed_implementation_and_snapshot_invalidate_run(self):
        self.snapshot_mock.side_effect = [self.snapshot, {'inputs': {**self.snapshot['inputs'], 'code': 'c' * 64}}]
        with patch('unity_rag.answer_evaluation.ask', return_value=self.answer()), patch(
                'unity_rag.answer_evaluation._code_hash', side_effect=['a' * 64, 'b' * 64]):
            report = evaluate_answers('workspace', self.dataset)
        self.assertEqual(report['status'], 'invalid')
        self.assertEqual(len(report['validation_errors']), 2)
        self.assertEqual(report['implementation_sha256'], 'a' * 64)

    def test_changed_provider_and_stale_earlier_evidence_invalidate_run(self):
        class Provider:
            config = {'provider': 'scripted-test', 'model': 'initial'}
        provider = Provider()
        def mutate(*args, **kwargs):
            provider.config['model'] = 'changed'
            return self.answer()
        self.source_check.side_effect = ValueError('Source changed since discovery')
        with patch('unity_rag.answer_evaluation.ask', side_effect=mutate):
            report = evaluate_answers('workspace', self.dataset, provider=provider)
        self.assertEqual(report['status'], 'invalid')
        self.assertEqual(report['provider']['model'], 'initial')
        self.assertEqual(len(report['validation_errors']), 2)
        self.assertEqual(len(report['results']), 1)

    def test_answer_from_wrong_snapshot_is_a_retained_failed_case(self):
        value = {**self.answer(), 'snapshot': {**self.snapshot['inputs'], 'code': 'c' * 64}}
        with patch('unity_rag.answer_evaluation.ask', return_value=value):
            report = evaluate_answers('workspace', self.dataset)
        self.assertEqual(report['valid_outputs'], 0)
        self.assertEqual(report['results'][0]['question'], 'Question')

    def test_cli_returns_failure_for_invalid_or_failed_evaluations(self):
        from contextlib import redirect_stdout
        import io
        from unity_rag.cli import main
        for report in ({'status': 'invalid', 'valid_outputs': 1, 'questions': 1},
                       {'status': 'completed', 'valid_outputs': 0, 'questions': 1}):
            with patch('unity_rag.cli.provider_from_args', return_value=None), patch(
                    'unity_rag.cli.evaluate_answers', return_value=report), redirect_stdout(io.StringIO()), patch(
                    'sys.argv', ['unity-rag', 'evaluate-answers', str(self.dataset), '--workspace', 'workspace']):
                self.assertEqual(main(), 1)
