"""Frozen sample contracts; all model identities and responses here are synthetic."""
import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from unity_rag import answer_sample as sample
from unity_rag.answer_acceptance import POLICY
from unity_rag.benchmark import file_sha256
from unity_rag.workspace import refresh


class SampleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.source = self.project / 'Enemy.cs'
        self.source.write_text('public class Enemy {}\n', encoding='utf-8')
        self.dataset = self.root / 'questions.json'
        self.questions = {'schema_version': 1, 'questions': [
            {'id': 'case', 'question': 'Game.Enemy.CanAttack', 'category': 'code', 'human_review': 'Inspect source.',
             'references': [{'path': 'Enemy.cs', 'sha256': file_sha256(self.source),
                             'start_line': 1, 'end_line': 1, 'quote': 'public class Enemy {}'}]}]}
        self.save_dataset()
        self.manifest = self.root / 'manifest.json'
        self.data = {'schema_version': 1, 'kind': 'answer-sample', 'scope': 'fresh-candidate',
                     'repository': 'https://example.com/project', 'revision': 'a' * 40,
                     'previously_used_for_tuning': False, 'policy': dict(POLICY), 'questions': 1,
                     'dataset': 'questions.json', 'dataset_sha256': file_sha256(self.dataset),
                     'source_review': {'kind': 'agent', 'reviewer': 'Synthetic test', 'rationale': 'Fixture only.'},
                     'settings': {'mode': 'exact', 'limit': 5, 'expand': 0, 'context_chars': 24000,
                                  'followups': 0, 'context_tokens': None, 'token_encoding': 'utf8-bytes',
                                  'model_context_tokens': None},
                     'provider': {'model': 'fixture', 'endpoint': 'http://127.0.0.1:11434', 'timeout': 120,
                                  'thinking': False, 'max_output_tokens': 2048, 'seed': 42},
                     'model_digest': 'c' * 64}
        self.save_manifest()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.lock = self.root / 'lock.json'
        self.snapshot = {'inputs': {'discovery': 'fixture', 'code': 'a' * 64, 'assets': 'b' * 64}}
        for name, value in (('_generation', self.workspace), ('status', {'project': str(self.project)}),
                            ('_fresh', None), ('_git_revision', 'a' * 40), ('_workspace_hashes', {'code': 'd' * 64}),
                            ('retrieval_status', self.snapshot), ('_model_digest', 'c' * 64)):
            mock = patch.object(sample, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def save_dataset(self):
        self.dataset.write_text(json.dumps(self.questions), encoding='utf-8')

    def save_manifest(self):
        self.manifest.write_text(json.dumps(self.data), encoding='utf-8')

    def prepare(self):
        return sample.prepare_sample(self.manifest, self.workspace, self.lock)

    def test_prepare_freezes_without_generation_and_never_overwrites(self):
        with patch.object(sample, 'evaluate_answers') as generate:
            lock = self.prepare()
        generate.assert_not_called()
        self.assertEqual(lock['binding']['dataset_sha256'], file_sha256(self.dataset))
        self.assertIn('Program.cs', lock['binding']['implementation']['analyzer'])
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_changed_manifest_dataset_source_model_or_implementation_blocks_generation(self):
        self.prepare()
        for target in (self.manifest, self.dataset, self.source):
            original = target.read_bytes()
            target.write_bytes(original + b'\n')
            with patch.object(sample, 'evaluate_answers') as generate, self.assertRaises(ValueError):
                sample.run_sample(self.lock, self.root / 'never-created')
            generate.assert_not_called()
            self.assertFalse((self.root / 'never-created').exists())
            target.write_bytes(original)
        for name, value in (('_model_digest', 'f' * 64), ('_implementation', {'python': 'changed'}),
                            ('_workspace_hashes', {'code': 'changed'}), ('retrieval_status', {'inputs': 'changed'})):
            with patch.object(sample, name, return_value=value), patch.object(sample, 'evaluate_answers') as generate, self.assertRaises(ValueError):
                sample.run_sample(self.lock, self.root / 'never-created')
            generate.assert_not_called()

    def test_invalid_counts_guidance_quote_and_remote_provider_are_rejected(self):
        original = copy.deepcopy(self.data)
        for mutate in (lambda d: d.update(questions=2), lambda d: d.update(previously_used_for_tuning=True),
                       lambda d: d['policy'].update(supported_claim_rate=0.5),
                       lambda d: d['provider'].update(endpoint='https://example.com'),
                       lambda d: d['settings'].update(followups=True),
                       lambda d: d['source_review'].update(kind='human')):
            self.data = copy.deepcopy(original)
            mutate(self.data)
            self.save_manifest()
            with self.assertRaises(ValueError):
                self.prepare()
        self.data = original
        self.questions['questions'][0]['references'][0]['quote'] = 'different'
        self.save_dataset()
        self.data['dataset_sha256'] = file_sha256(self.dataset)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'line range'):
            self.prepare()

    def test_missing_failed_answer_is_retained_with_pending_review(self):
        self.prepare()
        report = {'schema_version': 2, 'kind': 'answer-evaluation-report', 'answer_style': 'explain',
                  'dataset_sha256': file_sha256(self.dataset), 'implementation_sha256': 'd' * 64,
                  'pipeline_sha256': 'e' * 64, 'draft_prompt_sha256': 'f' * 64,
                  'snapshot': self.snapshot['inputs'], 'status': 'completed', 'validation_errors': [],
                  'questions': 1, 'valid_outputs': 0, 'results': [
                      {'id': 'case', 'question': 'Game.Enemy.CanAttack', 'error': 'Model failed.',
                       'checks': {'valid_output_and_citations': False}}]}
        output = self.root / 'run'
        with patch.object(sample, 'evaluate_answers', return_value=report):
            result = sample.run_sample(self.lock, output)
        self.assertEqual(result['valid_outputs'], 0)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['review_status'], 'pending')
        self.assertTrue((output / 'started.json').is_file())
        self.assertEqual(json.loads((output / 'report.json').read_bytes())['results'][0]['error'], 'Model failed.')
        with self.assertRaises(FileExistsError):
            sample.run_sample(self.lock, output)

    def test_changes_during_generation_are_retained_as_invalid(self):
        self.prepare()
        # A real valid report is not needed to verify this invalidation branch;
        # packet construction is covered by the end-to-end and failed-case tests.
        report = {'status': 'completed', 'validation_errors': [], 'results': [], 'questions': 0, 'valid_outputs': 0}
        def generate(*args, **kwargs):
            self.source.write_text('changed')
            return report
        pending = {'review_status': 'pending', 'accepted': False}
        with patch.object(sample, 'evaluate_answers', side_effect=generate), patch.object(sample, 'acceptance_template', return_value={}), patch.object(sample, 'review_acceptance', return_value=pending):
            result = sample.run_sample(self.lock, self.root / 'invalid')
        self.assertEqual(result['status'], 'invalid')
        self.assertTrue(report['validation_errors'])

    def test_existing_output_rejected_before_model_identity_check(self):
        output = self.root / 'existing'
        output.mkdir()
        with patch.object(sample, '_model_digest') as identity, self.assertRaises(FileExistsError):
            sample.run_sample(self.lock, output)
        identity.assert_not_called()

    @unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
    def test_real_fixture_prepare_generation_and_unfilled_review(self):
        from unity_rag.discovery import status
        from unity_rag.quality_benchmark import _fresh, _generation, _workspace_hashes
        from unity_rag.retrieval import retrieval_status
        project = self.root / 'real-project'
        shutil.copytree(Path(__file__).parent / 'fixtures/answer_project', project)
        workspace = self.root / 'real-workspace'
        refresh(workspace, project, semantic=False)
        self.questions['questions'][0]['references'] = []
        self.save_dataset()
        self.data['dataset_sha256'] = file_sha256(self.dataset)
        self.save_manifest()
        def generate(provider, messages, schema):
            evidence = json.loads(messages[1]['content'])['evidence']
            key = next(k for k, row in evidence.items() if 'CanAttack' in row['title'])
            return {'claims': [{'text': 'CanAttack permits equality because it uses >=.',
                                'kind': 'fact', 'evidence_ids': [key]}],
                    'missing_evidence': [], 'followup_queries': []}
        with patch.object(sample, 'status', status), patch.object(sample, '_generation', _generation), patch.object(sample, '_fresh', _fresh), patch.object(sample, '_workspace_hashes', _workspace_hashes), patch.object(sample, 'retrieval_status', retrieval_status), patch.object(sample.OllamaProvider, 'generate', generate):
            sample.prepare_sample(self.manifest, workspace, self.lock)
            result = sample.run_sample(self.lock, self.root / 'real-run')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['valid_outputs'], 1)
        self.assertFalse(result['accepted'])
        pending = json.loads((self.root / 'real-run/pending.json').read_bytes())
        self.assertEqual(pending['claim_counts']['pending'], 1)


