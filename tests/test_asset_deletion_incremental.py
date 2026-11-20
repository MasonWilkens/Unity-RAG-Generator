"""An isolated source-asset deletion may only update the staged index copy."""
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from unity_rag.asset_map import asset_report, map_assets
from unity_rag.code_map import code_report
from unity_rag.workspace import refresh, resolve_workspace, workspace_status


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class AssetDeletionReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=self.root/'Project';self.workspace=self.root/'Index'
        shutil.copytree(Path(__file__).parent/'fixtures'/'retrieval_project',self.project)
        self.asset=self.project/'Assets'/'Independent.asset';self.meta=Path(str(self.asset)+'.meta')
        self.guid='1'*32
        self.asset.write_text('%YAML 1.1\n--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: Independent\n'
                              '  m_Script: {fileID: 11500000, guid: '+('a'*32)+', type: 3}\n'
                              '  armor: {fileID: 11400000, guid: '+('c'*32)+', type: 2}\n',encoding='utf-8')
        self.meta.write_text('fileFormatVersion: 2\nguid: '+self.guid+'\n',encoding='utf-8')
        refresh(self.workspace,self.project,semantic=False)

    def snapshot(self):
        code,assets=code_report(self.workspace),asset_report(self.workspace)
        for data in (code,assets):
            for key in ('discovery_scanned_at','code_fingerprint'):
                data.pop(key,None)
        with sqlite3.connect(resolve_workspace(self.workspace)/'inventory.sqlite3') as db:
            tables={n:db.execute(f'SELECT * FROM {n} ORDER BY rowid').fetchall()
                    for n in ('unity_assets','unity_objects','unity_edges','evidence_relations')}
            tables['evidence']=db.execute('SELECT * FROM evidence ORDER BY id').fetchall()
        return code,assets,tables

    def assert_clean_equivalent(self):
        before=self.snapshot();refresh(self.workspace,semantic=False,rebuild=True)
        self.assertEqual(before,self.snapshot())

    def test_unreferenced_asset_with_outgoing_edges_is_removed_from_staged_map(self):
        old=resolve_workspace(self.workspace)
        before=asset_report(self.workspace)
        self.assertTrue(any(e['source']=='asset:Assets/Independent.asset#11400000' for e in before['edges']))
        self.asset.unlink();self.meta.unlink()
        self.assertEqual(workspace_status(self.workspace)['refresh_plan']['mode'],'asset_delete')
        with patch('unity_rag.asset_map.map_assets',side_effect=AssertionError('asset map rebuilt')), \
             patch('unity_rag.code_map.map_code',side_effect=AssertionError('code map rebuilt')):
            result=refresh(self.workspace,semantic=False)
        self.assertEqual(result['refresh_mode'],'asset_delete')
        self.assertTrue((old/'complete.json').is_file())
        report=asset_report(self.workspace)
        self.assertFalse(any(a['path']=='Assets/Independent.asset' for a in report['assets']))
        self.assertFalse(any(e['source'].startswith('asset:Assets/Independent.asset#') for e in report['edges']))
        with sqlite3.connect(resolve_workspace(self.workspace)/'inventory.sqlite3') as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM evidence WHERE entity LIKE 'asset:Assets/Independent.asset%'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM evidence_relations WHERE source LIKE 'asset:Assets/Independent.asset%'").fetchone()[0],0)
        self.assert_clean_equivalent()

    def test_remaining_reference_to_deleted_guid_forces_full_mapping(self):
        holder=self.project/'Assets'/'Holder.asset'
        holder.write_text('%YAML 1.1\n--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: Holder\n'
                          '  link: {fileID: 999, guid: '+self.guid+', type: 2}\n',encoding='utf-8')
        Path(str(holder)+'.meta').write_text('guid: '+('2'*32)+'\n',encoding='utf-8')
        refresh(self.workspace,semantic=False,rebuild=True)
        before=asset_report(self.workspace)
        self.assertTrue(any(e.get('reference',{}).get('guid')==self.guid and e['resolution']=='missing_file_id' for e in before['edges']))
        self.asset.unlink();self.meta.unlink()
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            result=refresh(self.workspace,semantic=False)
        self.assertEqual(result['refresh_mode'],'full');self.assertEqual(mapper.call_count,1)
        self.assertTrue(any(e.get('reference',{}).get('guid')==self.guid and e['resolution']=='external_or_missing_guid'
                            for e in asset_report(self.workspace)['edges']))
        self.assert_clean_equivalent()

    def test_duplicate_guid_and_missing_metadata_force_full_mapping(self):
        duplicate=self.project/'Assets'/'Duplicate.asset'
        duplicate.write_text(self.asset.read_text(encoding='utf-8'),encoding='utf-8')
        Path(str(duplicate)+'.meta').write_text('guid: '+self.guid+'\n',encoding='utf-8')
        refresh(self.workspace,semantic=False,rebuild=True)
        self.asset.unlink();self.meta.unlink()
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()

    def test_resolved_incoming_reference_to_deleted_asset_is_rebuilt(self):
        holder=self.project/'Assets'/'Holder.asset'
        holder.write_text('%YAML 1.1\n--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: Holder\n'
                          '  link: {fileID: 11400000, guid: '+self.guid+', type: 2}\n',encoding='utf-8')
        Path(str(holder)+'.meta').write_text('guid: '+('2'*32)+'\n',encoding='utf-8')
        refresh(self.workspace,semantic=False,rebuild=True)
        self.assertTrue(any(e.get('target_asset')=='asset:Assets/Independent.asset' and e['resolution']=='resolved_object'
                            for e in asset_report(self.workspace)['edges']))
        self.asset.unlink();self.meta.unlink()
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1)
        self.assertTrue(any(e.get('reference',{}).get('guid')==self.guid and e['resolution']=='external_or_missing_guid'
                            for e in asset_report(self.workspace)['edges']))
        self.assert_clean_equivalent()

    def test_missing_toolchain_provenance_requires_full_mapping(self):
        from unity_rag import workspace as workspace_module
        self.asset.unlink();self.meta.unlink()
        with patch.object(workspace_module,'_check_cached_code_toolchain',side_effect=ValueError('missing provenance')), \
             patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()

    def test_missing_copied_object_row_falls_back(self):
        from unity_rag import workspace as workspace_module
        original=workspace_module._backup
        self.asset.unlink();self.meta.unlink()
        def corrupt(source,destination):
            original(source,destination)
            if Path(destination).name=='reused-inventory.sqlite3':
                with sqlite3.connect(destination) as db:
                    db.execute('DELETE FROM unity_objects WHERE asset=?',('asset:Assets/Independent.asset',))
        with patch.object(workspace_module,'_backup',side_effect=corrupt), \
             patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()

    def test_metadata_only_or_extra_source_changes_do_not_plan_incremental_delete(self):
        self.meta.unlink()
        self.assertEqual(workspace_status(self.workspace)['refresh_plan']['mode'],'full')

    def test_semantic_vectors_for_deleted_evidence_are_removed(self):
        from tests.test_semantic import FixtureProvider
        provider=FixtureProvider()
        refresh(self.workspace,semantic=True,provider=provider)
        before=resolve_workspace(self.workspace)
        with sqlite3.connect(before/'inventory.sqlite3') as db:
            removed={r[0] for r in db.execute("SELECT id FROM evidence WHERE entity LIKE 'asset:Assets/Independent.asset%'")}
        self.assertTrue(removed)
        self.asset.unlink();self.meta.unlink()
        result=refresh(self.workspace,semantic=True,provider=provider)
        self.assertEqual(result['refresh_mode'],'asset_delete')
        self.assertGreater(result['semantic']['cache_hits'],0)
        after=resolve_workspace(self.workspace)
        with sqlite3.connect(after/'semantic.sqlite3') as db:
            active={r[0] for r in db.execute('SELECT chunk FROM vectors')}
        self.assertTrue(removed.isdisjoint(active))
