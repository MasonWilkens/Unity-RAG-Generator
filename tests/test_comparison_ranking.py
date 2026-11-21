"""Frozen comparison passages plus ambiguity, provenance, and budget contracts."""
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
from unity_rag.comparison_ranking import promote_type_comparison
from unity_rag.discovery import status
from unity_rag.passage_evaluation import evaluate_passages
from unity_rag.passage_selection import focus_lines
from unity_rag.retrieval import retrieve
from unity_rag.workspace import refresh, resolve_workspace

FIXTURE = Path(__file__).parent / 'fixtures/type-comparison-v1'
QUESTION = 'Compare PulseEmitter and RampEmitter for charge and cooldown.'


class ComparisonGuardTests(unittest.TestCase):
    def test_frozen_source_and_label_identity(self):
        lock = json.loads((FIXTURE / 'lock.json').read_bytes())
        actual = {p.relative_to(FIXTURE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (FIXTURE / 'project').rglob('*') if p.is_file()}
        actual['labels.json'] = hashlib.sha256((FIXTURE / 'labels.json').read_bytes()).hexdigest()
        self.assertEqual(actual, lock['sha256'])
        self.assertEqual(lock['baseline_commit'], 'd68b007da2ff4781bfed6570cb2a7bdfbc256e1c')
        self.assertEqual(lock['settings'], {'mode': 'search', 'limit': 5, 'expand': 0, 'context_chars': 24000})

    def test_unsupported_exact_filtered_and_oversize_requests_do_not_scan(self):
        class NoDatabase:
            def execute(self, *args):
                raise AssertionError('Unexpected database access')
        queries = ('How does PulseEmitter update charge?', 'Locate PulseEmitter.Tick.',
                   'Compare PulseEmitter and RampEmitter.',
                   'How do PulseEmitter and RampEmitter use charge?',
                   'Compare PulseEmitter and RampEmitter and ThirdEmitter for charge.',
                   'Compare PulseEmitter and RampEmitter for saved prefab values.',
                   QUESTION + ' word' * 40, QUESTION + 'x' * 4096)
        for question in queries:
            self.assertEqual(promote_type_comparison(NoDatabase(), [], question), ([], False))
        for rows, kind in (([], 'asset'), ([{'match': 'exact_alias'}], None),
                           ([{'ranking_diagnostics': {'scene_component_anchor': True}}], None)):
            self.assertEqual(promote_type_comparison(NoDatabase(), rows, QUESTION, kind), (rows, False))


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class ComparisonRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name) / 'Game'
        cls.work = Path(cls.tmp.name) / 'Knowledge'
        shutil.copytree(FIXTURE / 'project', cls.root)
        lock = json.loads((FIXTURE / 'lock.json').read_bytes())
        refresh(cls.work, cls.root, semantic=False, chunk_budget=lock['chunk_budget'])
        cls.generation = resolve_workspace(cls.work)
        with closing(sqlite3.connect(cls.generation / 'inventory.sqlite3')) as db:
            cls.rows = [json.loads(raw) for raw, in db.execute('SELECT data FROM evidence')]
        # Deliberately favor one type/decoys; comparison selection must still
        # supply executable passages from both sides in semantic and hybrid.
        cls.scores = {r['id']: (1 if 'PulseEmitter' in r['title'] else .01) for r in cls.rows}

    def query(self, question=QUESTION, **kwargs):
        with patch('unity_rag.semantic.rank', return_value=self.scores):
            return retrieve(self.work, question, mode=kwargs.pop('mode', 'search'),
                            limit=kwargs.pop('limit', 5), **kwargs)

    @staticmethod
    def selected(result):
        return [r for r in result['results'] if 'type_comparison' in r.get('ranking_diagnostics', {})]

    def assert_literal(self, row):
        source = next(s for s in row['sources'] if s.get('role') != 'reference_dependency')
        lines = (self.root / source['path']).read_text(encoding='utf-8').splitlines()
        self.assertEqual(row['text'], '\n'.join(lines[source['start_line'] - 1:source['end_line']]))

    def test_frozen_passages_survive_retrieval_and_real_answer_packing(self):
        lock = json.loads((FIXTURE / 'lock.json').read_bytes())
        result = evaluate_passages(self.work, FIXTURE / 'labels.json', **lock['settings'])
        self.assertEqual(result['summary']['retrieved']['passed'], 9)
        self.assertEqual(result['summary']['packed']['passed'], 9)
        self.assertEqual(result['summary']['packed']['groups_found'], 34)
        for case in result['cases']:
            self.assertLessEqual(case['context_budget']['used_chars'], 24000)

    def test_frozen_fallback_controls_preserve_original_order(self):
        controls = json.loads((FIXTURE / 'labels.json').read_bytes())['controls']
        with closing(sqlite3.connect(self.generation / 'inventory.sqlite3')) as db:
            for case in controls:
                original = self.query(case['question'])['results']
                self.assertEqual(promote_type_comparison(db, original, case['question']), (original, False), case['question'])

    def test_overload_and_both_sides_in_all_modes_without_call_claims(self):
        for mode in ('search', 'semantic', 'hybrid'):
            result = self.query(mode=mode, limit=2)
            selected = self.selected(result)
            self.assertTrue(result['type_comparison'])
            self.assertFalse(result['cross_file_call'])
            self.assertEqual([r['title'] for r in selected], ['PulseEmitter.Tick(int)', 'RampEmitter.Advance(int)'])
            for side, row in enumerate(selected):
                self.assert_literal(row)
                self.assertIn('(System.Int32)', row['entity_id'])
                diagnostic = row['ranking_diagnostics']['type_comparison']
                self.assertEqual(diagnostic['side'], side)
                self.assertEqual(len(diagnostic['scopes']), 2)
                self.assertNotIn('call_site', diagnostic)
                self.assertEqual(focus_lines(row, QUESTION)[2], 'parsed_references')
        answer = ask(self.work, QUESTION, style='extractive', followups=0, mode='search')
        text = '\n'.join(r['text'] for r in answer['evidence'].values())
        self.assertIn('charge += elapsed;', text)
        self.assertIn('charge = System.Math.Max(0, charge - elapsed);', text)

    def test_qualified_names_do_not_collapse_and_order_is_symmetric(self):
        for left, right in [('Workshop.Stepper', 'Runtime.Stepper'), ('Runtime.Stepper', 'Workshop.Stepper')]:
            result = self.query(f'Compare `{left}` versus `{right}` for charge and cooldown.', limit=2)
            self.assertEqual([r['title'] for r in self.selected(result)], [f'{left}.Tick(int)', f'{right}.Tick(int)'])
            for row in self.selected(result):
                self.assert_literal(row)

    def test_long_chunks_stay_separate_and_round_robin(self):
        selected = self.selected(self.query('Compare TallEmitter and WideEmitter for charge and cooldown.'))
        self.assertEqual(len(selected), 4)
        self.assertEqual([r['ranking_diagnostics']['type_comparison']['side'] for r in selected], [0, 1, 0, 1])
        for owner in ('TallEmitter', 'WideEmitter'):
            rows = [r for r in selected if owner in r['title']]
            self.assertEqual(len(rows), 2)
            self.assertEqual(len({r['entity_id'] for r in rows}), 1)
            self.assertNotEqual(rows[0]['sources'][0]['start_line'], rows[1]['sources'][0]['start_line'])
        for row in selected:
            self.assert_literal(row)

    def test_both_types_and_partial_declarations_are_freshness_dependencies(self):
        selected = self.selected(self.query())
        expected = {'Assets/PulseEmitter.cs', 'Assets/PulseEmitter.State.cs',
                    'Assets/RampEmitter.cs', 'Assets/RampEmitter.State.cs'}
        self.assertTrue(selected)
        for row in selected:
            self.assertTrue(expected <= {s['path'] for s in row['sources']})
        for name in expected:
            path = self.root / name
            original = path.read_bytes()
            try:
                path.write_bytes(original + b'\n// changed\n')
                with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                    verify_sources(status(self.work), selected)
            finally:
                path.write_bytes(original)

    def test_result_character_and_token_budgets_remain_authoritative(self):
        for limit, budget in ((1, 1000), (2, 4000), (5, 12000)):
            result = self.query(limit=limit, context_chars=budget)
            self.assertLessEqual(len(result['results']), limit)
            self.assertLessEqual(result['context_budget']['used_chars'], budget)
            for row in result['results']:
                self.assert_literal(row)
        answer = ask(self.work, QUESTION, mode='search', style='extractive', followups=0,
                     context_tokens=4000, token_encoding='utf8-bytes')
        self.assertLessEqual(answer['context_budget']['used_tokens'], 4000)
        for row in answer['evidence'].values():
            self.assert_literal(row)

    def test_exact_lookup_kind_filters_and_third_names_are_preserved(self):
        exact = self.query('Locate PulseEmitter.Tick.')
        self.assertFalse(exact['type_comparison'])
        self.assertTrue(all(r['match'] == 'exact_alias' for r in exact['results']))
        self.assertFalse(self.query(kind='asset')['type_comparison'])
        self.assertTrue(self.query(kind='code_member')['type_comparison'])
        self.assertFalse(self.query('Compare PulseEmitter and RampEmitter for charge from ThirdEmitter.')['type_comparison'])
        self.assertFalse(self.query('Compare PulseEmitter and RampEmitter for charge from thirdemitter.')['type_comparison'])

    def test_scope_and_reference_bounds_fail_closed(self):
        original = self.query()['results']
        for case in ('chunks', 'references', 'type_alias'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'copy.sqlite3'
                shutil.copyfile(self.generation / 'inventory.sqlite3', path)
                with closing(sqlite3.connect(path)) as db, db:
                    if case == 'chunks':
                        row = next(r for r in self.rows if r['title'] == 'PulseEmitter.Tick(int)')
                        db.executemany('INSERT INTO evidence (id,entity,kind,data) VALUES (?,?,?,?)',
                            [(f'extra-{i}', row['entity_id'], row['kind'], json.dumps({**row, 'id': f'extra-{i}'})) for i in range(201)])
                    elif case == 'references':
                        source, raw = db.execute("SELECT source,data FROM evidence_relations WHERE source LIKE '%PulseEmitter.Tick%' AND kind='references' LIMIT 1").fetchone()
                        db.executemany('INSERT INTO evidence_relations (source,kind,data) VALUES (?,?,?)', [(source, 'references', raw)] * 1001)
                    else:
                        row = next(r for r in self.rows if r['title'] == 'PulseEmitter' and r['kind'] == 'code_type')
                        entity = row['entity_id'].replace('Assembly-CSharp', 'AnotherAssembly')
                        db.execute('INSERT INTO evidence (id,entity,kind,data) VALUES (?,?,?,?)',
                                   ('collision', entity, 'code_type', json.dumps({**row, 'entity_id': entity, 'id': 'collision'})))
                        db.execute('INSERT INTO evidence_aliases VALUES (?,?)', ('pulseemitter', entity))
                    self.assertEqual(promote_type_comparison(db, original, QUESTION), (original, False), case)

    def test_existing_anchors_retain_priority_and_unrelated_scope_blocks(self):
        caller = next(r for r in self.rows if r['title'] == 'PulseEmitter.Tick(int)')
        target = next(r for r in self.rows if r['title'] == 'RampEmitter.Advance(int)')
        other = next(r for r in self.rows if r['title'] == 'ThirdEmitter.Tick()')
        with closing(sqlite3.connect(self.generation / 'inventory.sqlite3')) as db:
            anchored = {**target, 'ranking_diagnostics': {'named_type_method': True}}
            result, selected = promote_type_comparison(db, [anchored, caller], QUESTION)
            self.assertTrue(selected)
            self.assertEqual(result[0]['id'], target['id'])
            self.assertTrue(result[0]['ranking_diagnostics']['named_type_method'])
            outside = {**other, 'ranking_diagnostics': {'named_type_method': True}}
            self.assertEqual(promote_type_comparison(db, [outside, caller], QUESTION), ([outside, caller], False))


if __name__ == '__main__':
    unittest.main()
