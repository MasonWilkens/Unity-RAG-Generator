"""Multipart selection uses declared scopes and real identifier references."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.answers import ask, verify_sources
from unity_rag.discovery import status
from unity_rag.multipart_ranking import promote_multipart_code
from unity_rag.passage_selection import focus_lines
from unity_rag.retrieval import retrieve
from unity_rag.workspace import refresh, resolve_workspace


class MultipartGuardTests(unittest.TestCase):
    def test_unrelated_exact_filtered_and_asset_requests_do_not_scan(self):
        class NoDatabase:
            def execute(self, *args):
                raise AssertionError('must not query')
        for query in ('Find PumpLoop', 'How does PumpLoop work?',
                      'What is serialized in the scene and prefab?',
                      'What are the signatures and declarations of PumpLoop?'):
            self.assertEqual(promote_multipart_code(NoDatabase(), [], query), ([], False))
        query = 'What does PumpLoop do with heat and pressure?'
        for rows, kind in (([], 'code_type'), ([{'match': 'exact_alias'}], None),
                           ([{'ranking_diagnostics': {'scene_component_anchor': 'scene'}}], None)):
            self.assertEqual(promote_multipart_code(NoDatabase(), rows, query, kind), (rows, False))

    def test_oversized_query_abstains_before_database_access(self):
        class NoDatabase:
            def execute(self, *args):
                raise AssertionError('must not query')
        query = 'What does PumpLoop do with heat and ' + 'pressure ' * 40
        self.assertEqual(promote_multipart_code(NoDatabase(), [], query), ([], False))


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class MultipartRankingTests(unittest.TestCase):
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
            'BurstEmitter.cs': '''public class BurstEmitter {
 public float clockTime, lastEmissionTime, interval;
 public bool inputPressed;
 public void Begin() { lastEmissionTime = -interval; }
 public void Tick() {
  if (inputPressed && clockTime >= lastEmissionTime + interval) {
   Emit();
   lastEmissionTime = clockTime;
  }
 }
 private void Emit() { }
 public void Explanation() { var prose = "input interval conditions emit last emission time"; }
}
''',
            'ExpiryAgent.cs': '''public class ExpiryAgent {
 public int expirationDelay;
 public object objectHandle;
 public void Schedule() { Queue("Release", expirationDelay); }
 public void Release() { Remove(objectHandle); }
 private void Queue(string method, int delay) { }
 private void Remove(object target) { }
}
public class StringOnlyAgent {
 public void Explain() { var words = "expiration object remove"; }
}
''',
            'PumpLoop.cs': '''public partial class PumpLoop {
 public int heat, pressure;
 public void Vent() { heat--; }
}
''',
            'PumpLoop.Other.cs': '''public partial class PumpLoop {
 public void Pump() { pressure++; }
}
''',
            'Ambiguity.cs': '''namespace One { public class SharedMixer {
 public int heat, pressure; public void Step() { heat += pressure; }
} }
namespace Two { public class SharedMixer {
 public int heat, pressure; public void Step() { pressure += heat; }
} }
public class SoloCache { public int heat; public void Step() { heat++; } }
public class NeighborCache { public int pressure; public void Step() { pressure++; } }
public class OverloadUnit {
 public int heat, pressure;
 public void Process(int delta) { heat += delta; }
 public void Process(float delta) { pressure += (int)delta; }
}
''',
            'RouteNode.cs': '''public class RouteNode {
 public event System.Action Changed;
 public void Touch() { Changed?.Invoke(); }
}
''',
            'RouteState.cs': '''public class RouteState {
 private RouteNode _activeRoute;
 private bool pointCacheCurrent;
 private void OnChanged() { }
 public RouteNode ActiveRoute {
  get { return _activeRoute; }
  set {
   _activeRoute.Changed -= OnChanged;
   pointCacheCurrent = false;
   _activeRoute = value;
   _activeRoute.Changed += OnChanged;
  }
 }
}
''',
            'RoutePreview.cs': '''public class RoutePreview {
 private RouteNode _activeRoute;
 private bool pointCacheCurrent;
 private void OnChanged() { }
 public RouteNode ActiveRoute {
  get {
   _activeRoute.Changed -= OnChanged;
   pointCacheCurrent = false;
   return _activeRoute;
  }
  set { _activeRoute = value; }
 }
}
''',
            'DuplicateProperties.cs': '\n'.join('''namespace %s {
 public class ChannelState {
  private RouteNode _channel; private bool packetCacheCurrent;
  private void OnChanged() { }
  public RouteNode CurrentChannel {
   get { return _channel; }
   set { _channel.Changed += OnChanged; packetCacheCurrent = false; _channel = value; }
  }
 }
}''' % name for name in ('Left', 'Right')),
            'LongLoop.cs': ('public class LongLoop { public int heat, pressure;\n public void Step() {\n  heat++;\n'
                            + ''.join(f'  int filler{i} = {i};\n' for i in range(230))
                            + '  pressure++;\n }\n}\n'),
            'HugeType.cs': ('public class HugeType { public int heat, pressure;\n'
                            + ''.join(f' public void Step{i}() {{ heat += pressure; }}\n' for i in range(205)) + '}\n'),
            'AbstractUnit.cs': '''public abstract class AbstractUnit {
 public abstract RouteNode Read(RouteNode value);
}
''',
        }
        for name, source in sources.items():
            (cls.root / 'Assets' / name).write_text(source, encoding='utf-8')
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
        return [r for r in result['results'] if 'multipart_code' in r.get('ranking_diagnostics', {})]

    def assert_literal(self, row):
        source = next(s for s in row['sources'] if s.get('role') != 'reference_dependency')
        lines = (self.root / source['path']).read_text(encoding='utf-8').splitlines()
        self.assertEqual(row['text'], '\n'.join(lines[source['start_line'] - 1:source['end_line']]))
        for span in row.get('ranking_diagnostics', {}).get('multipart_code', {}).get('reference_spans', []):
            self.assertEqual(span['path'], source['path'])
            self.assertLessEqual(source['start_line'], span['start_line'])
            self.assertLessEqual(span['end_line'], source['end_line'])

    def test_conditions_and_timestamp_promote_implementation_over_fields(self):
        question = 'What input and interval conditions allow BurstEmitter to emit, and when does it record the last emission time?'
        for mode in ('search', 'semantic', 'hybrid'):
            result = self.query(question, mode=mode)
            selected = self.selected(result)
            self.assertTrue(selected)
            self.assertEqual(selected[0]['title'], 'BurstEmitter.Tick()')
            self.assertIn('lastEmissionTime = clockTime;', selected[0]['text'])
            self.assertIn('inputPressed && clockTime >=', selected[0]['text'])
            self.assertTrue(all(r['kind'] == 'code_member' for r in selected))
            self.assertFalse(any('Explanation' in r['title'] for r in selected))
            for row in selected:
                self.assert_literal(row)

    def test_scheduling_and_completion_bodies_both_reach_answer_context(self):
        question = 'How does ExpiryAgent schedule expiration and remove its object?'
        result = self.query(question)
        titles = {r['title'] for r in result['results']}
        self.assertTrue({'ExpiryAgent.Schedule()', 'ExpiryAgent.Release()'} <= titles)
        answer = ask(self.work, question, mode='search', style='extractive', followups=0)
        texts = '\n'.join(r['text'] for r in answer['evidence'].values())
        self.assertIn('Queue("Release", expirationDelay)', texts)
        self.assertIn('Remove(objectHandle)', texts)
        # Retrieval does not turn the string argument into a confirmed call.
        for row in self.selected(result):
            self.assertNotIn('Release', row['ranking_diagnostics']['multipart_code']['matched_terms'])

    def test_property_mutation_uses_setter_not_competing_getter(self):
        question = 'How does replacing the active route update event handlers and invalidate the point cache?'
        for mode in ('search', 'semantic', 'hybrid'):
            selected = self.selected(self.query(question, mode=mode))
            self.assertEqual([r['title'] for r in selected], ['RouteState.ActiveRoute'])
            self.assertEqual(selected[0]['ranking_diagnostics']['multipart_code']['rule'], 'property_setter')
            self.assertIn('pointCacheCurrent = false;', selected[0]['text'])
            self.assert_literal(selected[0])

    def test_property_selection_carries_event_and_competing_declaration_dependencies(self):
        question = 'How does replacing the active route update event handlers and invalidate the point cache?'
        row = self.selected(self.query(question))[0]
        paths = {s['path'] for s in row['sources'] if s.get('role') == 'reference_dependency'}
        self.assertTrue({'Assets/RouteNode.cs', 'Assets/RoutePreview.cs'} <= paths)
        for name in ('RouteNode.cs', 'RoutePreview.cs'):
            path = self.root / 'Assets' / name
            original = path.read_bytes()
            try:
                path.write_bytes(original + b'\n// changed\n')
                with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                    verify_sources(status(self.work), [row])
            finally:
                path.write_bytes(original)

    def test_explicit_property_owner_cannot_select_a_competitor(self):
        for owner in ('RoutePreview', 'route preview', 'PumpLoop'):
            question = f'How does {owner} replace the active route and invalidate the point cache?'
            self.assertFalse(self.query(question)['multipart_code'], question)
        question = 'How does RouteState replace the active route and invalidate the point cache?'
        selected = self.selected(self.query(question))
        self.assertEqual([r['title'] for r in selected], ['RouteState.ActiveRoute'])

    def test_ambiguous_properties_and_types_do_not_get_a_selected_scope(self):
        for question in ('How does replacing the current channel update event handlers and invalidate the packet cache?',
                         'What does SharedMixer do with heat and pressure?',
                         'How does PumpLoop interact with BurstEmitter and handle pressure?'):
            result = self.query(question)
            self.assertFalse(result['multipart_code'], question)
            self.assertFalse(self.selected(result), question)

    def test_comments_strings_and_neighbor_types_do_not_complete_coverage(self):
        for question in ('What does StringOnlyAgent do with expiration and object removal?',
                         'What does SoloCache do with heat and pressure?'):
            self.assertFalse(self.query(question)['multipart_code'])

    def test_partial_type_members_and_all_declaration_files_are_retained(self):
        question = 'What does PumpLoop do to reduce heat and raise pressure?'
        selected = self.selected(self.query(question))
        self.assertEqual({r['title'] for r in selected}, {'PumpLoop.Vent()', 'PumpLoop.Pump()'})
        for row in selected:
            self.assertTrue({'Assets/PumpLoop.cs', 'Assets/PumpLoop.Other.cs'} <= {s['path'] for s in row['sources']})
            self.assert_literal(row)
        path = self.root / 'Assets/PumpLoop.Other.cs'
        original = path.read_bytes()
        try:
            path.write_bytes(original + b'\n// stale scope\n')
            with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                verify_sources(status(self.work), selected)
        finally:
            path.write_bytes(original)

    def test_overloads_keep_distinct_identities(self):
        selected = self.selected(self.query('What does OverloadUnit do with heat and pressure?'))
        self.assertEqual(len(selected), 2)
        self.assertEqual(len({r['entity_id'] for r in selected}), 2)
        self.assertTrue(all('.Process(' in r['entity_id'] for r in selected))

    def test_long_method_can_contribute_two_distinct_literal_windows(self):
        result = self.query('What does LongLoop do with heat and pressure?', context_chars=24000)
        selected = self.selected(result)
        self.assertEqual(len(selected), 2)
        self.assertEqual(len({r['entity_id'] for r in selected}), 1)
        text = '\n'.join(r['text'] for r in selected)
        self.assertIn('heat++;', text)
        self.assertIn('pressure++;', text)
        for row in selected:
            self.assert_literal(row)
            self.assertIsNotNone(focus_lines(row))

    def test_exact_lookup_kind_and_context_limits_remain_authoritative(self):
        exact = self.query('OverloadUnit.Process', mode='exact')
        self.assertFalse(exact['multipart_code'])
        self.assertTrue(all(r['match'] == 'exact_alias' for r in exact['results']))
        lookup = self.query('Locate OverloadUnit.Process.')
        self.assertFalse(lookup['multipart_code'])
        filtered = self.query('What does PumpLoop do with heat and pressure?', kind='asset')
        self.assertFalse(filtered['multipart_code'])
        self.assertTrue(all(r['kind'] == 'asset' for r in filtered['results']))
        small = self.query('What does LongLoop do with heat and pressure?', context_chars=700, limit=1)
        self.assertLessEqual(len(small['results']), 1)
        self.assertLessEqual(small['context_budget']['used_chars'], 700)
        for row in small['results']:
            self.assert_literal(row)

    def test_large_scope_abstains_instead_of_taking_first_two_hundred_methods(self):
        result = self.query('What does HugeType do with heat and pressure?')
        self.assertFalse(result['multipart_code'])

    def test_reference_bounds_preserve_original_order_without_partial_results(self):
        original = self.query('What does PumpLoop do with heat and pressure?')['results']
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'copy.sqlite3'
            shutil.copyfile(self.generation / 'inventory.sqlite3', database)
            with closing(sqlite3.connect(database)) as db, db:
                source, raw = db.execute("SELECT source,data FROM evidence_relations WHERE source LIKE '%PumpLoop.Vent' AND kind='references' LIMIT 1").fetchone()
                db.executemany('INSERT INTO evidence_relations (source,kind,data) VALUES (?,?,?)',
                               [(source, 'references', raw)] * 1001)
                result, used = promote_multipart_code(db, original, 'What does PumpLoop do with heat and pressure?')
                self.assertFalse(used)
                self.assertEqual(result, original)

    def test_abstract_declaration_is_not_executable_evidence(self):
        from unity_rag.multipart_ranking import _Reader
        row = next(r for r in self.rows if r['kind'] == 'code_member' and r['title'].startswith('AbstractUnit.Read('))
        with closing(sqlite3.connect(self.generation / 'inventory.sqlite3')) as db:
            self.assertFalse(_Reader(db).executable(row))


if __name__ == '__main__':
    unittest.main()
