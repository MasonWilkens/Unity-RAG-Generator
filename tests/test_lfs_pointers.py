import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest

from unity_rag.discovery import scan, status
from unity_rag.asset_map import map_assets

POINTER = 'version https://git-lfs.github.com/spec/v1\noid sha256:' + 'a' * 64 + '\nsize 12345\n'


class LfsPointerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Game'
        self.work = Path(self.temp.name) / 'Knowledge'
        (self.root / 'Assets').mkdir(parents=True)
        (self.root / 'ProjectSettings').mkdir()
        (self.root / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 2022.3.1f1\n')

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
        return path

    def test_valid_pointers_are_unavailable_and_not_parsed(self):
        scene = self.write('Assets/Level.unity', POINTER)
        self.write('Assets/Enemy.cs', POINTER.replace('\n', '\r\n'))
        self.write('Assets/Settings.asset', '\ufeff' + POINTER)
        self.write('Assets/Material.mat', POINTER.replace('oid sha256:', 'ext-0-foo bar\noid sha256:'))
        self.write('Assets/.secret', POINTER)
        report = scan(self.root, self.work)
        rows = {r['path']: r for r in report['entries']}
        for name in ('Level.unity', 'Enemy.cs', 'Settings.asset', 'Material.mat'):
            row = rows['Assets/' + name]
            self.assertEqual(row['status'], 'lfs_pointer')
            self.assertEqual(row['reason'], 'unmaterialized_git_lfs_pointer')
            self.assertEqual(row['sha256'], hashlib.sha256((self.root / row['path']).read_bytes()).hexdigest())
        self.assertEqual(rows['Assets/.secret']['status'], 'excluded')
        self.assertEqual(report['counts']['lfs_pointer'], 4)
        self.assertEqual(status(self.work), report)
        assets = map_assets(self.work)
        mapped = {a['path']: a for a in assets['assets']}
        self.assertEqual(mapped['Assets/Level.unity']['parse_status'], 'unavailable_lfs_pointer')
        self.assertFalse(any(o['path'] == 'Assets/Level.unity' for o in assets['objects']))
        self.assertEqual(scene.read_text(), POINTER)

    def test_references_to_pointers_are_not_reported_as_available(self):
        guid = 'b' * 32
        self.write('Assets/Settings.asset', POINTER)
        self.write('Assets/Settings.asset.meta', 'fileFormatVersion: 2\nguid: ' + guid + '\n')
        self.write('Assets/Ref.prefab', '--- !u!114 &1\nMonoBehaviour:\n  target: {fileID: 11400000, guid: ' + guid + ', type: 2}\n')
        scan(self.root, self.work)
        report = map_assets(self.work)
        edge = next(e for e in report['edges'] if e['property'] == 'target')
        self.assertEqual(edge['target_asset'], 'asset:Assets/Settings.asset')
        self.assertEqual(edge['resolution'], 'unavailable_lfs_pointer')
        self.assertIsNone(edge['target'])

    def test_legacy_keyword_index_skips_pointer_text(self):
        from unity_rag.cli import index, search
        self.write('Assets/Missing.cs', POINTER)
        self.write('Assets/Present.cs', 'public class Present {}\n')
        database = Path(self.temp.name) / 'legacy.sqlite3'
        report = index(self.root, database)
        self.assertEqual(report['files'], 2)
        self.assertEqual(report['skipped'], 1)
        self.assertEqual(search(database, 'sha256'), [])
        self.assertTrue(search(database, 'Present'))

    def test_non_pointers_are_not_false_positives(self):
        variants = [
            'version https://git-lfs.github.com/spec/v1\n',
            POINTER.replace('size 12345', 'size nope'),
            POINTER.replace('a' * 64, 'a' * 63),
            POINTER + 'extra non-pointer content\n',
            '# documentation\n' + POINTER,
        ]
        for i, text in enumerate(variants):
            self.write(f'Assets/Example{i}.txt', text)
        report = scan(self.root, self.work)
        rows = {r['path']: r for r in report['entries']}
        for i in range(len(variants)):
            self.assertEqual(rows[f'Assets/Example{i}.txt']['status'], 'supported')

    @unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
    def test_materializing_pointer_forces_full_refresh_and_preserves_old_generation(self):
        from unity_rag.code_map import code_report
        from unity_rag.workspace import refresh, resolve_workspace, workspace_status
        source = self.write('Assets/Materialized.cs', POINTER)
        first = refresh(self.work, self.root, semantic=False)
        old = resolve_workspace(self.work)
        self.assertEqual(first['state'], 'complete')
        self.assertFalse(any(f['path'] == 'Assets/Materialized.cs' for f in code_report(self.work)['files']))
        source.write_text('public class Materialized {}\n', encoding='utf-8')
        health = workspace_status(self.work)
        self.assertFalse(health['fresh'])
        self.assertEqual(health['refresh_plan']['mode'], 'full')
        second = refresh(self.work, self.root, semantic=False)
        self.assertEqual(second['refresh_mode'], 'full')
        self.assertNotEqual(resolve_workspace(self.work), old)
        self.assertTrue(any(f['path'] == 'Assets/Materialized.cs' for f in code_report(self.work)['files']))
        self.assertTrue(old.is_dir())
        self.assertTrue(workspace_status(self.work)['fresh'])

    @unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
    def test_pointer_scripts_do_not_enter_code_map(self):
        from unity_rag.code_map import map_code
        self.write('Assets/Missing.cs', POINTER)
        self.write('Assets/Present.cs', 'public class Present {}\n')
        scan(self.root, self.work)
        report = map_code(self.work)
        paths = {f['path'] for f in report['files']}
        self.assertIn('Assets/Present.cs', paths)
        self.assertNotIn('Assets/Missing.cs', paths)


if __name__ == '__main__':
    unittest.main()
