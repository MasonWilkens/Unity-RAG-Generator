"""Named-type method ranking uses bounded parsed references and fresh sources."""
from pathlib import Path
import shutil
import tempfile
import unittest

from unity_rag.answers import verify_sources
from unity_rag.discovery import status
from unity_rag.retrieval import _promote_unique_named_type_method, retrieve
from unity_rag.workspace import refresh


class MethodRankingBoundsTests(unittest.TestCase):
    def test_oversized_reference_abstains(self):
        class Rows(list):
            def fetchall(self):
                return list(self)

        class FakeDatabase:
            def execute(self, statement, arguments):
                if 'evidence_aliases' in statement:
                    return Rows([('Assembly::T:Ship',)] if arguments[0] == 'ship' else [])
                if 'evidence_relations' in statement:
                    return iter([(' ' * 1000001,)])
                raise AssertionError('Unexpected query')

        rows = [{'id': 'chunk:test', 'kind': 'code_member', 'entity_id': 'Assembly::M:Ship.Run',
                 'parent': 'Assembly::T:Ship',
                 'sources': [{'path': 'Assets/Ship.cs', 'start_line': 1, 'end_line': 20}]}]
        ranked, used = _promote_unique_named_type_method(
            FakeDatabase(), rows, 'How does Ship use acceleration and target?')
        self.assertFalse(used)
        self.assertEqual(ranked, rows)


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class NamedTypeMethodRankingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Game'
        self.work = Path(self.temp.name) / 'Index'
        (self.root / 'Assets').mkdir(parents=True)
        (self.root / 'ProjectSettings').mkdir()
        (self.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 6000.0.1f1\n')
        self.source = self.root / 'Assets/Ship.cs'
        self.text = '''public class Ship {
 public int target;
 public void Accelerate(int value) { }
 public void FixedUpdate() { target = 1; Accelerate(target); }
 public void Decoy() { /* target acceleration */ var message = "target acceleration"; }
 public void Other() { target = 2; }
}
'''
        self.source.write_text(self.text)

    def build(self):
        refresh(self.work, self.root, semantic=False)

    def query(self, question='How does Ship choose acceleration from target?'):
        return retrieve(self.work, question, limit=20, expand=0, context_chars=100000)

    def promoted(self, result):
        return [item for item in result['results']
                if 'named_type_method' in item.get('ranking_diagnostics', {})]

    def test_unique_method_uses_parsed_identifiers_not_comments(self):
        self.build()
        result = self.promoted(self.query())
        self.assertEqual(len(result), 1)
        self.assertIn('::M:Ship.FixedUpdate', result[0]['entity_id'])
        self.assertFalse(any('Decoy' in item['entity_id'] for item in result))
        original = self.source.read_bytes()
        self.source.write_bytes(original + b'\n// changed\n')
        try:
            with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                verify_sources(status(self.work), result)
        finally:
            self.source.write_bytes(original)

    def test_equal_scoring_methods_abstain(self):
        self.source.write_text(self.text.replace(
            ' public void Decoy()',
            ' public void Tie() { target = 3; Accelerate(target); }\n public void Decoy()'))
        self.build()
        self.assertFalse(self.promoted(self.query()))

    def test_duplicate_type_name_abstains(self):
        (self.root / 'Assets/Other.cs').write_text(
            'namespace Different { public class Ship { public void Go() {} } }')
        self.build()
        self.assertFalse(self.promoted(self.query()))

    def test_non_how_and_kind_filters_abstain(self):
        self.build()
        self.assertFalse(self.promoted(self.query('What is Ship acceleration and target?')))
        filtered = retrieve(self.work, 'How does Ship choose acceleration from target?',
                            kind='asset_object', limit=20, context_chars=100000)
        self.assertFalse(self.promoted(filtered))

    def test_terms_in_separate_method_chunks_do_not_combine(self):
        filler = ''.join('  int filler%d = %d;\n' % (i, i) for i in range(150))
        self.source.write_text('public class Ship { public int target; public void Accelerate(int value) {} '
                               'public void FixedUpdate() { target = 1;\n' + filler + ' Accelerate(5); } }')
        self.build()
        self.assertFalse(self.promoted(self.query()))

    def test_partial_type_source_is_a_freshness_dependency(self):
        self.source.write_text('public partial class Ship { public int target; }')
        methods = self.root / 'Assets/ShipMethods.cs'
        methods.write_text('public partial class Ship { public void Accelerate(int value) {} '
                           'public void FixedUpdate() { target = 1; Accelerate(target); } }')
        self.build()
        result = self.promoted(self.query())
        self.assertEqual(len(result), 1)
        self.assertIn('Assets/Ship.cs', {source['path'] for source in result[0]['sources']
                                        if source.get('role') == 'reference_dependency'})
        original = self.source.read_bytes()
        self.source.write_bytes(original + b'\n// changed\n')
        try:
            with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                verify_sources(status(self.work), result)
        finally:
            self.source.write_bytes(original)
