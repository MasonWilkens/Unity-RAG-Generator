import json
import unittest

from unity_rag.context_packing import fit_record, counter, count_evidence
from unity_rag.passage_selection import focus_lines
from unity_rag.semantic import PassageScores, passage_windows
from unity_rag.asset_map import fingerprint


class PassageSelectionTests(unittest.TestCase):
    def record(self):
        lines = ['ordinary setup line %d' % i for i in range(110)]
        lines[92] = 'heat = Math.Max(0, heat - CoolingPerSecond * elapsed);'
        return {'id': 'chunk:long', 'entity_id': 'method:cool', 'kind': 'code_member', 'title': 'AdvanceHeat',
                'text': '\n'.join(lines), 'evidence_type': 'original_source', 'sources': [
                    {'path': 'Assets/Heat.cs', 'start_line': 200, 'end_line': 309, 'sha256': 'abc'}]}

    def test_budget_keeps_tail_behavior_and_exact_citations(self):
        row = self.record()
        packed, clipped = fit_record({}, 'E1', row, 1100, len, query='CoolingPerSecond elapsed')
        self.assertTrue(clipped)
        self.assertIn('CoolingPerSecond', packed['text'])
        source = packed['sources'][0]
        self.assertGreater(source['start_line'], 200)
        self.assertEqual(packed['text'], '\n'.join(row['text'].splitlines()[source['start_line']-200:source['end_line']-199]))
        self.assertLessEqual(count_evidence({'E1': packed}, len), 1100)
        self.assertEqual(packed['packing']['original_chunk_id'], row['id'])

    def test_legacy_semantic_hash_recovers_window_without_rebuilding(self):
        row = self.record()
        scores = PassageScores({'passage_version': 1, 'window_chars': 1000, 'stride_chars': 800})
        text, start, end = list(passage_windows(row))[-2]
        scores.winners[row['id']] = fingerprint(text)
        passage = scores.passage(row)
        self.assertEqual((passage['start_char'], passage['end_char']), (start, end))
        row['ranking_diagnostics'] = {'selected_passage': 'semantic', 'semantic_passage': passage}
        packed, clipped = fit_record({}, 'E1', row, 1400, len, focus=focus_lines(row), query='CoolingPerSecond elapsed')
        self.assertTrue(clipped)
        self.assertIn('CoolingPerSecond', packed['text'])
        scores.winners[row['id']] = 'wrong hash'
        self.assertIsNone(scores.passage(row))
        scores.config['passage_version'] = 99
        self.assertIsNone(scores.passage(row))

    def test_dependencies_survive_clipping_and_other_files_never_supply_focus(self):
        row = self.record()
        dependency = {'path': 'Assets/Other.cs', 'sha256': 'def', 'start_line': None,
                      'end_line': None, 'role': 'reference_dependency'}
        row['sources'].append(dependency)
        row['ranking_diagnostics'] = {'behavior_anchor': {'reference_spans': [
            {'path': 'Assets/Other.cs', 'start_line': 292, 'end_line': 292},
            {'path': 'Assets/Heat.cs', 'start_line': 292, 'end_line': 292}]}}
        packed, clipped = fit_record({}, 'E1', row, 1300, len, focus=focus_lines(row))
        self.assertTrue(clipped)
        self.assertIn('CoolingPerSecond', packed['text'])
        self.assertEqual(packed['sources'][1], dependency)
        self.assertEqual(packed['packing']['selection'], 'parsed_references')

    def test_lexical_representative_does_not_use_other_channel_focus(self):
        row = self.record()
        row['ranking_diagnostics'] = {'selected_passage': 'keyword', 'semantic_passage': {'start_char': 0, 'end_char': 1000}}
        self.assertEqual(focus_lines(row, 'CoolingPerSecond'), (92, 92, 'query_terms'))

    def test_tiny_budget_and_multisource_do_not_invent_text(self):
        row = self.record()
        self.assertEqual(fit_record({}, 'E1', row, 10, len, query='CoolingPerSecond'), (None, False))
        row['sources'].append({**row['sources'][0], 'path': 'Assets/Partial.cs'})
        self.assertEqual(fit_record({}, 'E1', row, 1100, len, query='CoolingPerSecond'), (None, False))

    def test_method_name_scopes_without_outweighing_requested_body_behavior(self):
        row = self.record()
        row['title'] = 'ThermalController.AdvanceHeat(float)'
        lines = row['text'].splitlines()
        lines[0] = 'public float AdvanceHeat(float elapsed)'
        lines[96] = 'if (heat == 0) venting = false;'
        row['text'] = '\n'.join(lines)
        packed, _ = fit_record({}, 'E1', row, 1100, len,
            query='How does ThermalController AdvanceHeat stop venting when heat reaches zero?')
        self.assertIn('venting = false', packed['text'])

    def test_non_ascii_counter_measures_actual_serialized_evidence(self):
        row = self.record()
        row['text'] = row['text'].replace('ordinary', 'λ日本語')
        count = counter('utf8-bytes')
        packed, clipped = fit_record({}, 'E1', row, 1100, count, query='CoolingPerSecond')
        self.assertTrue(clipped)
        self.assertLessEqual(len(json.dumps({'E1': packed}, ensure_ascii=False).encode()), 1100)
        self.assertIn('CoolingPerSecond', packed['text'])
