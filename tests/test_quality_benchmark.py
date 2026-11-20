from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.cli import main
from unity_rag.quality_benchmark import (DEFAULT_SETTINGS, _plan, evaluate_quality_benchmark,
                                         freeze_quality_benchmark, write_quality_report)
from unity_rag.quality_reporting import diagnose, markdown_report, rate, summarize
from unity_rag.workspace import refresh, resolve_workspace


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2), encoding='utf-8')


class QualityReportingTests(unittest.TestCase):
    def test_module_entry_point_preserves_failed_preflight_exit_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.run([sys.executable, '-m', 'unity_rag', 'creation-preflight',
                                     '--project', str(root / 'missing-project'), '--workspace', str(root / 'index')],
                                    capture_output=True, text=True,
                                    cwd=Path(__file__).resolve().parents[1], timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['ready_for_attempt'])

    def row(self, **changes):
        value = {'id': 'q', 'category': 'code', 'status': 'completed', 'expected_outcome': 'evidence',
                 'negative_labels': 0, 'answer_status': 'evidence_found', 'failures': [],
                 'retrieved': {'group_hits': [True], 'expected_outcome_met': True},
                 'packed': {'group_hits': [True], 'expected_outcome_met': True,
                            'negative_passages_found': 0, 'passed': True}}
        return {**value, **changes}

    def test_missing_groups_distinguish_index_retrieval_and_packing(self):
        row = self.row()
        row['packed'].update(group_hits=[False], expected_outcome_met=False, passed=False)
        self.assertEqual(diagnose(row, [True]), ['context_packing'])
        row['retrieved']['group_hits'] = [False]
        self.assertEqual(diagnose(row, [True]), ['retrieval_selection'])
        self.assertEqual(diagnose(row, [False]), ['index_representation'])

    def test_errors_stay_in_denominators_and_empty_metrics_are_not_successes(self):
        error = self.row(status='error', failures=['evaluation_error'])
        result = summarize([self.row(), error])
        self.assertEqual(result['all_essential_packed']['passed'], 1)
        self.assertEqual(result['all_essential_packed']['total'], 2)
        self.assertEqual(result['all_essential_packed']['rate'], .5)
        self.assertIsNone(result['no_evidence_outcome']['rate'])
        self.assertIsNone(rate(0, 0)['wilson_95'])
        self.assertLess(rate(1, 1)['wilson_95'][0], .9)

    def test_negatives_and_actual_empty_answer_status_are_separate(self):
        row = self.row(expected_outcome='insufficient_evidence', negative_labels=1)
        row['packed']['negative_passages_found'] = 1
        self.assertEqual(diagnose(row, []), ['negative_evidence', 'no_evidence_outcome'])
        row['answer_status'] = 'insufficient_evidence'
        row['packed']['negative_passages_found'] = 0
        self.assertEqual(diagnose(row, []), [])


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class QualityBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.project_rows, cls.datasets, cls.source_paths = [], {}, {}
        for name, split, text in [('Nebula', 'development', 'Nebula pulses expire after seven seconds.'),
                                  ('Orchid', 'held-out', 'Orchid valves stay closed during calibration.')]:
            project, work = cls.root / name, cls.root / (name + '-index')
            (project / 'Assets').mkdir(parents=True)
            (project / 'ProjectSettings').mkdir()
            (project / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 2022.3.1f1\n')
            source = project / 'Assets/Guide.md'
            source.write_text(text + '\n', encoding='utf-8')
            cls.source_paths[name] = source
            refresh(work, project, semantic=False)
            cls.project_rows.append({'name': name, 'split': split, 'repository': 'fixture:' + name,
                                     'previously_used_for_tuning': split == 'development',
                                     'workspace': str(work), 'dataset': name + '.json'})
            cls.datasets[name] = {'schema_version': 2, 'split': split,
                'label_review': {'reviewer': 'test author', 'kind': 'agent', 'rationale': 'Authored literal fixture control.'},
                'cases': [{'id': 'behavior', 'question': text, 'category': 'behavior',
                           'passage_groups': [[{'path': 'Assets/Guide.md', 'quote': text, 'start_line': 1, 'end_line': 1}]],
                           'reviewed_source_sha256': {'Assets/Guide.md': hashlib.sha256(source.read_bytes()).hexdigest()},
                           'negative_passages': []}]}

    def setUp(self):
        self.run_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.run_temp.cleanup)
        self.run_root = Path(self.run_temp.name)
        self.manifest, self.lock = self.run_root / 'manifest.json', self.run_root / 'frozen.json'
        self.plan = {'schema_version': 1, 'kind': 'quality-benchmark', 'name': 'Fixture quality',
                     'scope': 'synthetic-control', 'projects': copy.deepcopy(self.project_rows),
                     'settings': {**DEFAULT_SETTINGS, 'context_chars': 1400}}
        for name, dataset in self.datasets.items():
            write_json(self.run_root / (name + '.json'), dataset)
        self.save_plan()

    def save_plan(self):
        write_json(self.manifest, self.plan)

    def freeze(self):
        self.save_plan()
        return freeze_quality_benchmark(self.manifest, self.lock)

    def test_freeze_precedes_retrieval_and_pins_generation(self):
        with patch('unity_rag.quality_benchmark.evaluate_passage_case', side_effect=AssertionError('evaluation before freeze')):
            frozen = self.freeze()
        for row, source in zip(frozen['projects'], self.project_rows):
            self.assertEqual((self.lock.parent / row['workspace']).resolve(), resolve_workspace(source['workspace']))
            self.assertEqual(set(row['workspace_files']), {'complete.json', 'inventory.sqlite3'})
        with self.assertRaises(FileExistsError):
            freeze_quality_benchmark(self.manifest, self.lock)

    def test_real_pipeline_baseline_and_reports_do_not_claim_answer_quality(self):
        self.freeze()
        report = evaluate_quality_benchmark(self.lock)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['summary']['all_essential_packed']['rate'], 1)
        self.assertEqual(report['splits']['held-out']['all_essential_packed']['total'], 1)
        self.assertTrue(report['retrieval_gate_passed'])
        self.assertFalse(report['production_gate_passed'])
        self.assertFalse(report['corpus_requirements_met'])
        self.assertEqual(report['answer_quality']['status'], 'not_evaluated')
        output, markdown = write_quality_report(report, self.run_root / 'baseline.json')
        self.assertIn('not evaluated', markdown.read_text(encoding='utf-8'))
        self.assertEqual(json.loads(output.read_text())['lock_sha256'], hashlib.sha256(self.lock.read_bytes()).hexdigest())
        original = output.read_bytes()
        with self.assertRaises(ValueError):
            write_quality_report(report, output)
        self.assertEqual(output.read_bytes(), original)

    def test_repository_aliases_and_tuned_holdouts_are_rejected(self):
        self.plan['projects'][0]['repository'] = 'https://github.com/example/game.git'
        self.plan['projects'][1]['repository'] = 'https://GitHub.com/Example/Game/'
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.freeze()
        self.plan['projects'] = copy.deepcopy(self.project_rows)
        self.plan['projects'][1]['previously_used_for_tuning'] = True
        with self.assertRaisesRegex(ValueError, 'previously tuned'):
            self.freeze()

    def test_same_project_cannot_be_disguised_by_another_repository_name(self):
        self.plan['projects'][1]['workspace'] = self.plan['projects'][0]['workspace']
        with self.assertRaisesRegex(ValueError, 'repeated source'):
            self.freeze()

    def test_invalid_settings_and_weakened_corpus_minimum_fail_closed(self):
        for settings in ({'limit': True}, {'context_chars': 2}, {'context_tokens': -1}, {'mode': 'typo'},
                         {'unknown': 1}, []):
            with self.subTest(settings=settings):
                self.plan['settings'] = settings
                with self.assertRaises(ValueError):
                    self.freeze()
        self.plan['settings'] = DEFAULT_SETTINGS
        self.plan['targets'] = {'minimum_projects': 1}
        with self.assertRaisesRegex(ValueError, 'cannot weaken'):
            self.freeze()

    def test_review_identity_categories_and_literal_ranges_are_required(self):
        path = self.run_root / 'Orchid.json'
        for mutate in (lambda d: d.pop('label_review'),
                       lambda d: d['cases'][0].pop('category'),
                       lambda d: d['cases'][0]['passage_groups'][0][0].update(start_line=2, end_line=2),
                       lambda d: d['cases'].append(d['cases'][0])):
            with self.subTest(mutate=mutate):
                dataset = copy.deepcopy(self.datasets['Orchid'])
                mutate(dataset)
                write_json(path, dataset)
                with self.assertRaises(ValueError):
                    self.freeze()

    def test_changed_dataset_stops_before_any_retrieval(self):
        self.freeze()
        path = self.run_root / 'Orchid.json'
        path.write_bytes(path.read_bytes() + b' ')
        with patch('unity_rag.quality_benchmark.evaluate_passage_case', side_effect=AssertionError('must not run')):
            with self.assertRaisesRegex(ValueError, 'Frozen dataset changed'):
                evaluate_quality_benchmark(self.lock)

    def test_new_unlabeled_source_is_also_detected(self):
        self.freeze()
        path = self.source_paths['Orchid'].with_name('New.md')
        path.write_text('new unrelated source', encoding='utf-8')
        self.addCleanup(path.unlink)
        with self.assertRaisesRegex(ValueError, 'sources changed'):
            evaluate_quality_benchmark(self.lock)

    def test_failed_case_is_retained_in_held_out_denominator(self):
        self.freeze()
        from unity_rag.quality_benchmark import evaluate_passage_case
        def fail_one(workspace, case, inventory, **options):
            if 'Orchid' in inventory['project']:
                raise ValueError('injected retrieval error')
            return evaluate_passage_case(workspace, case, inventory, **options)
        with patch('unity_rag.quality_benchmark.evaluate_passage_case', side_effect=fail_one):
            report = evaluate_quality_benchmark(self.lock)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['splits']['held-out']['all_essential_packed']['rate'], 0)
        self.assertEqual(report['splits']['held-out']['all_essential_packed']['total'], 1)
        self.assertEqual(report['projects'][1]['cases'][0]['failures'], ['evaluation_error'])
        self.assertFalse(report['retrieval_gate_passed'])
        self.assertIn('evaluation_error', markdown_report(report))

    def test_diagnostic_failure_retains_every_case(self):
        self.freeze()
        with patch('unity_rag.quality_benchmark._indexed_groups', side_effect=ValueError('injected diagnostics failure')):
            report = evaluate_quality_benchmark(self.lock)
        self.assertEqual(report['summary']['questions'], 2)
        self.assertEqual(report['summary']['errors'], 2)
        self.assertFalse(report['retrieval_gate_passed'])

    def test_lock_mutation_during_evaluation_invalidates_report(self):
        self.freeze()
        from unity_rag.quality_benchmark import evaluate_passage_case
        def mutate(*args, **kwargs):
            result = evaluate_passage_case(*args, **kwargs)
            self.lock.write_bytes(self.lock.read_bytes() + b' ')
            return result
        with patch('unity_rag.quality_benchmark.evaluate_passage_case', side_effect=mutate):
            report = evaluate_quality_benchmark(self.lock)
        self.assertEqual(report['status'], 'invalid')
        self.assertFalse(report['retrieval_gate_passed'])

    def test_frozen_index_bytes_are_checked_before_reading(self):
        self.freeze()
        with patch('unity_rag.quality_benchmark._workspace_hashes', return_value={'inventory.sqlite3': 'modified'}), \
             patch('unity_rag.quality_benchmark.status', side_effect=AssertionError('must not read changed index')):
            with self.assertRaisesRegex(ValueError, 'Frozen workspace changed'):
                evaluate_quality_benchmark(self.lock)

    def test_mutation_during_run_preserves_results_but_invalidates_acceptance(self):
        self.freeze()
        from unity_rag.quality_benchmark import evaluate_passage_case
        def mutate(*args, **options):
            result = evaluate_passage_case(*args, **options)
            path = self.run_root / 'Orchid.json'
            path.write_bytes(path.read_bytes() + b' ')
            return result
        with patch('unity_rag.quality_benchmark.evaluate_passage_case', side_effect=mutate):
            report = evaluate_quality_benchmark(self.lock)
        self.assertEqual(report['status'], 'invalid')
        self.assertEqual(report['summary']['questions'], 2)
        self.assertFalse(report['retrieval_gate_passed'])
        self.assertIn('Frozen dataset changed', report['validation_errors'][0])

    def test_rebuilt_candidate_keeps_frozen_sources_and_labels(self):
        self.freeze()
        bindings = {}
        for project in self.project_rows:
            work = self.run_root / (project['name'] + '-candidate')
            refresh(work, self.source_paths[project['name']].parents[1], semantic=False, chunk_budget=1024)
            bindings[project['name']] = str(work)
        path = self.run_root / 'bindings.json'
        write_json(path, bindings)
        report = evaluate_quality_benchmark(self.lock, workspaces=path)
        self.assertTrue(report['retrieval_gate_passed'])
        bindings.pop('Orchid')
        write_json(path, bindings)
        with self.assertRaisesRegex(ValueError, 'exactly'):
            evaluate_quality_benchmark(self.lock, workspaces=path)

    def test_cli_writes_reports_and_returns_gate_status(self):
        self.freeze()
        report = evaluate_quality_benchmark(self.lock)
        for status, gate, expected in [('completed', True, 0), ('completed', False, 1), ('invalid', False, 2)]:
            with self.subTest(status=status, gate=gate):
                report.update(status=status, retrieval_gate_passed=gate)
                output = self.run_root / f'report-{expected}.json'
                with patch('sys.argv', ['unity-rag', 'evaluate-quality', str(self.lock), '--output', str(output)]), \
                     patch('unity_rag.quality_benchmark.evaluate_quality_benchmark', return_value=report), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(main(), expected)
                self.assertTrue(output.exists())
                self.assertTrue(output.with_suffix('.md').exists())


if __name__ == '__main__':
    unittest.main()
