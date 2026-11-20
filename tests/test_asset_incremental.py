"""Scalar asset reuse must equal full mapping and fail closed on graph changes."""
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
class ScalarAssetReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=self.root/'Project';self.workspace=self.root/'Index'
        shutil.copytree(Path(__file__).parent/'fixtures'/'retrieval_project',self.project)
        self.asset=self.project/'Assets'/'Armor.asset';self.meta=Path(str(self.asset)+'.meta')
        self.original=self.asset.read_text(encoding='utf-8');self.original_meta=self.meta.read_text(encoding='utf-8')
        refresh(self.workspace,self.project,semantic=False)

    def snapshot(self):
        code=code_report(self.workspace);assets=asset_report(self.workspace)
        for data in (code,assets):
            for key in ('discovery_scanned_at','code_fingerprint'):
                data.pop(key,None)
        with sqlite3.connect(resolve_workspace(self.workspace)/'inventory.sqlite3') as db:
            tables={name:db.execute(f'SELECT * FROM {name} ORDER BY rowid').fetchall()
                    for name in ('unity_assets','unity_objects','unity_edges')}
            tables['evidence']=db.execute('SELECT * FROM evidence ORDER BY id').fetchall()
        return code,assets,tables

    def assert_clean_equivalent(self):
        incremental=self.snapshot();refresh(self.workspace,semantic=False,rebuild=True)
        self.assertEqual(incremental,self.snapshot())

    def test_scalar_and_name_edits_preserve_incoming_guid_links_and_match_clean(self):
        for old,new in (('armorResistance: 0.5','armorResistance: 0.7'),('ArmorSettings','ArmorAdjusted')):
            self.asset.write_text(self.asset.read_text(encoding='utf-8').replace(old,new),encoding='utf-8')
            self.assertEqual(workspace_status(self.workspace)['refresh_plan']['mode'],'asset_scalar')
            with patch('unity_rag.asset_map.map_assets',side_effect=AssertionError('asset map rebuilt')), \
                 patch('unity_rag.code_map.map_code',side_effect=AssertionError('code map rebuilt')):
                result=refresh(self.workspace,semantic=False)
            self.assertEqual(result['refresh_mode'],'asset_scalar')
            report=asset_report(self.workspace)
            self.assertTrue(any(e.get('target_asset')=='asset:Assets/Armor.asset' and e['resolution']=='resolved_object'
                                for e in report['edges']))
            self.assert_clean_equivalent()

    def test_reference_location_identity_parse_and_metadata_changes_fall_back(self):
        scenarios={
            'reference': self.original.replace('armorResistance: 0.5','armorResistance: {fileID: 9}'),
            'location': self.original+'  additionalValue: 4\n',
            'identity': self.original.replace('&11400000','&11400001'),
            'malformed': self.original.replace('armorResistance: 0.5','armorResistance: [broken'),
            'metadata': self.original.replace('armorResistance: 0.5','armorResistance: 0.8'),
        }
        for label,text in scenarios.items():
            with self.subTest(label=label):
                self.asset.write_text(text,encoding='utf-8')
                if label=='metadata':
                    self.meta.write_text(self.original_meta.replace('c'*32,'d'*32),encoding='utf-8')
                with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
                    result=refresh(self.workspace,semantic=False)
                self.assertEqual(result['refresh_mode'],'full')
                self.assertEqual(mapper.call_count,1)
                self.assert_clean_equivalent()
                self.asset.write_text(self.original,encoding='utf-8');self.meta.write_text(self.original_meta,encoding='utf-8')
                refresh(self.workspace,semantic=False,rebuild=True)

    def test_prefab_override_target_forces_full_mapping(self):
        path=self.project/'Assets'/'StrongGoblin.prefab'
        text=path.read_text(encoding='utf-8').replace('b'*32,'c'*32).replace('fileID: 3, guid:', 'fileID: 11400000, guid:').replace('propertyPath: damage','propertyPath: armorResistance')
        path.write_text(text,encoding='utf-8');refresh(self.workspace,semantic=False,rebuild=True)
        before=asset_report(self.workspace)
        self.assertTrue(any(o.get('target')=='asset:Assets/Armor.asset#11400000' and o.get('base_value')=='0.5'
                            for o in before['overrides']))
        self.asset.write_text(self.original.replace('armorResistance: 0.5','armorResistance: 0.7'),encoding='utf-8')
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            result=refresh(self.workspace,semantic=False)
        self.assertEqual(result['refresh_mode'],'full');self.assertEqual(mapper.call_count,1)
        self.assertTrue(any(o.get('base_value')=='0.7' for o in asset_report(self.workspace)['overrides']))
        self.assert_clean_equivalent()

    def test_reference_key_order_change_rebuilds_unresolved_evidence(self):
        guid='f'*32
        original=self.original+f'  testRef: {{fileID: 7, guid: {guid}, type: 2}}\n'
        self.asset.write_text(original,encoding='utf-8');refresh(self.workspace,semantic=False,rebuild=True)
        changed=original.replace(f'fileID: 7, guid: {guid}',f'guid: {guid}, fileID: 7')
        self.asset.write_text(changed,encoding='utf-8')
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            result=refresh(self.workspace,semantic=False)
        self.assertEqual(result['refresh_mode'],'full');self.assertEqual(mapper.call_count,1)
        self.assert_clean_equivalent()

    def test_duplicate_guid_and_asset_deletion_force_full_mapping(self):
        duplicate=self.project/'Assets'/'Duplicate.asset'
        duplicate.write_text(self.original,encoding='utf-8')
        Path(str(duplicate)+'.meta').write_text(self.original_meta,encoding='utf-8')
        refresh(self.workspace,semantic=False,rebuild=True)
        self.asset.write_text(self.original.replace('armorResistance: 0.5','armorResistance: 0.7'),encoding='utf-8')
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()
        self.asset.unlink()
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()

    def test_mutation_during_scalar_parse_does_not_publish(self):
        import unity_rag.unity_yaml as unity_yaml
        self.asset.write_text(self.original.replace('armorResistance: 0.5','armorResistance: 0.7'),encoding='utf-8')
        original=unity_yaml.parse_unity;pinned=resolve_workspace(self.workspace)
        def mutate(text,path):
            parsed=original(text,path)
            self.asset.write_text(text.replace('armorResistance: 0.7','armorResistance: 0.9'),encoding='utf-8')
            return parsed
        with patch.object(unity_yaml,'parse_unity',side_effect=mutate):
            with self.assertRaisesRegex(ValueError,'changed (during refresh|since discovery)'):
                refresh(self.workspace,semantic=False)
        self.assertEqual(resolve_workspace(self.workspace),pinned)

    def test_missing_or_changed_code_toolchain_forces_full_mapping(self):
        from unity_rag import code_map, workspace as workspace_module
        original_metadata=workspace_module._metadata;original_run=code_map._run
        for label in ('missing','changed'):
            self.asset.write_text(self.asset.read_text(encoding='utf-8').replace(
                'armorResistance: 0.5' if label=='missing' else 'armorResistance: 0.7',
                'armorResistance: 0.7' if label=='missing' else 'armorResistance: 0.8'),encoding='utf-8')
            def metadata(work,key):
                value=original_metadata(work,key)
                if label=='missing' and key=='code_report': value['environment_fingerprint']=None
                return value
            def run(command,**kwargs):
                value=original_run(command,**kwargs)
                if label=='changed' and 'input' in kwargs and not json.loads(kwargs['input'])['files']:
                    response=json.loads(value);response['environment_fingerprint']='0'*64;return json.dumps(response)
                return value
            with patch.object(workspace_module,'_metadata',side_effect=metadata), \
                 patch.object(code_map,'_run',side_effect=run), \
                 patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
                result=refresh(self.workspace,semantic=False)
            self.assertEqual(result['refresh_mode'],'full');self.assertEqual(mapper.call_count,1)
            self.assert_clean_equivalent()
