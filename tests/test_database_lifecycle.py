"""Database handles must close even when callers retain them and GC cannot help."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.asset_map import asset_report, map_assets
from unity_rag.cli import connect
from unity_rag.discovery import scan
from unity_rag.retrieval import _open, retrieve
from unity_rag.workspace import refresh, seal_workspace, workspace_status
from test_semantic import FixtureProvider


class DatabaseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'Game'
        self.work = self.root / 'Knowledge'
        shutil.copytree(Path(__file__).parent / 'fixtures/retrieval_project', self.project)

    @contextmanager
    def retained_connections(self):
        original = sqlite3.connect
        opened = []

        def record(*args, **kwargs):
            db = original(*args, **kwargs)
            opened.append(db)  # Strong references prevent collection on every Python version.
            return db

        with patch('sqlite3.connect', side_effect=record):
            try:
                yield
            finally:
                leaked = 0
                for db in opened:
                    try:
                        db.execute('SELECT 1')
                        leaked += 1
                    except sqlite3.ProgrammingError:
                        pass
                    finally:
                        db.close()
                self.assertGreater(len(opened), 0)
                self.assertEqual(leaked, 0, 'Connections remained usable after their operation ended')

    @unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
    def test_build_read_refresh_and_seal_release_all_connections(self):
        provider = FixtureProvider()
        with self.retained_connections():
            refresh(self.work, self.project, provider=provider)
            self.assertTrue(workspace_status(self.work)['healthy'])
            self.assertTrue(retrieve(self.work, 'CanAttack', mode='hybrid', provider=provider)['results'])
            self.assertEqual(refresh(self.work, self.project, provider=provider)['state'], 'unchanged')
            seal_workspace(self.work)
            self.assertTrue(workspace_status(self.work)['healthy'])

    def test_failed_asset_write_closes_and_rolls_back(self):
        with self.retained_connections():
            scan(self.project, self.work)
            before = map_assets(self.work)
            with patch('unity_rag.asset_map.encode_asset_report', side_effect=RuntimeError('injected failure')):
                with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                    map_assets(self.work, compress_report=True)
            self.assertEqual(asset_report(self.work), before)

    def test_malformed_retrieval_metadata_closes_connection(self):
        self.work.mkdir()
        with closing(sqlite3.connect(self.work / 'inventory.sqlite3')) as db, db:
            db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
            db.execute("INSERT INTO metadata VALUES ('retrieval_report', 'malformed')")
        with self.retained_connections(), patch('unity_rag.retrieval._database_stamp', return_value={}):
            with self.assertRaises(json.JSONDecodeError):
                _open(self.work)

    def test_legacy_schema_failure_closes_connection(self):
        class BrokenConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith('CREATE'):
                    raise sqlite3.OperationalError('injected schema failure')
                return super().execute(sql, *args)

        original = sqlite3.connect
        db = original(self.root / 'legacy.sqlite3', factory=BrokenConnection)
        try:
            with patch('unity_rag.cli.sqlite3.connect', return_value=db):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'injected schema failure'):
                    connect(self.root / 'legacy.sqlite3')
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
