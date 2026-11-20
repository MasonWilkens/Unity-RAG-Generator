import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.ranking import fuse, reserve_source_matches
from unity_rag.source_excerpts import source_excerpts


def row(ident, kind, match):
    return {'id': ident, 'entity_id': ident, 'kind': kind, 'match': match, 'text': ident,
            'sources': [{'path': ident + '.cs'}]}


class SourceAwareTests(unittest.TestCase):
    def test_preserves_lexical_code_or_asset_and_exact_alias_precedence(self):
        keyword = [row('doc', 'documentation', 'keyword'), row('field', 'asset_object', 'keyword')]
        dense = [row('dense', 'code_member', 'semantic')]
        fused = fuse(keyword, dense, {'dense': .9}, 'hybrid', 100)
        reserved = reserve_source_matches(keyword, fused)
        self.assertEqual([r['id'] for r in reserved[:2]], ['doc', 'field'])
        self.assertEqual(reserved[1]['score'], next(r['score'] for r in fused if r['id'] == 'field'))
        self.assertTrue(reserved[1]['ranking_diagnostics']['reserved_lexical_passage'])
        self.assertEqual(reserved[1]['ranking_diagnostics']['selected_passage'], 'keyword')
        self.assertEqual(reserved[1]['ranking_diagnostics']['keyword_passage_id'], 'field')
        exact = row('exact', 'code_member', 'exact_alias')
        fused = fuse(keyword + [exact], dense, {'dense': .9}, 'hybrid', 100)
        self.assertEqual(reserve_source_matches(keyword + [exact], fused)[0]['id'], 'exact')

    def test_reservation_prefers_substantive_passage_over_one_line_property(self):
        doc = row('doc', 'documentation', 'keyword')
        prop = row('prop', 'code_member', 'keyword')
        prop['sources'][0].update(start_line=10, end_line=10)
        method = row('method', 'code_member', 'keyword')
        method['sources'][0].update(start_line=20, end_line=40)
        keyword = [doc, prop, method]
        fused = fuse(keyword, [], {}, 'hybrid', 100)
        self.assertEqual([r['id'] for r in reserve_source_matches(keyword, fused)[:2]], ['doc', 'method'])

    def test_current_excerpt_exact_bounds_and_stale_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / 'Assets' / 'Thing.asset'
            p.parent.mkdir()
            text = 'first\n  Id: example_anti_air\n  DisplayName: Anti Air\n  WeaponLookup: rifle\nend\n'
            p.write_text(text, encoding='utf-8')
            data = p.read_bytes()
            path = 'Assets/Thing.asset'
            digest = hashlib.sha256(data).hexdigest()
            entries = {path: {'path': path, 'status': 'supported', 'size': len(data), 'sha256': digest}}
            item = {'id': 'asset', 'entity_id': 'asset', 'kind': 'asset', 'sources': [{'path': path, 'sha256': digest}]}
            result = source_excerpts(root, entries, [item], 'Anti Air display name')
            self.assertEqual(len(result), 1)
            source = result[0]['sources'][0]
            self.assertEqual(result[0]['text'], '\n'.join(text.splitlines()[source['start_line']-1:source['end_line']]))
            self.assertIn('DisplayName: Anti Air', result[0]['text'])
            p.write_text('different', encoding='utf-8')
            self.assertEqual(source_excerpts(root, entries, [item], 'Anti Air'), [])

    def test_secret_paths_and_unindexed_files_are_never_read(self):
        item = {'id':'x', 'kind':'asset', 'sources':[{'path':'Assets/.env/data.asset', 'sha256':'x'}]}
        with patch.object(Path, 'open', side_effect=AssertionError('must not open')):
            self.assertEqual(source_excerpts(Path('unused'), {}, [item], 'data'), [])

    def test_excerpt_respects_selected_asset_object_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / 'Assets' / 'Catalog.asset'
            p.parent.mkdir()
            lines = ['Id: wrong_anti_air', 'DisplayName: Anti Air wrong', 'padding',
                     'Id: correct_anti_air', 'DisplayName: Anti Air correct', 'WeaponLookup: Cannon']
            p.write_text('\n'.join(lines), encoding='utf-8')
            raw = p.read_bytes()
            path = 'Assets/Catalog.asset'
            digest = hashlib.sha256(raw).hexdigest()
            entries = {path: {'path': path, 'status':'supported', 'size':len(raw), 'sha256':digest}}
            item = {'id':'chunk', 'entity_id':'object', 'kind':'asset_object',
                    'text':'m_Definitions.2.Id: correct_anti_air\nm_Definitions.2.DisplayName: Anti Air correct',
                    'sources':[{'path':path, 'sha256':digest, 'start_line':1, 'end_line':6}]}
            excerpt = source_excerpts(root, entries, [item], 'correct Anti Air')[0]
            self.assertIn('correct_anti_air', excerpt['text'])
            self.assertLessEqual(excerpt['sources'][0]['start_line'], 4)
            self.assertGreaterEqual(excerpt['sources'][0]['end_line'], 4)

    def test_ambiguous_parsed_field_does_not_anchor_an_excerpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / 'Assets' / 'Catalog.asset'
            p.parent.mkdir()
            p.write_text('DisplayName: Same\nother: one\nDisplayName: Same\n', encoding='utf-8')
            raw = p.read_bytes(); digest = hashlib.sha256(raw).hexdigest(); path = 'Assets/Catalog.asset'
            entries = {path: {'path':path, 'status':'supported', 'size':len(raw), 'sha256':digest}}
            item = {'kind':'asset_object', 'text':'m_Definitions.1.DisplayName: Same',
                    'sources':[{'path':path, 'sha256':digest}]}
            self.assertEqual(source_excerpts(root, entries, [item], 'Same'), [])

    def test_serialized_file_can_surface_multiple_query_aspects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = root / 'Assets' / 'Unit.asset'; p.parent.mkdir()
            lines = ['m_PrefabAsset: 0'] + ['padding'] * 10 + ['DefaultWeapon: cannon'] + ['padding'] * 10 + ['IconLookup: unit'] + ['padding'] * 10 + ['CharacterTemplate: guid']
            p.write_text('\n'.join(lines), encoding='utf-8')
            raw = p.read_bytes(); digest = hashlib.sha256(raw).hexdigest(); path = 'Assets/Unit.asset'
            entries = {path: {'path':path, 'status':'supported', 'size':len(raw), 'sha256':digest}}
            item = {'kind':'asset', 'sources':[{'path':path, 'sha256':digest}]}
            excerpts = source_excerpts(root, entries, [item], 'unit lookup weapon template guid prefab')
            self.assertTrue(any('DefaultWeapon: cannon' in x['text'] for x in excerpts))
            self.assertTrue(any('CharacterTemplate: guid' in x['text'] for x in excerpts))


if __name__ == '__main__':
    unittest.main()
