"""Named root position retrieval remains bounded, cautious, and cited."""
from pathlib import Path
import shutil
import tempfile
import unittest

from unity_rag.answers import verify_sources
from unity_rag.discovery import status
from unity_rag.retrieval import _promote_named_root_position, retrieve
from unity_rag.workspace import refresh

GUID = 'b' * 32


def document(typ, ident, name, body):
    return f'--- !u!{typ} &{ident}\n{name}:\n{body}'


def instance(ident, name, x='25', target_go=1, target_transform=2, structural='[]'):
    return document(1001, ident, 'PrefabInstance', f'''  m_SourcePrefab: {{fileID: 100100000, guid: {GUID}, type: 3}}
  m_Modification:
    m_Modifications:
    - target: {{fileID: {target_go}, guid: {GUID}, type: 3}}
      propertyPath: m_Name
      value: {name}
      objectReference: {{fileID: 0}}
    - target: {{fileID: {target_transform}, guid: {GUID}, type: 3}}
      propertyPath: m_LocalPosition.x
      value: {x}
      objectReference: {{fileID: 0}}
    m_RemovedComponents: {structural}
    m_AddedComponents: []
''')


class RankingBoundsTests(unittest.TestCase):
    def test_oversized_prefab_record_abstains(self):
        class Rows(list):
            def fetchall(self):
                return list(self)

        class FakeDatabase:
            def execute(self, statement, arguments):
                if 'evidence_aliases' in statement:
                    return Rows([('asset:Assets/Arena.prefab',)])
                if 'unity_objects' in statement:
                    return iter([(' ' * 1000001,)])
                raise AssertionError('Unexpected query')
        rows = [{'kind': 'asset_object', 'entity_id': 'asset:Assets/Arena.prefab#100'}]
        ranked, used = _promote_named_root_position(
            FakeDatabase(), rows, 'What is the serialized x position of Wind in Arena prefab?')
        self.assertFalse(used)
        self.assertEqual(ranked, rows)


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class RootPositionRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / 'Game'
        cls.work = Path(cls.tmp.name) / 'Index'
        (cls.root / 'Assets').mkdir(parents=True)
        (cls.root / 'ProjectSettings').mkdir()
        (cls.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 6000.0.1f1\n')
        base = (document(1, 1, 'GameObject', '  m_Name: Wind\n')
                + document(1, 4, 'GameObject', '  m_Name: Child\n')
                + document(4, 2, 'Transform', '  m_GameObject: {fileID: 1}\n'
                           '  m_Father: {fileID: 0}\n  m_LocalPosition: {x: 10, y: 20, z: 30}\n'))
        (cls.root / 'Assets/Base.prefab').write_text(base)
        (cls.root / 'Assets/Base.prefab.meta').write_text('fileFormatVersion: 2\nguid: ' + GUID + '\n')
        cls.path = cls.root / 'Assets/Arena.prefab'
        cls.path.write_text(instance(100, 'Windsurfer (8)') + instance(101, 'Shared', x='2')
                            + instance(102, 'Shared', x='3')
                            + instance(103, 'Structural', structural='[{fileID: 1}]')
                            + instance(104, 'WrongLink', target_go=4) + instance(105, 'Windsurfer', x='50'))
        refresh(cls.work, cls.root, semantic=False)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def query(self, question):
        return retrieve(self.work, question, limit=20, expand=0, context_chars=100000)

    def promoted(self, result):
        return [item for item in result['results']
                if 'named_root_position' in item.get('ranking_diagnostics', {})]

    def test_correct_and_cited(self):
        result = self.query('What is the serialized x position of Windsurfer (8) in the Arena prefab and its base value?')
        promoted = self.promoted(result)
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0]['detail']['base_value'], '10')
        self.assertEqual(promoted[0]['detail']['effective_value'], '25')
        self.assertEqual(promoted[0]['ranking_diagnostics']['named_root_position'], 'asset:Assets/Arena.prefab#100')
        source_lines = self.path.read_text().splitlines()
        self.assertTrue(any(source['path'] == 'Assets/Arena.prefab'
                            and 'propertyPath: m_Name' in '\n'.join(source_lines[source['start_line'] - 1:source['end_line']])
                            for source in promoted[0]['sources'] if source.get('start_line')))
        self.assertIn('Assets/Base.prefab.meta', {source['path'] for source in promoted[0]['sources']})
        verify_sources(status(self.work), promoted)

    def test_ambiguous_or_unsupported_abstains(self):
        questions = [
            'What is the serialized x position of Shared in Arena prefab?',
            'Compare the serialized x position of Windsurfer (8) and Shared in Arena prefab?',
            'Compare the serialized x position of Windsurfer and Windsurfer (8) in Arena prefab?',
            'What is the serialized x position of Structural in Arena prefab?',
            'What is the serialized x position of WrongLink in Arena prefab?',
            'What is serialized x position of Windsurfer (8)?',
            'What is the x position of Windsurfer (8) in Arena prefab?',
            'What are serialized x position and y position of Windsurfer (8) in Arena prefab?',
        ]
        for question in questions:
            with self.subTest(question=question):
                self.assertFalse(self.promoted(self.query(question)))

    def test_duplicate_prefab_filenames_abstain(self):
        second = self.root / 'Assets/Other/Arena.prefab'
        second.parent.mkdir(exist_ok=True)
        second.write_text(document(1, 20, 'GameObject', '  m_Name: Other\n'))
        try:
            refresh(self.work, semantic=False, rebuild=True)
            question = 'What is the serialized x position of Windsurfer (8) in Arena prefab?'
            self.assertFalse(self.promoted(self.query(question)))
        finally:
            second.unlink()
            refresh(self.work, semantic=False, rebuild=True)

    def test_stale_sources_are_detected(self):
        question = 'What is the serialized x position of Windsurfer (8) in the Arena prefab?'
        promoted = self.promoted(self.query(question))
        original = self.path.read_bytes()
        try:
            self.path.write_bytes(original + b'\n# changed\n')
            with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                verify_sources(status(self.work), promoted)
        finally:
            self.path.write_bytes(original)