class RecordedSampleTests(unittest.TestCase):
    def test_frozen_manifest_dataset_and_sources_are_explicit(self):
        root = Path(__file__).resolve().parents[1] / 'benchmarks/answer_sample_v1'
        manifest = json.loads((root / 'manifest.json').read_bytes())
        self.assertEqual(manifest['dataset_sha256'], file_sha256(root / manifest['dataset']))
        cases = json.loads((root / manifest['dataset']).read_bytes())['questions']
        self.assertEqual(len(cases), 12)
        self.assertEqual(len({c['id'] for c in cases}), 12)
        self.assertEqual(manifest['policy'], POLICY)
        self.assertEqual(manifest['source_review']['kind'], 'agent')
        self.assertEqual({c['category'] for c in cases},
                         {'code', 'multipart-code', 'cross-file', 'asset-code', 'runtime-limit', 'missing-source'})


class SampleCommandTests(unittest.TestCase):
    def test_cli_distinguishes_completed_capture_from_failed_capture(self):
        from scripts.evaluate_answer_sample import main
        for status, valid, expected in (('completed', 2, 0), ('completed', 1, 1), ('invalid', 2, 1)):
            result = {'status': status, 'valid_outputs': valid, 'questions': 2,
                      'review_status': 'pending', 'accepted': False}
            with patch('sys.argv', ['sample', 'run', 'lock.json', '--output', 'new-run']), patch(
                    'scripts.evaluate_answer_sample.run_sample', return_value=result), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), expected)
        with patch('sys.argv', ['sample', 'prepare', 'manifest.json', '--workspace', 'work', '--output', 'lock']), patch(
                'scripts.evaluate_answer_sample.prepare_sample', side_effect=FileExistsError('existing')), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 1)

    def test_model_identity_requires_exact_model_and_digest_and_disables_proxies(self):
        from unittest.mock import Mock
        provider = sample.OllamaProvider('fixture')
        for models, accepted in (([{'name': 'fixture', 'digest': 'a' * 64}], True),
                                  ([{'name': 'other', 'digest': 'a' * 64}], False),
                                  ([{'name': 'fixture', 'digest': 'short'}], False),
                                  ([{'name': 'fixture', 'digest': 'a' * 64}] * 2, False)):
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps({'models': models}).encode())
            with patch.object(sample, 'build_opener', return_value=opener) as factory:
                if accepted:
                    self.assertEqual(sample._model_digest(provider), 'a' * 64)
                else:
                    with self.assertRaises(ValueError):
                        sample._model_digest(provider)
            self.assertEqual(factory.call_args.args[0].proxies, {})
            self.assertIsInstance(factory.call_args.args[1], sample.NoRedirect)
            self.assertEqual(opener.open.call_args.args[0].full_url, 'http://127.0.0.1:11434/api/tags')


if __name__ == '__main__':
    unittest.main()
