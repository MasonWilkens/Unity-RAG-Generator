import gc
import json
from pathlib import Path
from contextlib import closing
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.asset_map import map_assets
from unity_rag.code_map import map_code
from unity_rag.discovery import scan
from unity_rag.retrieval import build_retrieval


class FailingConnection(sqlite3.Connection):
    def executemany(self, sql, values):
        if sql.startswith('INSERT INTO evidence VALUES'):
            iterator = iter(values)
            def interrupted():
                yield next(iterator)
                raise RuntimeError('injected insert failure')
            return super().executemany(sql, interrupted())
        return super().executemany(sql, values)


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class RetrievalBuildSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.project = cls.base / 'Game'
        cls.work = cls.base / 'Knowledge'
        shutil.copytree(Path(__file__).parent / 'fixtures/retrieval_project', cls.project)
        scan(cls.project, cls.work); map_code(cls.work); map_assets(cls.work); build_retrieval(cls.work)

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        cls.temp.cleanup()

    def snapshot(self):
        with closing(sqlite3.connect(self.work / 'inventory.sqlite3')) as db:
            return (db.execute('SELECT id, data FROM evidence ORDER BY id').fetchall(),
                    db.execute("SELECT key, value FROM metadata ORDER BY key").fetchall())

    def test_changed_map_stamp_refuses_before_replacing_evidence(self):
        before = self.snapshot()
        with patch('unity_rag.retrieval._database_stamp', return_value={'discovery': 'changed'}):
            with self.assertRaisesRegex(ValueError, 'Maps changed during retrieval'):
                build_retrieval(self.work)
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_map_change_before_transaction_preserves_evidence(self):
        before = self.snapshot()
        original = sqlite3.connect
        original_report = dict(before[1])['report']
        injected = False
        def connection(*args, **kwargs):
            nonlocal injected
            if not kwargs.get('uri') and not injected:
                injected = True
                with closing(original(*args, **kwargs)) as writer:
                    report = json.loads(original_report)
                    report['scanned_at'] = 'externally-changed'
                    writer.execute("UPDATE metadata SET value=? WHERE key='report'", (json.dumps(report),))
                    writer.commit()
            return original(*args, **kwargs)
        try:
            with patch('unity_rag.retrieval.sqlite3.connect', side_effect=connection):
                with self.assertRaises(ValueError):
                    build_retrieval(self.work)
            self.assertTrue(injected)
            after = self.snapshot()
            self.assertEqual(after[0], before[0])
            self.assertEqual(dict(after[1])['retrieval_report'], dict(before[1])['retrieval_report'])
        finally:
            gc.collect()
            with closing(original(self.work / 'inventory.sqlite3')) as writer:
                writer.execute("UPDATE metadata SET value=? WHERE key='report'", (original_report,))
                writer.commit()

    def test_map_change_inside_write_transaction_rolls_back(self):
        import unity_rag.retrieval as retrieval
        before = self.snapshot()
        original = retrieval._database_stamp
        def mutate_then_check(db):
            report = json.loads(db.execute("SELECT value FROM metadata WHERE key='report'").fetchone()[0])
            report['scanned_at'] = 'changed-during-build'
            db.execute("UPDATE metadata SET value=? WHERE key='report'", (json.dumps(report),))
            return original(db)
        with patch('unity_rag.retrieval._database_stamp', side_effect=mutate_then_check):
            with self.assertRaises(ValueError):
                build_retrieval(self.work)
        self.assertEqual(self.snapshot(), before)

    def test_failed_mid_insert_rolls_back_previous_evidence(self):
        before = self.snapshot()
        original = sqlite3.connect
        opened = []
        def connection(*args, **kwargs):
            if not kwargs.get('uri'):
                kwargs['factory'] = FailingConnection
                db = original(*args, **kwargs)
                opened.append(db)
                return db
            return original(*args, **kwargs)
        try:
            with patch('unity_rag.retrieval.sqlite3.connect', side_effect=connection):
                with self.assertRaisesRegex(RuntimeError, 'injected insert failure'):
                    build_retrieval(self.work)
        finally:
            for db in opened:
                db.close()
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
