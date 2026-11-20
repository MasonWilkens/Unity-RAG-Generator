import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unity_rag.benchmark import benchmark, freeze_benchmark, file_sha256


class BenchmarkTests(unittest.TestCase):
    def test_project_splits_and_macro_average(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, split in [('a', 'development'), ('b', 'held-out')]:
                (root / (name + '.json')).write_text(json.dumps({'split': split, 'questions': [{'id': 'q'}]}))
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'schema_version': 1, 'projects': [
                {'name': 'A', 'split': 'development', 'workspace': 'a', 'dataset': 'a.json'},
                {'name': 'B', 'split': 'held-out', 'workspace': 'b', 'dataset': 'b.json'}]}))
            def project(workspace):
                return {'project': str(workspace)}
            report = {'recall_at_k': .5, 'questions': 1, 'results': [{'category': 'code', 'recall_at_k': .5}]}
            with patch('unity_rag.benchmark.status', side_effect=project), patch('unity_rag.benchmark.evaluate', return_value=report):
                result = benchmark(manifest)
            self.assertEqual(result['splits']['held-out']['macro_project_recall_at_k'], .5)
            self.assertEqual(result['projects'][0]['categories']['code']['questions'], 1)
            with patch('unity_rag.benchmark.status', return_value={'project': str(root)}):
                with patch('unity_rag.benchmark.evaluate', return_value=report):
                    with self.assertRaisesRegex(ValueError, 'only once'):
                        benchmark(manifest)


class FrozenBenchmarkTests(unittest.TestCase):
    def test_freeze_pins_generation_and_rejects_changed_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / 'workspace'
            generation = work / 'generations' / ('a' * 32)
            generation.mkdir(parents=True)
            (generation / 'complete.json').write_text(json.dumps({'generation': generation.name}))
            (generation / 'inventory.sqlite3').write_bytes(b'fixture database bytes')
            (work / 'active.json').write_text(json.dumps({'generation': generation.name}))
            dataset = root / 'dataset.json'
            dataset.write_text(json.dumps({'split':'development', 'questions':[{'id':'one'}]}))
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'schema_version':1, 'projects':[
                {'name':'Fixture', 'split':'development', 'workspace':'workspace', 'dataset':'dataset.json'}]}))
            baseline = root / 'baseline.json'
            report = {'snapshot':{'discovery':'fixed'}, 'dataset_sha256':file_sha256(dataset),
                      'recall_at_k':1, 'questions':1, 'results':[{'category':'code','recall_at_k':1}]}
            baseline.write_text(json.dumps({'manifest_sha256':file_sha256(manifest),
                'settings':{'mode':'search','limit':10,'expand':0},
                'projects':[{'name':'Fixture','report':report}]}))
            frozen = root / 'frozen.json'
            with patch('unity_rag.benchmark.retrieval_status', return_value={'inputs':report['snapshot']}), \
                    patch('unity_rag.benchmark.status', return_value={'project':str(root / 'project')}), \
                    patch('unity_rag.benchmark.evaluate', return_value=report):
                freeze_benchmark(manifest, frozen, baseline)
                # Advancing the active pointer must not change the pinned benchmark.
                (work / 'active.json').write_text(json.dumps({'generation':'b'*32}))
                self.assertTrue(benchmark(frozen)['frozen_inputs_verified'])
                with self.assertRaisesRegex(ValueError, 'settings'):
                    benchmark(frozen, limit=5)
                for target, message in ((dataset,'dataset changed'), (baseline,'baseline changed'),
                                        (generation/'inventory.sqlite3','workspace changed')):
                    original = target.read_bytes()
                    target.write_bytes(original + b' ')
                    try:
                        with self.assertRaisesRegex(ValueError, message):
                            benchmark(frozen)
                    finally:
                        target.write_bytes(original)
                with patch('unity_rag.benchmark.evaluate', side_effect=lambda *a, **kw:
                           (dataset.write_bytes(dataset.read_bytes()+b' '), report)[1]):
                    with self.assertRaisesRegex(ValueError, 'dataset changed'):
                        benchmark(frozen)
