from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from unity_rag.asset_map import (
    _REPORT_MAGIC, _REPORT_MAX_BYTES, asset_report, decode_asset_report, encode_asset_report,
)
from unity_rag.retrieval import _database_stamp, retrieve
from unity_rag.workspace import (
    _atomic, _pipeline_compatible, _published_pointer, refresh, resolve_workspace, workspace_status,
)


class AssetReportCodecTests(unittest.TestCase):
    def test_canonical_and_legacy_round_trip(self):
        values = ({}, {'a': [1, 2, 3], 'unicode': 'é', 'nested': {'x': True, 'y': None}},
                  {'text': 'a' * 2000000})
        for value in values:
            raw = json.dumps(value)
            self.assertEqual(decode_asset_report(raw), raw)
            self.assertEqual(decode_asset_report(encode_asset_report(value)), raw)

    def test_unknown_representation_is_rejected(self):
        for value in (None, b'unknown', 123, b'URAGAR1\x00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_asset_report(value)

    def test_corruption_truncation_and_trailing_data_are_rejected(self):
        blob = encode_asset_report({'foo': 'bar' * 100})
        for invalid in (blob[:-1], blob + b'extra', blob[:20] + b'corrupt' + blob[27:]):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                decode_asset_report(invalid)

    def test_oversize_and_wrong_length_are_rejected(self):
        for size in (_REPORT_MAX_BYTES + 1, 1, 100):
            blob = _REPORT_MAGIC + struct.pack('>Q', size) + zlib.compress(b'{}')
            with self.subTest(size=size), self.assertRaises(ValueError):
                decode_asset_report(blob)
        with patch('unity_rag.asset_map._REPORT_MAX_BYTES', 10):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                encode_asset_report({'a': 'x' * 100})


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class AssetReportStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project, self.work = root / 'Game', root / 'Knowledge'
        shutil.copytree(Path(__file__).parent / 'fixtures/retrieval_project', self.project)

    def raw_report(self, generation=None):
        generation = generation or resolve_workspace(self.work)
        with closing(sqlite3.connect(generation / 'inventory.sqlite3')) as db, db:
            return db.execute("SELECT typeof(value), value FROM metadata WHERE key='asset_report'").fetchone()

    def test_default_is_legacy_text(self):
        refresh(self.work, self.project, semantic=False)
        self.assertEqual(self.raw_report()[0], 'text')
        self.assertTrue(workspace_status(self.work)['healthy'])
        self.assertTrue(asset_report(self.work)['assets'])

    def test_opt_in_and_incremental_refresh_preserve_encoding(self):
        refresh(self.work, self.project, semantic=False, compress_asset_report=True)
        old = resolve_workspace(self.work)
        self.assertEqual(self.raw_report(old)[0], 'blob')
        self.assertTrue(retrieve(self.work, 'Enemy', mode='search')['results'])
        (self.project / 'Assets/newnotes.md').write_text('new documentation\n')
        result = refresh(self.work)
        new = resolve_workspace(self.work)
        self.assertEqual(result['refresh_mode'], 'documentation')
        self.assertNotEqual(new, old)
        self.assertEqual(self.raw_report(new)[0], 'blob')
        self.assertEqual(self.raw_report(old)[0], 'blob')
        self.assertTrue(workspace_status(self.work)['healthy'])
        manifest = json.loads((new / 'complete.json').read_text())
        self.assertTrue(manifest['settings']['compress_asset_report'])

    def test_explicit_switch_to_text_preserves_prior_generation(self):
        refresh(self.work, self.project, semantic=False, compress_asset_report=True)
        old = resolve_workspace(self.work)
        refresh(self.work, compress_asset_report=False)
        new = resolve_workspace(self.work)
        self.assertNotEqual(new, old)
        self.assertEqual(self.raw_report(old)[0], 'blob')
        self.assertEqual(self.raw_report(new)[0], 'text')
        self.assertTrue(workspace_status(self.work)['healthy'])

    def test_failed_compression_preserves_active_generation(self):
        refresh(self.work, self.project, semantic=False)
        old = resolve_workspace(self.work)
        pointer = (self.work / 'active.json').read_bytes()
        with patch('unity_rag.asset_map.encode_asset_report', side_effect=ValueError('injected')):
            with self.assertRaisesRegex(ValueError, 'injected'):
                refresh(self.work, compress_asset_report=True)
        self.assertEqual(pointer, (self.work / 'active.json').read_bytes())
        self.assertEqual(resolve_workspace(self.work), old)
        self.assertEqual(self.raw_report(old)[0], 'text')
        self.assertTrue(asset_report(self.work)['assets'])

    def test_legacy_text_report_remaps_after_mapper_change(self):
        refresh(self.work, self.project, semantic=False)
        generation = resolve_workspace(self.work)
        manifest = json.loads((generation / 'complete.json').read_text())
        manifest['settings']['pipeline'] = '82369f7ab5f7b77e4b58360dba7798ce5ae53bf716c980c90adda4a87897012a'
        manifest['settings'].pop('compress_asset_report')
        _atomic(generation / 'complete.json', manifest)
        _atomic(self.work / 'active.json', _published_pointer(generation, generation.name))
        # Compression alone was compatible with this older mapper. The nested
        # override resolver changes map contents, so today's pipeline must remap.
        with patch('unity_rag.workspace._pipeline_hash', return_value='4c312291034db34e036b2945c2a630af241b6f35edbe0556f4333ea826cf0cdb'):
            self.assertTrue(_pipeline_compatible(manifest['settings']['pipeline']))
        self.assertFalse(_pipeline_compatible(manifest['settings']['pipeline']))
        (self.project / 'Assets/legacy-note.md').write_text('legacy documentation\n')
        result = refresh(self.work)
        self.assertEqual(result['refresh_mode'], 'full')
        self.assertEqual(self.raw_report()[0], 'text')

    def test_corrupted_blob_fails_closed(self):
        refresh(self.work, self.project, semantic=False, compress_asset_report=True)
        generation = resolve_workspace(self.work)
        with closing(sqlite3.connect(generation / 'inventory.sqlite3')) as db, db:
            blob = db.execute("SELECT value FROM metadata WHERE key='asset_report'").fetchone()[0]
            db.execute("UPDATE metadata SET value=? WHERE key='asset_report'", (blob[:-1],))
        with self.assertRaisesRegex(ValueError, 'Compressed asset report'):
            asset_report(self.work)

    def test_text_and_compressed_have_identical_canonical_stamp(self):
        refresh(self.work, self.project, semantic=False, compress_asset_report=True)
        generation = resolve_workspace(self.work)
        with closing(sqlite3.connect(generation / 'inventory.sqlite3')) as db, db:
            before = _database_stamp(db, include_hashes=True)
            raw = decode_asset_report(db.execute("SELECT value FROM metadata WHERE key='asset_report'").fetchone()[0])
            db.execute("UPDATE metadata SET value=? WHERE key='asset_report'", (raw,))
            after = _database_stamp(db, include_hashes=True)
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
