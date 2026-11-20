from pathlib import Path
import shutil
import tempfile
import unittest

from unity_rag.asset_map import map_assets
from unity_rag.code_map import map_code
from unity_rag.discovery import scan
from unity_rag.retrieval import build_retrieval, retrieve


def prefab(name, value):
    return f'''%YAML 1.1
--- !u!1 &1
GameObject:
  m_Name: {name}
  m_Component:
  - component: {{fileID: 2}}
--- !u!114 &2
MonoBehaviour:
  m_GameObject: {{fileID: 1}}
  MaxConnectedPlayers: {value}
'''


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class PrefabIdentifierRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / 'Game'
        cls.work = Path(cls.temp.name) / 'Knowledge'
        (cls.root / 'Assets').mkdir(parents=True)
        (cls.root / 'ProjectSettings').mkdir()
        (cls.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 2022.3.1f1\n')
        (cls.root / 'Assets/ConnectionManager.prefab').write_text(prefab('ConnectionManager', 8))
        (cls.root / 'Assets/One.prefab').write_text(prefab('DuplicateManager', 3))
        (cls.root / 'Assets/Two.prefab').write_text(prefab('DuplicateManager', 4))
        (cls.root / 'Assets/ConnectionManager.cs').write_text('public class ConnectionManager { public int MaxConnectedPlayers = 9; }\n')
        scan(cls.root, cls.work)
        map_code(cls.work)
        map_assets(cls.work)
        build_retrieval(cls.work)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_explicit_unique_prefab_identifier_promotes_its_serialized_component(self):
        result = retrieve(self.work, 'What is the maximum player count on the ConnectionManager prefab?',
                          mode='search', limit=5, context_chars=12000)
        anchored = [r for r in result['results'] if r.get('ranking_diagnostics', {}).get('prefab_identifier') == 'ConnectionManager']
        self.assertTrue(anchored)
        self.assertTrue(any(r['title'].startswith('Assets/ConnectionManager.prefab / MonoBehaviour')
                            and 'MaxConnectedPlayers: 8' in r['text'] for r in anchored))
        self.assertTrue(all(any(s['path'] == 'Assets/ConnectionManager.prefab' for s in r['sources']) for r in anchored))
        self.assertIn('prefab', result['ranking'].lower())

    def test_ambiguous_or_non_prefab_queries_do_not_anchor(self):
        for query in ('What is the value on the DuplicateManager prefab?',
                      'What is the player count for ConnectionManager?',
                      'What is the value on the ConnectionManager DuplicateManager prefab?'):
            result = retrieve(self.work, query, mode='search', limit=10)
            self.assertFalse(any('prefab_identifier' in r.get('ranking_diagnostics', {}) for r in result['results']))

    def test_exact_mode_and_kind_filter_remain_bounded(self):
        exact = retrieve(self.work, 'ConnectionManager', mode='exact', limit=5)
        self.assertTrue(exact['results'])
        self.assertTrue(all(r['match'] == 'exact_alias' for r in exact['results']))
        filtered = retrieve(self.work, 'ConnectionManager prefab value', mode='search', kind='code_member', limit=5)
        self.assertTrue(all(r['kind'] == 'code_member' for r in filtered['results']))
        self.assertFalse(any('prefab_identifier' in r.get('ranking_diagnostics', {}) for r in filtered['results']))


if __name__ == '__main__':
    unittest.main()
