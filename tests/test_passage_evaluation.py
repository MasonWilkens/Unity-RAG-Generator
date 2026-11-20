import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.discovery import scan
from unity_rag.passage_evaluation import evaluate_passages


class PassageEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project, self.work, self.dataset = root/'project', root/'index', root/'cases.json'
        (self.project/'Assets').mkdir(parents=True)
        (self.project/'ProjectSettings').mkdir()
        self.path = self.project/'Assets/Guide.md'
        self.path.write_text('header\ncorrect behavior\nother valid explanation\nmisleading old text\n', encoding='utf-8')
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        scan(self.project, self.work)
        self.case = {'id': 'case', 'question': 'Which behavior?', 'passage_groups': [[
            self.passage('correct behavior', 2), self.passage('other valid explanation', 3)]],
            'negative_passages': [self.passage('misleading old text', 4)],
            'reviewed_source_sha256': {'Assets/Guide.md': self.digest}}
        self.record = {'text': 'correct behavior', 'sources': [
            {'path': 'Assets/Guide.md', 'start_line': 2, 'end_line': 2, 'sha256': self.digest}]}
        self.retrieval = {'results': [self.record], 'context': [], 'candidate_count': 1,
                          'source_freshness': [{'path': 'Assets/Guide.md', 'status': 'current', 'sha256': self.digest}]}
        self.answer = {'evidence': {'E1': self.record}, 'context_budget': {}}

    def passage(self, quote, line):
        return {'path': 'Assets/Guide.md', 'quote': quote, 'start_line': line, 'end_line': line}

    def evaluate(self):
        self.dataset.write_text(json.dumps({'schema_version': 2, 'cases': [self.case]}), encoding='utf-8')
        with patch('unity_rag.passage_evaluation.retrieve', return_value=self.retrieval), \
             patch('unity_rag.passage_evaluation.ask', return_value=self.answer), \
             patch('unity_rag.passage_evaluation.retrieval_status', return_value={'inputs': {'fixed': True}}):
            return evaluate_passages(self.work, self.dataset)

    def test_one_acceptable_alternative_passes_both_stages(self):
        report = self.evaluate()
        self.assertEqual(report['summary']['packed']['groups_found'], 1)
        self.assertEqual(report['summary']['packed']['passed'], 1)
        self.assertEqual(report['settings']['retrieval_context_chars'], 1000000)

    def test_same_file_without_packed_quote_does_not_pass(self):
        self.answer['evidence'] = {'E1': {**self.record, 'text': 'header'}}
        report = self.evaluate()
        self.assertEqual(report['summary']['retrieved']['passed'], 1)
        self.assertEqual(report['summary']['packed']['passed'], 0)

    def test_negative_passage_disqualifies_an_otherwise_valid_result(self):
        self.record['text'] = 'correct behavior\nother valid explanation\nmisleading old text'
        self.record['sources'][0]['end_line'] = 4
        report = self.evaluate()
        self.assertEqual(report['summary']['packed']['groups_found'], 1)
        self.assertEqual(report['summary']['packed']['negative_passages_found'], 1)
        self.assertEqual(report['summary']['packed']['passed'], 0)

    def test_stale_review_labels_stop_evaluation(self):
        self.path.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'stale or invalid'):
            self.evaluate()

    def test_insufficient_outcome_requires_no_returned_evidence(self):
        self.case.update(passage_groups=[], negative_passages=[], expected_outcome='insufficient_evidence')
        self.assertEqual(self.evaluate()['summary']['packed']['passed'], 0)
        self.retrieval['results'] = []
        self.answer['evidence'] = {}
        self.assertEqual(self.evaluate()['summary']['packed']['passed'], 1)
