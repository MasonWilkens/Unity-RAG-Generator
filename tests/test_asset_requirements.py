"""Unavailable saved values cannot be answered with neighboring code defaults."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.answers import ask, format_answer
from unity_rag.asset_requirements import serialized_asset_requirement
from unity_rag.retrieval import retrieve, format_results
from unity_rag.workspace import refresh, resolve_workspace


class AssetRequirementBoundsTests(unittest.TestCase):
    def test_unrelated_questions_do_not_scan(self):
        class NoDatabase:
            def execute(self, *args):
                raise AssertionError('Unexpected database access')
        for question in ('How does SaveManager store the score?', 'Locate OpaqueBox.prefab',
                         'How does PrefabLoader initialize a prefab?', 'What is the saved clock?',
                         'What is serialized in the prefab ' + 'value ' * 70):
            self.assertIsNone(serialized_asset_requirement(NoDatabase(), question))

    def test_large_inventory_abstains_without_truncated_name_resolution(self):
        with closing(sqlite3.connect(':memory:')) as db, db:
            db.execute('CREATE TABLE files (path,kind,status,reason,sha256)')
            db.executemany('INSERT INTO files VALUES (?,?,?,?,?)',
                           [(f'Assets/Box{i}.prefab', 'prefab', 'binary', 'binary_or_non_utf8', 'a') for i in range(10001)])
            self.assertIsNone(serialized_asset_requirement(db, 'What value is saved in the Box1 prefab?'))


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class AssetRequirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name) / 'Game'
        cls.work = Path(cls.tmp.name) / 'Knowledge'
        (cls.root / 'Assets').mkdir(parents=True)
        (cls.root / 'ProjectSettings').mkdir()
        (cls.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 6000.0.1f1\n')
        (cls.root / 'Assets/OpaqueBoxDefaults.cs').write_text('''public class OpaqueBoxDefaults {
 public int savedCapacity = 999;
 public int scriptReference = 22;
 public int GetCapacity() { return savedCapacity; }
}
''', encoding='utf-8')
        for name in ('OpaqueBox.prefab', 'StorageBank.asset', 'BadScene.unity', 'Panel.prefab', 'Cool Panel.prefab'):
            (cls.root / 'Assets' / name).write_bytes(b'\0binary serialized capacity scriptReference')
        (cls.root / 'Assets/BrokenBox.prefab').write_text('not Unity serialization\n')
        (cls.root / 'Assets/MissingPayload.prefab').write_text(
            'version https://git-lfs.github.com/spec/v1\noid sha256:' + 'a' * 64 + '\nsize 5000\n')
        cls.yaml = '%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  m_Name: ReadableBox\n  savedCapacity: 13\n'
        (cls.root / 'Assets/ReadableBox.prefab').write_text(cls.yaml, encoding='utf-8')
        for folder in ('A', 'B'):
            (cls.root / 'Assets' / folder).mkdir()
        (cls.root / 'Assets/A/TwinBox.prefab').write_bytes(b'\0binary')
        (cls.root / 'Assets/B/TwinBox.prefab').write_text(cls.yaml, encoding='utf-8')
        refresh(cls.work, cls.root, semantic=False)
        cls.generation = resolve_workspace(cls.work)
        with closing(sqlite3.connect(cls.generation / 'inventory.sqlite3')) as db:
            cls.scores = {json.loads(raw)['id']: 1 for raw, in db.execute('SELECT data FROM evidence')}

    def query(self, question, mode='search', **kwargs):
        with patch('unity_rag.semantic.rank', return_value=self.scores):
            return retrieve(self.work, question, mode=mode, **kwargs)

    def test_binary_asset_questions_return_a_snapshot_gap_in_all_modes(self):
        for question in ('What savedCapacity is serialized in Assets/OpaqueBox.prefab?',
                         'Which scriptReference is saved in the OpaqueBox prefab?',
                         'What savedCapacity is stored in the asset named StorageBank?',
                         'What value is saved in the BadScene scene?',
                         'What value is saved in the "Cool Panel" prefab?',
                         'What value is saved in prefab named "Cool Panel"?'):
            for mode in ('search', 'semantic', 'hybrid'):
                result = self.query(question, mode=mode, expand=5)
                self.assertEqual(result['results'], [], question)
                self.assertEqual(result['context'], [], question)
                self.assertEqual(result['candidate_count'], 0)
                self.assertEqual(result['asset_requirement']['status'], 'source_unavailable')
                self.assertEqual(result['asset_requirement']['scope'], 'indexed_snapshot')
                self.assertIn('indexed snapshot', format_results(result))

    def test_both_answer_styles_report_the_gap_without_a_generation_call(self):
        class NoGeneration:
            config = {'provider': 'test', 'model': 'no-call'}
            def generate(self, *args):
                raise AssertionError('Unavailable serialization must not call generation')
        question = 'What savedCapacity is serialized in the OpaqueBox prefab?'
        for style in ('extractive', 'explain'):
            result = ask(self.work, question, NoGeneration() if style == 'explain' else None,
                         mode='search', style=style, followups=2, show_sources=True)
            self.assertEqual(result['status'], 'insufficient_evidence')
            self.assertEqual(result['model_calls'], 0)
            self.assertEqual(result['evidence'], {})
            self.assertEqual(result['claims'], [])
            self.assertIn('indexed snapshot', format_answer(result))
            self.assertIn('OpaqueBox.prefab', ' '.join(result['missing_evidence']))

    def test_lfs_and_malformed_serialization_have_distinct_reasons(self):
        lfs = self.query('What value is saved in Assets/MissingPayload.prefab?')['asset_requirement']
        self.assertEqual(lfs['inventory_status'], 'lfs_pointer')
        self.assertIn('unmaterialized_git_lfs_pointer', lfs['description'])
        malformed = self.query('What value is saved in Assets/BrokenBox.prefab?')['asset_requirement']
        self.assertEqual(malformed['inventory_status'], 'supported')
        self.assertEqual(malformed['parse_status'], 'unparsed')
        self.assertEqual(malformed['status'], 'serialization_unavailable')

    def test_supported_serialized_asset_keeps_literal_evidence(self):
        question = 'What savedCapacity is serialized in Assets/ReadableBox.prefab?'
        result = self.query(question)
        self.assertEqual(result['asset_requirement']['status'], 'available')
        self.assertTrue(result['results'])
        answer = ask(self.work, question, mode='search', style='extractive', followups=0)
        self.assertNotEqual(answer['status'], 'insufficient_evidence')
        self.assertTrue(any('savedCapacity: 13' in r['text'] for r in answer['evidence'].values()))

    def test_duplicate_names_require_a_full_path_even_if_one_copy_is_readable(self):
        ambiguous = self.query('What value is saved in the TwinBox prefab?')
        self.assertEqual(ambiguous['asset_requirement']['status'], 'ambiguous_target')
        self.assertEqual(ambiguous['results'], [])
        self.assertEqual(len(ambiguous['asset_requirement']['paths']), 2)
        supported = self.query('What value is saved in Assets/B/TwinBox.prefab?')
        self.assertEqual(supported['asset_requirement']['status'], 'available')
        binary = self.query('What value is saved in Assets/A/TwinBox.prefab?')
        self.assertEqual(binary['asset_requirement']['status'], 'source_unavailable')

    def test_lookup_kind_filters_and_code_questions_remain_available(self):
        exact = self.query('Assets/OpaqueBox.prefab', mode='exact')
        self.assertIsNone(exact['asset_requirement'])
        self.assertTrue(exact['results'])
        question = 'What savedCapacity is serialized in Assets/OpaqueBox.prefab?'
        filtered = self.query(question, kind='code_member')
        self.assertIsNone(filtered['asset_requirement'])
        self.assertTrue(filtered['results'])
        ordinary = self.query('How does OpaqueBoxDefaults return savedCapacity?')
        self.assertIsNone(ordinary['asset_requirement'])
        self.assertTrue(ordinary['results'])
        loading = self.query('How does OpaqueBoxDefaults load stored data from Assets/OpaqueBox.prefab?')
        self.assertIsNone(loading['asset_requirement'])
        self.assertTrue(loading['results'])

    def test_partial_names_and_multiple_different_targets_do_not_guess(self):
        for question in ('What value is saved in the Old Panel prefab?',
                         'What value is saved in prefab named Panel Pro?',
                         'What value is saved in "Old Panel.prefab"?',
                         'What value is saved in Old Panel.prefab?',
                         'What values are saved in the OpaqueBox prefab and the ReadableBox prefab?'):
            self.assertIsNone(self.query(question)['asset_requirement'], question)

    def test_gap_remains_scoped_to_old_snapshot_until_refresh(self):
        path = self.root / 'Assets/OpaqueBox.prefab'
        original = path.read_bytes()
        try:
            path.write_text(self.yaml, encoding='utf-8')
            result = self.query('What value is serialized in Assets/OpaqueBox.prefab?')
            self.assertEqual(result['asset_requirement']['scope'], 'indexed_snapshot')
            self.assertEqual(result['results'], [])
            self.assertIn('refresh', result['asset_requirement']['description'])
        finally:
            path.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
