from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from unity_rag.workspace import refresh, resolve_workspace, workspace_status, writer_lock, watch, seal_workspace, _atomic, _published_pointer
from unity_rag.discovery import scan, status
from unity_rag.code_map import code_report
from unity_rag.asset_map import asset_report
from unity_rag.retrieval import retrieve
from test_semantic import FixtureProvider


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = self.base / 'Game'
        shutil.copytree(Path(__file__).parent / 'fixtures/retrieval_project', self.project)
        self.work = self.base / 'Knowledge'
        self.provider = FixtureProvider()

    def tearDown(self):
        self.temp.cleanup()

    def build(self, **kwargs):
        return refresh(self.work, self.project, provider=self.provider, **kwargs)

    def evidence(self, workspace):
        with closing(sqlite3.connect(resolve_workspace(workspace) / 'inventory.sqlite3')) as db, db:
            return db.execute('SELECT * FROM evidence ORDER BY id').fetchall()

    def test_changes_cache_and_clean_rebuild_equivalence(self):
        self.build()
        pinned = resolve_workspace(self.work)
        initial = self.evidence(pinned)
        self.assertEqual(self.build()['state'], 'unchanged')
        self.assertTrue(workspace_status(self.work)['fresh'])
        script = next(self.project.rglob('*.cs'))
        script.write_text(script.read_text(encoding='utf-8') + '\npublic class NewMarker {}\n', encoding='utf-8')
        added = self.project / 'Assets' / 'New.cs'
        added.write_text('public class NewlyAdded {}', encoding='utf-8')
        self.assertFalse(workspace_status(self.work)['fresh'])
        result = self.build()
        self.assertIn(script.relative_to(self.project).as_posix(), result['changes']['modified'])
        self.assertIn('Assets/New.cs', result['changes']['added'])
        self.assertGreater(result['semantic']['cache_hits'], 0)
        self.assertEqual(self.evidence(pinned), initial)
        added.rename(added.with_name('Renamed.cs'))
        result = self.build()
        self.assertIn({'from':'Assets/New.cs', 'to':'Assets/Renamed.cs'}, result['changes']['renamed'])
        added.with_name('Renamed.cs').unlink()
        self.assertIn('Assets/Renamed.cs', self.build()['changes']['deleted'])
        prefab = self.project / 'Assets' / 'Goblin.prefab'
        prefab.write_text(prefab.read_text(encoding='utf-8').replace('Goblin', 'RenamedGoblin'), encoding='utf-8')
        metadata = self.project / 'Assets' / 'Enemy.cs.meta'
        metadata.unlink()
        self.build()
        clean = self.base / 'Clean'
        refresh(clean, self.project, provider=FixtureProvider())
        self.assertEqual(self.evidence(self.work), self.evidence(clean))
        for reader in (code_report, asset_report):
            left, right = reader(self.work), reader(clean)
            # Independently compiled helper binaries can differ even when their
            # semantic maps match; provenance is intentionally workspace-local.
            for key in ('discovery_scanned_at', 'code_fingerprint', 'environment_fingerprint'):
                left.pop(key, None)
                right.pop(key, None)
            self.assertEqual(left, right)
        self.assertTrue(retrieve(self.work, 'NewMarker')['results'])
        self.assertEqual([r['state'] for r in watch(self.work, provider=self.provider, iterations=1)], ['unchanged'])
        with self.assertRaises(ValueError):
            list(watch(self.work, interval=0))
        with self.assertRaisesRegex(ValueError, 'immutable'):
            scan(self.project, self.work)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            scan(self.project, pinned)

    def test_failure_concurrent_mutation_and_interrupted_recovery(self):
        self.build()
        pinned = resolve_workspace(self.work)
        with patch('unity_rag.asset_map.map_assets', side_effect=ValueError('simulated failure')):
            with self.assertRaisesRegex(ValueError, 'simulated failure'):
                self.build(rebuild=True)
        self.assertEqual(resolve_workspace(self.work), pinned)
        self.assertTrue(workspace_status(self.work)['healthy'])
        original = __import__('unity_rag.retrieval', fromlist=['build_retrieval']).build_retrieval
        def mutate(workspace, **options):
            result = original(workspace, **options)
            (self.project / 'Assets' / 'Late.cs').write_text('class Late {}', encoding='utf-8')
            return result
        with patch('unity_rag.retrieval.build_retrieval', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'changed during refresh'):
                self.build(rebuild=True)
        self.assertEqual(resolve_workspace(self.work), pinned)
        (self.work / 'refresh-state.json').write_text(json.dumps({'state':'running'}), encoding='utf-8')
        self.assertTrue(workspace_status(self.work)['interrupted'])
        self.build()
        self.assertFalse(workspace_status(self.work)['interrupted'])
        with writer_lock(self.work):
            with self.assertRaisesRegex(ValueError, 'already running'):
                self.build()
        self.assertTrue(workspace_status(self.work)['fresh'])

    def test_document_only_reuses_maps_and_matches_full_rebuild(self):
        self.build()
        old = resolve_workspace(self.work)
        doc = self.project / 'Assets' / 'Combat.md'
        doc.write_text(doc.read_text(encoding='utf-8') + '\nUniqueNewDocMarker explains testing.\n', encoding='utf-8')
        self.assertEqual(workspace_status(self.work)['refresh_plan']['mode'], 'documentation')
        with patch('unity_rag.code_map.map_code', side_effect=AssertionError('code remapped')), \
             patch('unity_rag.asset_map.map_assets', side_effect=AssertionError('assets remapped')):
            start = time.perf_counter()
            result = self.build()
            reuse_seconds = time.perf_counter() - start
        self.assertEqual(result['refresh_mode'], 'documentation')
        self.assertNotEqual(resolve_workspace(self.work), old)
        self.assertTrue(workspace_status(self.work)['fresh'])
        self.assertTrue(retrieve(self.work, 'UniqueNewDocMarker')['results'])
        reused_evidence = self.evidence(self.work)
        start = time.perf_counter()
        self.build(rebuild=True)
        full_seconds = time.perf_counter() - start
        print(f'Fixture timing: documentation={reuse_seconds:.3f}s full={full_seconds:.3f}s')
        self.assertEqual(reused_evidence, self.evidence(self.work))

    def test_document_reuse_failure_preserves_published_generation(self):
        self.build()
        old = resolve_workspace(self.work)
        doc = self.project / 'Assets' / 'Combat.md'
        doc.write_text(doc.read_text(encoding='utf-8') + '\nDifferent content.\n', encoding='utf-8')
        with patch('unity_rag.workspace._reuse_maps_for_documents', side_effect=RuntimeError('injected failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                self.build()
        self.assertEqual(resolve_workspace(self.work), old)

    def test_script_edit_add_delete_remaps_dependencies_and_matches_full(self):
        self.build()
        edited = self.project / 'Assets' / 'Targeting.cs'
        edited.write_text(edited.read_text(encoding='utf-8').replace('return id;', 'return id + 1;'), encoding='utf-8')
        added = self.project / 'Assets' / 'Probe.cs'
        timings = []
        for operation in ('edit', 'add', 'rename', 'delete'):
            if operation == 'add':
                added.write_text('namespace Game { public class Probe { public bool Run(Enemy e) { return e.CanAttack(2); } } }\n', encoding='utf-8')
            elif operation == 'rename':
                renamed = added.with_name('RenamedProbe.cs')
                added.rename(renamed)
                added = renamed
            elif operation == 'delete':
                added.unlink()
            with patch('unity_rag.asset_map.map_assets', side_effect=AssertionError('asset map unexpectedly rebuilt')):
                started = time.perf_counter()
                result = self.build()
                elapsed = time.perf_counter() - started
            self.assertEqual(result['refresh_mode'], 'script')
            evidence = self.evidence(self.work)
            left_code, left_assets = code_report(self.work), asset_report(self.work)
            with closing(sqlite3.connect(resolve_workspace(self.work) / 'inventory.sqlite3')) as db, db:
                left_edges = db.execute('SELECT * FROM code_edges ORDER BY data').fetchall()
            started = time.perf_counter()
            self.build(rebuild=True)
            timings.append((operation, round(elapsed,3), round(time.perf_counter()-started,3)))
            self.assertEqual(evidence, self.evidence(self.work))
            right_code, right_assets = code_report(self.work), asset_report(self.work)
            for left, right in ((left_code,right_code),(left_assets,right_assets)):
                for key in ('discovery_scanned_at','code_fingerprint'):
                    left.pop(key,None);right.pop(key,None)
                self.assertEqual(left,right)
            with closing(sqlite3.connect(resolve_workspace(self.work) / 'inventory.sqlite3')) as db, db:
                self.assertEqual(left_edges, db.execute('SELECT * FROM code_edges ORDER BY data').fetchall())
        print('Script fixture timings (operation, reuse assets, full):', timings)

    def test_serialized_script_dependency_change_forces_full_asset_map(self):
        self.build()
        script = self.project / 'Assets' / 'Enemy.cs'
        script.write_text(script.read_text(encoding='utf-8').replace('class Enemy :', 'class RenamedEnemy :'), encoding='utf-8')
        self.assertEqual(workspace_status(self.work)['refresh_plan']['mode'], 'script')
        original = __import__('unity_rag.asset_map', fromlist=['map_assets']).map_assets
        with patch('unity_rag.asset_map.map_assets', wraps=original) as mapper:
            result = self.build()
        self.assertEqual(result['refresh_mode'], 'full')
        self.assertEqual(mapper.call_count, 1)

    def test_changed_base_in_another_file_invalidates_serialized_script(self):
        attached = self.project / 'Assets' / 'Enemy.cs'
        attached.write_text(attached.read_text(encoding='utf-8').replace(
            'public class CombatBase {}', 'public class CombatBase : DynamicBase {}'), encoding='utf-8')
        dep = self.project / 'Assets' / 'DynamicBase.cs'
        dep.write_text('namespace UnityEngine { public class MonoBehaviour {} }\nnamespace Game { public class DynamicBase {} }\n', encoding='utf-8')
        self.build()
        before = next(x for x in code_report(self.work)['declarations'] if x['name'] == 'Enemy')
        self.assertEqual(before['unity_type'], 'none')
        dep.write_text('namespace UnityEngine { public class MonoBehaviour {} }\nnamespace Game { public class DynamicBase : UnityEngine.MonoBehaviour {} }\n', encoding='utf-8')
        original = __import__('unity_rag.asset_map', fromlist=['map_assets']).map_assets
        with patch('unity_rag.asset_map.map_assets', wraps=original) as mapper:
            result = self.build()
        self.assertEqual(result['refresh_mode'], 'full')
        self.assertEqual(mapper.call_count, 1)
        after = next(x for x in code_report(self.work)['declarations'] if x['name'] == 'Enemy')
        self.assertEqual(after['unity_type'], 'UnityEngine.MonoBehaviour')

    def test_interrupted_script_reuse_keeps_previous_generation(self):
        self.build()
        pinned = resolve_workspace(self.work)
        path = self.project / 'Assets' / 'Targeting.cs'
        path.write_text(path.read_text(encoding='utf-8') + '\n// edit\n', encoding='utf-8')
        with patch('unity_rag.workspace._remap_code_reuse_assets', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.build()
        self.assertEqual(resolve_workspace(self.work), pinned)

    def test_opt_in_path_and_incoming_expansion_with_stale_doc_guard(self):
        doc = self.project / 'Assets' / 'Combat.md'
        doc.write_text(doc.read_text(encoding='utf-8') + '\nSee `Assets/Armor.asset`.\n', encoding='utf-8')
        self.build()
        result = retrieve(self.work, 'document:Assets/Combat.md', mode='exact', path_refs=2)
        self.assertTrue(any(c.get('expansion', {}).get('reason') == 'document_path_mention' for c in result['context']))
        symbol = next(d['id'] for d in code_report(self.work)['declarations'] if d['name'] == 'CanAttack')
        incoming = retrieve(self.work, symbol, mode='exact', incoming_expand=3)
        self.assertTrue(any(c.get('expansion', {}).get('reason') == 'incoming_reference' and 'Tick' in c.get('title', '')
                            for c in incoming['context']))
        doc.write_text(doc.read_text(encoding='utf-8') + '\nchanged\n', encoding='utf-8')
        stale = retrieve(self.work, 'document:Assets/Combat.md', mode='exact', path_refs=2)
        self.assertFalse(any(c.get('expansion', {}).get('reason') == 'document_path_mention' for c in stale['context']))
        self.assertTrue(any(x['path'] == 'Assets/Combat.md' and x['status'] == 'stale' for x in stale['source_freshness']))

    def test_deleted_document_and_incompatible_pipeline_force_full_mapping(self):
        (self.project / 'Assets' / 'Combat.md.meta').write_text('guid: 12345678901234567890123456789012\n', encoding='utf-8')
        self.build()
        old = resolve_workspace(self.work)
        doc = self.project / 'Assets' / 'Combat.md'
        contents = doc.read_bytes()
        doc.unlink()
        with patch('unity_rag.code_map.map_code', side_effect=RuntimeError('full map required')):
            with self.assertRaisesRegex(RuntimeError, 'full map required'):
                self.build()
        self.assertEqual(resolve_workspace(self.work), old)
        doc.write_bytes(contents)
        with patch('unity_rag.workspace._pipeline_compatible', return_value=False), \
             patch('unity_rag.code_map.map_code', side_effect=RuntimeError('incompatible pipeline')):
            with self.assertRaisesRegex(RuntimeError, 'incompatible pipeline'):
                self.build()
        self.assertEqual(resolve_workspace(self.work), old)

    def test_document_add_rename_delete_match_clean_rebuild(self):
        self.build()
        path = self.project / 'Assets' / 'AddedDoc.md'
        path.write_text('Unique new content for retrieval.\n', encoding='utf-8')
        timings = []
        for operation in ('add', 'rename', 'delete'):
            if operation == 'rename':
                new = path.with_name('RenamedDoc.md')
                path.rename(new)
                path = new
            elif operation == 'delete':
                path.unlink()
            with patch('unity_rag.code_map.map_code', side_effect=AssertionError('code remapped')), \
                 patch('unity_rag.asset_map.map_assets', side_effect=AssertionError('assets remapped')):
                start = time.perf_counter()
                result = self.build()
                elapsed = time.perf_counter() - start
            self.assertEqual(result['refresh_mode'], 'documentation')
            evidence = self.evidence(self.work)
            start = time.perf_counter()
            self.build(rebuild=True)
            timings.append((operation, round(elapsed, 3), round(time.perf_counter() - start, 3)))
            self.assertEqual(evidence, self.evidence(self.work))
        print('Document-change fixture timings (operation, reuse, full):', timings)

    def test_reader_stays_on_one_generation_during_publication(self):
        self.build()
        old = resolve_workspace(self.work)
        self.build(rebuild=True)
        new = resolve_workspace(self.work)
        pointer = self.work / 'active.json'
        pointer.write_text(json.dumps({'generation': old.name}), encoding='utf-8')
        from unity_rag.semantic import rank
        def publish_during_rank(workspace, *args, **kwargs):
            self.assertEqual(Path(workspace), old)
            pointer.write_text(json.dumps({'generation': new.name}), encoding='utf-8')
            return rank(workspace, *args, **kwargs)
        with patch('unity_rag.semantic.rank', side_effect=publish_during_rank):
            result = retrieve(self.work, 'attack', mode='semantic', provider=self.provider)
        self.assertTrue(result['results'])
        self.assertEqual(resolve_workspace(self.work), new)

    def test_seal_migration_preserves_snapshot_and_does_not_promote_stale_sources(self):
        self.build()
        original = resolve_workspace(self.work)
        manifest = json.loads((original / 'complete.json').read_text())
        manifest.pop('metadata_seal')
        _atomic(original / 'complete.json', manifest)
        _atomic(self.work / 'active.json', _published_pointer(original, original.name))
        evidence = self.evidence(original)
        calls = self.provider.calls
        doc = self.project / 'Assets' / 'Combat.md'
        doc.write_text(doc.read_text(encoding='utf-8') + '\nChanged after snapshot.\n', encoding='utf-8')
        result = seal_workspace(self.work)
        new = resolve_workspace(self.work)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['sealed_from'], original.name)
        self.assertEqual(self.provider.calls, calls)
        self.assertEqual(self.evidence(new), evidence)
        self.assertFalse(workspace_status(self.work)['fresh'])
        self.assertTrue((original / 'complete.json').exists())
        self.assertEqual(seal_workspace(self.work)['state'], 'unchanged')

    def test_failed_seal_copy_does_not_publish(self):
        self.build()
        original = resolve_workspace(self.work)
        manifest = json.loads((original / 'complete.json').read_text())
        manifest.pop('metadata_seal')
        _atomic(original / 'complete.json', manifest)
        _atomic(self.work / 'active.json', _published_pointer(original, original.name))
        with patch('unity_rag.workspace._backup', side_effect=RuntimeError('injected copy failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected copy failure'):
                seal_workspace(self.work)
        self.assertEqual(resolve_workspace(self.work), original)

    def test_unbound_seal_is_cloned_with_a_checked_pointer(self):
        self.build()
        original = resolve_workspace(self.work)
        _atomic(self.work / 'active.json', {'generation': original.name})
        result = seal_workspace(self.work)
        self.assertEqual(result['state'], 'complete')
        self.assertNotEqual(resolve_workspace(self.work), original)
        pointer = json.loads((self.work / 'active.json').read_text())
        self.assertIn('manifest_sha256', pointer)

    def test_seal_refuses_missing_semantic_snapshot(self):
        self.build()
        original = resolve_workspace(self.work)
        (original / 'semantic.sqlite3').unlink()
        with self.assertRaisesRegex(ValueError, 'semantic index is missing'):
            seal_workspace(self.work)
        self.assertEqual(resolve_workspace(self.work), original)

    def test_active_manifest_corruption_is_detected(self):
        self.build()
        active = resolve_workspace(self.work)
        manifest = json.loads((active / 'complete.json').read_text())
        manifest['metadata_seal']['stamp']['assets'] = 'invalid'
        _atomic(active / 'complete.json', manifest)
        with self.assertRaisesRegex(ValueError, 'manifest checksum'):
            resolve_workspace(self.work)
        with self.assertRaisesRegex(ValueError, 'manifest checksum'):
            retrieve(active, 'attack')

    def test_sealed_generation_detects_modified_map_record(self):
        self.build()
        active = resolve_workspace(self.work)
        with closing(sqlite3.connect(active / 'inventory.sqlite3')) as db, db:
            raw = db.execute("SELECT value FROM metadata WHERE key='asset_report'").fetchone()[0]
            altered = json.loads(raw)
            altered['coverage_note'] += ' modified'
            db.execute("UPDATE metadata SET value=? WHERE key='asset_report'", (json.dumps(altered),))
        with self.assertRaisesRegex(ValueError, 'Retrieval index is stale'):
            retrieve(active, 'attack')

    def test_legacy_adoption_configuration_and_pointer_validation(self):
        scan(self.project, self.work)
        self.build(defines=['CUSTOM'])
        self.assertTrue((self.work / 'inventory.sqlite3').exists())
        self.assertEqual(code_report(self.work)['defines'], ['CUSTOM'])
        self.assertEqual(self.build()['state'], 'unchanged')
        stale = self.base / 'StaleLegacy'
        stale.mkdir()
        for name in ('inventory.sqlite3', 'semantic.sqlite3'):
            shutil.copyfile(resolve_workspace(self.work) / name, stale / name)
        scan(self.project, stale)
        refresh(stale, provider=self.provider)
        self.assertEqual(code_report(stale)['defines'], ['CUSTOM'])
        self.assertTrue(workspace_status(stale)['fresh'])
        result = self.build(defines=['OTHER'])
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(code_report(self.work)['defines'], ['OTHER'])
        (self.work / 'active.json').write_text('{"generation":"../../escape"}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            resolve_workspace(self.work)

    @unittest.skipUnless(shutil.which('git'), 'Requires git')
    def test_branch_and_dirty_tree_refresh(self):
        def git(*args):
            subprocess.run(['git', '-C', str(self.project), *args], check=True, capture_output=True)
        git('init')
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'Fixture')
        self.build()
        git('checkout', '-b', 'other')
        result = self.build()
        self.assertTrue(result['changes']['git_changed'])
        self.assertEqual(status(self.work)['git']['branch'], 'other')
        (self.project / 'Assets' / 'Dirty.cs').write_text('class Dirty {}', encoding='utf-8')
        self.build()
        self.assertTrue(status(self.work)['git']['dirty'])
        clean = self.base / 'Clean'
        refresh(clean, self.project, provider=FixtureProvider())
        self.assertEqual(self.evidence(self.work), self.evidence(clean))


if __name__ == '__main__':
    unittest.main()
