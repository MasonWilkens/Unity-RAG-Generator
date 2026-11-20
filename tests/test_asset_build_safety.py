from contextlib import closing
import gc
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.asset_map import asset_report, map_assets
from unity_rag.discovery import scan


class FailingConnection(sqlite3.Connection):
    def executemany(self, sql, values):
        if sql.startswith('INSERT INTO unity_objects VALUES'):
            iterator = iter(values)
            def interrupted():
                yield next(iterator)
                raise RuntimeError('injected asset insert failure')
            return super().executemany(sql, interrupted())
        return super().executemany(sql, values)


class AssetBuildSafetyTests(unittest.TestCase):
    def test_failed_mid_insert_preserves_previous_asset_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'Game'
            work = Path(directory) / 'Knowledge'
            (root / 'Assets').mkdir(parents=True)
            (root / 'ProjectSettings').mkdir()
            (root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 2022.3.1f1\n')
            (root / 'Assets/Thing.prefab').write_text('--- !u!1 &1\nGameObject:\n  m_Name: Thing\n')
            scan(root, work)
            before = map_assets(work)
            with closing(sqlite3.connect(work / 'inventory.sqlite3')) as db:
                old_objects = db.execute('SELECT id, asset, data FROM unity_objects ORDER BY id').fetchall()
            original = sqlite3.connect
            opened = []
            def connection(*args, **kwargs):
                if not kwargs.get('uri'):
                    kwargs['factory'] = FailingConnection
                    db = original(*args, **kwargs); opened.append(db); return db
                return original(*args, **kwargs)
            try:
                with patch('unity_rag.asset_map.sqlite3.connect', side_effect=connection):
                    with self.assertRaisesRegex(RuntimeError, 'injected asset insert failure'):
                        map_assets(work)
            finally:
                for db in opened: db.close()
                gc.collect()
            self.assertEqual(asset_report(work), before)
            with closing(original(work / 'inventory.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT id, asset, data FROM unity_objects ORDER BY id').fetchall(), old_objects)
            gc.collect()


if __name__ == '__main__':
    unittest.main()
