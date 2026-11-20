"""Semantic corpus selection retains the complete lexical index and embedding cache."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.semantic import build_semantic, rank


class CountingProvider:
    config = {'provider': 'fixture', 'model': 'scope-v1'}

    def __init__(self):
        self.passage_calls = 0
        self.query_calls = 0

    def passages(self, texts):
        self.passage_calls += len(texts)
        return [[1, 0] for _ in texts]

    def query(self, text):
        self.query_calls += 1
        return [1, 0]


class SemanticScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.database = self.workspace / 'inventory.sqlite3'
        self.report = {'snapshot': 'unchanged-test-fixture'}
        self.provider = CountingProvider()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE evidence (id TEXT PRIMARY KEY, kind TEXT, data TEXT)')
            for identity, kind, text in [('code', 'code_member', 'void Fire() {}'),
                                         ('doc', 'documentation', '# Projectile selection'),
                                         ('asset', 'asset', 'Prefab GUID and identity')]:
                item = {'id': identity, 'kind': kind, 'title': identity, 'text': text}
                db.execute('INSERT INTO evidence VALUES (?,?,?)', (identity, kind, json.dumps(item)))
        self.open_patch = patch('unity_rag.retrieval._open',
                                side_effect=lambda _: (sqlite3.connect(self.database), self.report))
        self.status_patch = patch('unity_rag.retrieval.retrieval_status', return_value=self.report)
        self.open_patch.start()
        self.status_patch.start()
        self.addCleanup(self.open_patch.stop)
        self.addCleanup(self.status_patch.stop)

    def active_chunks(self):
        with closing(sqlite3.connect(self.workspace / 'semantic.sqlite3')) as db, db:
            return {row[0] for row in db.execute('SELECT DISTINCT chunk FROM vectors')}

    def test_subset_filters_embeddings_without_removing_lexical_evidence(self):
        result = build_semantic(self.workspace, self.provider,
                                include_kinds=['documentation', 'code_member', 'documentation'])
        self.assertEqual(result['include_kinds'], ['code_member', 'documentation'])
        self.assertEqual((result['chunks'], result['excluded_chunks']), (2, 1))
        self.assertEqual(self.active_chunks(), {'code', 'doc'})
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 3)
        self.assertEqual(set(rank(self.workspace, 'question', self.provider)), {'code', 'doc'})

    def test_narrowing_and_restoring_scope_reuses_unchanged_cached_vectors(self):
        complete = build_semantic(self.workspace, self.provider)
        calls = self.provider.passage_calls
        narrowed = build_semantic(self.workspace, self.provider, include_kinds=['code_member'])
        self.assertEqual(narrowed['config_id'], complete['config_id'])
        self.assertEqual(narrowed['config'], complete['config'])
        self.assertEqual(narrowed['embedded'], 0)
        self.assertEqual(narrowed['cache_hits'], 1)
        self.assertEqual(self.provider.passage_calls, calls)
        self.assertEqual(self.active_chunks(), {'code'})
        restored = build_semantic(self.workspace, self.provider)
        self.assertIsNone(restored['include_kinds'])
        self.assertEqual(restored['excluded_chunks'], 0)
        self.assertEqual(restored['embedded'], 0)
        self.assertEqual(self.provider.passage_calls, calls)
        self.assertEqual(self.active_chunks(), {'code', 'doc', 'asset'})

    def test_invalid_scope_fails_before_opening_index_or_calling_model(self):
        for kinds in ([], 'code_member', ['unknown'], [None], ['code_member', 1], ('code_member',)):
            with self.subTest(kinds=kinds), patch('unity_rag.retrieval._open') as opened:
                with self.assertRaisesRegex(ValueError, 'Semantic kinds'):
                    build_semantic(self.workspace, self.provider, include_kinds=kinds)
                opened.assert_not_called()
        self.assertEqual(self.provider.passage_calls, 0)
        self.assertFalse((self.workspace / 'semantic.sqlite3').exists())

    def test_valid_kind_with_no_records_needs_no_embedding_or_query_inference(self):
        result = build_semantic(self.workspace, self.provider, include_kinds=['prefab_override'])
        self.assertEqual((result['chunks'], result['passages'], result['excluded_chunks']), (0, 0, 3))
        self.assertIsNone(result['dimension'])
        self.assertEqual(rank(self.workspace, 'question', self.provider), {})
        self.assertEqual((self.provider.passage_calls, self.provider.query_calls), (0, 0))


if __name__ == '__main__':
    unittest.main()
