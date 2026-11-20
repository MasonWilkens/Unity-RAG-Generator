"""Offline contract checks; CI never downloads the real evaluation repositories."""
import hashlib
import json
from pathlib import Path
import unittest

from unity_rag.quality_benchmark import _plan
from unity_rag.passage_evaluation import passage_labels


class PublicQualityCorpusTests(unittest.TestCase):
    def test_published_questions_preserve_splits_review_identity_and_provenance(self):
        root = Path(__file__).resolve().parents[1] / 'benchmarks/quality_v1'
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        _plan(manifest, 'quality-benchmark')
        total, held_out = 0, 0
        for project in manifest['projects']:
            self.assertRegex(project['revision'], r'^[0-9a-f]{40}$')
            dataset = json.loads((root / project['dataset']).read_text(encoding='utf-8'))
            self.assertEqual(dataset['repository'], project['repository'])
            self.assertEqual(dataset['revision'], project['revision'])
            self.assertEqual(dataset['split'], project['split'])
            self.assertEqual(dataset['label_review']['kind'], 'agent')
            self.assertFalse(project['previously_used_for_tuning'])
            cases = dataset['cases']
            total += len(cases)
            held_out += len(cases) if project['split'] == 'held-out' else 0
            self.assertEqual(len(cases), len({case['id'] for case in cases}))
            for case in cases:
                labels, _, _, _ = passage_labels(case)
                self.assertTrue(case['category'])
                for label in labels['required_passages']:
                    self.assertRegex(case['reviewed_source_sha256'][label['path']], r'^[0-9a-f]{64}$')
                    self.assertGreaterEqual(label['start_line'], 1)
                    self.assertGreaterEqual(label['end_line'], label['start_line'])
                    self.assertTrue(label['quote'].strip())
            license_text = (root / (project['name'].lower() + '-LICENSE.txt')).read_text(encoding='utf-8')
            self.assertIn('MIT License', license_text)
        self.assertEqual((total, held_out), (60, 20))

    def test_recorded_baseline_binds_exact_published_labels(self):
        root = Path(__file__).resolve().parents[1] / 'benchmarks/quality_v1'
        report = json.loads((root / 'baseline.json').read_text(encoding='utf-8'))
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(report['manifest_sha256'], hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest())
        by_name = {p['name']: p for p in report['projects']}
        for project in manifest['projects']:
            digest = hashlib.sha256((root / project['dataset']).read_bytes()).hexdigest()
            self.assertEqual(by_name[project['name']]['dataset_sha256'], digest)
        self.assertFalse(report['production_gate_passed'])
        self.assertEqual(report['answer_quality']['status'], 'not_evaluated')


if __name__ == '__main__':
    unittest.main()
