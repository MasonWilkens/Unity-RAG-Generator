import copy
import unittest

from unity_rag.grounding import review_claims


class Provider:
    def __init__(self, response):
        self.response = response

    def generate(self, messages, schema):
        return copy.deepcopy(self.response)


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.claims = [{'text': 'The method returns two.', 'kind': 'fact', 'evidence_ids': ['E1']}]
        self.evidence = {'E1': {'text': 'return 2;'}, 'E2': {'text': 'return 3;'}}
        self.response = {'runtime_required': False, 'decisions': [{
            'claim_index': 0, 'verdict': 'supported', 'reason': 'Direct return statement.',
            'quotes': [{'evidence_id': 'E1', 'quote': 'return 2;'}]}]}

    def run_review(self):
        return review_claims('What does it return?', self.claims, self.evidence, Provider(self.response))

    def test_valid_quote_preserves_claim_without_rewrite(self):
        kept, review = self.run_review()
        self.assertEqual(kept[0]['text'], self.claims[0]['text'])
        self.assertEqual(kept[0]['supporting_quotes'][0]['quote'], 'return 2;')
        self.assertEqual(review['removed_claims'], 0)

    def test_invented_or_uncited_or_empty_quote_removes_claim(self):
        for quote in [{'evidence_id': 'E1', 'quote': 'return 999;'},
                      {'evidence_id': 'E2', 'quote': 'return 3;'},
                      {'evidence_id': 'E1', 'quote': ' '}]:
            self.response['decisions'][0]['quotes'] = [quote]
            kept, review = self.run_review()
            self.assertEqual(kept, [])
            self.assertEqual(review['removed_claims'], 1)
            self.assertEqual(review['decisions'][0]['draft_claim'], self.claims[0])
        self.response['decisions'][0]['quotes'] = []
        self.assertEqual(self.run_review()[0], [])

    def test_uncertain_and_unsupported_are_not_exposed_as_answer(self):
        for verdict in ['uncertain', 'unsupported']:
            self.response['decisions'][0]['verdict'] = verdict
            self.assertEqual(self.run_review()[0], [])

    def test_review_must_cover_each_claim_exactly_once(self):
        self.response['decisions'] = []
        with self.assertRaises(ValueError):
            self.run_review()
        self.response['decisions'] = [{'claim_index': False, 'verdict': 'supported',
                                      'reason': '', 'quotes': []}]
        with self.assertRaises(ValueError):
            self.run_review()
