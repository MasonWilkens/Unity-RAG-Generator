import unittest

from unity_rag.query_ranking import explicit_symbol_lookup


class ExplicitSymbolLookupTests(unittest.TestCase):
    def test_complete_identifier_requests(self):
        for query, expected in (
            ('Locate MissingAttackNetwork.', 'MissingAttackNetwork'),
            ('show me Game.Targeting.FindTarget(int)!', 'Game.Targeting.FindTarget(int)'),
            ('Find `speed`?', 'speed'),
            ('locate "Enemy"', 'Enemy'),
            ('Find snake_case', 'snake_case'),
        ):
            with self.subTest(query=query):
                self.assertEqual(explicit_symbol_lookup(query), expected)

    def test_prose_and_ambiguous_syntax_remain_searches(self):
        for query in ('find movement', 'Find Enemy GameObject', 'Find Enemy and Targeting',
                      'Where does Enemy attack?', 'Locate MissingType in the scene',
                      'find "Enemy', 'find Enemy*', 'Find Assets/Enemy.cs', 'find Enemy; DROP TABLE evidence'):
            with self.subTest(query=query):
                self.assertIsNone(explicit_symbol_lookup(query))
