"""Isolated script-or-null asset additions equal clean maps or rebuild fully."""
from contextlib import closing
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
class AssetAdditionReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=self.root/'Project';self.workspace=self.root/'Index'
        shutil.copytree(Path(__file__).parent/'fixtures'/'retrieval_project',self.project)
        script=self.project/'Assets'/'Enemy.cs'
        text=script.read_text(encoding='utf-8').replace('public class Enemy : CombatBase','public class Enemy : UnityEngine.ScriptableObject')
        script.write_text('namespace UnityEngine { public class ScriptableObject {} }\n'+text,encoding='utf-8')
        self.asset=self.project/'Assets'/'Added.asset';self.meta=Path(str(self.asset)+'.meta')
        self.guid='2'*32;self.script_guid='a'*32
        self.text=('%YAML 1.1\n--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: Added\n'
                   '  m_GameObject: {fileID: 0}\n  m_EditorClassIdentifier: \n'
                   '  m_Script: {fileID: 11500000, guid: '+self.script_guid+', type: 3}\n  value: 7\n')
        refresh(self.workspace,self.project,semantic=False)

    def add(self,text=None,guid=None):
        self.asset.write_text(self.text if text is None else text,encoding='utf-8')
        self.meta.write_text('fileFormatVersion: 2\nguid: '+(self.guid if guid is None else guid)+'\n',encoding='utf-8')

    def snapshot(self):
        code,assets=code_report(self.workspace),asset_report(self.workspace)
        for data in (code,assets):
            for key in ('discovery_scanned_at','code_fingerprint'):
                data.pop(key,None)
        with closing(sqlite3.connect(resolve_workspace(self.workspace)/'inventory.sqlite3')) as db, db:
            # SQLite rows are unordered; compare every raw row as a multiset.
            tables={n:sorted(db.execute(f'SELECT * FROM {n}').fetchall(),key=repr)
                    for n in ('unity_assets','unity_objects','unity_edges')}
            tables['relations']=db.execute('SELECT * FROM evidence_relations ORDER BY rowid').fetchall()
            tables['evidence']=db.execute('SELECT * FROM evidence ORDER BY id').fetchall()
            tables['aliases']=db.execute('SELECT * FROM evidence_aliases ORDER BY alias,entity').fetchall()
        return code,assets,tables

    def assert_clean_equivalent(self):
        incremental=self.snapshot();refresh(self.workspace,semantic=False,rebuild=True)
        self.assertEqual(incremental,self.snapshot())

    def test_script_link_and_null_links_match_clean_maps_and_evidence(self):
        self.add();old=resolve_workspace(self.workspace)
        self.assertEqual(workspace_status(self.workspace)['refresh_plan']['mode'],'asset_add')
        with patch('unity_rag.asset_map.map_assets',side_effect=AssertionError('asset map rebuilt')), \
             patch('unity_rag.code_map.map_code',side_effect=AssertionError('code map rebuilt')):
            result=refresh(self.workspace,semantic=False)
        self.assertEqual(result['refresh_mode'],'asset_add');self.assertTrue((old/'complete.json').is_file())
        report=asset_report(self.workspace)
        added=next(o for o in report['objects'] if o['path']=='Assets/Added.asset')
        self.assertEqual(added['role'],'scriptable_object_candidate')
        self.assertEqual(added['script']['resolution'],'filename_candidate')
        self.assertTrue(any(e['source']==added['id'] and e['kind']=='script_type' for e in report['edges']))
        self.assert_clean_equivalent()

    def test_existing_unresolved_reference_becomes_resolved_by_full_fallback(self):
        waiting=self.project/'Assets'/'Waiting.asset'
        waiting.write_text('%YAML 1.1\n--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: Waiting\n'
                           '  link: {fileID: 11400000, guid: '+self.guid+', type: 2}\n',encoding='utf-8')
        Path(str(waiting)+'.meta').write_text('guid: '+('3'*32)+'\n',encoding='utf-8')
        refresh(self.workspace,semantic=False,rebuild=True)
        self.assertTrue(any(e.get('reference',{}).get('guid')==self.guid and e['resolution']=='external_or_missing_guid'
                            for e in asset_report(self.workspace)['edges']))
        self.add()
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1)
        self.assertTrue(any(e.get('reference',{}).get('guid')==self.guid and e['resolution']=='resolved_object'
                            and e.get('target_asset')=='asset:Assets/Added.asset' for e in asset_report(self.workspace)['edges']))
        self.assert_clean_equivalent()

    def test_explicit_script_identifier_and_multiple_objects_match_clean(self):
        explicit=self.text.replace('m_EditorClassIdentifier: \n','m_EditorClassIdentifier: Assembly-CSharp::Game.Enemy\n')
        second=explicit+'--- !u!114 &11400001\nMonoBehaviour:\n  m_Name: Second\n  m_GameObject: {fileID: 0}\n  m_Script: {fileID: 0}\n  value: 3\n'
        self.add(second)
        with patch('unity_rag.asset_map.map_assets',side_effect=AssertionError('asset map rebuilt')):
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'asset_add')
        report=asset_report(self.workspace)
        added=[o for o in report['objects'] if o['path']=='Assets/Added.asset']
        self.assertEqual(len(added),2)
        self.assertEqual(added[0]['script']['resolution'],'serialized_type_identifier')
        self.assertTrue(any(e['source']==added[0]['id'] and e['resolution']=='explicit_identifier' for e in report['edges']))
        self.assert_clean_equivalent()

    def test_duplicate_guid_and_unresolved_or_other_nonnull_links_fall_back(self):
        cases=[('duplicate',self.text,'c'*32),
               ('invalid-metadata',self.text,'not-a-guid'),
               ('zero-guid',self.text,'0'*32),
               ('missing-script',self.text.replace(self.script_guid,'f'*32),self.guid),
               ('unresolved-ordinary-link',self.text+'  other: {fileID: 11400000, guid: '+('f'*32)+', type: 2}\n',self.guid),
               ('unresolved-local-link',self.text+'  other: {fileID: 999999}\n',self.guid),
               ('invalid-fileid',self.text+'  other: {fileID: nope, guid: '+('c'*32)+', type: 2}\n',self.guid),
               ('special-component',self.text+'  m_Component: {fileID: 11400000, guid: '+('c'*32)+', type: 2}\n',self.guid),
               ('special-link',self.text.replace('m_GameObject: {fileID: 0}',
                    'm_GameObject: {fileID: 11400000, guid: '+('c'*32)+', type: 2}'),self.guid)]
        for label,text,guid in cases:
            with self.subTest(label=label):
                self.add(text,guid)
                with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
                    self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
                self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()
                self.asset.unlink();self.meta.unlink();refresh(self.workspace,semantic=False,rebuild=True)

    def test_semantic_cache_keeps_old_vectors_and_embeds_new_evidence(self):
        from tests.test_semantic import FixtureProvider
        provider=FixtureProvider();refresh(self.workspace,semantic=True,provider=provider)
        self.add(self.text+'  other: {fileID: 11400000, guid: '+('c'*32)+', type: 2}\n')
        result=refresh(self.workspace,semantic=True,provider=provider)
        self.assertEqual(result['refresh_mode'],'asset_add')
        self.assertGreater(result['semantic']['cache_hits'],0);self.assertGreater(result['semantic']['embedded'],0)
        active=resolve_workspace(self.workspace)
        with closing(sqlite3.connect(active/'inventory.sqlite3')) as db, db:
            evidence={r[0] for r in db.execute('SELECT id FROM evidence')}
            added={r[0] for r in db.execute("SELECT id FROM evidence WHERE entity LIKE 'asset:Assets/Added.asset%'")}
        with closing(sqlite3.connect(active/'semantic.sqlite3')) as db, db:
            vectors={r[0] for r in db.execute('SELECT chunk FROM vectors')}
        self.assertTrue(added & vectors);self.assertTrue(vectors <= evidence)

    def test_ambiguous_or_unresolved_script_type_uses_full_fallback(self):
        script=self.project/'Assets'/'Enemy.cs';original=script.read_text(encoding='utf-8')
        for label,contents in [('ambiguous',original+'\nnamespace Other { public class Enemy {} }\n'),
                               ('unresolved',original.replace('UnityEngine.ScriptableObject','MissingBase'))]:
            with self.subTest(label=label):
                script.write_text(contents,encoding='utf-8');refresh(self.workspace,semantic=False,rebuild=True)
                self.add()
                with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
                    self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
                self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()
                self.asset.unlink();self.meta.unlink()
        script.write_text(original,encoding='utf-8')

    def test_script_guid_in_excluded_secret_directory_is_not_read_or_indexed(self):
        hidden=self.project/'Assets'/'.env';hidden.mkdir()
        secret=hidden/'api.secret';secret.write_text('SYNTHETIC_PRIVATE_CANARY',encoding='utf-8')
        Path(str(secret)+'.meta').write_text('guid: '+('f'*32)+'\n',encoding='utf-8')
        self.add(self.text.replace(self.script_guid,'f'*32))
        original_open=Path.open
        def guarded(path,*args,**kwargs):
            if '.env' in path.parts: raise AssertionError('excluded secret path was opened')
            return original_open(path,*args,**kwargs)
        with patch.object(Path,'open',guarded):
            result=refresh(self.workspace,semantic=False)
        self.assertEqual(result['refresh_mode'],'full')
        report=str(asset_report(self.workspace))
        self.assertNotIn('SYNTHETIC_PRIVATE_CANARY',report);self.assertNotIn('api.secret',report)

    def test_missing_toolchain_provenance_and_object_limit_fall_back(self):
        from unity_rag import workspace as workspace_module
        self.add()
        with patch.object(workspace_module,'_check_cached_code_toolchain',side_effect=ValueError('missing provenance')), \
             patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()
        self.asset.unlink();self.meta.unlink();refresh(self.workspace,semantic=False,rebuild=True)
        many=''.join(f'--- !u!114 &{i+1}\nMonoBehaviour:\n  m_Name: Item{i}\n' for i in range(129))
        self.add(many)
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()

    def test_nonnull_generic_links_to_existing_and_local_objects_match_clean(self):
        text=(self.text+'  other: {fileID: 11400000, guid: '+('c'*32)+', type: 2}\n'
              '  local: {fileID: 11400001}\n'
              '  zeroGuidLocal: {fileID: 11400001, guid: '+('0'*32)+', type: 2}\n'
              '  explicitSelf: {fileID: 11400000, guid: '+self.guid+', type: 2}\n'
              '--- !u!114 &11400001\nMonoBehaviour:\n  m_Name: LocalTarget\n  value: 5\n')
        self.add(text)
        with patch('unity_rag.asset_map.map_assets',side_effect=AssertionError('asset map rebuilt')), \
             patch('unity_rag.code_map.map_code',side_effect=AssertionError('code map rebuilt')):
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'asset_add')
        edges=[e for e in asset_report(self.workspace)['edges'] if e['source']=='asset:Assets/Added.asset#11400000']
        targets={e['property']:e['target'] for e in edges if e['property'] in ('other','local','zeroGuidLocal','explicitSelf')}
        self.assertEqual(targets,{'other':'asset:Assets/Armor.asset#11400000',
                                  'local':'asset:Assets/Added.asset#11400001',
                                  'zeroGuidLocal':'asset:Assets/Added.asset#11400001',
                                  'explicitSelf':'asset:Assets/Added.asset#11400000'})
        self.assert_clean_equivalent()

    def test_ambiguous_generic_guid_and_metadata_only_target_fall_back(self):
        duplicate=self.project/'Assets'/'Duplicate.asset'
        duplicate.write_text('%YAML 1.1\n--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: Duplicate\n',encoding='utf-8')
        Path(str(duplicate)+'.meta').write_text('guid: '+('c'*32)+'\n',encoding='utf-8')
        refresh(self.workspace,semantic=False,rebuild=True)
        generic=self.text+'  other: {fileID: 11400000, guid: '+('c'*32)+', type: 2}\n'
        self.add(generic)
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()
        self.asset.unlink();self.meta.unlink();refresh(self.workspace,semantic=False,rebuild=True)
        generic=self.text+'  other: {fileID: 11500000, guid: '+self.script_guid+', type: 3}\n'
        self.add(generic)
        with patch('unity_rag.asset_map.map_assets',wraps=map_assets) as mapper:
            self.assertEqual(refresh(self.workspace,semantic=False)['refresh_mode'],'full')
        self.assertEqual(mapper.call_count,1);self.assert_clean_equivalent()

    def test_interrupted_addition_preserves_previous_generation(self):
        old=resolve_workspace(self.workspace);self.add()
        with patch('unity_rag.workspace._reuse_maps_for_isolated_asset_addition',side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                refresh(self.workspace,semantic=False)
        self.assertEqual(resolve_workspace(self.workspace),old)

    def test_asset_or_metadata_alone_is_not_incremental_addition(self):
        self.asset.write_text(self.text,encoding='utf-8')
        self.assertEqual(workspace_status(self.workspace)['refresh_plan']['mode'],'full')
