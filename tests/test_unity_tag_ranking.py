"""Explicit project-defined Unity tags use source-backed, bounded ranking."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from unity_rag.answers import verify_sources
from unity_rag.discovery import status
from unity_rag.retrieval import _promote_declared_unity_tag, retrieve
from unity_rag.workspace import refresh


def document(typ, ident, name, body):
    return f'--- !u!{typ} &{ident}\n{name}:\n{body}'


class UnityTagBoundsTests(unittest.TestCase):
    def test_punctuation_boundaries_reject_partial_tag_names(self):
        class Rows(list):
            def fetchall(self):
                return list(self)

        class FakeDatabase:
            def __init__(self):
                self.sought_definition = False

            def execute(self, statement, arguments):
                if 'unity_objects' in statement:
                    return Rows([(json.dumps({'id': 'manager', 'type': 'TagManager',
                                               'data': {'tags': ['X', 'UI', 'Enemy-Boss']}}),)])
                if 'evidence' in statement:
                    self.sought_definition = True
                    return Rows([])
                raise AssertionError('Unexpected query')

        for question in ('Where is the Unknown-X GameObject tag assigned?',
                         'Where is the other.UI GameObject tag assigned?',
                         'Where is the Super-Enemy-Boss GameObject tag assigned?',
                         'Where is the tag named X-extra assigned?',
                         'Where is the tag named UI.other assigned?'):
            with self.subTest(question=question):
                db = FakeDatabase()
                ranked, used = _promote_declared_unity_tag(db, [], question)
                self.assertFalse(used)
                self.assertEqual(ranked, [])
                self.assertFalse(db.sought_definition)
        for question in ('Where is the X GameObject tag assigned?', 'Where is the tag named UI assigned?',
                         'Find the tag named UI.',
                         'Where is the Enemy-Boss GameObject tag assigned?'):
            with self.subTest(question=question):
                db = FakeDatabase()
                _promote_declared_unity_tag(db, [], question)
                self.assertTrue(db.sought_definition)

    def test_oversized_tag_manager_abstains(self):
        class Rows(list):
            def fetchall(self):
                return list(self)

        class FakeDatabase:
            def execute(self, statement, arguments):
                if 'unity_objects' in statement:
                    return Rows([(' ' * 1000001,)])
                raise AssertionError('Unexpected query')

        rows = [{'kind': 'code_member', 'entity_id': 'test'}]
        ranked, used = _promote_declared_unity_tag(FakeDatabase(), rows, 'Where is the waypoint tag?')
        self.assertFalse(used)
        self.assertEqual(ranked, rows)


class _TagQuoteRows(list):
    def fetchall(self): return list(self)
    def fetchone(self): return self[0] if self else None

class _TagQuoteDB:
    def execute(self, sql, args):
        if 'unity_objects' in sql and 'id>=' in sql:
            return _TagQuoteRows([(json.dumps({'id':'manager','type':'TagManager','data':{'tags':['X']}}),)])
        if 'evidence WHERE entity' in sql:
            return _TagQuoteRows([(json.dumps({'id':'def','entity_id':'manager','kind':'asset_object','text':'tags.0: X','sources':[{'path':'ProjectSettings/TagManager.asset','sha256':'abc'}]}),)])
        if 'evidence_fts' in sql:
            return _TagQuoteRows([(json.dumps({'id':'go','entity_id':'object','kind':'asset_object','text':'m_TagString: X','sources':[{'path':'Assets/A.prefab','sha256':'def'}]}),)])
        if 'unity_objects' in sql and 'id=?' in sql:
            return _TagQuoteRows([(json.dumps({'id':'object','type':'GameObject','data':{'m_TagString':'X'}}),)])
        raise AssertionError(sql)

class QuotedTagBoundary(unittest.TestCase):
    def test_unrelated_quoted_multiword_names_do_not_promote_x(self):
        for question in ('Where is the "Unknown X" GameObject tag assigned?',
                         'Where is the tag named "X Unknown" assigned?',
                         "Where is the 'Unknown X' GameObject tag assigned?",
                         'Where is the `Unknown X` GameObject tag assigned?'):
            with self.subTest(question=question):
                _, promoted = _promote_declared_unity_tag(_TagQuoteDB(), [], question)
                self.assertFalse(promoted)

    def test_valid_exact_quoted_and_unrelated_quote_controls(self):
        questions = (
            'Where is the "X" GameObject tag assigned?',
            "Where is the 'X' GameObject tag assigned?",
            'Where is the `X` GameObject tag assigned?',
            'Where is the tag named "X" assigned?',
            'Find the tag named X.',
            'For "an unrelated thing", where is the X GameObject tag assigned?',
            'Where is the X GameObject tag assigned, not "another thing"?',
            "Where's the X GameObject tag assigned?",
            "Where is the players' X GameObject tag assigned?",
        )
        for question in questions:
            with self.subTest(question=question):
                _, promoted = _promote_declared_unity_tag(_TagQuoteDB(), [], question)
                self.assertTrue(promoted)

    def test_malformed_or_escaped_quote_does_not_promote(self):
        questions = (
            'Where is the "X GameObject tag assigned?',
            'Where is the X" GameObject tag assigned?',
            "Where is the 'X\" GameObject tag assigned?",
            r'Where is the \"X\" GameObject tag assigned?',
            'Where is the tag named "X assigned?',
            'Where is the tag named X" assigned?',
        )
        for question in questions:
            with self.subTest(question=question):
                _, promoted = _promote_declared_unity_tag(_TagQuoteDB(), [], question)
                self.assertFalse(promoted)

    def test_unquoted_exact_name_remains_eligible(self):
        _, promoted = _promote_declared_unity_tag(_TagQuoteDB(), [], 'Where is the X GameObject tag assigned?')
        self.assertTrue(promoted)


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class UnityTagRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / 'Game'
        cls.work = Path(cls.tmp.name) / 'Index'
        (cls.root / 'Assets').mkdir(parents=True)
        (cls.root / 'ProjectSettings').mkdir()
        (cls.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 6000.0.1f1\n')
        cls.manager = cls.root / 'ProjectSettings/TagManager.asset'
        cls.manager.write_text(document(78, 1, 'TagManager', '  tags:\n  - waypoint\n  - boat\n  - UI\n  - Player Tag\n  - Enemy-Boss\n  - X\n  - V1.2\n  - 敵\n'), encoding='utf-8')
        prefab = (document(1, 1, 'GameObject', '  m_Name: Marker\n  m_TagString: waypoint\n')
                  + document(1, 2, 'GameObject', '  m_Name: Ship\n  m_TagString: boat\n')
                  + document(1, 3, 'GameObject', '  m_Name: UiObject\n  m_TagString: UI\n')
                  + document(1, 4, 'GameObject', '  m_Name: PlayerObject\n  m_TagString: Player Tag\n')
                  + document(1, 5, 'GameObject', '  m_Name: EnemyObject\n  m_TagString: Enemy-Boss\n')
                  + document(1, 6, 'GameObject', '  m_Name: XObject\n  m_TagString: X\n')
                  + document(1, 7, 'GameObject', '  m_Name: VersionObject\n  m_TagString: V1.2\n')
                  + document(1, 8, 'GameObject', '  m_Name: UnicodeObject\n  m_TagString: 敵\n'))
        (cls.root / 'Assets/Objects.prefab').write_text(prefab, encoding='utf-8')
        (cls.root / 'Assets/Tags.cs').write_text('''public class Fake { public bool CompareTag(string tag) { return true; } }
public class Tags {
 public bool Check(Fake other) { return other.CompareTag("waypoint"); }
 public bool Decoy(Fake other) { /* other.CompareTag("waypoint") */ return false; }
 public bool WrongCase(Fake other) { return other.CompareTag("WayPoint"); }
 public bool BoatDecoy(Fake other) { /* other.CompareTag("boat") */ return false; }
 public bool CheckUI(Fake other) { return other.CompareTag("UI"); }
 public bool CheckSpace(Fake other) { return other.CompareTag("Player Tag"); }
 public bool CheckDash(Fake other) { return other.CompareTag("Enemy-Boss"); }
 public bool CheckX(Fake other) { return other.CompareTag("X"); }
 public bool CheckDot(Fake other) { return other.CompareTag("V1.2"); }
 public bool CheckUnicode(Fake other) { return other.CompareTag("敵"); }
}
''', encoding='utf-8')
        refresh(cls.work, cls.root, semantic=False)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def query(self, question, **kwargs):
        return retrieve(self.work, question, limit=20, expand=0, context_chars=100000, **kwargs)

    def promoted(self, result):
        return [item for item in result['results']
                if 'declared_unity_tag' in item.get('ranking_diagnostics', {})]

    def test_definition_assignment_and_parsed_call(self):
        question = 'Where is the waypoint GameObject tag defined, assigned, and checked?'
        results = self.promoted(self.query(question))
        self.assertEqual(len(results), 3)
        self.assertEqual([item['sources'][0]['path'] for item in results],
                         ['ProjectSettings/TagManager.asset', 'Assets/Objects.prefab', 'Assets/Tags.cs'])
        self.assertIn('::M:Tags.Check(', results[2]['entity_id'])
        self.assertTrue(all(any(source['path'] == 'ProjectSettings/TagManager.asset'
                                for source in item['sources']) for item in results))
        verify_sources(status(self.work), results)
        self.assertTrue(self.promoted(self.query('Where is the tag named waypoint used?')))

    def test_short_space_and_hyphen_tag_names(self):
        for tag in ('UI', 'Player Tag', 'Enemy-Boss', 'X', 'V1.2', '敵'):
            with self.subTest(tag=tag):
                results = self.promoted(self.query(f'Where is the {tag} GameObject tag defined, assigned and checked?'))
                self.assertEqual([item['sources'][0]['path'] for item in results],
                                 ['ProjectSettings/TagManager.asset', 'Assets/Objects.prefab', 'Assets/Tags.cs'])

    def test_quoted_multiword_tag_name_is_exact(self):
        for quote in ('"', "'", '`'):
            with self.subTest(quote=quote):
                question = f'Where is the {quote}Player Tag{quote} GameObject tag assigned?'
                items = self.promoted(self.query(question))
                self.assertEqual([item['sources'][0]['path'] for item in items],
                                 ['ProjectSettings/TagManager.asset', 'Assets/Objects.prefab', 'Assets/Tags.cs'])

    def test_unicode_tag_cli_handles_legacy_console_encoding(self):
        env = dict(os.environ, PYTHONIOENCODING='cp1252')
        args = [sys.executable, '-m', 'unity_rag', 'retrieve',
                'Where is the 敵 GameObject tag defined and assigned?',
                '--workspace', str(self.work), '--limit', '5']
        result = subprocess.run(args, env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode('ascii', errors='replace'))
        self.assertIn(b'\\u6575', result.stdout)
        utf8 = subprocess.run(args, env=dict(os.environ, PYTHONIOENCODING='utf-8'), capture_output=True)
        self.assertEqual(utf8.returncode, 0)
        self.assertIn('敵', utf8.stdout.decode('utf-8'))
        machine = subprocess.run(args + ['--json'], env=env, capture_output=True)
        self.assertEqual(machine.returncode, 0)
        self.assertTrue(json.loads(machine.stdout.decode('ascii'))['results'])
        ask_args = [sys.executable, '-m', 'unity_rag', 'ask',
                    'Where is the 敵 GameObject tag assigned?', '--workspace', str(self.work),
                    '--style', 'extractive', '--mode', 'search', '--followups', '0']
        answer = subprocess.run(ask_args, env=env, capture_output=True)
        self.assertEqual(answer.returncode, 0, answer.stderr.decode('ascii', errors='replace'))
        self.assertIn(b'\\u6575', answer.stdout)

    def test_overlapping_and_unsupported_names_abstain(self):
        original = self.manager.read_bytes()
        try:
            self.manager.write_text(document(78, 1, 'TagManager', '  tags:\n  - Player Tag\n  - Tag\n  - \'Unsafe"Name\'\n'))
            refresh(self.work, semantic=False, rebuild=True)
            self.assertFalse(self.promoted(self.query('Where is the Player Tag GameObject tag assigned?')))
            self.assertFalse(self.promoted(self.query('Where is the Unsafe"Name GameObject tag assigned?')))
        finally:
            self.manager.write_bytes(original)
            refresh(self.work, semantic=False, rebuild=True)

    def test_case_insensitive_name_collision_abstains(self):
        original = self.manager.read_bytes()
        try:
            self.manager.write_text(document(78, 1, 'TagManager', '  tags:\n  - UI\n  - ui\n'), encoding='utf-8')
            refresh(self.work, semantic=False, rebuild=True)
            self.assertFalse(self.promoted(self.query('Where is the UI GameObject tag assigned?')))
        finally:
            self.manager.write_bytes(original)
            refresh(self.work, semantic=False, rebuild=True)

    def test_comment_only_call_is_not_promoted(self):
        results = self.promoted(self.query('Where is the boat GameObject tag defined and assigned?'))
        self.assertEqual([item['sources'][0]['path'] for item in results],
                         ['ProjectSettings/TagManager.asset', 'Assets/Objects.prefab'])

    def test_duplicate_declarations_abstain(self):
        original = self.manager.read_bytes()
        try:
            self.manager.write_text(document(78, 1, 'TagManager', '  tags:\n  - waypoint\n  - waypoint\n'))
            refresh(self.work, semantic=False, rebuild=True)
            self.assertFalse(self.promoted(self.query('Where is the waypoint GameObject tag used?')))
        finally:
            self.manager.write_bytes(original)
            refresh(self.work, semantic=False, rebuild=True)

    def test_ambiguous_or_implicit_tags_abstain(self):
        questions = ['How do waypoints use Unity tags in boat?',
                     'Compare the waypoint tag and boat tag',
                     'Compare waypoint and boat tags',
                     'Where is the unknown GameObject tag used?']
        for question in questions:
            with self.subTest(question=question):
                self.assertFalse(self.promoted(self.query(question)))
        self.assertFalse(self.promoted(self.query('Where is the waypoint tag used?', kind='code_member')))
        self.assertFalse(self.promoted(self.query('Where is the waypoint tag used?', mode='exact')))

    def test_stale_tag_definition_is_dependency(self):
        results = self.promoted(self.query('Where is the waypoint GameObject tag assigned and checked?'))
        self.assertTrue(results)
        original = self.manager.read_bytes()
        try:
            self.manager.write_bytes(original + b'\n# changed\n')
            with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                verify_sources(status(self.work), results)
        finally:
            self.manager.write_bytes(original)
