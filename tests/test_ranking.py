import unittest

from unity_rag.ranking import fuse


def row(chunk, entity, match='keyword'):
    return {'id': chunk, 'entity_id': entity, 'match': match, 'text': chunk,
            'sources': [{'path': chunk + '.cs'}]}


class RankingTests(unittest.TestCase):
    def test_duplicate_passages_do_not_occupy_multiple_ranks(self):
        dense = [row('a1', 'a', 'semantic'), row('a2', 'a', 'semantic'), row('b', 'b', 'semantic')]
        result = fuse([], dense, {'a1': .9, 'a2': .8, 'b': .7}, 'semantic', 10)
        self.assertEqual([r['entity_id'] for r in result], ['a', 'b'])
        self.assertEqual(result[1]['ranking_diagnostics']['semantic_rank'], 2)
        self.assertEqual(result[0]['sources'], dense[0]['sources'])
        self.assertIsNone(result[0]['ranking_diagnostics']['rrf'])

    def test_weight_and_exact_precedence(self):
        keyword = [row('k', 'k'), row('exact', 'exact', 'exact_alias')]
        dense = [row('d', 'd', 'semantic')]
        scores = {'d': .9, 'k': .1, 'exact': .01}
        semantic_first = fuse(keyword, dense, scores, 'hybrid', 10, .8)
        keyword_first = fuse(keyword, dense, scores, 'hybrid', 10, .2)
        self.assertEqual([r['id'] for r in semantic_first], ['exact', 'd', 'k'])
        self.assertEqual([r['id'] for r in keyword_first], ['exact', 'k', 'd'])

    def test_invalid_weight_and_deterministic_ties(self):
        for weight in [-1, 2, float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                fuse([], [], {}, 'hybrid', 10, weight)
        rows = [row('b', 'b', 'semantic'), row('a', 'a', 'semantic')]
        result = fuse([], rows, {'a': .5, 'b': .5}, 'semantic', 10)
        self.assertEqual([r['id'] for r in result], ['a', 'b'])


class RankingChannelTests(unittest.TestCase):
    def test_disabled_channels_contribute_neither_candidates_nor_passages(self):
        keyword = [row('lex-a', 'a'), row('lex-only', 'b')]
        dense = [row('dense-a', 'a', 'semantic'), row('dense-only', 'c', 'semantic')]
        scores = {'dense-a': .9, 'dense-only': .8, 'lex-a': .2}
        zero = fuse(keyword, dense, scores, 'hybrid', 10, 0)
        one = fuse(keyword, dense, scores, 'hybrid', 10, 1)
        self.assertEqual([r['id'] for r in zero], ['lex-a', 'lex-only'])
        self.assertEqual([r['id'] for r in one], ['dense-a', 'dense-only'])
        self.assertEqual(zero[0]['sources'], keyword[0]['sources'])
        self.assertEqual(zero[0]['ranking_diagnostics']['cosine'], .2)
        self.assertEqual(one[0]['ranking_diagnostics']['cosine'], .9)
        self.assertEqual(zero[0]['ranking_diagnostics']['selected_passage'], 'keyword')

    def test_exact_alias_survives_disabled_lexical_channel(self):
        result = fuse([row('exact', 'x', 'exact_alias'), row('ordinary', 'y')],
                      [row('dense', 'z', 'semantic')], {'dense': .9}, 'hybrid', 10, 1)
        self.assertEqual([r['id'] for r in result], ['exact', 'dense'])

    def test_stronger_channel_selects_passage_and_reports_its_cosine(self):
        keyword = [row('lex-a', 'a')]
        keyword[0]['ranking_diagnostics'] = {'literal_rank': 1, 'expanded_rank': 2}
        dense = [row('dense-a', 'a', 'semantic')]
        scores = {'lex-a': .1, 'dense-a': .9}
        for weight, expected in ((.2, 'lex-a'), (.8, 'dense-a')):
            result = fuse(keyword, dense, scores, 'hybrid', 10, weight)[0]
            self.assertEqual(result['id'], expected)
            self.assertEqual(result['ranking_diagnostics']['cosine'], scores[expected])
            self.assertEqual(result['ranking_diagnostics']['literal_rank'], 1)
        self.assertNotIn('score', keyword[0])  # inputs are not modified
