import tempfile
import shutil
import unittest
from pathlib import Path

from unity_rag.discovery import scan
from unity_rag.asset_map import map_assets

GUID = 'b' * 32


def unity_document(class_id, file_id, name, body):
    return f'--- !u!{class_id} &{file_id}\n{name}:\n{body}'


def prefab_instance(prop, *, target=2, source=GUID, target_guid=GUID,
                    value='25', reference='{fileID: 0}', structural='[]'):
    return unity_document(1001, 100, 'PrefabInstance', f'''  m_SourcePrefab: {{fileID: 100100000, guid: {source}, type: 3}}
  m_Modification:
    m_Modifications:
    - target: {{fileID: {target}, guid: {target_guid}, type: 3}}
      propertyPath: {prop}
      value: {value}
      objectReference: {reference}
    m_RemovedComponents: {structural}
    m_AddedComponents: []
''')


class NestedPrefabOverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'Game'
        self.workspace = Path(self.tmp.name) / 'Index'
        self.write('ProjectSettings/ProjectVersion.txt', 'm_EditorVersion: 6000.0.1f1\n')
        self.base = unity_document(4, 2, 'Transform', '''  m_LocalPosition: {x: 10, y: 20, z: 30}
  custom:
    child:
      amount: 17
  items:
  - value: 9
  reference: {fileID: 5}
''')
        self.write('Assets/Base.prefab', self.base, GUID)

    def write(self, path, text, guid=None):
        destination = self.project / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding='utf-8')
        if guid:
            Path(str(destination) + '.meta').write_text('fileFormatVersion: 2\nguid: ' + guid + '\n', encoding='utf-8')

    def report(self, text):
        self.write('Assets/Scene.unity', text)
        scan(self.project, self.workspace)
        return map_assets(self.workspace)['overrides']

    def test_nested_scalars_have_both_exact_source_locations(self):
        for prop, expected in [('m_LocalPosition.x', '10'), ('custom.child.amount', '17')]:
            with self.subTest(prop=prop):
                override = self.report(prefab_instance(prop))[0]
                self.assertEqual(override['resolution'], 'resolved_scalar_override')
                self.assertEqual(override['base_value'], expected)
                self.assertEqual(override['effective_value'], '25')
                self.assertEqual(override['property'], prop)
                base_line = override['base_location']['start_line']
                self.assertIn(expected, self.base.splitlines()[base_line - 1])
                self.assertEqual(override['base_location']['path'], 'Assets/Base.prefab')
                self.assertEqual(override['location']['path'], 'Assets/Scene.unity')

    def test_nested_override_rejects_arrays_references_and_non_scalars(self):
        for prop, ref in [('items.Array.data[0].value', '{fileID: 0}'),
                          ('custom.child', '{fileID: 0}'),
                          ('reference.fileID', '{fileID: 0}'),
                          ('m_LocalPosition.x', '{fileID: 8}')]:
            with self.subTest(prop=prop, ref=ref):
                override = self.report(prefab_instance(prop, reference=ref))[0]
                self.assertEqual(override['resolution'], 'unsupported_override')
                self.assertNotIn('effective_value', override)

    def test_collision_with_literal_dotted_key_fails_closed(self):
        self.write('Assets/Base.prefab', self.base + '  m_LocalPosition.x: 999\n', GUID)
        override = self.report(prefab_instance('m_LocalPosition.x'))[0]
        self.assertEqual(override['resolution'], 'unsupported_override')
        self.assertNotIn('effective_value', override)

    def test_structural_and_duplicate_overrides_remain_unsupported(self):
        structural = self.report(prefab_instance('m_LocalPosition.x', structural='[{fileID: 2}]'))[0]
        self.assertEqual(structural['resolution'], 'unsupported_override')
        text = prefab_instance('m_LocalPosition.x')
        item = '''    - target: {fileID: 2, guid: %s, type: 3}
      propertyPath: m_LocalPosition.x
      value: 30
      objectReference: {fileID: 0}
''' % GUID
        text = text.replace('    m_RemovedComponents:', item + '    m_RemovedComponents:')
        overrides = self.report(text)
        self.assertEqual([o['resolution'] for o in overrides], ['ambiguous_duplicate_override'] * 2)
        self.assertTrue(all('effective_value' not in o for o in overrides))

    def test_nested_source_chain_and_mismatched_target_are_rejected(self):
        other = 'c' * 32
        self.write('Assets/Other.prefab', self.base, other)
        mismatch = self.report(prefab_instance('m_LocalPosition.x', target_guid=other))[0]
        self.assertEqual(mismatch['resolution'], 'unsupported_override')
        self.write('Assets/Variant.prefab', prefab_instance('m_LocalPosition.x'), other)
        nested = self.report(prefab_instance('m_LocalPosition.x', source=other))[0]
        self.assertEqual(nested['resolution'], 'unsupported_override')

    @unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
    def test_retrieval_and_refresh_track_nested_override_sources(self):
        from unity_rag.answers import verify_sources
        from unity_rag.asset_map import asset_report
        from unity_rag.discovery import status
        from unity_rag.retrieval import retrieve
        from unity_rag.workspace import refresh

        scene = self.project / 'Assets/Scene.unity'
        self.write('Assets/Scene.unity', prefab_instance('m_LocalPosition.x'))
        refresh(self.workspace, self.project, semantic=False)
        def result():
            results = retrieve(self.workspace, 'm_LocalPosition.x', kind='prefab_override',
                               limit=10, context_chars=100000)['results']
            return next(row for row in results if row['detail'].get('property') == 'm_LocalPosition.x')
        row = result()
        self.assertEqual(row['evidence_type'], 'derived_interpretation')
        self.assertEqual(row['detail']['base_value'], '10')
        self.assertEqual(row['detail']['effective_value'], '25')
        self.assertEqual({s['path'] for s in row['sources'] if s.get('role') != 'reference_dependency'},
                         {'Assets/Base.prefab', 'Assets/Scene.unity'})
        self.assertIn('Assets/Base.prefab.meta', {s['path'] for s in row['sources']
                                                 if s.get('role') == 'reference_dependency'})
        for path in ('Assets/Base.prefab', 'Assets/Base.prefab.meta', 'Assets/Scene.unity'):
            target = self.project / path
            original = target.read_bytes()
            try:
                target.write_bytes(original + b'\n# changed\n')
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'changed since discovery'):
                    verify_sources(status(self.workspace), [result()])
            finally:
                target.write_bytes(original)
        base = self.project / 'Assets/Base.prefab'
        base.write_text(self.base.replace('x: 10', 'x: 11'), encoding='utf-8')
        self.assertEqual(refresh(self.workspace, semantic=False)['refresh_mode'], 'full')
        self.assertEqual(result()['detail']['base_value'], '11')
        scene.write_text(prefab_instance('m_LocalPosition.x', value='27'), encoding='utf-8')
        self.assertEqual(refresh(self.workspace, semantic=False)['refresh_mode'], 'full')
        self.assertEqual(result()['detail']['effective_value'], '27')
        before = asset_report(self.workspace)['overrides']
        refresh(self.workspace, semantic=False, rebuild=True)
        self.assertEqual(before, asset_report(self.workspace)['overrides'])
