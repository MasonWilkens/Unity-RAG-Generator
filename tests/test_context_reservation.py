import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.retrieval import _pack_context, retrieve
from unity_rag.source_evaluation import evaluate_sources


def row(name, length):
    return {'id': name, 'text': 'x' * length}


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


class ContextReservationTests(unittest.TestCase):
    def test_opt_in_reservation_keeps_primary_and_a_related_record(self):
        primary, other, related = row('primary', 70), row('other', 350), row('related', 260)
        cap = size(primary) + size(other) + 10
        baseline = _pack_context([primary, other], [related], [], cap, 0)
        self.assertEqual([x['id'] for x in baseline[0]], ['primary', 'other'])
        self.assertEqual(baseline[1], [])
        balanced = _pack_context([primary, other], [related], [], cap, size(related))
        self.assertEqual([x['id'] for x in balanced[0]], ['primary'])
        self.assertEqual([x['id'] for x in balanced[1]], ['related'])
        self.assertLessEqual(balanced[2], cap)
        self.assertEqual(balanced[3], 1)
        self.assertEqual(balanced[4], size(related))

    def test_reserve_never_displaces_the_first_primary_that_fits(self):
        primary, related = row('primary', 200), row('related', 250)
        cap = size(primary) + 20
        result = _pack_context([primary], [related], [], cap, cap)
        self.assertEqual(result[0], [primary])
        self.assertEqual(result[1], [])
        self.assertLessEqual(result[2], cap)

    def test_oversized_context_is_skipped_and_unused_capacity_is_reused(self):
        primary, large, small = row('primary', 50), row('large', 800), row('small', 40)
        cap = size(primary) + size(small) + 10
        result = _pack_context([primary], [large, small], [], cap, size(small))
        self.assertEqual(result[0], [primary])
        self.assertEqual(result[1], [small])
        self.assertEqual(result[3], 1)
        self.assertEqual(result[4], size(small))

    def test_source_excerpt_priority_and_zero_budget(self):
        primary, excerpt, related = row('primary', 50), row('excerpt', 40), row('related', 30)
        cap = size(primary) + size(excerpt) + size(related)
        result = _pack_context([primary], [excerpt, related], [excerpt], cap, size(related))
        self.assertEqual(result[:2], ([primary], [excerpt, related]))
        self.assertEqual(result[2], cap)
        self.assertEqual(result[4], size(related))
        empty = _pack_context([primary], [excerpt], [excerpt], 1, 0)
        self.assertEqual(empty[:2], ([], []))
        self.assertEqual(empty[2:4], (0, 2))


    def test_public_api_rejects_invalid_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            for value in (-1, 11, 1.5, True):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'reservation'):
                    retrieve(directory, 'query', context_chars=10, reserve_related_chars=value)

    def test_source_evaluation_records_and_forwards_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / 'cases.json'
            dataset.write_text(json.dumps({'schema_version': 1, 'cases': [
                {'id': 'case', 'question': 'why?', 'expected_sources': ['A.cs']}]}))
            response = {'results': [{'sources': [{'path': 'A.cs'}]}], 'context': [],
                        'source_freshness': [{'path': 'A.cs', 'status': 'current'}]}
            with patch('unity_rag.source_evaluation.retrieve', return_value=response) as mock:
                report = evaluate_sources(directory, dataset, context_chars=12000,
                                          incoming_expand=10, reserve_related_chars=3600)
            self.assertEqual(report['reserve_related_chars'], 3600)
            self.assertEqual(mock.call_args.kwargs['reserve_related_chars'], 3600)
            self.assertEqual(mock.call_args.kwargs['incoming_expand'], 10)



if __name__ == '__main__':
    unittest.main()
