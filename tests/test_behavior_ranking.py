"""Behavior anchors use actual method chunks and abstain on ambiguous context."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.answers import verify_sources
from unity_rag.behavior_ranking import concepts, promote_behavior_methods
from unity_rag.discovery import status
from unity_rag.retrieval import retrieve
from unity_rag.workspace import refresh, resolve_workspace


class BehaviorBoundsTests(unittest.TestCase):
    def test_non_behavior_asset_and_protected_queries_do_not_scan(self):
        class NoDatabase:
            def execute(self, *args):
                raise AssertionError('must not query')
        for query in ('Find CapacityBank', 'How is the cost calculated?',
                      'How does the prefab serialize its cost?', 'How does the scene update?',
                      'How does Capacity.Bank drain and recharge?'):
            self.assertEqual(promote_behavior_methods(NoDatabase(), [], query), ([], False))
        self.assertEqual(promote_behavior_methods(NoDatabase(), [], 'How does power change cost?', 'asset'), ([], False))
        for diagnostics in ({'named_type_method': 'owner'}, {'reserved_lexical_passage': True},
                            {'related_method_terms': ['cost']}):
            rows = [{'ranking_diagnostics': diagnostics}]
            self.assertEqual(promote_behavior_methods(NoDatabase(), rows, 'How does power change cost?'), (rows, False))
        rows = [{'match': 'exact_alias'}]
        self.assertEqual(promote_behavior_methods(NoDatabase(), rows, 'How does power change cost?'), (rows, False))

    def test_reference_bounds_abstain_without_partial_promotion(self):
        row = {'id': 'a', 'entity_id': 'A::M:Pricing.GetCost', 'kind': 'code_member',
               'title': 'Pricing.GetCost()', 'parent': 'A::T:Pricing',
               'sources': [{'path': 'Assets/Pricing.cs', 'start_line': 1, 'end_line': 10, 'sha256': 'fixture'}]}
        class Database:
            def __init__(self, records): self.records = records
            def execute(self, *args): return iter(self.records)
        edge = json.dumps({'text': 'power', 'location': {'path': 'Assets/Pricing.cs', 'start_line': 2, 'end_line': 2}})
        for records in ([(' ' * 1000001,)], [(edge,)] * 1001):
            with patch('unity_rag.behavior_ranking._type_scope', return_value=(None, [])):
                self.assertEqual(promote_behavior_methods(Database(records), [row], 'How does power change cost?'), ([row], False))

    def test_inflections_do_not_create_synonyms_or_strip_unsafe_endings(self):
        self.assertEqual(concepts('drain draining drains'), {'drain'})
        self.assertEqual(concepts('charge charging charges'), {'charg'})
        self.assertNotEqual(concepts('inactive'), concepts('active'))
        self.assertEqual(concepts('status analysis boss'), {'status', 'analysis', 'boss'})


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class BehaviorRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / 'Game'
        cls.work = Path(cls.tmp.name) / 'Index'
        (cls.root / 'Assets').mkdir(parents=True)
        (cls.root / 'ProjectSettings').mkdir()
        (cls.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 6000.0.1f1\n', encoding='utf-8')
        (cls.root / 'Assets/CapacityBank.cs').write_text("""public partial class CapacityBank {
 public float CurrentCharge, DrainPerSecond;
 public bool IsRecharging;
 public int capacitybank;
 public void Advance(float elapsed) {
  CurrentCharge = System.Math.Max(0, CurrentCharge - DrainPerSecond * elapsed);
  if (CurrentCharge <= 0) IsRecharging = false;
  capacitybank++;
 }
 public void Update() { Advance(1); }
 public void Explain() { /* drain and stop recharging */ var text = "drain and stop recharging"; }
}
""", encoding='utf-8')
        cls.partial = cls.root / 'Assets/CapacityBank.Properties.cs'
        cls.partial.write_text('public partial class CapacityBank { public int Extra; }', encoding='utf-8')
        (cls.root / 'Assets/Costs.cs').write_text("""public class CostRules {
 public float power, PowerRatio;
 public float GetYieldAmount() { return PowerRatio * 2; }
 public float GetCost() { return power * 2; }
}
public class DecoyPricing {
 public float GetCost() { var text = "power cost"; return 1; }
}
public class ReversePricing { public float cost; public float ComputePower() { return cost / 2; } }
public class PrimaryMeter { public float pressure; public float GetDuration() { return pressure * 2; } }
public class BackupMeter { public float pressure; public float GetDuration() { return pressure * 3; } }
public class Gauge { public int temperature, pressure; public void Step() { temperature += pressure; } }
public class BudgetModel { public static int Default; public int GetPrice() { return BudgetModel.Default; } }
namespace One { public class SharedRelay { public int drain, charge; public void Run() { drain += charge; } } }
namespace Two { public class SharedRelay { public int drain, charge; public void Run() { charge += drain; } } }
""", encoding='utf-8')
        filler = ''.join(f'  int filler{i} = {i};\n' for i in range(150))
        (cls.root / 'Assets/SplitReservoir.cs').write_text(
            'public class SplitReservoir { public float ChargeRate, DrainRate; public void Advance() {\n'
            ' var charge = ChargeRate;\n' + filler + ' var drain = DrainRate;\n} }', encoding='utf-8')
        refresh(cls.work, cls.root, semantic=False)
        with closing(sqlite3.connect(resolve_workspace(cls.work) / 'inventory.sqlite3')) as db:
            cls.rows = [json.loads(raw) for raw, in db.execute('SELECT data FROM evidence')]
        cls.scores = {row['id']: (1.0 if row['kind'] == 'code_type' or 'Explain' in row['title'] else .2)
                      for row in cls.rows}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def query(self, text='How does capacity bank drain and stop recharging?', mode='search', **kwargs):
        with patch('unity_rag.semantic.rank', return_value=self.scores):
            return retrieve(self.work, text, mode=mode, limit=10, **kwargs)

    def anchors(self, result):
        return [row for row in result['results'] if 'behavior_anchor' in row.get('ranking_diagnostics', {})]

    def test_scoped_body_coverage_in_all_modes_ignores_fields_comments_and_strings(self):
        for mode in ('search', 'semantic', 'hybrid'):
            result = self.query(mode=mode)
            anchors = self.anchors(result)
            self.assertEqual([r['title'] for r in anchors], ['CapacityBank.Advance(float)'])
            self.assertEqual(result['results'][0]['id'], anchors[0]['id'])
            diag = anchors[0]['ranking_diagnostics']['behavior_anchor']
            self.assertEqual(diag['rule'], 'scoped_identifier_coverage')
            self.assertEqual(set(diag['matched_terms']), {'drain', 'recharg'})
            source = anchors[0]['sources'][0]
            for span in diag['reference_spans']:
                self.assertEqual(span['path'], source['path'])
                self.assertLessEqual(source['start_line'], span['start_line'])
                self.assertLessEqual(span['end_line'], source['end_line'])

    def test_explicit_capitalized_type_also_scopes_semantic_candidates(self):
        result = self.query('How does Gauge combine temperature and pressure?', mode='semantic')
        self.assertEqual([r['title'] for r in self.anchors(result)], ['Gauge.Step()'])

    def test_calculation_name_requires_separate_input_in_the_same_chunk(self):
        for mode in ('search', 'semantic', 'hybrid'):
            result = self.query('How does power change the cost?', mode=mode)
            anchors = self.anchors(result)
            self.assertEqual([r['title'] for r in anchors], ['CostRules.GetCost()'])
            self.assertEqual(anchors[0]['ranking_diagnostics']['behavior_anchor']['rule'], 'calculation_name_and_input')
        self.assertFalse(self.anchors(self.query('How does budget change the price?')))
        symbolic = self.query('How does PowerRatio change YieldAmount?', mode='semantic')
        self.assertEqual([r['title'] for r in self.anchors(symbolic)], ['CostRules.GetYieldAmount()'])

    def test_calculation_input_and_output_roles_are_not_reversed(self):
        for mode in ('search', 'semantic', 'hybrid'):
            forward = self.query('How does power change the cost?', mode=mode)
            self.assertEqual([r['title'] for r in self.anchors(forward)], ['CostRules.GetCost()'])
            reverse = self.query('How does cost change the power?', mode=mode)
            self.assertEqual([r['title'] for r in self.anchors(reverse)], ['ReversePricing.ComputePower()'])
        for question in ('How does cost change with power?', 'How does PowerChangeCost?',
                         'How does cost change the price with power?'):
            self.assertFalse(self.anchors(self.query(question)), question)

    def test_ambiguous_calculations_and_type_names_abstain(self):
        for query in ('How does pressure change the duration?', 'How does shared relay drain and charge?',
                      'How does capacity bank use cost rules while draining and recharging?'):
            self.assertFalse(self.anchors(self.query(query)), query)

    def test_explicit_member_in_resolved_type_keeps_priority(self):
        for mode in ('search', 'semantic', 'hybrid'):
            result = self.query('How does capacity bank update when disconnected?', mode=mode)
            self.assertEqual(result['results'][0]['title'], 'CapacityBank.Update()')
            self.assertEqual(self.anchors(result)[0]['ranking_diagnostics']['behavior_anchor']['rule'], 'scoped_member_name')

    def test_generic_type_fragment_and_separate_chunks_do_not_qualify(self):
        self.assertFalse(self.anchors(self.query('How does bank drain and stop recharging?')))
        self.assertFalse(self.anchors(self.query('How does capacitybank recharge?')))
        self.assertFalse(self.anchors(self.query('How does capacity, bank drain and stop recharging?')))
        self.assertFalse(self.anchors(self.query('How does split reservoir charge and drain?')))

    def test_exact_filters_and_tiny_context_budget_remain_authoritative(self):
        result = self.query('CapacityBank.Advance', mode='exact')
        self.assertTrue(result['results'])
        self.assertTrue(all(r['match'] == 'exact_alias' for r in result['results']))
        self.assertFalse(self.anchors(result))
        self.assertFalse(self.anchors(self.query(kind='asset_object')))
        result = self.query(context_chars=1)
        self.assertEqual(result['results'], [])
        self.assertLessEqual(result['context_budget']['used_chars'], 1)

    def test_partial_type_is_a_freshness_dependency(self):
        rows = self.anchors(self.query())
        self.assertTrue(any(s['path'] == 'Assets/CapacityBank.Properties.cs' and s.get('role') == 'reference_dependency'
                            for s in rows[0]['sources']))
        original = self.partial.read_bytes()
        try:
            self.partial.write_bytes(original + b'\n// changed declaration')
            with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                verify_sources(status(self.work), rows)
        finally:
            self.partial.write_bytes(original)

    def test_promotion_does_not_discard_other_chunks_from_that_method(self):
        original = next(r for r in self.rows if r['title'] == 'CapacityBank.Advance(float)')
        extra = {**original, 'id': 'extra-chunk'}
        with closing(sqlite3.connect(resolve_workspace(self.work) / 'inventory.sqlite3')) as db:
            result, used = promote_behavior_methods(db, [original, extra], 'How does capacity bank drain and stop recharging?')
        self.assertTrue(used)
        self.assertEqual({r['id'] for r in result}, {original['id'], extra['id']})
