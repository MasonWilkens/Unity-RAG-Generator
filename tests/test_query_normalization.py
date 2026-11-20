from pathlib import Path
import shutil
import tempfile
import unittest

from unity_rag.asset_map import map_assets
from unity_rag.code_map import map_code
from unity_rag.discovery import scan
from unity_rag.retrieval import _search_terms, build_retrieval, retrieve, terms


class SearchTermsTests(unittest.TestCase):
    def test_common_inflections_add_bounded_literal_variants(self):
        expanded = _search_terms('How are items removing stacks and initialized from defaults?')
        for expected in ('items', 'item', 'removing', 'remove', 'stacks', 'stack', 'initialized', 'initialize', 'init'):
            self.assertIn(expected, expanded)
        for excluded in ('and', 'from'):
            self.assertNotIn(excluded, expanded)
        self.assertEqual(_search_terms('from'), ['from'])
        self.assertNotIn('remove', terms('removing'))  # index tokenization remains unchanged

    def test_short_past_tense_variants_cover_add_and_save(self):
        expanded = _search_terms('How are items added and saved?')
        self.assertIn('add', expanded)
        self.assertIn('save', expanded)

    def test_does_not_strip_unsafe_plural_endings_or_accept_fts_syntax(self):
        expanded = _search_terms('boss status analysis OR "bad" *')
        for invented in ('bos', 'statu', 'analysi'):
            self.assertNotIn(invented, expanded)
        self.assertTrue(all(t.isalnum() or '_' in t for t in expanded))
        self.assertEqual(len(expanded), len(set(expanded)))


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class SearchNormalizationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / 'Game'
        cls.work = Path(cls.temp.name) / 'Knowledge'
        (cls.root / 'Assets').mkdir(parents=True)
        (cls.root / 'ProjectSettings').mkdir()
        (cls.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 2022.3.1f1\n')
        (cls.root / 'Assets/InventorySO.cs').write_text('''public class InventorySO {
    int stacks;
    public void Init() { stacks = 0; }
    public void Remove(int item) { stacks -= item; }
}
''')
        scan(cls.root, cls.work); map_code(cls.work); map_assets(cls.work); build_retrieval(cls.work)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_search_reports_expanded_terms_and_exact_mode_is_unchanged(self):
        result = retrieve(self.work, 'What happens when removing an inventory item?', mode='search', limit=5)
        self.assertIn('remove', result['query_terms'])
        self.assertTrue(any('InventorySO.Remove' in r['title'] for r in result['results']))
        exact = retrieve(self.work, 'Remove', mode='exact')
        self.assertTrue(exact['results'])
        self.assertTrue(all(r['match'] == 'exact_alias' for r in exact['results']))
        self.assertEqual(exact['query_terms'], [])


if __name__ == '__main__':
    unittest.main()
