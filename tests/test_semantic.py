import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from unity_rag.discovery import scan
from unity_rag.code_map import map_code
from unity_rag.asset_map import map_assets
from unity_rag.retrieval import build_retrieval, retrieve
from unity_rag.semantic import build_semantic, semantic_status, normalized


class FixtureProvider:
    """Controlled vectors test ranking mechanics, not model quality."""
    config = {"provider": "fixture", "model": "v1"}

    def __init__(self):
        self.calls = 0

    def passages(self, texts):
        self.calls += len(texts)
        return [[1, 0] if 'CanAttack' in text else [0, 1] for text in texts]

    def query(self, text):
        return [1, 0]


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class SemanticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.project = cls.base/'Game'
        cls.original = cls.base/'Original'
        shutil.copytree(Path(__file__).parent/'fixtures/retrieval_project', cls.project)
        scan(cls.project, cls.original)
        map_code(cls.original)
        map_assets(cls.original)
        build_retrieval(cls.original)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.base))
        shutil.copyfile(self.original/'inventory.sqlite3', self.work/'inventory.sqlite3')
        self.provider = FixtureProvider()

    def test_cache_and_configuration_version(self):
        first = build_semantic(self.work, self.provider)
        self.assertGreater(first['embedded'], 0)
        calls = self.provider.calls
        second = build_semantic(self.work, self.provider)
        self.assertEqual(second['embedded'], 0)
        self.assertEqual(self.provider.calls, calls)
        self.provider.config = {**self.provider.config, 'model': 'v2'}
        with self.assertRaisesRegex(ValueError, 'configuration'):
            retrieve(self.work, 'attack', mode='semantic', provider=self.provider)
        third = build_semantic(self.work, self.provider)
        self.assertNotEqual(first['config_id'], third['config_id'])
        self.assertGreater(third['embedded'], 0)

    def test_semantic_hybrid_exact_and_budget(self):
        build_semantic(self.work, self.provider)
        dense = retrieve(self.work, 'strike readiness', mode='semantic', provider=self.provider)
        self.assertTrue(dense['results'])
        self.assertEqual(dense['results'][0]['ranking_diagnostics']['cosine'], 1)
        exact = retrieve(self.work, 'CanAttack', mode='exact')['results']
        hybrid = retrieve(self.work, 'CanAttack', mode='hybrid', provider=self.provider, expand=2)
        self.assertEqual(exact[0]['id'], hybrid['results'][0]['id'])
        self.assertEqual(hybrid['results'][0]['match'], 'exact_alias')
        self.assertTrue(hybrid['context'])
        bounded = retrieve(self.work, 'attack', mode='hybrid', provider=self.provider, context_chars=1)
        self.assertEqual(bounded['results'], [])
        self.assertGreater(bounded['context_budget']['omitted_records'], 0)
        filtered = retrieve(self.work, 'attack', mode='semantic', provider=self.provider, kind='asset')
        self.assertTrue(all(r['kind']=='asset' for r in filtered['results']))

    def test_failed_rebuild_keeps_active_index(self):
        before = build_semantic(self.work, self.provider)
        self.provider.config = {**self.provider.config, 'model': 'broken'}
        self.provider.passages = lambda texts: [[float('nan'), 1] for _ in texts]
        with self.assertRaisesRegex(ValueError, 'non-finite'):
            build_semantic(self.work, self.provider)
        self.assertEqual(semantic_status(self.work), before)

    def test_cancellation_preserves_active_and_checkpoints_new_vectors(self):
        before = build_semantic(self.work, self.provider)
        self.provider.config = {**self.provider.config, 'model':'cancel-probe'}
        events = []
        def cancel(phase, counts):
            events.append(phase)
            if phase == 'batch_complete':
                raise KeyboardInterrupt('controlled cancellation')
        with self.assertRaises(KeyboardInterrupt):
            build_semantic(self.work, self.provider, observe=cancel)
        self.assertIn('evidence_loaded', events)
        self.assertIn('passages_prepared', events)
        self.assertEqual(semantic_status(self.work), before)
        with sqlite3.connect(self.work/'semantic.sqlite3') as db:
            configs = [json.loads(r[0])['config'] for r in db.execute('SELECT data FROM active')]
            self.assertEqual(configs, [before['config']])
            count = db.execute('SELECT COUNT(*) FROM cache WHERE config!=?', (before['config_id'],)).fetchone()[0]
            self.assertEqual(count, 0)
        self.provider.calls = 0
        resumed = build_semantic(self.work, self.provider)
        self.assertEqual(resumed['cache_hits'], 4)
        self.assertEqual(resumed['embedded'], self.provider.calls)
        self.assertGreater(self.provider.calls, 0)

    def test_source_aware_keeps_budget_and_rejects_other_modes(self):
        build_semantic(self.work, self.provider)
        result = retrieve(self.work, 'attack', mode='hybrid', provider=self.provider,
                          source_aware=True, path_refs=3, context_chars=1)
        self.assertEqual(result['results'], [])
        self.assertEqual(result['context'], [])
        self.assertLessEqual(result['context_budget']['used_chars'], 1)
        with self.assertRaisesRegex(ValueError, 'opt-in hybrid'):
            retrieve(self.work, 'attack', mode='search', source_aware=True)

    def test_changed_snapshot_rejected(self):
        build_semantic(self.work, self.provider)
        with sqlite3.connect(self.work/'inventory.sqlite3') as db:
            raw = db.execute("SELECT value FROM metadata WHERE key='retrieval_report'").fetchone()[0]
            report = json.loads(raw)
            report['limits'].append('changed')
            db.execute("UPDATE metadata SET value=? WHERE key='retrieval_report'", (json.dumps(report),))
        with self.assertRaisesRegex(ValueError, 'stale'):
            semantic_status(self.work)

    def test_invalid_vectors(self):
        for vector in ([], [0, 0], [float('inf')]):
            with self.assertRaises(ValueError):
                normalized(vector)
        with self.assertRaises(ValueError):
            normalized([1, 0], 3)

    def test_missing_index_and_bad_provider_batch(self):
        with self.assertRaisesRegex(ValueError, 'No semantic index'):
            retrieve(self.work, 'attack', mode='semantic', provider=self.provider)
        self.provider.passages = lambda texts: []
        with self.assertRaisesRegex(ValueError, 'wrong number'):
            build_semantic(self.work, self.provider)
        with self.assertRaisesRegex(ValueError, 'No completed'):
            semantic_status(self.work)

    def test_query_dimension_is_checked(self):
        build_semantic(self.work, self.provider)
        self.provider.query = lambda text: [1, 0, 0]
        with self.assertRaisesRegex(ValueError, 'dimension'):
            retrieve(self.work, 'attack', mode='hybrid', provider=self.provider)
