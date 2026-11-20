from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from unity_rag.asset_map import asset_report, map_assets
from unity_rag.code_map import map_code
from unity_rag.discovery import scan
from unity_rag.retrieval import build_retrieval, retrieve
from unity_rag.answers import ask
from unity_rag.semantic import SEMANTIC_KINDS


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class AssetSourceRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.project = Path(cls.temp.name) / 'project'
        cls.work = Path(cls.temp.name) / 'workspace'
        shutil.copytree(Path(__file__).parent / 'fixtures/retrieval_project', cls.project)
        cls.large = ('%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n'
                     '  label: "literal spelling" # keep this comment\n  values:\n'
                     + ''.join(f'  - element{i:03d}\n' for i in range(180))
                     + '  specialCooldown: 17\n--- !u!1 &2\nGameObject:\n  m_Name: SecondObject\n')
        (cls.project / 'Assets/Large.prefab').write_text(cls.large, encoding='utf-8')
        (cls.project / 'Assets/Broken.prefab').write_text('not Unity serialization\n', encoding='utf-8')
        (cls.project / 'Assets/Binary.prefab').write_bytes(b'\x00binary')
        scan(cls.project, cls.work)
        map_code(cls.work)
        map_assets(cls.work)
        build_retrieval(cls.work, chunk_budget=512)

    def source_rows(self):
        with closing(sqlite3.connect(self.work / 'inventory.sqlite3')) as db:
            return [json.loads(raw) for raw, in db.execute("SELECT data FROM evidence WHERE kind='asset_source'")]

    def test_chunks_are_literal_bounded_and_confined_to_one_object(self):
        objects = {obj['id']: obj for obj in asset_report(self.work)['objects']}
        rows = self.source_rows()
        self.assertTrue(rows)
        for row in rows:
            obj = objects[row['entity_id']]
            source = row['sources'][0]
            lines = (self.project / source['path']).read_text(encoding='utf-8-sig').splitlines()
            self.assertEqual(row['text'], '\n'.join(lines[source['start_line'] - 1:source['end_line']]))
            self.assertGreaterEqual(source['start_line'], obj['location']['start_line'])
            self.assertLessEqual(source['end_line'], obj['location']['end_line'])
            self.assertLessEqual(len(row['text'].encode()), 512)
            self.assertEqual(row['parent'], obj['asset'])
            self.assertEqual(row['evidence_type'], 'original_source')

    def test_large_object_keeps_lists_comments_and_every_line(self):
        rows = [r for r in self.source_rows() if r['entity_id'] == 'asset:Assets/Large.prefab#1']
        self.assertGreater(len(rows), 1)
        self.assertTrue(any('  label: "literal spelling" # keep this comment' in r['text'] for r in rows))
        covered = {i for row in rows for i in range(row['sources'][0]['start_line'], row['sources'][0]['end_line'] + 1)}
        obj = next(o for o in asset_report(self.work)['objects'] if o['id'] == rows[0]['entity_id'])
        self.assertEqual(covered, set(range(obj['location']['start_line'], obj['location']['end_line'] + 1)))

    def test_source_kind_filter_and_answer_citations(self):
        self.assertIn('asset_source', SEMANTIC_KINDS)
        result = retrieve(self.work, 'specialCooldown', kind='asset_source', limit=1)
        self.assertEqual(len(result['results']), 1)
        self.assertIn('  specialCooldown: 17', result['results'][0]['text'])
        answer = ask(self.work, 'What specialCooldown value is serialized in Assets/Large.prefab?', mode='search', style='extractive',
                     followups=0, context_chars=1500)
        literal = [r for r in answer['evidence'].values() if r['provenance']['record_kind'] == 'asset_source']
        self.assertTrue(literal)
        for row in literal:
            source = row['sources'][0]
            lines = (self.project / source['path']).read_text().splitlines()
            self.assertEqual(row['text'], '\n'.join(lines[source['start_line'] - 1:source['end_line']]))

    def test_failed_or_binary_assets_do_not_gain_literal_evidence(self):
        paths = {s['path'] for row in self.source_rows() for s in row['sources']}
        self.assertNotIn('Assets/Broken.prefab', paths)
        self.assertNotIn('Assets/Binary.prefab', paths)

    def test_parsed_relationships_remain_available(self):
        result = retrieve(self.work, 'movement speed', kind='asset_object')
        self.assertTrue(result['results'])
        self.assertTrue(all(r['evidence_type'] == 'parsed_fact' for r in result['results']))
        edges = retrieve(self.work, 'Assets/Enemy.cs', mode='used-by')['results']
        self.assertTrue(any(e['kind'] == 'uses_script' for e in edges))

    def test_literal_index_does_not_change_general_keyword_statistics(self):
        with closing(sqlite3.connect(self.work / 'inventory.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM evidence_fts f JOIN evidence e ON e.id=f.id WHERE e.kind='asset_source'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM asset_source_fts').fetchone()[0], len(self.source_rows()))
        result = retrieve(self.work, 'attack cooldown')
        self.assertTrue(result['results'])
        self.assertFalse(any(r['kind'] == 'asset_source' for r in result['results']))

    def test_older_index_without_source_table_still_supports_parsed_queries(self):
        legacy = Path(self.temp.name) / 'legacy'
        legacy.mkdir()
        shutil.copyfile(self.work / 'inventory.sqlite3', legacy / 'inventory.sqlite3')
        with closing(sqlite3.connect(legacy / 'inventory.sqlite3')) as db, db:
            db.execute('DROP TABLE asset_source_fts')
            db.execute("DELETE FROM evidence WHERE kind='asset_source'")
        self.assertEqual(retrieve(legacy, 'specialCooldown', kind='asset_source')['results'], [])
        result = retrieve(legacy, 'What specialCooldown value is saved in the prefab?')
        self.assertTrue(result['results'])
        self.assertFalse(any(r['kind'] == 'asset_source' for r in result['results']))
