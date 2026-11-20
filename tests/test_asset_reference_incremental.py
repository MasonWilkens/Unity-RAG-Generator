"""Reference updates preserve resolved graph and evidence or rebuild fully."""
from contextlib import closing
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
class AssetReferenceReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=self.root/'Project';self.workspace=self.root/'Index'
        shutil.copytree(Path(__file__).parent/'fixtures'/'retrieval_project',self.project)
        self.asset=self.project/'Assets'/'Armor.asset';self.d='1'*32;self.e='2'*32
        for name,guid in (('Shield',self.d),('Backup',self.e)):
            target=self.project/'Assets'/f'{name}.asset'
            target.write_text(f'%YAML 1.1\n--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: {name}\n  value: 10\n',encoding='utf-8')
            Path(str(target)+'.meta').write_text(f'fileFormatVersion: 2\nguid: {guid}\n',encoding='utf-8')
        self.initial=self.asset.read_text(encoding='utf-8')+f'  linkedConfig: {{fileID: 11400000, guid: {self.d}, type: 2}}\n'
        self.asset.write_text(self.initial,encoding='utf-8');refresh(self.workspace,self.project,semantic=False)

    def snapshot(self):
        code=code_report(self.workspace);assets=asset_report(self.workspace)
        for data in (code,assets):
            for key in ('discovery_scanned_at','code_fingerprint'):
                data.pop(key,None)
        with closing(sqlite3.connect(resolve_workspace(self.workspace)/'inventory.sqlite3')) as db, db:
            tables={name:db.execute(f'SELECT * FROM {name} ORDER BY rowid').fetchall()
                    for name in ('unity_assets','unity_objects','unity_edges','evidence_relations')}
            tables['evidence']=db.execute('SELECT * FROM evidence ORDER BY id').fetchall()
            tables['aliases']=db.execute('SELECT * FROM evidence_aliases ORDER BY alias,entity').fetchall()
        return code,assets,tables

    def assert_clean_equivalent(self):
        incremental=self.snapshot();refresh(self.workspace,semantic=False,rebuild=True)
        self.assertEqual(incremental,self.snapshot())

    def test_retarget_null_restore_and_reference_key_order_match_clean(self):
        source='asset:Assets/Armor.asset#11400000'
        cases=[(self.d,self.e,'asset:Assets/Backup.asset'),
               ('fileID: 11400000','fileID: 0',None),
               ('fileID: 0','fileID: 11400000','asset:Assets/Backup.asset'),
               (f'fileID: 11400000, guid: {self.e}',f'guid: {self.e}, fileID: 11400000','asset:Assets/Backup.asset')]
        for old,new,expected in cases:
            previous=next(e for e in asset_report(self.workspace)['edges'] if e['source']==source and e['property']=='linkedConfig')['target_asset']
            self.asset.write_text(self.asset.read_text(encoding='utf-8').replace(old,new),encoding='utf-8')
            self.assertEqual(workspace_status(self.workspace)['refresh_plan']['mode'],'asset_scalar')
            with patch('unity_rag.asset_map.map_assets',side_effect=AssertionError('asset map rebuilt')), \
                 patch('unity_rag.code_map.map_code',side_effect=AssertionError('code map rebuilt')):
                result=refresh(self.workspace,semantic=False)
            self.assertEqual(result['refresh_mode'],'asset_reference')
            edges=[e for e in asset_report(self.workspace)['edges'] if e['source']==source and e['property']=='linkedConfig']
            self.assertEqual(len(edges),1);self.assertEqual(edges[0]['target_asset'],expected)
            self.assertEqual(edges[0]['resolution'],'null' if expected is None else 'resolved_object')
            with closing(sqlite3.connect(resolve_workspace(self.workspace)/'inventory.sqlite3')) as db, db:
                relations=[json.loads(r[0]) for r in db.execute('SELECT data FROM evidence_relations WHERE source=?',(source,))]
            links=[r for r in relations if r.get('property')=='linkedConfig']
            self.assertEqual([r.get('target_asset') for r in links],[] if expected is None else [expected])
            if previous and previous != expected:
                self.assertNotIn(previous,[r.get('target_asset') for r in links])
            self.assert_clean_equivalent()

    def test_missing_ambiguous_and_missing_object_targets_fall_back(self):
        for label,old,new in [('missing',self.d,'f'*32),('fileid','fileID: 11400000, guid: '+self.d,'fileID: 11400001, guid: '+self.d)]:
            with self.subTest(label=label):
                self.asset.write_text(self.initial.replace(old,new),encoding='utf-8')
                with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
                    self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
                self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()
                self.asset.write_text(self.initial,encoding='utf-8');refresh(self.workspace,semantic=False,rebuild=True)
        duplicate=self.project/'Assets'/'Duplicate.asset'
        duplicate.write_text('%YAML 1.1\n--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: Duplicate\n',encoding='utf-8')
        Path(str(duplicate)+'.meta').write_text(f'fileFormatVersion: 2\nguid: {self.e}\n',encoding='utf-8')
        refresh(self.workspace,semantic=False,rebuild=True)
        self.asset.write_text(self.initial.replace(self.d,self.e),encoding='utf-8')
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()

    def test_script_reference_changes_still_require_full_mapping(self):
        initial=self.initial+f'  m_Script: {{fileID: 0, guid: {self.d}, type: 2}}\n'
        self.asset.write_text(initial,encoding='utf-8');refresh(self.workspace,semantic=False,rebuild=True)
        self.asset.write_text(initial.replace('m_Script: {fileID: 0','m_Script: {fileID: 11400000'),encoding='utf-8')
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()

    def test_more_than_128_reference_changes_fall_back(self):
        many=self.asset.read_text(encoding='utf-8')+''.join(
            f'  extra{i}: {{fileID: 11400000, guid: {self.d}, type: 2}}\n' for i in range(129))
        self.asset.write_text(many,encoding='utf-8');refresh(self.workspace,semantic=False,rebuild=True)
        self.asset.write_text(many.replace(self.d,self.e),encoding='utf-8')
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()

    def test_missing_copied_edge_falls_back_without_stale_relationship(self):
        from unity_rag import workspace as workspace_module
        original=workspace_module._reuse_maps_for_documents
        self.asset.write_text(self.initial.replace(self.d,self.e),encoding='utf-8')
        def corrupt(previous,destination,inventory,delta):
            original(previous,destination,inventory,delta)
            with closing(sqlite3.connect(destination/'inventory.sqlite3')) as db, db:
                rows=db.execute('SELECT rowid,data FROM unity_edges WHERE source=?',
                                ('asset:Assets/Armor.asset#11400000',)).fetchall()
                rowid=next(rowid for rowid,data in rows if json.loads(data).get('property')=='linkedConfig')
                db.execute('DELETE FROM unity_edges WHERE rowid=?',(rowid,))
        with patch.object(workspace_module,'_reuse_maps_for_documents',side_effect=corrupt), \
             patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()

    def test_reference_to_excluded_secret_does_not_read_or_index_it(self):
        hidden=self.project/'Assets'/'.env';hidden.mkdir()
        secret=hidden/'api.secret';secret.write_text('SYNTHETIC_PRIVATE_CANARY',encoding='utf-8')
        Path(str(secret)+'.meta').write_text('guid: '+('f'*32)+'\n',encoding='utf-8')
        self.asset.write_text(self.initial.replace(self.d,'f'*32),encoding='utf-8')
        original_open=Path.open
        def guarded(path,*args,**kwargs):
            if '.env' in path.parts: raise AssertionError('excluded secret path was opened')
            return original_open(path,*args,**kwargs)
        with patch.object(Path,'open',guarded):
            result=refresh(self.workspace,semantic=False)
        self.assertEqual(result['refresh_mode'],'full')
        report=json.dumps(asset_report(self.workspace))
        self.assertNotIn('SYNTHETIC_PRIVATE_CANARY',report);self.assertNotIn('api.secret',report)
        excluded=[a for a in asset_report(self.workspace)['assets'] if a['path']=='Assets/.env']
        self.assertTrue(all(a['inventory_status']=='excluded' and a['sha256'] is None for a in excluded))

    def test_reference_update_reuses_semantic_cache_and_updates_changed_passages(self):
        from tests.test_semantic import FixtureProvider
        from unity_rag.retrieval import retrieve
        provider=FixtureProvider()
        refresh(self.workspace,semantic=True,provider=provider)
        self.asset.write_text(self.initial.replace(self.d,self.e),encoding='utf-8')
        result=refresh(self.workspace,semantic=True,provider=provider)
        self.assertEqual(result['refresh_mode'],'asset_reference')
        self.assertGreater(result['semantic']['cache_hits'],0)
        self.assertGreater(result['semantic']['embedded'],0)
        found=retrieve(self.workspace,'Armor linked configuration',mode='semantic',provider=provider)
        self.assertTrue(all(s['status']=='current' for s in found['source_freshness']))
