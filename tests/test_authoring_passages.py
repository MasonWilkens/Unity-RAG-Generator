import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.discovery import scan
from unity_rag.source_evaluation import evaluate_sources


class AuthoringPassageEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project, self.work, self.dataset = root / 'project', root / 'index', root / 'cases.json'
        (self.project / 'Assets').mkdir(parents=True)
        (self.project / 'ProjectSettings').mkdir()
        self.source = self.project / 'Assets' / 'Guide.md'
        self.source.write_text('header\nreviewed unique passage\ntail\n', encoding='utf-8')
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        scan(self.project, self.work)
        self.case = {'id': 'case', 'question': 'Which passage?', 'expected_sources': ['Assets/Guide.md'],
                     'reviewed_source_sha256': {'Assets/Guide.md': self.digest},
                     'required_passages': [{'path': 'Assets/Guide.md', 'quote': 'reviewed unique passage',
                                            'start_line': 2, 'end_line': 2}]}
        self.hit = {'text': 'reviewed unique passage', 'sources': [{'path': 'Assets/Guide.md',
                    'start_line': 1, 'end_line': 3}]}
        self.result = {'results': [self.hit], 'context': [], 'source_freshness': [
            {'path': 'Assets/Guide.md', 'status': 'current', 'sha256': self.digest}]}

    def evaluate(self):
        self.dataset.write_text(json.dumps({'schema_version': 1, 'cases': [self.case]}), encoding='utf-8')
        with patch('unity_rag.source_evaluation.retrieve', return_value=self.result):
            return evaluate_sources(self.work, self.dataset)

    def test_current_exact_quote_and_range_pass(self):
        report = self.evaluate()
        row = report['cases'][0]
        self.assertEqual(report['all_required_passages_current'], 1)
        self.assertTrue(row['required_passages'][0]['matched_current'])

    def test_same_path_wrong_text_or_range_does_not_pass(self):
        self.hit['text'] = 'header only'
        self.assertFalse(self.evaluate()['cases'][0]['required_passages'][0]['returned'])
        self.hit['text'] = 'reviewed unique passage'
        self.hit['sources'][0]['start_line'] = 3
        self.assertFalse(self.evaluate()['cases'][0]['required_passages'][0]['returned'])
        self.hit['sources'] = [{'path': 'Assets/Other.md', 'start_line': 1, 'end_line': 3}]
        self.assertFalse(self.evaluate()['cases'][0]['required_passages'][0]['returned'])

    def test_stale_source_and_hash_mismatch_are_not_promoted(self):
        self.source.write_text('changed source', encoding='utf-8')
        check = self.evaluate()['cases'][0]['required_passages'][0]
        self.assertEqual(check['review_status'], 'stale')
        self.assertTrue(check['returned'])
        self.assertFalse(check['matched_current'])
        self.source.write_text('header\nreviewed unique passage\ntail\n', encoding='utf-8')
        self.case['reviewed_source_sha256']['Assets/Guide.md'] = 'a' * 64
        check = self.evaluate()['cases'][0]['required_passages'][0]
        self.assertEqual(check['review_status'], 'snapshot_mismatch')
        self.assertFalse(check['matched_current'])

    def test_invalid_review_and_noncurrent_retrieval_fail_closed(self):
        self.case['required_passages'][0]['start_line'] = 1
        self.case['required_passages'][0]['end_line'] = 1
        self.assertEqual(self.evaluate()['cases'][0]['required_passages'][0]['review_status'], 'invalid_review_quote')
        self.case['required_passages'][0]['start_line'] = 2
        self.case['required_passages'][0]['end_line'] = 2
        self.result['source_freshness'][0]['status'] = 'stale'
        self.assertFalse(self.evaluate()['cases'][0]['required_passages'][0]['matched_current'])

    def test_unversioned_or_malformed_passage_is_rejected(self):
        self.case['reviewed_source_sha256'] = {}
        with self.assertRaisesRegex(ValueError, 'reviewed SHA-256'):
            self.evaluate()
        self.case['reviewed_source_sha256'] = {'Assets/Guide.md': self.digest}
        self.case['required_passages'][0]['start_line'] = True
        with self.assertRaisesRegex(ValueError, 'line range'):
            self.evaluate()

    def test_source_change_during_second_read_is_reported_stale(self):
        with patch('unity_rag.code_map._read_snapshot_file', side_effect=ValueError('source changed')):
            check = self.evaluate()['cases'][0]['required_passages'][0]
        self.assertEqual(check['review_status'], 'stale')
        self.assertFalse(check['matched_current'])
    def test_sensitive_path_never_read(self):
        path = 'Assets/.env/creds.secret'
        self.case['expected_sources'] = [path]
        self.case['reviewed_source_sha256'] = {path: 'a' * 64}
        self.case['required_passages'] = [{'path': path, 'quote': 'do not read', 'start_line': 1, 'end_line': 1}]
        with patch('unity_rag.code_map._read_snapshot_file', side_effect=AssertionError('secret read')):
            check = self.evaluate()['cases'][0]['required_passages'][0]
        self.assertEqual(check['review_status'], 'excluded')
        self.assertFalse(check['matched_current'])

class AuthoringDatasetContractTests(unittest.TestCase):
    def test_reviewed_sets_are_versioned_disjoint_and_passage_backed(self):
        # Public contract controls use authored source, not private-project labels.
        root = Path(__file__).parent / 'fixtures' / 'datasets'
        project = root.parent / 'greenhouse_project'
        ids = set()
        for name, split in [('authoring-development.json', 'development'),
                            ('authoring-heldout.json', 'heldout-same-project')]:
            data = json.loads((root / name).read_text(encoding='utf-8-sig'))
            self.assertEqual((data['schema_version'], data['split']), (1, split))
            self.assertTrue(data['cases'])
            for case in data['cases']:
                self.assertNotIn(case['id'], ids)
                ids.add(case['id'])
                self.assertTrue(case['question'])
                self.assertTrue(case['required_passages'])
                for passage in case['required_passages']:
                    self.assertIn(passage['path'], case['expected_sources'])
                    self.assertRegex(case['reviewed_source_sha256'][passage['path']], r'^[0-9a-f]{64}$')
                    self.assertTrue(passage['quote'].strip())
                    self.assertGreaterEqual(passage['start_line'], 1)
                    self.assertGreaterEqual(passage['end_line'], passage['start_line'])
                    raw = (project / passage['path']).read_bytes()
                    self.assertEqual(hashlib.sha256(raw).hexdigest(),
                                     case['reviewed_source_sha256'][passage['path']])
                    lines = raw.decode('utf-8').splitlines()
                    self.assertIn(passage['quote'], '\n'.join(
                        lines[passage['start_line']-1:passage['end_line']]))

if __name__ == '__main__':
    unittest.main()
