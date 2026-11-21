"""Offline contract checks; CI never downloads the real evaluation repositories."""
import hashlib
import json
from pathlib import Path
import unittest

from unity_rag.quality_benchmark import _plan
from unity_rag.passage_evaluation import passage_labels
from unity_rag.quality_reporting import summarize


class PublicQualityCorpusTests(unittest.TestCase):
    def test_published_questions_preserve_splits_review_identity_and_provenance(self):
        root = Path(__file__).resolve().parents[1] / 'benchmarks/quality_v1'
        self.assertNotIn(b'\r\n', (root / 'manifest.json').read_bytes(), 'Freeze published manifests with LF bytes.')
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        _plan(manifest, 'quality-benchmark')
        total, held_out = 0, 0
        for project in manifest['projects']:
            self.assertRegex(project['revision'], r'^[0-9a-f]{40}$')
            self.assertNotIn(b'\r\n', (root / project['dataset']).read_bytes(), 'Freeze published labels with LF bytes.')
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

    def check_query_candidate(self, candidate, previous_candidate):
        root = Path(__file__).resolve().parents[1] / 'benchmarks/quality_v1'
        report = json.loads((root / candidate).read_bytes())
        for relative, comparison_key in (('baseline.json', 'comparison'),
                                          (previous_candidate, 'previous_candidate_comparison')):
            original_bytes = (root / relative).read_bytes()
            original = json.loads(original_bytes)
            comparison = report[comparison_key]
            self.assertEqual(comparison['baseline_sha256'], hashlib.sha256(original_bytes).hexdigest())
            for key in ('lock_sha256', 'manifest_sha256', 'settings', 'targets'):
                self.assertEqual(report[key], original[key])
            old = {p['name']: p for p in original['projects']}
            self.assertEqual(set(old), {p['name'] for p in report['projects']})
            improved, regressed = [], []
            for project in report['projects']:
                previous = old[project['name']]
                for key in ('repository', 'revision', 'split', 'source_sha256', 'dataset_sha256', 'label_review'):
                    self.assertEqual(project[key], previous[key])
                # This batch changes queries only; the previous candidate's
                # immutable snapshot is reused rather than rebuilding inputs.
                if comparison_key == 'previous_candidate_comparison':
                    self.assertEqual(project['snapshot'], previous['snapshot'])
                old_cases = {c['id']: c for c in previous['cases']}
                self.assertEqual(set(old_cases), {c['id'] for c in project['cases']})
                for case in project['cases']:
                    prior = old_cases[case['id']]
                    for key in ('question', 'expected_outcome', 'category'):
                        self.assertEqual(case[key], prior[key])
                    identity = [project['name'], case['id']]
                    if prior['failures'] and not case['failures']:
                        improved.append(identity)
                    if not prior['failures'] and case['failures']:
                        regressed.append(identity)
            self.assertEqual(comparison['improved_cases'], improved)
            self.assertEqual(comparison['regressed_cases'], regressed)
        cases = [case for p in report['projects'] for case in p['cases']]
        self.assertEqual(report['summary'], summarize(cases))
        for project in report['projects']:
            self.assertEqual(project['summary'], summarize(project['cases']))
        for category, summary in report['categories'].items():
            self.assertEqual(summary, summarize([c for c in cases if c['category'] == category]))
        for split, summary in report['splits'].items():
            calculated = summarize([c for p in report['projects'] if p['split'] == split for c in p['cases']])
            self.assertEqual({key: summary[key] for key in calculated}, calculated)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['validation_errors'], [])
        self.assertFalse(report['production_gate_passed'])
        self.assertEqual(report['answer_quality']['status'], 'not_evaluated')

    def test_multipart_candidate_preserves_inputs_counts_and_both_comparisons(self):
        self.check_query_candidate('candidates/multipart-code-v1.json', 'candidates/literal-assets-v1.json')

    def test_cross_file_candidate_preserves_inputs_counts_and_both_comparisons(self):
        self.check_query_candidate('candidates/cross-file-v1.json', 'candidates/multipart-code-v1.json')

    def test_literal_asset_candidate_preserves_frozen_contract_and_comparison(self):
        root = Path(__file__).resolve().parents[1] / 'benchmarks/quality_v1'
        baseline_bytes = (root / 'baseline.json').read_bytes()
        baseline = json.loads(baseline_bytes)
        report = json.loads((root / 'candidates/literal-assets-v1.json').read_bytes())
        self.assertEqual(report['comparison']['baseline_sha256'], hashlib.sha256(baseline_bytes).hexdigest())
        for key in ('lock_sha256', 'manifest_sha256', 'settings', 'targets'):
            self.assertEqual(report[key], baseline[key])
        old = {p['name']: p for p in baseline['projects']}
        improved, regressed = [], []
        for project in report['projects']:
            original = old[project['name']]
            for key in ('repository', 'revision', 'split', 'source_sha256', 'dataset_sha256', 'label_review'):
                self.assertEqual(project[key], original[key])
            old_cases = {c['id']: c for c in original['cases']}
            self.assertEqual(set(old_cases), {c['id'] for c in project['cases']})
            for case in project['cases']:
                previous = old_cases[case['id']]
                for key in ('question', 'expected_outcome', 'category'):
                    self.assertEqual(case[key], previous[key])
                identity = [project['name'], case['id']]
                if previous['failures'] and not case['failures']:
                    improved.append(identity)
                if not previous['failures'] and case['failures']:
                    regressed.append(identity)
        self.assertEqual(report['comparison']['improved_cases'], improved)
        self.assertEqual(report['comparison']['regressed_cases'], regressed)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['validation_errors'], [])
        self.assertFalse(report['production_gate_passed'])
        self.assertEqual(report['answer_quality']['status'], 'not_evaluated')


if __name__ == '__main__':
    unittest.main()
