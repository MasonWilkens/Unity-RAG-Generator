"""Content-keyed cache must never hide metadata mutations."""
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unity_rag import retrieval
from unity_rag.asset_map import fingerprint


class RetrievalStampCacheTests(unittest.TestCase):
    def setUp(self):
        retrieval._stamp_cache.clear()
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        self.code = {'discovery_scanned_at': 'generation-a', 'value': 1}
        self.assets = {'discovery_scanned_at': 'generation-a', 'code_fingerprint': fingerprint(self.code), 'value': 2}
        self.put('report', {'scanned_at': 'generation-a'})
        self.put('code_report', self.code)
        self.put('asset_report', self.assets)

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value)))

    def test_unchanged_reports_reuse_digests_but_modified_bytes_revalidate(self):
        with patch.object(retrieval, 'fingerprint', wraps=fingerprint) as hashed:
            initial = retrieval._database_stamp(self.db)
            self.assertEqual(hashed.call_count, 2)
            self.assertEqual(retrieval._database_stamp(self.db), initial)
            self.assertEqual(hashed.call_count, 2)
            self.assets['value'] = 3
            self.put('asset_report', self.assets)
            self.assertNotEqual(retrieval._database_stamp(self.db)['assets'], initial['assets'])
            self.assertEqual(hashed.call_count, 4)

    def test_mutated_code_or_inventory_never_bypasses_consistency(self):
        retrieval._database_stamp(self.db)
        self.code['value'] = 999
        self.put('code_report', self.code)
        with self.assertRaisesRegex(ValueError, 'Asset links are stale'):
            retrieval._database_stamp(self.db)
        self.code['value'] = 1
        self.put('code_report', self.code)
        self.put('report', {'scanned_at': 'generation-b'})
        with self.assertRaisesRegex(ValueError, 'Maps are stale'):
            retrieval._database_stamp(self.db)

    def test_missing_record_fails_closed(self):
        retrieval._database_stamp(self.db)
        self.db.execute("DELETE FROM metadata WHERE key='asset_report'")
        with self.assertRaisesRegex(ValueError, 'Run scan'):
            retrieval._database_stamp(self.db)

    def test_cache_is_bounded(self):
        for value in range(8):
            self.assets['value'] = value
            self.put('asset_report', self.assets)
            retrieval._database_stamp(self.db)
        self.assertLessEqual(len(retrieval._stamp_cache), 4)

    def test_seal_reuses_stamp_only_for_matching_complete_content(self):
        stamp, code_raw, asset_raw = retrieval._database_stamp(self.db, include_hashes=True)
        seal = {'version': 1, 'code_raw_sha256': code_raw, 'asset_raw_sha256': asset_raw, 'stamp': stamp}
        retrieval._stamp_cache.clear()
        with patch.object(retrieval, 'fingerprint', side_effect=AssertionError('sealed content was reparsed')):
            self.assertEqual(retrieval._database_stamp(self.db, seal), stamp)
        self.assets['value'] = 777
        self.put('asset_report', self.assets)
        with patch.object(retrieval, 'fingerprint', wraps=fingerprint) as hashed:
            self.assertNotEqual(retrieval._database_stamp(self.db, seal)['assets'], stamp['assets'])
            self.assertEqual(hashed.call_count, 2)

    def test_missing_or_malformed_seal_uses_full_validation(self):
        for seal in (None, {}, {'version': 999}, {'version': 1, 'stamp': 'bad'}):
            retrieval._stamp_cache.clear()
            with patch.object(retrieval, 'fingerprint', wraps=fingerprint) as hashed:
                retrieval._database_stamp(self.db, seal)
                self.assertEqual(hashed.call_count, 2)

    def test_open_rejects_changed_maps_after_cache_was_warmed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.sqlite3'
            disk = sqlite3.connect(path)
            self.db.commit()
            self.db.backup(disk)
            stamp = retrieval._database_stamp(disk)
            disk.execute('INSERT INTO metadata VALUES (?,?)', ('retrieval_report', json.dumps({'inputs': stamp})))
            disk.commit()
            disk.close()
            db, _ = retrieval._open(directory)
            db.close()
            disk = sqlite3.connect(path)
            self.assets['value'] = 456
            disk.execute('UPDATE metadata SET value=? WHERE key=?', (json.dumps(self.assets), 'asset_report'))
            disk.commit()
            disk.close()
            with self.assertRaisesRegex(ValueError, 'Retrieval index is stale'):
                retrieval._open(directory)


if __name__ == '__main__':
    unittest.main()
