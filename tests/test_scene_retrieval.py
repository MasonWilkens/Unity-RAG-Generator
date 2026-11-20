import json
from pathlib import Path
import shutil
import tempfile
import unittest

from unity_rag.answers import _guard_serialized_scene_claims, ask, verify_sources
from unity_rag.asset_map import map_assets
from unity_rag.code_map import map_code
from unity_rag.discovery import scan, status
from unity_rag.retrieval import build_retrieval, retrieve


def make_scene(guid, values):
    components = ''.join(f'  - component: {{fileID: {i}}}\n' for i in range(1, len(values) + 1))
    docs = ['%YAML 1.1\n--- !u!1 &100\nGameObject:\n  m_Name: FixtureRoot\n  m_Component:\n' + components]
    for i, value in enumerate(values, 1):
        docs.append(f'''--- !u!114 &{i}
MonoBehaviour:
  m_ObjectHideFlags: 0
  m_GameObject: {{fileID: 100}}
  m_Enabled: 1
  m_Script: {{fileID: 11500000, guid: {guid}, type: 3}}
  m_ActivatesAutomatically: {value}
''')
    return ''.join(docs)


class ScriptedProvider:
    config = {'provider': 'fixture'}

    def __init__(self, target):
        self.target = target

    def generate(self, messages, schema):
        evidence = json.loads(messages[1]['content'])['evidence']
        if self.target == 'code':
            key = next(k for k, value in evidence.items() if any(
                source['path'].endswith('.cs') and not source.get('role')
                for source in value['sources']))
        else:
            key = next(k for k, value in evidence.items()
                       if 'm_ActivatesAutomatically: 0' in value['text'])
        return {'claims': [{'kind': 'fact', 'text': 'A test claim.', 'evidence_ids': [key]}],
                'missing_evidence': [], 'followup_queries': []}


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class SceneRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Game'
        self.work = Path(self.temp.name) / 'Knowledge'
        (self.root / 'Assets').mkdir(parents=True)
        (self.root / 'ProjectSettings').mkdir()
        (self.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 2022.3.1f1\n')
        self.guid = '11111111111111111111111111111111'
        (self.root / 'Assets/SceneDemo.cs').write_text(
            'public class SceneDemo { public bool m_ActivatesAutomatically = true; '
            'public bool IsActive() { return m_ActivatesAutomatically; } }\n')
        (self.root / 'Assets/SceneDemo.cs.meta').write_text('fileFormatVersion: 2\nguid: ' + self.guid + '\n')
        (self.root / 'Assets/SceneDemo.unity').write_text(make_scene(self.guid, [0]))

    def build(self):
        scan(self.root, self.work)
        map_code(self.work)
        map_assets(self.work)
        build_retrieval(self.work)

    def test_unique_component_precedes_conflicting_code_initializer(self):
        self.build()
        result = retrieve(self.work, 'In the SceneDemo scene, is activation enabled in its serialized component?',
                          mode='search', limit=5)
        self.assertIn('m_ActivatesAutomatically: 0', result['results'][0]['text'])
        self.assertEqual(result['results'][0]['ranking_diagnostics']['scene_component_anchor'], 'Assets/SceneDemo.unity')
        self.assertIn('named-scene', result['ranking'])

    def test_ambiguous_components_do_not_anchor(self):
        (self.root / 'Assets/SceneDemo.unity').write_text(make_scene(self.guid, [0, 1]))
        self.build()
        result = retrieve(self.work, 'What is serialized in the SceneDemo scene?', mode='search', limit=10)
        self.assertFalse(any('scene_component_anchor' in row.get('ranking_diagnostics', {}) for row in result['results']))

    def test_ambiguous_scene_paths_do_not_anchor(self):
        other = self.root / 'Assets/Other'
        other.mkdir()
        (other / 'SceneDemo.unity').write_text(make_scene(self.guid, [1]))
        self.build()
        result = retrieve(self.work, 'What is serialized in the SceneDemo scene?', mode='search', limit=10)
        self.assertFalse(any('scene_component_anchor' in row.get('ranking_diagnostics', {}) for row in result['results']))

    def test_non_scene_and_kind_filter_do_not_anchor(self):
        self.build()
        for question, kind in [('What is SceneDemo default?', None),
                               ('What is serialized in the SceneDemo scene?', 'code_member')]:
            result = retrieve(self.work, question, mode='search', kind=kind, limit=10)
            self.assertFalse(any('scene_component_anchor' in row.get('ranking_diagnostics', {})
                                 for row in result['results']))

    def test_asset_object_filter_does_not_add_code_members(self):
        self.build()
        question = 'What is serialized in the SceneDemo scene and how does SceneDemo use it?'
        result = retrieve(self.work, question, mode='search', kind='asset_object', limit=8)
        self.assertTrue(result['results'])
        self.assertTrue(all(row['kind'] == 'asset_object' for row in result['results']))
        self.assertFalse(any('scene_field_usage' in row.get('ranking_diagnostics', {})
                             for row in result['results']))

    def test_large_component_anchor_tracks_script_when_link_is_in_another_chunk(self):
        path = self.root / 'Assets/SceneDemo.unity'
        path.write_text(path.read_text() + ''.join(f'  fillerField{i}: {i}\n' for i in range(80)))
        self.build()
        question = 'What is serialized in the SceneDemo scene component?'
        result = retrieve(self.work, question, mode='search', limit=8, context_chars=100000)
        row = next(row for row in result['results'] if 'm_ActivatesAutomatically: 0' in row['text'])
        self.assertNotIn('m_Script ->', row['text'])
        dependencies = {source['path'] for source in row['sources'] if source.get('role') == 'reference_dependency'}
        self.assertTrue({'Assets/SceneDemo.cs', 'Assets/SceneDemo.cs.meta'} <= dependencies)
        meta = self.root / 'Assets/SceneDemo.cs.meta'
        meta.write_text(meta.read_text() + '# changed\n')
        result = retrieve(self.work, question, mode='search', limit=8, context_chars=100000)
        row = next(row for row in result['results'] if 'm_ActivatesAutomatically: 0' in row['text'])
        with self.assertRaisesRegex(ValueError, 'changed since discovery'):
            verify_sources(status(self.work), [row])

    def test_linked_method_tracks_scene_and_script_metadata(self):
        self.build()
        question = 'What is serialized in the SceneDemo scene and how does SceneDemo use it?'
        result = retrieve(self.work, question, mode='search', limit=8, context_chars=12000)
        linked = [row for row in result['results'] if 'scene_field_usage' in row.get('ranking_diagnostics', {})]
        row = next(row for row in linked if 'IsActive' in row['title'])
        dependencies = {source['path'] for source in row['sources'] if source.get('role') == 'reference_dependency'}
        self.assertTrue({'Assets/SceneDemo.unity', 'Assets/SceneDemo.cs.meta'} <= dependencies)
        meta = self.root / 'Assets/SceneDemo.cs.meta'
        meta.write_text(meta.read_text() + '# changed\n')
        result = retrieve(self.work, question, mode='search', limit=8, context_chars=12000)
        self.assertIn(('Assets/SceneDemo.cs.meta', 'stale'),
                      {(row['path'], row['status']) for row in result['source_freshness']})
        with self.assertRaisesRegex(ValueError, 'changed since discovery'):
            verify_sources(status(self.work), result['results'])

    def test_guard_rejects_code_only_scene_claim(self):
        self.build()
        question = 'In the SceneDemo scene, is activation enabled in its serialized component?'
        answer = ask(self.work, question, ScriptedProvider('code'), mode='search', followups=0)
        self.assertEqual(answer['status'], 'insufficient_evidence')
        self.assertFalse(answer['claims'])
        self.assertIn('code default', answer['missing_evidence'][-1])

    def test_guard_allows_cited_scene_component(self):
        self.build()
        question = 'In the SceneDemo scene, is activation enabled in its serialized component?'
        answer = ask(self.work, question, ScriptedProvider('scene'), mode='search', followups=0)
        self.assertEqual(answer['status'], 'answered')
        self.assertEqual(len(answer['claims']), 1)


    def test_plain_scene_name_disambiguates_script_named_scene(self):
        (self.root / 'Assets/Arena.unity').write_text(make_scene(self.guid, [0]))
        self.build()
        questions = [f'In the {name} scene, is activation enabled in its serialized SceneDemo component?'
                     for name in ('Arena', 'arena')]
        questions.append('In the scene named Arena, is activation enabled in its serialized SceneDemo component?')
        questions.append('In the Arena.unity scene, is activation enabled in its serialized SceneDemo component?')
        for question in questions:
            result = retrieve(self.work, question, mode='search', limit=5)
            self.assertEqual(result['results'][0]['ranking_diagnostics']['scene_component_anchor'], 'Assets/Arena.unity')
            answer = ask(self.work, question, ScriptedProvider('scene'), mode='search', followups=0)
            self.assertEqual(answer['status'], 'answered')

    def test_unsupported_multiword_scene_does_not_select_suffix(self):
        (self.root / 'Assets/Arena.unity').write_text(make_scene(self.guid, [0]))
        self.build()
        for question in ('What does SceneDemo serialize in the Main Arena scene?',
                         'What does SceneDemo serialize in the Main-Arena scene?',
                         'What does SceneDemo serialize in the scene named Main Arena?'):
            with self.subTest(question=question):
                result = retrieve(self.work, question, mode='search', limit=10)
                self.assertFalse(any('scene_component_anchor' in row.get('ranking_diagnostics', {}) for row in result['results']))

    def test_unsupported_multiword_scene_cannot_validate_another_scene(self):
        (self.root / 'Assets/Arena.unity').write_text(make_scene(self.guid, [0]))
        self.build()
        question = 'In the Main Arena scene, is activation enabled in its serialized SceneDemo component?'
        answer = ask(self.work, question, ScriptedProvider('scene'), mode='search', followups=0)
        self.assertEqual(answer['status'], 'insufficient_evidence')
        self.assertFalse(answer['claims'])
        self.assertTrue(any('scene source' in text for text in answer['missing_evidence']))

    def test_duplicate_plain_scene_name_does_not_anchor(self):
        (self.root / 'Assets/Arena.unity').write_text(make_scene(self.guid, [0]))
        other = self.root / 'Assets/Other'
        other.mkdir()
        (other / 'Arena.unity').write_text(make_scene(self.guid, [1]))
        self.build()
        question = 'What does SceneDemo serialize in the Arena scene?'
        result = retrieve(self.work, question, mode='search', limit=10)
        self.assertFalse(any('scene_component_anchor' in row.get('ranking_diagnostics', {}) for row in result['results']))


class SceneGuardTests(unittest.TestCase):
    def test_unrelated_scene_does_not_count(self):
        answer = {'claims': [{'evidence_ids': ['E1']}], 'evidence_gaps': []}
        evidence = {'E1': {'provenance': {'record_kind': 'asset_object'},
                           'sources': [{'path': 'Assets/OtherScene.unity'}]}}
        self.assertTrue(_guard_serialized_scene_claims('What is serialized in the SceneDemo scene?', answer, evidence))
        self.assertFalse(answer['claims'])

    def test_dependency_does_not_count_as_direct_citation(self):
        answer = {'claims': [{'evidence_ids': ['E1']}], 'evidence_gaps': []}
        evidence = {'E1': {'provenance': {'record_kind': 'asset_object'},
                           'sources': [{'path': 'Assets/SceneDemo.unity', 'role': 'reference_dependency'}]}}
        self.assertTrue(_guard_serialized_scene_claims('What is serialized in the SceneDemo scene?', answer, evidence))

    def test_ambiguous_scene_names_require_disambiguation(self):
        answer = {'claims': [{'evidence_ids': ['E1']}], 'evidence_gaps': []}
        evidence = {'E1': {'provenance': {'record_kind': 'asset_object'}, 'text': 'role: component',
                           'sources': [{'path': 'Assets/One/SceneDemo.unity'}]}}
        inventory = {'entries': [{'path': 'Assets/One/SceneDemo.unity'}, {'path': 'Assets/Two/SceneDemo.unity'}]}
        self.assertTrue(_guard_serialized_scene_claims(
            'What is serialized in the SceneDemo scene component?', answer, evidence, inventory))
        self.assertFalse(answer['claims'])

    def test_scene_root_does_not_support_component_claim(self):
        answer = {'claims': [{'evidence_ids': ['E1']}], 'evidence_gaps': []}
        evidence = {'E1': {'provenance': {'record_kind': 'asset_object'}, 'text': 'Object: SceneRoots',
                           'sources': [{'path': 'Assets/SceneDemo.unity'}]}}
        self.assertTrue(_guard_serialized_scene_claims(
            'What is serialized in the SceneDemo scene component?', answer, evidence))

    def test_nonserialized_question_is_unchanged(self):
        answer = {'claims': [{'evidence_ids': ['E1']}], 'evidence_gaps': []}
        self.assertFalse(_guard_serialized_scene_claims('How does SceneDemo work?', answer, {}))
        self.assertEqual(len(answer['claims']), 1)


if __name__ == '__main__':
    unittest.main()
