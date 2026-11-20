"""Explicit object lookup must respect type, hierarchy, ambiguity and citations."""
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.answers import verify_sources
from unity_rag.discovery import status
from unity_rag.query_ranking import promote_named_gameobjects
from unity_rag.retrieval import retrieve
from unity_rag.workspace import refresh, resolve_workspace


def gameobject(ident, name, father=0, stripped=False, missing=False, rect=False):
    transform = ident + 1
    type_id, type_name = (224, 'RectTransform') if rect else (4, 'Transform')
    obj = f"--- !u!1 &{ident}{' stripped' if stripped else ''}\nGameObject:\n  m_Name: {name}\n  m_Component:\n  - component: {{fileID: {transform}}}\n"
    if not missing:
        obj += f"--- !u!{type_id} &{transform}\n{type_name}:\n  m_GameObject: {{fileID: {ident}}}\n  m_Father: {{fileID: {father}}}\n"
    return obj


class ObjectLookupBoundsTests(unittest.TestCase):
    def test_unsupported_questions_do_not_query_database(self):
        class NoDatabase:
            def execute(self, *args):
                raise AssertionError('unsupported requests must not query objects')
        for question in ('How does Beacon find a GameObject?', 'Find the Beacon GameObject components',
                         'Find Beacon or Beacon2 GameObject', 'Find Beacon Transform',
                         'Find the Beacon root GameObject without a collider', 'Find Beacon Room GameObject'):
            self.assertEqual(promote_named_gameobjects(NoDatabase(), [], question), ([], False))
        self.assertEqual(promote_named_gameobjects(NoDatabase(), [], 'Find Beacon GameObject', 'code_member'), ([], False))

    def test_object_count_and_record_size_limits_abstain(self):
        class Rows(list):
            def fetchall(self):
                return self
        class Database:
            def __init__(self, records):
                self.records = records
            def execute(self, *args):
                return Rows(self.records)
        for records in ([('id', '{}')] * 101, [('id', ' ' * 1000001)]):
            self.assertEqual(promote_named_gameobjects(Database(records), [], 'Find Beacon GameObject'), ([], False))


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class NamedObjectRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / 'Game'
        cls.work = Path(cls.tmp.name) / 'Index'
        (cls.root / 'Assets').mkdir(parents=True)
        (cls.root / 'ProjectSettings').mkdir()
        (cls.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 6000.0.1f1\n', encoding='utf-8')
        cls.prefab = cls.root / 'Assets/Outpost.prefab'
        cls.prefab.write_text(gameobject(1, 'Beacon') + gameobject(3, 'Beacon', father=2)
                              + gameobject(5, 'Cargo Bay', rect=True) + gameobject(7, 'Detached', missing=True)
                              + gameobject(9, 'Hidden', stripped=True)
                              + '--- !u!114 &99\nMonoBehaviour:\n  m_Name: Beacon\n  m_GameObject: {fileID: 1}\n', encoding='utf-8')
        (cls.root / 'Assets/Other.prefab').write_text(gameobject(1, 'Beacon'), encoding='utf-8')
        refresh(cls.work, cls.root, semantic=False)
        # Controlled scores make the component and child outrank the root, so
        # tests exercise resolution independently of any learned model's quality.
        with closing(sqlite3.connect(resolve_workspace(cls.work) / 'inventory.sqlite3')) as db:
            rows = [json.loads(raw) for raw, in db.execute('SELECT data FROM evidence')]
        cls.scores = {r['id']: (1.0 if r['entity_id'].endswith('#99') else .5) for r in rows}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def query(self, text='Find the Beacon root GameObject.', mode='search', **kwargs):
        with patch('unity_rag.semantic.rank', return_value=self.scores):
            return retrieve(self.work, text, mode=mode, limit=20, context_chars=100000, **kwargs)

    def promoted(self, result):
        return [r for r in result['results'] if 'named_unity_object' in r.get('ranking_diagnostics', {})]

    def test_root_resolution_all_modes_preserves_ambiguity_and_dependency(self):
        for mode in ('search', 'semantic', 'hybrid'):
            result = self.query(mode=mode)
            promoted = self.promoted(result)
            self.assertEqual(len(promoted), 2)
            self.assertEqual({r['entity_id'] for r in promoted},
                             {'asset:Assets/Outpost.prefab#1', 'asset:Assets/Other.prefab#1'})
            self.assertEqual(result['results'][:2], promoted)
            for row in promoted:
                self.assertTrue(row['text'].startswith('Object: GameObject;'))
                self.assertEqual(row['ranking_diagnostics']['selected_passage'], 'named_object')
                self.assertTrue(row['ranking_diagnostics']['named_unity_object']['serialized_root_verified'])
                dependency = [s for s in row['sources'] if s.get('role') == 'reference_dependency']
                self.assertTrue(dependency)
                self.assertTrue(all(s['sha256'] for s in dependency))

    def test_plain_name_keeps_children_and_components_never_match_type(self):
        result = self.query('Locate Beacon GameObject')
        promoted = self.promoted(result)
        self.assertEqual(len(promoted), 3)
        self.assertTrue(all(r['entity_id'] != 'asset:Assets/Outpost.prefab#99' for r in promoted))
        self.assertTrue(all(not r['ranking_diagnostics']['named_unity_object']['serialized_root_verified'] for r in promoted))

    def test_quoted_names_recttransform_and_case(self):
        rows = self.promoted(self.query('show me the "cargo bay" root gameobject'))
        self.assertEqual([r['entity_id'] for r in rows], ['asset:Assets/Outpost.prefab#5'])

    def test_missing_link_stripped_unknown_and_wrong_kind_abstain(self):
        for name in ('Detached', 'Hidden', 'Unknown'):
            self.assertFalse(self.promoted(self.query(f'Find {name} root GameObject')))
        self.assertFalse(self.promoted(self.query(kind='asset')))
        self.assertFalse(self.promoted(self.query(mode='exact')))
        self.assertFalse(self.promoted(self.query('How does Beacon find its GameObject?')))

    def test_fixed_budget_and_exact_aliases(self):
        with patch('unity_rag.semantic.rank', return_value=self.scores):
            result = retrieve(self.work, 'Find Beacon root GameObject', mode='hybrid', context_chars=1)
        self.assertEqual(result['results'], [])
        self.assertLessEqual(result['context_budget']['used_chars'], 1)
        result = self.query('Beacon', mode='hybrid')
        self.assertEqual(result['results'][0]['match'], 'exact_alias')

    def test_changed_hierarchy_requires_refresh(self):
        rows = self.promoted(self.query())
        original = self.prefab.read_bytes()
        try:
            self.prefab.write_bytes(original.replace(b'm_Father: {fileID: 0}', b'm_Father: {fileID: 2}'))
            with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                verify_sources(status(self.work), rows)
            result = self.query()
            self.assertTrue(any(r['status'] != 'current' for r in result['source_freshness']))
        finally:
            self.prefab.write_bytes(original)

    def test_wrong_backlink_or_external_parent_does_not_prove_root(self):
        # Alter a disposable database, never the active fixture generation.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'test.sqlite3'
            shutil.copyfile(resolve_workspace(self.work) / 'inventory.sqlite3', target)
            with closing(sqlite3.connect(target)) as db:
                for key, value in (('m_GameObject', {'fileID': '777'}),
                                   ('m_Father', {'fileID': '0', 'guid': 'a' * 32}),
                                   ('m_PrefabInstance', {'fileID': '100'})):
                    raw = db.execute('SELECT data FROM unity_objects WHERE id=?', ('asset:Assets/Outpost.prefab#2',)).fetchone()[0]
                    obj = json.loads(raw)
                    obj['data']['m_GameObject'] = {'fileID': '1'}
                    obj['data']['m_Father'] = {'fileID': '0'}
                    obj['data'][key] = value
                    db.execute('UPDATE unity_objects SET data=? WHERE id=?',
                               (json.dumps(obj), 'asset:Assets/Outpost.prefab#2'))
                    ranked, _ = promote_named_gameobjects(db, [], 'Find Beacon root GameObject')
                    self.assertNotIn('asset:Assets/Outpost.prefab#1', [r['entity_id'] for r in ranked])
