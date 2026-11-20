"""Opt-in conditional coverage is bounded, snapshot-consistent, and read-only."""
from contextlib import redirect_stdout, closing
import hashlib
import importlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag import cli
from unity_rag.coverage import coverage
from unity_rag.discovery import scan, status


class ConditionalCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Game'
        self.work = Path(self.temp.name) / 'Index'
        (self.root / 'Assets').mkdir(parents=True)
        (self.root / 'ProjectSettings').mkdir()
        (self.root / 'Assets/Sample.cs').write_text('class Sample {}\n')
        (self.root / 'Assets/Other.cs').write_text('class Other {}\n')
        (self.root / 'Assets/Unmapped.cs').write_text('class Unmapped {}\n')
        scan(self.root, self.work)
        self.database = self.work / 'inventory.sqlite3'

    def save(self, value):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('code_report',?)", (value,))

    def report(self):
        return {'discovery_scanned_at': status(self.work)['scanned_at'], 'defines': ['UNITY_IOS'],
                'files': [{'path': 'Assets/Sample.cs'}, {'path': 'Assets/Other.cs'}], 'conditions': [
                    {'kind': 'DisabledTextTrivia', 'path': 'Assets/Sample.cs', 'text': 'PRIVATE_MARKER'},
                    {'kind': 'DisabledTextTrivia', 'path': 'Assets/Sample.cs', 'text': 'OTHER_MARKER'},
                    {'kind': 'IfDirectiveTrivia', 'path': 'Assets/Sample.cs', 'text': '#if TEST'},
                    {'kind': 'DisabledTextTrivia', 'path': 'Assets/Other.cs', 'text': 'PRIVATE_MARKER'}]}

    def test_current_counts_only_mapped_requested_scripts_and_is_read_only(self):
        self.save(json.dumps(self.report()))
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        result = coverage(self.work, ['Assets/Sample.cs', 'Assets/Other.cs', 'Assets/Unmapped.cs'],
                          include_conditional=True)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)
        self.assertEqual(result['conditional_compilation'], {
            'status': 'current', 'supplied_defines': ['UNITY_IOS'],
            'inactive_text_regions': 3, 'files_with_inactive_text': 2})
        rows = {row['path']: row for row in result['requested_paths']}
        self.assertEqual(rows['Assets/Sample.cs']['inactive_preprocessor_regions'], 2)
        self.assertEqual(rows['Assets/Other.cs']['inactive_preprocessor_regions'], 1)
        self.assertIsNone(rows['Assets/Unmapped.cs']['inactive_preprocessor_regions'])
        self.assertNotIn('PRIVATE_MARKER', json.dumps(result))
        (self.root / 'Assets/Sample.cs').write_text('changed')
        result = coverage(self.work, ['Assets/Sample.cs'], include_conditional=True)
        self.assertEqual(result['requested_paths'][0]['source_status'], 'stale')
        self.assertEqual(result['requested_paths'][0]['inactive_preprocessor_regions'], 2)

    def test_opt_in_uses_one_read_transaction(self):
        module = importlib.import_module('unity_rag.coverage')
        original = module._conditional_coverage
        self.save(json.dumps(self.report()))
        def check(db, *args):
            self.assertTrue(db.in_transaction)
            return original(db, *args)
        with patch('unity_rag.coverage._conditional_coverage', side_effect=check):
            result = coverage(self.work, include_conditional=True)
        self.assertEqual(result['conditional_compilation']['status'], 'current')

    def test_concurrent_code_map_update_does_not_mix_snapshots(self):
        module = importlib.import_module('unity_rag.coverage')
        original = module._conditional_coverage
        self.save(json.dumps(self.report()))
        with closing(sqlite3.connect(self.database)) as writer, writer:
            writer.execute('PRAGMA journal_mode=WAL')
        def replace_during_read(db, *args):
            updated = self.report()
            updated['defines'] = ['NEW_DEFINE']
            updated['conditions'] = []
            with closing(sqlite3.connect(self.database)) as writer, writer:
                writer.execute("UPDATE metadata SET value=? WHERE key='code_report'", (json.dumps(updated),))
            return original(db, *args)
        with patch('unity_rag.coverage._conditional_coverage', side_effect=replace_during_read):
            result = coverage(self.work, include_conditional=True)
        self.assertEqual(result['conditional_compilation']['supplied_defines'], ['UNITY_IOS'])
        self.assertEqual(result['conditional_compilation']['inactive_text_regions'], 3)
        updated = coverage(self.work, include_conditional=True)
        self.assertEqual(updated['conditional_compilation']['supplied_defines'], ['NEW_DEFINE'])

    def test_default_does_not_read_conditional_metadata(self):
        self.save(json.dumps(self.report()))
        with patch('unity_rag.coverage._conditional_coverage', side_effect=AssertionError('unexpected read')):
            result = coverage(self.work, ['Assets/Sample.cs'])
        self.assertNotIn('conditional_compilation', result)
        self.assertNotIn('inactive_preprocessor_regions', result['requested_paths'][0])

    def test_cli_opt_in_emits_conditional_data(self):
        self.save(json.dumps(self.report()))
        args = ['unity-rag', 'coverage', '--workspace', str(self.work),
                '--path', 'Assets/Sample.cs', '--conditional']
        with patch('sys.argv', args), redirect_stdout(io.StringIO()) as output:
            cli.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['conditional_compilation']['status'], 'current')
        self.assertEqual(result['requested_paths'][0]['inactive_preprocessor_regions'], 2)

    def test_invalid_opt_in_type_is_rejected(self):
        with self.assertRaises(ValueError):
            coverage(self.work, include_conditional='yes')

    def test_no_map_stale_invalid_and_oversized_are_not_zero(self):
        self.assertEqual(coverage(self.work, include_conditional=True)['conditional_compilation']['status'], 'not_mapped')
        report = self.report()
        report['discovery_scanned_at'] = 'old'
        self.save(json.dumps(report))
        result = coverage(self.work, ['Assets/Sample.cs'], include_conditional=True)
        self.assertEqual(result['conditional_compilation']['status'], 'stale')
        self.assertIsNone(result['requested_paths'][0]['inactive_preprocessor_regions'])
        self.save('{broken')
        self.assertEqual(coverage(self.work, include_conditional=True)['conditional_compilation']['status'], 'unavailable')
        self.save(json.dumps(self.report()))
        with patch('unity_rag.coverage._MAX_CODE_REPORT_BYTES', 10):
            self.assertEqual(coverage(self.work, include_conditional=True)['conditional_compilation']['status'], 'too_large')

    def test_missing_or_malformed_fields_are_unavailable(self):
        bad_reports = []
        report = self.report(); del report['conditions']; bad_reports.append(report)
        report = self.report(); report['defines'] = [1]; bad_reports.append(report)
        report = self.report(); report['conditions'] = ['not an object']; bad_reports.append(report)
        report = self.report(); report['conditions'] = [{'kind': 'DisabledTextTrivia'}]; bad_reports.append(report)
        report = self.report(); report['files'] = [None]; bad_reports.append(report)
        report = self.report(); report['files'] = [{'path': 'Assets/Sample.cs'}]; bad_reports.append(report)
        for report in bad_reports:
            with self.subTest(report=report):
                self.save(json.dumps(report))
                self.assertEqual(coverage(self.work, include_conditional=True)['conditional_compilation']['status'], 'unavailable')
