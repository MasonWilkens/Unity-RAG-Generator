import unittest
from unity_rag.context_packing import counter, count_evidence, fit_record


class ContextPackingTests(unittest.TestCase):
    def test_prefix_keeps_citation_exact_and_respects_budget(self):
        count = counter('utf8-bytes')
        record = {'text': '\n'.join(['α line ' + str(i) for i in range(20)]),
                  'evidence_type': 'original_source', 'sources': [{'path': 'a.cs',
                  'start_line': 10, 'end_line': 29, 'sha256': 'abc'}]}
        packed, clipped = fit_record({}, 'E1', record, 250, count)
        self.assertTrue(clipped)
        self.assertLessEqual(count_evidence({'E1': packed}, count), 250)
        self.assertEqual(packed['sources'][0]['end_line'], 10 + len(packed['text'].splitlines()) - 1)
        self.assertEqual(packed['text'].splitlines(), record['text'].splitlines()[:len(packed['text'].splitlines())])

    def test_parsed_facts_and_ambiguous_sources_are_not_clipped(self):
        count = counter('utf8-bytes')
        record = {'text': 'a\n' * 100, 'evidence_type': 'parsed_fact', 'sources': []}
        self.assertEqual(fit_record({}, 'E1', record, 50, count), (None, False))
        record['evidence_type'] = 'original_source'
        self.assertEqual(fit_record({}, 'E1', record, 50, count), (None, False))
