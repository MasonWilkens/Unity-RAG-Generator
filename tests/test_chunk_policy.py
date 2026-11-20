from contextlib import closing
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from unity_rag.chunking import ChunkPolicy
from unity_rag.workspace import refresh, resolve_workspace
from unity_rag.retrieval import retrieve


class ChunkPolicyTests(unittest.TestCase):
    def test_bounded_overlap_preserves_all_lines_and_progress(self):
        policy = ChunkPolicy(128)
        lines = ['λ line number %d padding' % i for i in range(260)]
        windows = list(policy.windows(lines))
        self.assertEqual({i for a, b in windows for i in range(a, b)}, set(range(len(lines))))
        self.assertTrue(all(policy.count('\n'.join(lines[a:b])) <= 128 for a, b in windows))
        self.assertTrue(all(a < b and b-a <= 120 for a, b in windows))
        self.assertTrue(all(windows[i+1][0] > a for i, (a,b) in enumerate(windows[:-1])))
        self.assertTrue(all(windows[i+1][0] < b for i, (a,b) in enumerate(windows[:-1])))

    def test_single_oversized_line_is_preserved_and_reported(self):
        policy = ChunkPolicy(128)
        lines = ['x' * 200, 'small']
        self.assertEqual(list(policy.windows(lines)), [(0, 1), (1, 2)])
        self.assertEqual(policy.oversized_lines, 1)

    @unittest.skipUnless(importlib.util.find_spec('tiktoken'), 'Optional token counter not installed')
    def test_real_token_budget_uses_selected_encoding(self):
        policy = ChunkPolicy(128, 'cl100k_base')
        lines = ['float serializedValue%d = 0.123456789f; // 日本語' % i for i in range(100)]
        self.assertEqual(policy.config['unit'], 'tokens')
        for a, b in policy.windows(lines):
            self.assertLessEqual(policy.count('\n'.join(lines[a:b])), 128)


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class ChunkBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.project, cls.work = root/'project', root/'workspace'
        shutil.copytree(Path(__file__).parent/'fixtures/passage_project', cls.project)
        (cls.project/'Assets/LargeType.cs').write_text('class LargeType {\n' + '\n'.join(
            ' public int Value%d;' % i for i in range(220)) + '\n}', encoding='utf-8')
        refresh(cls.work, cls.project, semantic=False, chunk_budget=1024)

    def test_new_source_kinds_have_literal_source_citations(self):
        for query, kind in [('CoolingStep', 'shader_source'), ('venting-status', 'ui_source'),
                            ('maximumHeat', 'configuration_source')]:
            result = retrieve(self.work, query, kind=kind)
            self.assertTrue(result['results'], query)
            for row in result['results']:
                self.assertEqual(row['evidence_type'], 'original_source')
                source = row['sources'][0]
                text = (self.project/source['path']).read_text(encoding='utf-8').splitlines()
                self.assertEqual(row['text'], '\n'.join(text[source['start_line']-1:source['end_line']]))

    def test_large_type_chunks_keep_identity_and_headers(self):
        rows = retrieve(self.work, 'LargeType', mode='exact', limit=100)['results']
        self.assertGreater(len(rows), 1)
        self.assertEqual(len({r['entity_id'] for r in rows}), 1)
        for row in rows:
            self.assertIn('class LargeType', row['text'])
            self.assertLessEqual(len(row['text'].encode()), 1024)

    def test_saved_policy_reuses_unchanged_generation(self):
        before = resolve_workspace(self.work)
        refresh(self.work)
        self.assertEqual(resolve_workspace(self.work), before)
        manifest = json.loads((before/'complete.json').read_text())
        self.assertEqual(manifest['settings']['chunking']['budget'], 1024)
        with closing(sqlite3.connect(before/'inventory.sqlite3')) as db, db:
            report = json.loads(db.execute("SELECT value FROM metadata WHERE key='retrieval_report'").fetchone()[0])
        self.assertEqual(report['chunking']['budget'], 1024)
