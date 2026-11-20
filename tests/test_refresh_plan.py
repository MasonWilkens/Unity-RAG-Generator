import unittest
from unittest.mock import patch
from pathlib import Path
from unity_rag.workspace import refresh_plan
from unity_rag import workspace


def row(path, kind='documentation', status='supported', sha='old'):
    return {'path': path, 'kind': kind, 'status': status, 'sha256': sha}


def inventory(entries):
    return {'entries': entries, 'git': {'available': False}, 'config': {'include': ['Assets']},
            'unity_version': '1', 'packages': [], 'package_lock': None, 'assemblies': []}


class RefreshPlanTests(unittest.TestCase):
    def test_modified_document_and_incompatible_pipeline(self):
        before, after = inventory([row('Assets/A.md')]), inventory([row('Assets/A.md', sha='new')])
        self.assertEqual(refresh_plan(before, after)['mode'], 'documentation')
        self.assertEqual(refresh_plan(before, after, pipeline_compatible=False)['mode'], 'full')
        self.assertEqual(refresh_plan(before, after, references_changed=True)['mode'], 'full')

    def test_document_and_script_changes_require_safe_metadata_boundaries(self):
        before = inventory([row('Assets/A.md')])
        self.assertEqual(refresh_plan(before, inventory([]))['mode'], 'documentation')
        self.assertEqual(refresh_plan(before, inventory([row('Assets/A.md'), row('Assets/B.md')]))['mode'], 'documentation')
        self.assertEqual(refresh_plan(before, inventory([row('Assets/B.md')]))['mode'], 'documentation')
        before_with_meta = inventory([row('Assets/A.md'), row('Assets/A.md.meta', 'metadata')])
        self.assertEqual(refresh_plan(before_with_meta, inventory([row('Assets/A.md.meta', 'metadata')]))['mode'], 'full')
        self.assertEqual(refresh_plan(inventory([row('Assets/A.cs', 'script')]),
                                      inventory([row('Assets/A.cs', 'script', sha='new')]))['mode'], 'script')
        script_with_meta = inventory([row('Assets/A.cs','script'),row('Assets/A.cs.meta','metadata')])
        self.assertEqual(refresh_plan(script_with_meta, inventory([row('Assets/A.cs.meta','metadata')]))['mode'], 'full')
        self.assertEqual(refresh_plan(inventory([row('Assets/A.cs','script')]),
                                      inventory([row('Assets/A.cs','script',sha='new')]), references_changed=True)['mode'], 'full')
        before = inventory([row('Assets/A.cs','script'),row('Assets/Thing.asset','asset')])
        after = inventory([row('Assets/A.cs','script',sha='new'),row('Assets/Thing.asset','asset',sha='new')])
        self.assertEqual(refresh_plan(before, after)['mode'], 'full')
        after = inventory([row('Assets/A.cs','script',sha='new'),row('Assets/Thing.asset','asset')])
        after['assemblies'] = [{'path':'Assets/New.asmdef'}]
        self.assertEqual(refresh_plan(before, after)['mode'], 'full')

    def test_threshold_counts_indexed_files_but_not_excluded_secrets(self):
        before = inventory([row(f'Assets/{i}.md') for i in range(50)] + [row('Assets/creds.secret', status='excluded')])
        after = inventory([row(f'Assets/{i}.md', sha='new') for i in range(50)] + [row('Assets/creds.secret', status='excluded', sha='new')])
        plan = refresh_plan(before, after)
        self.assertEqual(plan['indexed_changed_files'], 50)
        self.assertTrue(plan['refresh_pending'])
        self.assertFalse(plan['automatic_refresh'])
        self.assertFalse(refresh_plan(before, after, threshold=51)['refresh_pending'])
        with self.assertRaises(ValueError):
            refresh_plan(before, after, threshold=0)

    def test_serialized_asset_scalar_plan_requires_only_supported_modifications(self):
        before=inventory([row('Assets/A.asset','serialized_asset'),row('Assets/A.asset.meta','metadata')])
        after=inventory([row('Assets/A.asset','serialized_asset',sha='new'),row('Assets/A.asset.meta','metadata')])
        self.assertEqual(refresh_plan(before,after)['mode'],'asset_scalar')
        self.assertEqual(refresh_plan(before,after,references_changed=True)['mode'],'full')
        changed_meta=inventory([row('Assets/A.asset','serialized_asset',sha='new'),row('Assets/A.asset.meta','metadata',sha='new')])
        self.assertEqual(refresh_plan(before,changed_meta)['mode'],'full')
        self.assertEqual(refresh_plan(before,inventory([row('Assets/A.asset.meta','metadata')]))['mode'],'full')
        self.assertEqual(refresh_plan(before,inventory([]))['mode'],'asset_delete')
        self.assertEqual(refresh_plan(before,inventory(before['entries']+[row('Assets/B.asset','serialized_asset')]))['mode'],'full')
        self.assertEqual(refresh_plan(before,inventory(before['entries']+[row('Assets/B.asset','serialized_asset'),row('Assets/B.asset.meta','metadata')]))['mode'],'asset_add')
        self.assertEqual(refresh_plan(before,inventory([row('Assets/B.asset','serialized_asset'),row('Assets/A.asset.meta','metadata')]))['mode'],'full')
        with_secret=inventory(before['entries']+[row('Assets/creds.secret',status='excluded')])
        changed_secret=inventory(after['entries']+[row('Assets/creds.secret',status='excluded',sha='new')])
        self.assertEqual(refresh_plan(with_secret,changed_secret)['mode'],'full')

    def test_rename_counts_once(self):
        plan = refresh_plan(inventory([row('Assets/old.md')]), inventory([row('Assets/new.md')]), threshold=1)
        self.assertEqual(plan['indexed_changed_files'], 1)
        self.assertTrue(plan['refresh_pending'])

    def test_invalid_status_threshold_fails_before_accessing_workspace(self):
        with self.assertRaisesRegex(ValueError, 'positive integer'):
            workspace.workspace_status('missing-workspace', threshold=0)

    def test_pipeline_hash_excludes_unrelated_tools_but_rejects_incompatible_legacy(self):
        expected = workspace._pipeline_hash()
        self.assertTrue(workspace._pipeline_compatible(expected))
        # LFS-aware discovery changes what constitutes indexable source.
        # Old snapshots remain readable but must be fully refreshed before reuse.
        self.assertFalse(workspace._pipeline_compatible('40f2e992072d6e28a7e7612ad50d289799e065116a217ff726bef44e1f2186fa'))
        self.assertFalse(workspace._pipeline_compatible('9b74e770ddc2ee0729ecbf13e8bb9e81c12319487e8601883062879f51549ecf'))
        self.assertFalse(workspace._pipeline_compatible('different-parser-version'))
        with patch.object(workspace, '_pipeline_hash', return_value='different-index-code'):
            self.assertFalse(workspace._pipeline_compatible('40f2e992072d6e28a7e7612ad50d289799e065116a217ff726bef44e1f2186fa'))

    def test_query_only_changes_preserve_fingerprint_but_builder_changes_do_not(self):
        baseline = workspace._pipeline_hash()
        original = Path.read_bytes
        def changed_query(path):
            data = original(path)
            return data + b'\n# query-only change\n' if path.name == 'retrieval.py' else data
        with patch.object(Path, 'read_bytes', changed_query):
            self.assertEqual(workspace._pipeline_hash(), baseline)
        def changed_builder(path):
            data = original(path)
            return data.replace(b'def build_retrieval(', b'def changed_builder(', 1) if path.name == 'retrieval.py' else data
        with patch.object(Path, 'read_bytes', changed_builder):
            self.assertNotEqual(workspace._pipeline_hash(), baseline)


if __name__ == '__main__':
    unittest.main()
