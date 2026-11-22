"""Two-hop retrieval must carry both literal calls and fresh implementation evidence."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.answers import ask, verify_sources
from unity_rag.dependency_ranking import promote_dependency_chain
from unity_rag.discovery import status
from unity_rag.passage_evaluation import evaluate_passages
from unity_rag.passage_selection import focus_lines
from unity_rag.retrieval import retrieve
from unity_rag.workspace import refresh, resolve_workspace

FIXTURE = Path(__file__).parent / 'fixtures/dependency-chain-v1'
QUESTION = 'How does ParcelDock validate shipment and increment the audit stamp?'


class DependencyGuardTests(unittest.TestCase):
    def test_frozen_source_label_and_budget_identity(self):
        lock = json.loads((FIXTURE / 'lock.json').read_bytes())
        actual = {p.relative_to(FIXTURE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (FIXTURE / 'project').rglob('*') if p.is_file()}
        actual['labels.json'] = hashlib.sha256((FIXTURE / 'labels.json').read_bytes()).hexdigest()
        self.assertEqual(actual, lock['sha256'])
        self.assertEqual(lock['baseline_commit'], 'a3b84a23778aa66e2d80df3e7732e85951d79e98')
        self.assertEqual(lock['settings'], {'mode': 'search', 'limit': 5, 'expand': 0, 'context_chars': 24000})

    def test_unsupported_exact_filtered_and_oversize_requests_do_not_scan(self):
        class NoDatabase:
            def execute(self, *args):
                raise AssertionError('Unexpected database access')
        for question in ('Locate ParcelDock.Run.', 'How does ParcelDock work?',
                         'Compare ParcelDock and CountingDock for shipment.',
                         'What is serialized in the scene and prefab?', QUESTION + 'x' * 4096):
            self.assertEqual(promote_dependency_chain(NoDatabase(), [], question), ([], False))
        for rows, kind in (([], 'asset'), ([{'match': 'exact_alias'}], None),
                          ([{'ranking_diagnostics': {'type_comparison': True}}], None),
                          ([{'ranking_diagnostics': {'scene_component_anchor': True}}], None)):
            self.assertEqual(promote_dependency_chain(NoDatabase(), rows, QUESTION, kind), (rows, False))


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class DependencyRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name) / 'Game'
        cls.work = Path(cls.tmp.name) / 'Knowledge'
        shutil.copytree(FIXTURE / 'project', cls.root)
        # Supplemental dispatch regression: a default interface body is indexed,
        # yet an interface call cannot establish the implementation at runtime.
        (cls.root / 'Assets/DefaultInterface.cs').write_text('''public interface IDefaultBook {
 void Append(int payload) { DefaultState.auditStamp++; }
}
public static class DefaultState { public static int auditStamp; }
public static class DefaultRelay {
 public static void Transfer(int value, IDefaultBook book) { book.Append(value); }
}
public class DefaultDock {
 public static void Run(int shipment, IDefaultBook book) { if (shipment > 0) DefaultRelay.Transfer(shipment, book); }
}
''', encoding='utf-8')
        refresh(cls.work, cls.root, semantic=False, chunk_budget=1024)
        cls.generation = resolve_workspace(cls.work)
        with closing(sqlite3.connect(cls.generation / 'inventory.sqlite3')) as db:
            cls.rows = [json.loads(raw) for raw, in db.execute('SELECT data FROM evidence')]
        cls.scores = {r['id']: (1 if r['kind'] == 'code_type' else .2) for r in cls.rows}

    def query(self, question=QUESTION, **kwargs):
        with patch('unity_rag.semantic.rank', return_value=self.scores):
            return retrieve(self.work, question, mode=kwargs.pop('mode', 'search'),
                            limit=kwargs.pop('limit', 5), **kwargs)

    @staticmethod
    def selected(result):
        return [r for r in result['results'] if 'dependency_chain' in r.get('ranking_diagnostics', {})]

    def assert_literal(self, row):
        source = next(s for s in row['sources'] if s.get('role') != 'reference_dependency')
        lines = (self.root / source['path']).read_text(encoding='utf-8').splitlines()
        self.assertEqual(row['text'], '\n'.join(lines[source['start_line'] - 1:source['end_line']]))

    def test_frozen_calls_and_endpoint_passages_survive_real_answer_packing(self):
        lock = json.loads((FIXTURE / 'lock.json').read_bytes())
        result = evaluate_passages(self.work, FIXTURE / 'labels.json', **lock['settings'])
        self.assertEqual(result['summary']['retrieved']['passed'], 6)
        self.assertEqual(result['summary']['packed']['passed'], 6)
        self.assertEqual(result['summary']['packed']['groups_found'], 20)
        for case in result['cases']:
            self.assertLessEqual(case['context_budget']['used_chars'], 24000)

    def test_frozen_ambiguous_cyclic_deep_dynamic_and_irrelevant_controls_preserve_order(self):
        controls = json.loads((FIXTURE / 'labels.json').read_bytes())['controls']
        with closing(sqlite3.connect(self.generation / 'inventory.sqlite3')) as db:
            for case in controls:
                result = self.query(case['question'])
                self.assertFalse(result['dependency_chain'], case['question'])
                original = result['results']
                self.assertEqual(promote_dependency_chain(db, original, case['question']), (original, False))

    def test_literal_bridge_and_exact_namespace_overloads_in_all_modes(self):
        for mode in ('search', 'semantic', 'hybrid'):
            result = self.query(mode=mode)
            selected = self.selected(result)
            self.assertTrue(result['dependency_chain'])
            self.assertEqual([r['title'] for r in selected],
                             ['ParcelDock.Run(int)', 'ParcelRelay.Transfer(int)', 'Records.RecordBook.Append(int)'])
            for row in selected:
                self.assert_literal(row)
                diagnostic = row['ranking_diagnostics']['dependency_chain']
                self.assertEqual(diagnostic['depth'], 2)
                self.assertEqual(diagnostic['resolution'], 'resolved_static_path')
                self.assertEqual(len(set(diagnostic['chain'])), 3)
                self.assertTrue(all('(System.Int32)' in identity for identity in diagnostic['chain']))
                self.assertEqual(focus_lines(row, QUESTION)[2], 'parsed_references')
            self.assertEqual(selected[1]['ranking_diagnostics']['dependency_chain']['matched_terms'], [])
            edges = selected[0]['ranking_diagnostics']['dependency_chain']['edges']
            for index, edge in enumerate(edges):
                source = next(s for s in selected[index]['sources'] if s.get('role') != 'reference_dependency')
                self.assertEqual(edge['call_site']['path'], source['path'])
                self.assertTrue(source['start_line'] <= edge['call_site']['start_line'] <= edge['call_site']['end_line'] <= source['end_line'])

    def test_extends_one_hop_anchors_without_replacing_them(self):
        query = 'How does CountingDock validate shipment and update the route count and audit stamp?'
        result = self.query(query)
        self.assertTrue(result['cross_file_call'])
        self.assertTrue(result['dependency_chain'])
        self.assertEqual([r['title'] for r in self.selected(result)],
                         ['CountingDock.Run(int)', 'CountingRelay.Transfer(int)', 'Records.RecordBook.Append(int)'])
        self.assertTrue(all('cross_file_call' in r['ranking_diagnostics'] for r in self.selected(result)[:2]))
        direct = self.query('How does DirectDock validate shipment and increment the audit stamp?')
        self.assertTrue(direct['cross_file_call'])
        self.assertFalse(direct['dependency_chain'])

    def test_long_bridge_and_endpoint_keep_physical_citations(self):
        result = self.query('How does LengthyDock validate shipment and increment the audit stamp?')
        bridge = next(r for r in self.selected(result) if r['title'] == 'LengthyRelay.Transfer(int)')
        source = next(s for s in bridge['sources'] if s.get('role') != 'reference_dependency')
        self.assertGreater(source['start_line'], 100)
        self.assertIn('Records.RecordBook.Append(value);', bridge['text'])
        result = self.query('How does WideDock validate shipment and update the audit stamp and receipt counter?')
        selected = self.selected(result)
        self.assertEqual(len(selected), 4)
        leaves = [r for r in selected if r['title'] == 'LongRegister.Append(int)']
        self.assertEqual(len(leaves), 2)
        self.assertEqual(len({r['entity_id'] for r in leaves}), 1)
        for row in [bridge, *selected]:
            self.assert_literal(row)

    def test_all_hops_and_partial_declarations_are_freshness_dependencies(self):
        selected = self.selected(self.query())
        paths = {'Assets/ParcelDock.cs', 'Assets/ParcelDock.State.cs', 'Assets/ParcelRelay.cs',
                 'Assets/ParcelRelay.State.cs', 'Assets/RecordBook.cs', 'Assets/RecordBook.State.cs'}
        self.assertTrue(selected)
        for row in selected:
            self.assertTrue(paths <= {s['path'] for s in row['sources']})
        for name in paths:
            path = self.root / name
            original = path.read_bytes()
            try:
                path.write_bytes(original + b'\n// changed\n')
                with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                    verify_sources(status(self.work), selected)
            finally:
                path.write_bytes(original)

    def test_default_interface_body_is_not_treated_as_resolved_runtime_implementation(self):
        self.assertTrue(any(r['title'].startswith('IDefaultBook.Append') for r in self.rows))
        self.assertFalse(self.query('How does DefaultDock validate shipment and increment the audit stamp?')['dependency_chain'])

    def test_exact_filters_and_result_character_token_budgets_stay_authoritative(self):
        exact = self.query('Locate ParcelDock.Run.')
        self.assertFalse(exact['dependency_chain'])
        self.assertTrue(all(r['match'] == 'exact_alias' for r in exact['results']))
        self.assertFalse(self.query(kind='asset')['dependency_chain'])
        self.assertTrue(self.query(kind='code_member')['dependency_chain'])
        for limit, budget in ((1, 1000), (3, 10000), (5, 16000)):
            result = self.query(limit=limit, context_chars=budget)
            self.assertLessEqual(len(result['results']), limit)
            self.assertLessEqual(result['context_budget']['used_chars'], budget)
            for row in result['results']:
                if row['evidence_type'] == 'original_source':
                    self.assert_literal(row)
        answer = ask(self.work, QUESTION, mode='search', style='extractive', followups=0,
                     context_tokens=6000, token_encoding='utf8-bytes')
        self.assertLessEqual(answer['context_budget']['used_tokens'], 6000)
        for row in answer['evidence'].values():
            if row['evidence_type'] == 'original_source':
                self.assert_literal(row)

    def test_inconsistent_call_locations_and_graph_bounds_preserve_original_order(self):
        original = self.query()['results']
        for case in ('first-location', 'second-location', 'calls', 'paths', 'nodes', 'row-size'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'copy.sqlite3'
                shutil.copyfile(self.generation / 'inventory.sqlite3', path)
                with closing(sqlite3.connect(path)) as db, db:
                    source, raw = db.execute("SELECT source,data FROM evidence_relations WHERE source LIKE '%ParcelDock.Run%' AND kind='calls' LIMIT 1").fetchone()
                    if case.endswith('location'):
                        name = 'ParcelDock.Run' if case.startswith('first') else 'ParcelRelay.Transfer'
                        edges = db.execute("SELECT rowid,data FROM evidence_relations WHERE source LIKE ? AND kind='calls'", ('%' + name + '%',)).fetchall()
                        for identity, data in edges:
                            edge = json.loads(data)
                            edge['location']['start_line'] = edge['location']['end_line'] = 99999
                            db.execute('UPDATE evidence_relations SET data=? WHERE rowid=?', (json.dumps(edge), identity))
                    elif case in ('calls', 'paths'):
                        copies = 201 if case == 'calls' else 199
                        db.executemany('INSERT INTO evidence_relations (source,kind,data) VALUES (?,?,?)', [(source, 'calls', raw)] * copies)
                    elif case == 'nodes':
                        db.execute("DELETE FROM evidence_relations WHERE source=? AND kind='calls'", (source,))
                        edge = json.loads(raw)
                        for i in range(70):
                            target = f'Assembly-CSharp::M:Unknown{i}.Transfer(System.Int32)'
                            db.execute('INSERT INTO evidence_relations (source,target,kind,data) VALUES (?,?,?,?)',
                                       (source, target, 'calls', json.dumps({**edge, 'target': target})))
                    else:
                        row = next(r for r in self.rows if r['title'] == 'ParcelRelay.Transfer(int)')
                        db.execute('UPDATE evidence SET data=? WHERE id=?', (json.dumps({**row, 'text': 'x' * 1000001}), row['id']))
                    traced = []
                    db.set_trace_callback(traced.append)
                    self.assertEqual(promote_dependency_chain(db, original, QUESTION), (original, False), case)
                    if case == 'nodes':
                        self.assertLessEqual(sum('Unknown' in sql for sql in traced), 64)

    def test_unrelated_protected_anchor_prevents_chain_promotion(self):
        row = next(r for r in self.rows if r['title'] == 'WideRelay.Transfer(int)')
        anchored = {**row, 'ranking_diagnostics': {'named_type_method': True}}
        with closing(sqlite3.connect(self.generation / 'inventory.sqlite3')) as db:
            self.assertEqual(promote_dependency_chain(db, [anchored], QUESTION), ([anchored], False))


if __name__ == '__main__':
    unittest.main()
