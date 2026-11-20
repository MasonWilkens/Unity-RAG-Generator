from contextlib import closing
import gc
import hashlib
from contextlib import nullcontext
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from unity_rag.answers import ask, verify_sources
from unity_rag.asset_map import map_assets
from unity_rag.code_map import map_code
from unity_rag.discovery import scan, status
from unity_rag.extractive import source_blocks
from unity_rag.retrieval import build_retrieval, format_results, retrieve
from unity_rag.source_evaluation import evaluate_sources


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class ReferenceFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'Game'; self.work = self.base / 'Knowledge'
        shutil.copytree(Path(__file__).parent / 'fixtures/retrieval_project', self.root)
        scan(self.root, self.work); map_code(self.work); map_assets(self.work); build_retrieval(self.work)

    def object_result(self):
        response = retrieve(self.work, 'asset:Assets/Goblin.prefab#3', mode='exact', limit=1,
                            context_chars=100000)
        self.assertEqual(len(response['results']), 1)
        self.assertIn('armorSettings -> asset:Assets/Armor.asset#11400000 [resolved_object]', response['results'][0]['text'])
        return response

    def test_resolved_target_and_guid_are_tracked_as_dependencies(self):
        response = self.object_result(); row = response['results'][0]
        direct = [s['path'] for s in row['sources'] if s.get('role') != 'reference_dependency']
        dependencies = {s['path'] for s in row['sources'] if s.get('role') == 'reference_dependency'}
        self.assertEqual(direct, ['Assets/Goblin.prefab'])
        self.assertTrue({'Assets/Armor.asset', 'Assets/Armor.asset.meta',
                         'Assets/Enemy.cs', 'Assets/Enemy.cs.meta'} <= dependencies)
        self.assertTrue(all(s['status'] == 'current' for s in response['source_freshness']))
        verify_sources(status(self.work), [row])

    def test_changed_guid_or_target_content_is_reported_stale(self):
        for name in ('Assets/Armor.asset.meta', 'Assets/Armor.asset', 'Assets/Enemy.cs'):
            with self.subTest(name=name):
                target = self.root / name; original = target.read_bytes()
                try:
                    target.write_bytes(original + b'\n# changed\n')
                    response = self.object_result()
                    self.assertIn((name, 'stale'), {(s['path'], s['status']) for s in response['source_freshness']})
                    with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                        verify_sources(status(self.work), response['results'])
                finally:
                    target.write_bytes(original)

    def test_human_readable_results_warn_about_stale_reference_dependencies(self):
        target = self.root / 'Assets/Armor.asset.meta'
        target.write_bytes(target.read_bytes() + b'\n# changed\n')
        output = format_results(self.object_result())
        self.assertIn('Reference dependency: Assets/Armor.asset.meta [stale]', output)

    def test_only_displayed_reference_chunks_get_that_dependency(self):
        prefab = self.root / 'Assets/Goblin.prefab'
        original = prefab.read_text(encoding='utf-8')
        filler = ''.join(f'  filler{i}: {i}\n' for i in range(120))
        prefab.write_text(original.replace('  damage: 25\n', filler + '  damage: 25\n'), encoding='utf-8')
        scan(self.root, self.work); map_code(self.work); map_assets(self.work); build_retrieval(self.work)
        response = retrieve(self.work, 'asset:Assets/Goblin.prefab#3', mode='exact', limit=100, context_chars=1000000)
        linked = [r for r in response['results'] if 'armorSettings ->' in r['text']]
        other = [r for r in response['results'] if 'armorSettings ->' not in r['text']]
        self.assertTrue(linked); self.assertTrue(other)
        self.assertTrue(all('Assets/Armor.asset.meta' in {s['path'] for s in r['sources']} for r in linked))
        self.assertTrue(all('Assets/Armor.asset.meta' not in {s['path'] for s in r['sources']} for r in other))

    def test_multiline_serialized_key_still_tracks_reference_dependencies(self):
        prefab = self.root / 'Assets/Goblin.prefab'
        text = prefab.read_text(encoding='utf-8')
        text = text.replace('  armorSettings:', '  "odd\\nkey":')
        prefab.write_text(text, encoding='utf-8')
        scan(self.root, self.work); map_code(self.work); map_assets(self.work); build_retrieval(self.work)
        response = retrieve(self.work, 'asset:Assets/Goblin.prefab#3', mode='exact', limit=1, context_chars=100000)
        row = response['results'][0]
        self.assertIn('odd\nkey -> asset:Assets/Armor.asset#11400000 [resolved_object]', row['text'])
        self.assertIn('Assets/Armor.asset.meta', {s['path'] for s in row['sources'] if s.get('role') == 'reference_dependency'})

    def test_removed_reference_metadata_is_reported_missing(self):
        target = self.root / 'Assets/Armor.asset.meta'
        target.unlink()
        response = self.object_result()
        self.assertIn(('Assets/Armor.asset.meta', 'missing'),
                      {(s['path'], s['status']) for s in response['source_freshness']})
        with self.assertRaises(FileNotFoundError):
            verify_sources(status(self.work), response['results'])

    def test_unseen_duplicate_guid_requires_workspace_freshness_scan(self):
        from unity_rag.workspace import workspace_status
        (self.root / 'Assets/New.asset').write_text('--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: New\n')
        (self.root / 'Assets/New.asset.meta').write_text('fileFormatVersion: 2\nguid: ' + 'c' * 32 + '\n')
        response = self.object_result()
        self.assertTrue(all(s['status'] == 'current' for s in response['source_freshness']))
        self.assertFalse(workspace_status(self.work)['fresh'])

    def test_answer_fails_before_model_use_if_reference_metadata_changed(self):
        target = self.root / 'Assets/Armor.asset.meta'
        target.write_text(target.read_text().replace('c' * 32, 'd' * 32), encoding='utf-8')
        provider = Mock(); provider.config = {'provider': 'test'}
        with self.assertRaisesRegex(ValueError, 'changed since discovery'):
            ask(self.work, 'asset:Assets/Goblin.prefab#3', provider=provider, mode='exact',
                limit=1, expand=0, context_chars=100000, followups=0)
        provider.generate.assert_not_called()

    def test_graph_edges_track_the_guid_dependency(self):
        response = retrieve(self.work, 'Assets/Armor.asset', mode='used-by', limit=20, context_chars=100000)
        edge = next(e for e in response['results'] if e.get('property') == 'armorSettings')
        self.assertIn('Assets/Armor.asset.meta', {s['path'] for s in edge['sources'] if s.get('role') == 'reference_dependency'})

    def test_stale_reference_dependency_invalidates_reviewed_passage(self):
        target = self.root / 'Assets/Armor.asset.meta'
        target.write_text(target.read_text().replace('c' * 32, 'd' * 32), encoding='utf-8')
        source = self.root / 'Assets/Goblin.prefab'; data = source.read_bytes()
        lines = data.decode('utf-8-sig').splitlines()
        line = next(i + 1 for i, text in enumerate(lines) if 'movementSpeed: 4' in text)
        dataset = self.base / 'reviewed.json'
        dataset.write_text(json.dumps({'schema_version': 1, 'cases': [{
            'id': 'x', 'question': 'movement speed', 'expected_sources': ['Assets/Goblin.prefab'],
            'reviewed_source_sha256': {'Assets/Goblin.prefab': hashlib.sha256(data).hexdigest()},
            'required_passages': [{'path': 'Assets/Goblin.prefab', 'quote': 'movementSpeed: 4',
                                   'start_line': line, 'end_line': line}]}]}))
        report = evaluate_sources(self.work, dataset, mode='search', limit=1, expand=0, context_chars=100000)
        passage = report['cases'][0]['required_passages'][0]
        self.assertTrue(passage['returned'])
        self.assertFalse(passage['reference_dependencies_current'])
        self.assertFalse(passage['matched_current'])
        self.assertIn('Assets/Armor.asset.meta', report['cases'][0]['noncurrent_reference_dependencies'])

    def test_source_evaluation_rechecks_dependency_after_retrieval(self):
        response = self.object_result()
        source = self.root / 'Assets/Goblin.prefab'; data = source.read_bytes()
        line = next(i + 1 for i, text in enumerate(data.decode('utf-8-sig').splitlines()) if 'movementSpeed: 4' in text)
        dataset = self.base / 'reviewed-race.json'
        dataset.write_text(json.dumps({'schema_version': 1, 'cases': [{
            'id': 'x', 'question': 'movement speed', 'expected_sources': ['Assets/Goblin.prefab'],
            'reviewed_source_sha256': {'Assets/Goblin.prefab': hashlib.sha256(data).hexdigest()},
            'required_passages': [{'path': 'Assets/Goblin.prefab', 'quote': 'movementSpeed: 4',
                                   'start_line': line, 'end_line': line}]}]}))
        target = self.root / 'Assets/Armor.asset.meta'
        def return_then_mutate(*args, **kwargs):
            target.write_bytes(target.read_bytes() + b'\n# changed after retrieval\n')
            return response
        with patch('unity_rag.source_evaluation.retrieve', side_effect=return_then_mutate):
            report = evaluate_sources(self.work, dataset, mode='search', limit=1, expand=0, context_chars=100000)
        self.assertFalse(report['cases'][0]['required_passages'][0]['matched_current'])
        self.assertIn('Assets/Armor.asset.meta', report['cases'][0]['noncurrent_reference_dependencies'])

    def test_stale_reference_dependency_does_not_count_as_queue_source_hit(self):
        import unity_rag.work_queue as queue
        target = self.root / 'Assets/Armor.asset.meta'
        target.write_text(target.read_text().replace('c' * 32, 'd' * 32), encoding='utf-8')
        response = self.object_result()
        plan = {'project': str(self.root), 'items': [{'id': 'x', 'kind': 'rag_eval', 'cache_key': 'key',
                           'question': 'x', 'expected_sources': ['Assets/Goblin.prefab']}]}
        with patch.object(queue, 'run_root', return_value=self.base), \
             patch.object(queue, 'writer_lock', return_value=nullcontext()), \
             patch.object(queue, 'load_plan', return_value=(plan, {})), \
             patch.object(queue, 'check_fresh'), \
             patch.object(queue, '_atomic') as save, \
             patch('unity_rag.retrieval.retrieve', return_value=response):
            report = queue.evaluate(self.work)
        self.assertEqual(report['all_expected_hits'], 0)
        row = save.call_args.args[1]['key']
        self.assertIn('Assets/Armor.asset.meta', row['noncurrent_reference_dependencies'])
        self.assertEqual(row['reference_dependency_checks'], 1)
        self.assertEqual(row['retrieval_evaluation_fingerprint'], queue._retrieval_evaluation_fingerprint())

    def test_legacy_queue_evaluation_cache_is_not_silently_reused(self):
        import unity_rag.work_queue as queue
        plan = {'project': str(self.root), 'items': [{'id': 'x', 'kind': 'rag_eval', 'cache_key': 'key',
                           'question': 'x', 'expected_sources': ['Assets/Goblin.prefab']}]}
        cache = self.base / 'retrieval.json'
        cache.write_text(json.dumps({'key': {'id': 'x', 'status': 'completed', 'all_expected_hit': True}}))
        before = cache.read_bytes()
        with patch.object(queue, 'run_root', return_value=self.base), \
             patch.object(queue, 'writer_lock', return_value=nullcontext()), \
             patch.object(queue, 'load_plan', return_value=(plan, {})), \
             patch.object(queue, 'check_fresh'), patch.object(queue, '_atomic') as save:
            with self.assertRaisesRegex(ValueError, 'predates reference'):
                queue.evaluate(self.work)
        self.assertEqual(cache.read_bytes(), before)
        save.assert_not_called()

    def test_queue_rechecks_reference_dependencies_before_return(self):
        import unity_rag.work_queue as queue
        response = self.object_result()
        plan = {'project': str(self.root), 'items': [{'id': 'x', 'kind': 'rag_eval', 'cache_key': 'key',
                'question': 'x', 'expected_sources': ['Assets/Goblin.prefab']}]}
        target = self.root / 'Assets/Armor.asset.meta'
        def mutate_on_save(*args):
            target.write_bytes(target.read_bytes() + b'\n# changed during evaluation\n')
        with patch.object(queue, 'run_root', return_value=self.base), \
             patch.object(queue, 'writer_lock', return_value=nullcontext()), \
             patch.object(queue, 'load_plan', return_value=(plan, {})), \
             patch.object(queue, 'check_fresh'), \
             patch.object(queue, '_atomic', side_effect=mutate_on_save), \
             patch('unity_rag.retrieval.retrieve', return_value=response):
            with self.assertRaisesRegex(ValueError, 'reference dependenc'):
                queue.evaluate(self.work)

    def test_cached_queue_result_rechecks_reference_dependencies(self):
        import unity_rag.work_queue as queue
        plan = {'project': str(self.root), 'items': [{'id': 'x', 'kind': 'rag_eval', 'cache_key': 'key',
                'question': 'x', 'expected_sources': ['Assets/Goblin.prefab']}]}
        target = self.root / 'Assets/Armor.asset.meta'
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        cache = self.base / 'retrieval.json'
        cache.write_text(json.dumps({'key': {'id': 'x', 'status': 'completed', 'all_expected_hit': True,
                    'reference_dependency_checks': 1,
                    'retrieval_evaluation_fingerprint': queue._retrieval_evaluation_fingerprint(),
                    'reference_dependencies': [{'path': 'Assets/Armor.asset.meta', 'sha256': digest}]}}))
        before = cache.read_bytes()
        target.write_bytes(target.read_bytes() + b'\n# changed\n')
        with patch.object(queue, 'run_root', return_value=self.base), \
             patch.object(queue, 'writer_lock', return_value=nullcontext()), \
             patch.object(queue, 'load_plan', return_value=(plan, {})), \
             patch.object(queue, 'check_fresh'), patch.object(queue, '_atomic') as save:
            with self.assertRaisesRegex(ValueError, 'reference dependenc'):
                queue.evaluate(self.work)
        self.assertEqual(cache.read_bytes(), before)
        save.assert_not_called()

    def test_current_checked_queue_cache_can_be_reused(self):
        import unity_rag.work_queue as queue
        plan = {'project': str(self.root), 'items': [{'id': 'x', 'kind': 'rag_eval', 'cache_key': 'key',
                'question': 'x', 'expected_sources': ['Assets/Goblin.prefab']}]}
        target = self.root / 'Assets/Armor.asset.meta'
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        cache = self.base / 'retrieval.json'
        cache.write_text(json.dumps({'key': {'id': 'x', 'status': 'completed', 'all_expected_hit': True,
                    'reference_dependency_checks': 1,
                    'retrieval_evaluation_fingerprint': queue._retrieval_evaluation_fingerprint(),
                    'reference_dependencies': [{'path': 'Assets/Armor.asset.meta', 'sha256': digest}]}}))
        before = cache.read_bytes()
        with patch.object(queue, 'run_root', return_value=self.base), \
             patch.object(queue, 'writer_lock', return_value=nullcontext()), \
             patch.object(queue, 'load_plan', return_value=(plan, {})), \
             patch.object(queue, 'check_fresh'), patch.object(queue, '_atomic') as save:
            report = queue.evaluate(self.work)
        self.assertEqual(report['all_expected_hits'], 1)
        self.assertEqual(cache.read_bytes(), before)
        save.assert_not_called()

    def test_queue_cache_recorded_with_stale_dependencies_requires_review(self):
        import unity_rag.work_queue as queue
        plan = {'project': str(self.root), 'items': [{'id': 'x', 'kind': 'rag_eval', 'cache_key': 'key',
                'question': 'x', 'expected_sources': ['Assets/Goblin.prefab']}]}
        target = self.root / 'Assets/Armor.asset.meta'; digest = hashlib.sha256(target.read_bytes()).hexdigest()
        cache = self.base / 'retrieval.json'
        cache.write_text(json.dumps({'key': {'id': 'x', 'status': 'completed', 'all_expected_hit': False,
                    'reference_dependency_checks': 1, 'retrieval_evaluation_fingerprint': queue._retrieval_evaluation_fingerprint(),
                    'reference_dependencies': [{'path': 'Assets/Armor.asset.meta', 'sha256': digest}],
                    'noncurrent_reference_dependencies': ['Assets/Armor.asset.meta']}}))
        before = cache.read_bytes()
        with patch.object(queue, 'run_root', return_value=self.base), \
             patch.object(queue, 'writer_lock', return_value=nullcontext()), \
             patch.object(queue, 'load_plan', return_value=(plan, {})), \
             patch.object(queue, 'check_fresh'), patch.object(queue, '_atomic') as save:
            with self.assertRaisesRegex(ValueError, 'recorded noncurrent'):
                queue.evaluate(self.work)
        self.assertEqual(cache.read_bytes(), before); save.assert_not_called()

    def test_queue_cache_from_different_retrieval_code_requires_review(self):
        import unity_rag.work_queue as queue
        plan = {'project': str(self.root), 'items': [{'id': 'x', 'kind': 'rag_eval', 'cache_key': 'key',
                'question': 'x', 'expected_sources': ['Assets/Goblin.prefab']}]}
        cache = self.base / 'retrieval.json'
        cache.write_text(json.dumps({'key': {'id': 'x', 'status': 'completed', 'all_expected_hit': True,
                    'reference_dependency_checks': 1, 'retrieval_evaluation_fingerprint': 'old-code',
                    'reference_dependencies': []}}))
        before = cache.read_bytes()
        with patch.object(queue, 'run_root', return_value=self.base), \
             patch.object(queue, 'writer_lock', return_value=nullcontext()), \
             patch.object(queue, 'load_plan', return_value=(plan, {})), \
             patch.object(queue, 'check_fresh'), patch.object(queue, '_atomic') as save:
            with self.assertRaisesRegex(ValueError, 'retrieval.*code'):
                queue.evaluate(self.work)
        self.assertEqual(cache.read_bytes(), before)
        save.assert_not_called()

    def test_queue_detects_retrieval_code_change_during_evaluation(self):
        import unity_rag.work_queue as queue
        response = self.object_result()
        plan = {'project': str(self.root), 'items': [{'id': 'x', 'kind': 'rag_eval', 'cache_key': 'key',
                'question': 'x', 'expected_sources': ['Assets/Goblin.prefab']}]}
        with patch.object(queue, 'run_root', return_value=self.base), \
             patch.object(queue, 'writer_lock', return_value=nullcontext()), \
             patch.object(queue, 'load_plan', return_value=(plan, {})), \
             patch.object(queue, 'check_fresh'), patch.object(queue, '_atomic'), \
             patch.object(queue, '_retrieval_evaluation_fingerprint', side_effect=['before', 'after']), \
             patch('unity_rag.retrieval.retrieve', return_value=response):
            with self.assertRaisesRegex(ValueError, 'retrieval.*changed'):
                queue.evaluate(self.work)

    def test_resolved_prefab_override_tracks_base_guid_metadata(self):
        response = retrieve(self.work, 'damage', kind='prefab_override', limit=1, context_chars=100000)
        self.assertTrue(response['results'])
        self.assertIn('Assets/Goblin.prefab.meta', {s['path'] for s in response['results'][0]['sources']
                                                   if s.get('role') == 'reference_dependency'})
        target = self.root / 'Assets/Goblin.prefab.meta'
        target.write_bytes(target.read_bytes() + b'\n# changed\n')
        response = retrieve(self.work, 'damage', kind='prefab_override', limit=1, context_chars=100000)
        self.assertIn(('Assets/Goblin.prefab.meta', 'stale'),
                      {(s['path'], s['status']) for s in response['source_freshness']})

    def test_dependency_paths_are_not_counted_as_direct_evidence_or_excerpts(self):
        response = self.object_result(); row = response['results'][0]
        dataset = self.base / 'cases.json'
        dataset.write_text(json.dumps({'schema_version': 1, 'cases': [
            {'id': 'x', 'question': 'x', 'expected_sources': ['Assets/Armor.asset']}]}))
        with patch('unity_rag.source_evaluation.retrieve', return_value=response):
            report = evaluate_sources(self.work, dataset)
        self.assertFalse(report['cases'][0]['any_expected_hit'])
        blocks, _ = source_blocks(status(self.work), {'E1': row}, 100000)
        self.assertTrue(blocks)
        self.assertEqual({b['path'] for b in blocks.values()}, {'Assets/Goblin.prefab'})


if __name__ == '__main__':
    unittest.main()
