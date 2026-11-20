import hashlib
import io
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from unity_rag import cli
from unity_rag.coverage import coverage
from unity_rag.discovery import scan


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project, self.work = root / 'game', root / 'index'
        assets = self.project / 'Assets'
        assets.mkdir(parents=True)
        (self.project / 'ProjectSettings').mkdir()
        (assets / 'Guide.md').write_text('local sheet authoring guide\n', encoding='utf-8')
        (assets / 'Other.md').write_text('not in retrieval evidence\n', encoding='utf-8')
        (assets / 'export.csv').write_text('Name,Damage\nA,3\n', encoding='utf-8')
        scan(self.project, self.work)
        self.database = self.work / 'inventory.sqlite3'

    def evidence(self):
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE evidence (id TEXT, entity TEXT, kind TEXT, data TEXT)')
            data = json.dumps({'sources': [{'path': 'Assets/Guide.md'}]})
            db.execute('INSERT INTO evidence VALUES (?, ?, ?, ?)', ('1', '1', 'documentation', data))

    def test_snapshot_coverage_and_requested_current_stale_missing(self):
        self.evidence()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        result = coverage(self.work, ['Assets/Guide.md', 'Assets/Other.md', 'Assets/export.csv', 'Assets/unknown.md'])
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)
        self.assertEqual(result['evidence_records_by_kind'], {'documentation': 1})
        self.assertIn({'extension': '.csv', 'status': 'unsupported', 'files': 1}, result['spreadsheet_files'])
        rows = {row['path']: row for row in result['requested_paths']}
        self.assertEqual(rows['Assets/Guide.md']['evidence_records'], {'documentation': 1})
        self.assertEqual(rows['Assets/Guide.md']['source_status'], 'current')
        self.assertEqual(rows['Assets/Other.md']['evidence_records'], {})
        self.assertEqual(rows['Assets/export.csv']['source_status'], 'unsupported')
        self.assertEqual(rows['Assets/unknown.md']['inventory_status'], 'not_in_inventory')
        (self.project / 'Assets' / 'Guide.md').write_text('changed', encoding='utf-8')
        self.assertEqual(coverage(self.work, ['Assets/Guide.md'])['requested_paths'][0]['source_status'], 'stale')
        (self.project / 'Assets' / 'Guide.md').unlink()
        self.assertEqual(coverage(self.work, ['Assets/Guide.md'])['requested_paths'][0]['source_status'], 'missing')

    def test_unbuilt_retrieval_is_not_reported_as_zero_coverage(self):
        result = coverage(self.work, ['Assets/Guide.md'])
        self.assertFalse(result['evidence_table_present'])
        self.assertIsNone(result['evidence_records_by_kind'])
        self.assertIsNone(result['requested_paths'][0]['evidence_records'])

    def test_unsafe_paths_rejected_before_source_access(self):
        for path in ('.', '../other.txt', '/absolute', 'C:/outside', 'Assets/.env/key', 'Assets/token.secret', 'Assets\\Guide.md'):
            with self.subTest(path=path), patch('unity_rag.coverage.inspect_source', side_effect=AssertionError('source read')):
                with self.assertRaises(ValueError):
                    coverage(self.work, [path])
        with self.assertRaises(ValueError):
            coverage(self.work, ['Assets/Guide.md'] * 51)

    def test_report_stays_on_pinned_generation_if_active_pointer_changes(self):
        self.evidence()
        generations = self.work / 'generations'
        first, second = generations / ('a' * 32), generations / ('b' * 32)
        for directory in (first, second):
            directory.mkdir(parents=True)
            shutil.copy2(self.database, directory / 'inventory.sqlite3')
            (directory / 'complete.json').write_text(json.dumps({'generation': directory.name}), encoding='utf-8')
        (self.work / 'active.json').write_text(json.dumps({'generation': first.name}), encoding='utf-8')
        from unity_rag.discovery import status
        def switch_active(pinned):
            (self.work / 'active.json').write_text(json.dumps({'generation': second.name}), encoding='utf-8')
            return status(pinned)
        with patch('unity_rag.coverage.status', side_effect=switch_active):
            result = coverage(self.work, ['Assets/Guide.md'])
        self.assertEqual(result['generation'], first.name)
        self.assertEqual(result['requested_paths'][0]['evidence_records'], {'documentation': 1})
    def test_cli_returns_json_and_checks_path(self):
        self.evidence()
        with patch('sys.argv', ['unity-rag', 'coverage', '--workspace', str(self.work), '--path', 'Assets/Guide.md']), redirect_stdout(io.StringIO()) as out:
            cli.main()
        self.assertEqual(json.loads(out.getvalue())['requested_paths'][0]['source_status'], 'current')


if __name__ == '__main__':
    unittest.main()
