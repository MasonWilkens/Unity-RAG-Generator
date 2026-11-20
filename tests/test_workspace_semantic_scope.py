"""Managed refresh retains semantic scope without dropping lexical evidence."""
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

import unity_rag
from unity_rag.workspace import refresh, resolve_workspace
from test_semantic import FixtureProvider


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class WorkspaceSemanticScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / 'Game'
        fixtures = Path(unity_rag.__file__).parent.parent / 'tests' / 'fixtures'
        shutil.copytree(fixtures / 'retrieval_project', self.project)
        self.workspace = self.base / 'Knowledge'
        self.provider = FixtureProvider()

    def build(self, **options):
        result = refresh(self.workspace, self.project, provider=self.provider, **options)
        self.assertTrue(resolve_workspace(self.workspace).is_relative_to(self.base))
        return result

    def evidence(self):
        with sqlite3.connect(resolve_workspace(self.workspace) / 'inventory.sqlite3') as db:
            return dict(db.execute('SELECT id,kind FROM evidence'))

    def vector_ids(self):
        with sqlite3.connect(resolve_workspace(self.workspace) / 'semantic.sqlite3') as db:
            return {row[0] for row in db.execute('SELECT DISTINCT chunk FROM vectors')}

    def test_scope_survives_unchanged_and_changed_refresh_without_explicit_kinds(self):
        scope = ['code_member', 'code_type', 'documentation']
        progress = []
        first = self.build(semantic_kinds=list(reversed(scope)),
                           semantic_progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(first['settings']['semantic_kinds'], scope)
        self.assertEqual(first['semantic']['include_kinds'], scope)
        self.assertGreater(first['semantic']['excluded_chunks'], 0)
        self.assertTrue(progress)
        self.assertEqual(progress[-1], (first['semantic']['embedded'], first['semantic']['embedded']))
        evidence = self.evidence()
        self.assertEqual({evidence[identity] for identity in self.vector_ids()}, set(scope))
        self.assertIn('asset', evidence.values())
        calls = self.provider.calls
        unchanged = self.build()
        self.assertEqual(unchanged['state'], 'unchanged')
        self.assertEqual(unchanged['generation'], first['generation'])
        self.assertEqual(self.provider.calls, calls)

        documentation = self.project / 'Assets' / 'Combat.md'
        documentation.write_text(documentation.read_text(encoding='utf-8') +
                                 '\nAdditional targeting notes for refresh.\n', encoding='utf-8')
        changed = self.build()
        self.assertEqual(changed['state'], 'complete')
        self.assertNotEqual(changed['generation'], first['generation'])
        self.assertEqual(changed['settings']['semantic_kinds'], scope)
        self.assertEqual(changed['semantic']['include_kinds'], scope)
        self.assertGreater(changed['semantic']['cache_hits'], 0)
        evidence = self.evidence()
        self.assertEqual({evidence[identity] for identity in self.vector_ids()}, set(scope))
        self.assertIn('asset', evidence.values())

    def test_explicit_all_restores_asset_vectors_from_existing_cache(self):
        complete = self.build()
        evidence = self.evidence()
        calls = self.provider.calls
        complete_vectors = self.vector_ids()
        self.assertEqual(complete_vectors, set(evidence))
        self.assertIn('asset', {evidence[identity] for identity in complete_vectors})
        narrowed = self.build(semantic_kinds=['code_member', 'documentation'])
        self.assertEqual(narrowed['semantic']['embedded'], 0)
        self.assertGreater(narrowed['semantic']['excluded_chunks'], 0)
        self.assertLess(len(self.vector_ids()), len(complete_vectors))
        self.assertEqual(self.evidence(), evidence)
        restored = self.build(semantic_kinds=['all'])
        self.assertEqual(restored['state'], 'complete')
        self.assertIsNone(restored['settings']['semantic_kinds'])
        self.assertIsNone(restored['semantic']['include_kinds'])
        self.assertEqual(restored['semantic']['config_id'], complete['semantic']['config_id'])
        self.assertEqual(restored['semantic']['excluded_chunks'], 0)
        self.assertEqual(restored['semantic']['embedded'], 0)
        self.assertEqual(self.provider.calls, calls)
        self.assertEqual(self.vector_ids(), complete_vectors)
        self.assertEqual(self.build()['state'], 'unchanged')

    def test_interrupted_refresh_resumes_checkpoint_and_preserves_batch_setting(self):
        original = self.build(semantic_kinds=['documentation'], semantic_batch_size=2)
        pointer = (self.workspace/'active.json').read_bytes()
        self.provider.config = {**self.provider.config, 'model':'new-model'}
        def stop(done, total):
            raise KeyboardInterrupt('after durable batch')
        with self.assertRaises(KeyboardInterrupt):
            self.build(semantic_kinds=['all'], semantic_batch_size=2, semantic_progress=stop)
        self.assertEqual((self.workspace/'active.json').read_bytes(),pointer)
        resumed = self.build(semantic_kinds=['all'], semantic_batch_size=2)
        self.assertEqual(resumed['semantic']['cache_hits'],2)
        self.assertEqual(resumed['settings']['semantic_batch_size'],2)
        self.assertEqual(self.build()['state'],'unchanged')
        with self.assertRaisesRegex(ValueError,'batch size'):
            self.build(semantic_batch_size=0)

    def test_invalid_scope_preserves_active_snapshot_and_needs_no_inference(self):
        self.build(semantic_kinds=['documentation'])
        active = resolve_workspace(self.workspace)
        pointer = (self.workspace / 'active.json').read_bytes()
        generations = set((self.workspace / 'generations').iterdir())
        calls = self.provider.calls
        for scope in ([], 'code_member', ('code_member',), ['unknown'], [None],
                      ['all', 'code_member'], ['code_member', 1]):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, 'semantic kinds'):
                self.build(semantic_kinds=scope)
        self.assertEqual(resolve_workspace(self.workspace), active)
        self.assertEqual((self.workspace / 'active.json').read_bytes(), pointer)
        self.assertEqual(set((self.workspace / 'generations').iterdir()), generations)
        self.assertEqual(self.provider.calls, calls)
        manifest = json.loads((active / 'complete.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['settings']['semantic_kinds'], ['documentation'])


if __name__ == '__main__':
    unittest.main()
