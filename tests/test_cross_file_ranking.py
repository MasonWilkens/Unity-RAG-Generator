"""One-hop selection is grounded in call sites, not text similarity alone."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.answers import ask, verify_sources
from unity_rag.cross_file_ranking import promote_cross_file_call
from unity_rag.discovery import status
from unity_rag.retrieval import retrieve
from unity_rag.workspace import refresh, resolve_workspace


class CrossFileGuardTests(unittest.TestCase):
    def test_unrelated_exact_filtered_and_asset_queries_do_not_scan(self):
        class NoDatabase:
            def execute(self, *args):
                raise AssertionError('Unexpected database access')
        for question in ('Locate DeliveryGate.Run', 'How does DeliveryGate work?',
                         'What is serialized in the scene and prefab?'):
            self.assertEqual(promote_cross_file_call(NoDatabase(), [], question), ([], False))
        question = 'How does DeliveryGate handle shipment and audit stamp?'
        for rows, kind in (([], 'asset'), ([{'match': 'exact_alias'}], None),
                           ([{'ranking_diagnostics': {'scene_component_anchor': True}}], None)):
            self.assertEqual(promote_cross_file_call(NoDatabase(), rows, question, kind), (rows, False))


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class CrossFileRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name) / 'Game'
        cls.work = Path(cls.tmp.name) / 'Knowledge'
        (cls.root / 'Assets').mkdir(parents=True)
        (cls.root / 'ProjectSettings').mkdir()
        (cls.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 6000.0.1f1\n')
        sources = {
            'DeliveryGate.cs': '''public partial class DeliveryGate {
 public void Run(int shipment) {
  if (shipment > 0) LedgerWriter.Store(shipment);
 }
}
''',
            'DeliveryGate.Other.cs': 'public partial class DeliveryGate { public int revision; }\n',
            'LedgerWriter.cs': '''public static partial class LedgerWriter {
 private static int auditStamp, lastParcel, backupReceipt;
 public static void Store(int parcel) {
  auditStamp++;
  lastParcel = parcel;
 }
 public static void Store(string parcel) { backupReceipt++; }
}
''',
            'LedgerWriter.Other.cs': 'public static partial class LedgerWriter { public static int revision; }\n',
            'OtherWriter.cs': '''public static class OtherWriter {
 private static int auditStamp;
 public static void Store(int parcel) { auditStamp++; }
}
public class VirtualWriter {
 private int auditStamp;
 public virtual void Store(int parcel) { auditStamp++; }
}
public interface IWriter { void Store(int parcel); }
public static class MiddleWriter { public static void Store(int parcel) { LedgerWriter.Store(parcel); } }
''',
            'OtherGates.cs': '''public class TwinGate {
 public void Run(int shipment) { if(shipment > 0) { LedgerWriter.Store(shipment); OtherWriter.Store(shipment); } }
}
public class StringGate {
 public void Run(int shipment) { if(shipment > 0) Queue("LedgerWriter.Store audit stamp", shipment); }
 private void Queue(string name, int value) { }
}
public class MethodGroupGate {
 public void Run(int shipment) { if(shipment > 0) { System.Action<int> save = LedgerWriter.Store; save(shipment); } }
}
public class VirtualGate {
 public void Run(int shipment) { if(shipment > 0) new VirtualWriter().Store(shipment); }
}
public class InterfaceGate {
 public void Run(int shipment, IWriter writer) { if(shipment > 0) writer.Store(shipment); }
}
public class OneHopGate {
 public void Run(int shipment) { if(shipment > 0) MiddleWriter.Store(shipment); }
}
''',
            'UnresolvedGate.cs': '''public class UnresolvedGate {
 public void Run(int shipment) { if(shipment > 0) MissingWriter.Store(shipment); }
}
''',
            'LongRelay.cs': '''public class LongRelay {
 public void Run(int heat) { if (heat > 0) LongJournal.Commit(heat); }
}
''',
            'LongJournal.cs': ('public static class LongJournal { static int auditStamp, receiptCounter;\n'
                               ' public static void Commit(int value) {\n  auditStamp++;\n'
                               + ''.join(f'  int filler{i} = {i};\n' for i in range(220))
                               + '  receiptCounter++;\n }\n}\n'),
        }
        for name, text in sources.items():
            (cls.root / 'Assets' / name).write_text(text, encoding='utf-8')
        refresh(cls.work, cls.root, semantic=False, chunk_budget=1024)
        cls.generation = resolve_workspace(cls.work)
        with closing(sqlite3.connect(cls.generation / 'inventory.sqlite3')) as db:
            cls.rows = [json.loads(raw) for raw, in db.execute('SELECT data FROM evidence')]
        cls.scores = {r['id']: (1 if r['kind'] == 'code_type' else .2) for r in cls.rows}

    def query(self, question, mode='search', **kwargs):
        with patch('unity_rag.semantic.rank', return_value=self.scores):
            return retrieve(self.work, question, mode=mode, limit=kwargs.pop('limit', 5), **kwargs)

    @staticmethod
    def selected(result):
        return [r for r in result['results'] if 'cross_file_call' in r.get('ranking_diagnostics', {})]

    def assert_literal(self, row):
        source = next(s for s in row['sources'] if s.get('role') != 'reference_dependency')
        lines = (self.root / source['path']).read_text(encoding='utf-8').splitlines()
        self.assertEqual(row['text'], '\n'.join(lines[source['start_line'] - 1:source['end_line']]))

    def test_resolved_call_selects_both_files_and_exact_overload_in_all_modes(self):
        question = 'How does DeliveryGate validate shipment and increment the audit stamp?'
        for mode in ('search', 'semantic', 'hybrid'):
            result = self.query(question, mode=mode)
            selected = self.selected(result)
            self.assertTrue(result['cross_file_call'])
            self.assertEqual([r['title'] for r in selected], ['DeliveryGate.Run(int)', 'LedgerWriter.Store(int)'])
            for row in selected:
                self.assert_literal(row)
                diagnostic = row['ranking_diagnostics']['cross_file_call']
                self.assertEqual(diagnostic['resolution'], 'resolved_static_target')
                self.assertIn('Store(System.Int32)', diagnostic['target'])
        answer = ask(self.work, question, mode='search', style='extractive', followups=0)
        text = '\n'.join(r['text'] for r in answer['evidence'].values())
        self.assertIn('LedgerWriter.Store(shipment)', text)
        self.assertIn('auditStamp++;', text)

    def test_call_site_target_and_partial_declarations_are_freshness_dependencies(self):
        selected = self.selected(self.query('How does DeliveryGate validate shipment and increment the audit stamp?'))
        expected = {'Assets/DeliveryGate.cs', 'Assets/DeliveryGate.Other.cs',
                    'Assets/LedgerWriter.cs', 'Assets/LedgerWriter.Other.cs'}
        for row in selected:
            self.assertTrue(expected <= {s['path'] for s in row['sources']})
        self.assertTrue(selected)
        for name in expected:
            path = self.root / name
            original = path.read_bytes()
            try:
                path.write_bytes(original + b'\n// changed\n')
                with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                    verify_sources(status(self.work), selected)
            finally:
                path.write_bytes(original)

    def test_unresolved_polymorphic_string_delegate_and_two_hop_calls_are_not_inferred(self):
        for owner in ('StringGate', 'MethodGroupGate', 'VirtualGate', 'InterfaceGate', 'UnresolvedGate', 'OneHopGate', 'TwinGate'):
            question = f'How does {owner} validate shipment and increment the audit stamp?'
            self.assertFalse(self.query(question)['cross_file_call'], owner)

    def test_long_target_keeps_complementary_chunks_without_merging_citations(self):
        question = 'How does LongRelay check heat and update the audit stamp and receipt counter?'
        selected = self.selected(self.query(question))
        self.assertEqual(len(selected), 3)
        targets = [r for r in selected if r['title'] == 'LongJournal.Commit(int)']
        self.assertEqual(len(targets), 2)
        self.assertEqual(len({r['entity_id'] for r in targets}), 1)
        text = '\n'.join(r['text'] for r in targets)
        self.assertIn('auditStamp++;', text)
        self.assertIn('receiptCounter++;', text)
        for row in selected:
            self.assert_literal(row)

    def test_multiple_named_types_and_unrelated_terms_do_not_force_a_chain(self):
        for question in ('How does DeliveryGate use OtherWriter and update audit stamp?',
                         'How does DeliveryGate validate shipment and update nonexistentSatellite?'):
            self.assertFalse(self.query(question)['cross_file_call'])

    def test_exact_filters_result_and_context_budgets_stay_authoritative(self):
        exact = self.query('Locate LedgerWriter.Store.')
        self.assertFalse(exact['cross_file_call'])
        self.assertTrue(all(r['match'] == 'exact_alias' for r in exact['results']))
        question = 'How does DeliveryGate validate shipment and increment the audit stamp?'
        self.assertFalse(self.query(question, kind='asset')['cross_file_call'])
        result = self.query(question, limit=1, context_chars=1000)
        self.assertLessEqual(len(result['results']), 1)
        self.assertLessEqual(result['context_budget']['used_chars'], 1000)
        for row in result['results']:
            self.assert_literal(row)

    def test_call_bounds_preserve_the_complete_original_order(self):
        question = 'How does DeliveryGate validate shipment and increment the audit stamp?'
        original = self.query(question)['results']
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'copy.sqlite3'
            shutil.copyfile(self.generation / 'inventory.sqlite3', database)
            with closing(sqlite3.connect(database)) as db, db:
                source, raw = db.execute("SELECT source,data FROM evidence_relations WHERE source LIKE '%DeliveryGate.Run%' AND kind='calls' LIMIT 1").fetchone()
                db.executemany('INSERT INTO evidence_relations (source,kind,data) VALUES (?,?,?)', [(source, 'calls', raw)] * 201)
                self.assertEqual(promote_cross_file_call(db, original, question), (original, False))

    def test_existing_anchor_keeps_priority_and_other_scope_prevents_promotion(self):
        question = 'How does DeliveryGate validate shipment and increment the audit stamp?'
        caller = next(r for r in self.rows if r['title'] == 'DeliveryGate.Run(int)')
        target = next(r for r in self.rows if r['title'] == 'LedgerWriter.Store(int)')
        anchored = {**caller, 'ranking_diagnostics': {'named_type_method': True}}
        with closing(sqlite3.connect(self.generation / 'inventory.sqlite3')) as db:
            result, used = promote_cross_file_call(db, [anchored], question)
            self.assertTrue(used)
            self.assertEqual(result[0]['id'], caller['id'])
            self.assertTrue(result[0]['ranking_diagnostics']['named_type_method'])
            other = {**target, 'ranking_diagnostics': {'named_type_method': True}}
            self.assertEqual(promote_cross_file_call(db, [other], question), ([other], False))


if __name__ == '__main__':
    unittest.main()
