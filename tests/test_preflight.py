import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from unity_rag import cli
from unity_rag.preflight import creation_preflight


class CreationPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'game'
        (self.project / 'Assets').mkdir(parents=True)
        (self.project / 'ProjectSettings').mkdir()
        self.workspace = self.root / 'indexes' / 'new'
        self.modules = patch('unity_rag.preflight._module_available', return_value=True)
        self.dotnet = patch('unity_rag.preflight.shutil.which', return_value='dotnet')
        self.modules.start(); self.dotnet.start()
        self.addCleanup(self.modules.stop); self.addCleanup(self.dotnet.stop)

    def test_valid_preflight_does_not_create_workspace(self):
        result = creation_preflight(self.project, self.workspace, semantic=True)
        self.assertTrue(result['ready_for_attempt'])
        self.assertFalse(self.workspace.exists())
        self.assertTrue(result['semantic_requested'])
        self.assertIn('.cs', result['supported_extensions'])
        self.assertNotIn('.csv', result['supported_extensions'])

    def test_invalid_project_and_inside_workspace_fail(self):
        result = creation_preflight(self.root / 'missing', self.workspace)
        self.assertFalse(result['ready_for_attempt'])
        self.assertEqual(next(c['status'] for c in result['checks'] if c['name'] == 'unity_project'), 'fail')
        result = creation_preflight(self.project, self.project / 'Index')
        self.assertFalse(result['ready_for_attempt'])
        self.assertFalse((self.project / 'Index').exists())

    def test_existing_file_workspace_fails(self):
        file = self.root / 'not-a-directory'
        file.write_text('unchanged', encoding='utf-8')
        result = creation_preflight(self.project, file)
        self.assertFalse(result['ready_for_attempt'])
        self.assertEqual(file.read_text(encoding='utf-8'), 'unchanged')

    def test_existing_inventory_cannot_be_reused_for_another_project(self):
        from unity_rag.discovery import scan
        scan(self.project, self.workspace)
        other = self.root / 'other'
        (other / 'Assets').mkdir(parents=True)
        (other / 'ProjectSettings').mkdir()
        result = creation_preflight(other, self.workspace)
        self.assertFalse(result['ready_for_attempt'])
        self.assertEqual(next(c['status'] for c in result['checks'] if c['name'] == 'workspace_binding'), 'fail')

    def test_missing_dependencies_and_fts_fail_closed(self):
        with patch('unity_rag.preflight.shutil.which', return_value=None), \
             patch('unity_rag.preflight.sqlite3.connect', side_effect=sqlite3.OperationalError('unavailable')):
            result = creation_preflight(self.project, self.workspace)
        failed = {c['name'] for c in result['checks'] if c['status'] == 'fail'}
        self.assertTrue({'dotnet_on_path', 'sqlite_fts5'} <= failed)
        self.assertFalse(result['ready_for_attempt'])

    def test_optional_semantic_dependency_only_checked_when_requested(self):
        with patch('unity_rag.preflight._module_available', side_effect=lambda name: name == 'yaml'):
            self.assertTrue(creation_preflight(self.project, self.workspace)['ready_for_attempt'])
            result = creation_preflight(self.project, self.workspace, semantic=True)
        self.assertFalse(result['ready_for_attempt'])
        self.assertIn('fastembed_dependency', [c['name'] for c in result['checks'] if c['status'] == 'fail'])

    def test_disk_probe_failure_does_not_claim_readiness(self):
        with patch('unity_rag.preflight.shutil.disk_usage', side_effect=OSError('unavailable')):
            result = creation_preflight(self.project, self.workspace)
        self.assertFalse(result['ready_for_attempt'])
        self.assertIsNone(result['free_bytes'])

    def test_cli_exit_and_json(self):
        args = ['unity-rag', 'creation-preflight', '--project', str(self.project), '--workspace', str(self.workspace)]
        with patch('sys.argv', args), redirect_stdout(io.StringIO()) as out:
            exit_code = cli.main()
        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(out.getvalue())['ready_for_attempt'])
        with patch('sys.argv', args + ['--semantic']), \
             patch('unity_rag.preflight._module_available', side_effect=lambda name: name == 'yaml'), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 1)


if __name__ == '__main__':
    unittest.main()
