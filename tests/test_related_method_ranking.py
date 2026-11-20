import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.retrieval import _promote_related_members, _search_terms, retrieve
from unity_rag.source_evaluation import evaluate_sources


def member(name, ident, parent='T:Factory'):
    return {'id': ident, 'entity_id': ident, 'kind': 'code_member',
            'parent': parent, 'title': 'Game.Factory.' + name + '()', 'match': 'keyword'}


class RelatedMethodRankingTests(unittest.TestCase):
    def setUp(self):
        self.rows = [member('CreateActionFromData', 'create'),
                     member('Other', 'noise', 'T:Other'),
                     member('GetActionPool', 'pool'),
                     member('ReturnAction', 'return'),
                     member('Other', 'other')]

    def test_two_action_query_promotes_up_to_two_related_matches(self):
        question = 'How are actions created and returned to the pool?'
        original = copy.deepcopy(self.rows)
        ranked, used = _promote_related_members(self.rows, _search_terms(question), question)
        self.assertTrue(used)
        self.assertEqual([r['id'] for r in ranked], ['create', 'pool', 'return', 'noise', 'other'])
        self.assertIn('return', ranked[2]['ranking_diagnostics']['related_method_terms'])
        self.assertEqual(self.rows, original)

    def test_single_action_nonconjunctive_and_unrelated_class_do_not_promote(self):
        for question in ('What determines turn order priority speed?',
                         'How are actions created then returned to the pool?',
                         'How are actions created and queued?'):
            with self.subTest(question=question):
                ranked, used = _promote_related_members(self.rows, _search_terms(question), question)
                self.assertFalse(used)
                self.assertEqual(ranked, self.rows)
        rows = [self.rows[0], member('ReturnAction', 'elsewhere', 'T:Other')]
        question = 'How are actions created and returned?'
        ranked, used = _promote_related_members(rows, _search_terms(question), question)
        self.assertFalse(used)
        self.assertEqual(ranked, rows)

    def test_public_api_restricts_option_to_keyword_search(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode in ('exact', 'semantic', 'hybrid', 'references'):
                with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'related'):
                    retrieve(directory, 'question', mode=mode, related_rank=True)
            with self.assertRaisesRegex(ValueError, 'related'):
                retrieve(directory, 'question', related_rank='yes')

    def test_source_evaluation_records_and_forwards_option(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / 'cases.json'
            dataset.write_text(json.dumps({'schema_version': 1, 'cases': [
                {'id': 'case', 'question': 'why?', 'expected_sources': ['A.cs']}]}))
            response = {'results': [{'sources': [{'path': 'A.cs'}]}], 'context': [],
                        'source_freshness': [{'path': 'A.cs', 'status': 'current'}]}
            with patch('unity_rag.source_evaluation.retrieve', return_value=response) as mock:
                report = evaluate_sources(directory, dataset, related_rank=True)
            self.assertTrue(report['related_rank'])
            self.assertTrue(mock.call_args.kwargs['related_rank'])


if __name__ == '__main__':
    unittest.main()
