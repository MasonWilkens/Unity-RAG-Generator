"""Source, cache and spending boundaries for the draft factory; no remote calls."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from unity_rag import work_queue as queue


class FixedTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 1, 20, tzinfo=timezone.utc)


class QueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / 'Game'
        self.workspace = self.base / 'Knowledge'
        self.workspace.mkdir()
        self.source = self.project / 'Assets' / 'Weapon.cs'
        self.source.parent.mkdir(parents=True)
        self.source.write_text('class Weapon {\nconst int cap = 8;\n}\n', encoding='utf-8')
        self.manifest = {'version': 1, 'task_version': 'synthetic-v1', 'items': [{
            'id': 'cap', 'kind': 'fixture_draft', 'question': 'What is the cap?',
            'expected_sources': ['Assets/Weapon.cs'],
            'sources': [{'path': 'Assets/Weapon.cs', 'start_line': 1, 'end_line': 3}],
            'required_facts': [{'id': 'cap-value', 'statement': 'The cap is 8.',
                                'source_path': 'Assets/Weapon.cs', 'quote': 'const int cap = 8;',
                                'exact_values': ['8']}],
            'setup_requirements': ['Create a ready weapon.'],
            'behavior_assertions': ['Assert exactly 8 actions.']}]}
        self.manifest_path = self.base / 'manifest.json'
        self.write_manifest()
        self.time_patch = patch.object(queue, 'datetime', FixedTime)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    def write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding='utf-8')

    def entries(self):
        return {'Assets/Weapon.cs': {'path': 'Assets/Weapon.cs', 'status': 'supported',
                                    'sha256': hashlib.sha256(self.source.read_bytes()).hexdigest()}}

    def snapshot(self):
        return queue.snapshot_tasks(self.manifest, self.project, self.entries(), self.workspace.name)

    def prepare(self):
        inventory = {'project': str(self.project), 'entries': list(self.entries().values())}
        with patch.object(queue, 'inventory_status', return_value=inventory):
            queue.prepare(self.workspace, self.manifest_path)
        return queue.run_root(self.workspace)

    def artifact(self, task=None):
        task = task or self.snapshot()[0]
        fact = task['required_facts'][0]
        return {'id': task['id'], 'kind': task['kind'], 'question': task['question'],
                'answer': 'The cap is 8.',
                'facts': [{'id': fact['id'], 'statement': fact['statement'],
                           'exact_values': fact['exact_values'],
                           'citation': {'path': 'Assets/Weapon.cs', 'start_line': 2, 'end_line': 2,
                                        'sha256': task['sources'][0]['sha256'], 'quote': fact['quote']}}],
                'setup_requirements': list(task['setup_requirements']),
                'behavior_assertions': list(task['behavior_assertions']),
                'failure_classification': {'setup': 'Weapon not ready.', 'behavior': 'Action count differs.'},
                'limitations': ['Static draft; runtime behavior has not been verified.']}

    def wire(self, task=None):
        value = self.artifact(task)
        value.pop('setup_requirements')
        value.pop('behavior_assertions')
        value['facts'] = [{'id': fact['id'], 'citation': {
            key: fact['citation'][key] for key in ('path', 'start_line', 'end_line')}} for fact in value['facts']]
        return value

    def provider(self, draft=None, review=None):
        provider = Mock()
        provider.last_usage = {'input_tokens': 100, 'output_tokens': 50, 'estimated_cost_upper_usd': 0.0001}
        provider.generate.side_effect = [
            draft if draft is not None else {'items': [self.wire()]},
            review if review is not None else {'items': [{'id': 'cap', 'verdict': 'supported',
                'issues': [], 'checked_fact_ids': ['cap-value']}]}]
        return provider

    def test_sensitive_sources_rejected_before_open_or_hash(self):
        for relative in ['Assets/.env/key.txt', 'Assets/creds.secret', 'Assets/secrets/ordinary.cs',
                         '../outside.cs', str(self.source)]:
            with self.subTest(relative=relative):
                manifest = copy.deepcopy(self.manifest)
                item = manifest['items'][0]
                item['sources'][0]['path'] = relative
                item['expected_sources'] = [relative]
                item['required_facts'][0]['source_path'] = relative
                with patch.object(Path, 'read_bytes', side_effect=AssertionError('No source content read')), \
                     patch.object(queue.hashlib, 'sha256', side_effect=AssertionError('No hash permitted')):
                    with self.assertRaises(ValueError):
                        queue.snapshot_tasks(manifest, self.project, {relative: {'status': 'supported'}}, 'generation')

    def test_supported_index_scope_checked_before_content_read(self):
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('No source content read')):
            with self.assertRaisesRegex(ValueError, 'outside the supported index'):
                queue.snapshot_tasks(self.manifest, self.project, {}, 'generation')

    def test_source_index_mismatch_and_bad_ranges_rejected(self):
        entries = self.entries()
        entries['Assets/Weapon.cs']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Source/index mismatch'):
            queue.snapshot_tasks(self.manifest, self.project, entries, 'generation')
        for first, last in [(0, 2), (2, 1), (1, 4), (True, 2)]:
            with self.subTest(first=first, last=last):
                self.manifest['items'][0]['sources'][0].update(start_line=first, end_line=last)
                with self.assertRaisesRegex(ValueError, 'excerpt'):
                    self.snapshot()

    def test_unseen_quote_and_values_cannot_become_an_oracle(self):
        fact = self.manifest['items'][0]['required_facts'][0]
        fact['quote'] = 'const int cap = 80;'
        with self.assertRaisesRegex(ValueError, 'quote is missing'):
            self.snapshot()
        fact['exact_values'] = ['99']
        with self.assertRaisesRegex(ValueError, 'Exact values'):
            self.snapshot()

    def test_source_hash_task_version_and_generation_change_cache_identity(self):
        original = self.snapshot()[0]['cache_key']
        self.source.write_text('class Weapon {\nconst int cap = 8;\n}\n// later change\n', encoding='utf-8')
        changed_source = self.snapshot()[0]['cache_key']
        self.assertNotEqual(original, changed_source)
        self.manifest['task_version'] = 'synthetic-v2'
        changed_version = self.snapshot()[0]['cache_key']
        self.assertNotEqual(changed_source, changed_version)
        new_generation = queue.snapshot_tasks(self.manifest, self.project, self.entries(), 'new-generation')[0]['cache_key']
        self.assertNotEqual(changed_version, new_generation)

    def test_pilot_generation_review_is_read_only_and_detects_changed_sources(self):
        root = self.prepare()
        before_plan = (root / 'plan.json').read_bytes()
        before_state = (root / 'state.json').read_bytes()
        with patch.object(queue, 'resolve_workspace', return_value=self.base / 'new-generation'), \
             patch.object(queue, 'inventory_status', return_value={'entries': list(self.entries().values())}):
            result = queue.check_pilot(self.workspace)
            self.assertTrue(result['generation_changed'])
            self.assertTrue(result['review_required'])
            self.assertFalse(result['automatic_rebind'])
            self.assertEqual(result['source_issues'], [])
            self.source.write_text('changed source', encoding='utf-8')
            result = queue.check_pilot(self.workspace)
            self.assertEqual(result['source_issues'][0]['reason'], 'source_or_index_hash_changed')
        self.assertEqual((root / 'plan.json').read_bytes(), before_plan)
        self.assertEqual((root / 'state.json').read_bytes(), before_state)

    def test_duplicate_task_ids_and_overfull_pilot_rejected(self):
        self.manifest['items'].append(copy.deepcopy(self.manifest['items'][0]))
        with self.assertRaisesRegex(ValueError, 'unique'):
            queue.validate_manifest(self.manifest)
        self.manifest['items'] *= 6
        with self.assertRaisesRegex(ValueError, 'one to ten'):
            queue.validate_manifest(self.manifest)

    def test_exact_values_and_required_statements_cannot_be_weakened(self):
        task = self.snapshot()[0]
        for field, value, reason in [('statement', 'The cap may be 8.', 'required_statement_changed'),
                                     ('exact_values', ['7'], 'exact_values_changed')]:
            with self.subTest(field=field):
                artifact = self.artifact(task)
                artifact['facts'][0][field] = value
                self.assertTrue(any(e.startswith(reason) for e in queue.validate_artifact(task, artifact)))

    def test_citation_must_contain_exact_quote_hash_path_and_line(self):
        task = self.snapshot()[0]
        for field, value in [('path', 'Assets/Elsewhere.cs'), ('sha256', '0' * 64),
                             ('start_line', 1), ('quote', 'cap = 9')]:
            with self.subTest(field=field):
                artifact = self.artifact(task)
                citation = artifact['facts'][0]['citation']
                citation[field] = value
                if field == 'start_line':
                    citation['end_line'] = 1
                self.assertIn('citation_not_exact_or_visible:cap-value', queue.validate_artifact(task, artifact))

    def test_fixture_setup_and_behavior_are_independent_exact_requirements(self):
        task = self.snapshot()[0]
        for field in ['setup_requirements', 'behavior_assertions']:
            with self.subTest(field=field):
                artifact = self.artifact(task)
                artifact[field] = []
                self.assertIn(field + '_changed', queue.validate_artifact(task, artifact))
        artifact = self.artifact(task)
        artifact['failure_classification']['setup'] = ''
        self.assertIn('missing_failure_classification', queue.validate_artifact(task, artifact))

    def test_extra_generated_fields_and_duplicate_facts_are_rejected(self):
        task = self.snapshot()[0]
        artifact = self.artifact(task)
        artifact['shell_command'] = 'not executable'
        self.assertEqual(queue.validate_artifact(task, artifact), ['invalid_schema_or_sensitive_output'])
        artifact = self.artifact(task)
        artifact['facts'].append(copy.deepcopy(artifact['facts'][0]))
        self.assertIn('required_fact_set_changed', queue.validate_artifact(task, artifact))

    def test_full_synthetic_run_and_resume_make_exactly_two_calls(self):
        root = self.prepare()
        provider = self.provider()
        factory = Mock(return_value=provider)
        result = queue.run(self.workspace, 'unused-synthetic.secret', True, factory)
        self.assertTrue(result['finished'])
        self.assertEqual(result['counts'], {'accepted_draft': 1})
        self.assertFalse(result['items']['cap']['runtime_verified'])
        self.assertEqual(provider.generate.call_count, 2)
        self.assertEqual(factory.call_args.kwargs['service_tier'], 'default')
        self.assertEqual(factory.call_args.kwargs['budget_usd'], '1')
        self.assertEqual(factory.call_args.kwargs['max_calls'], 2)
        self.assertTrue((root / 'artifacts' / (self.snapshot()[0]['cache_key'] + '.json')).is_file())
        no_calls = Mock(side_effect=AssertionError('Resume must not construct a remote provider'))
        resumed = queue.run(self.workspace, 'unused-synthetic.secret', True, no_calls)
        self.assertEqual(resumed['counts'], {'accepted_draft': 1})
        no_calls.assert_not_called()

    def test_local_mismatch_is_not_overridden_by_model_agreement(self):
        self.prepare()
        artifact = self.wire()
        artifact['facts'][0]['citation']['end_line'] = 3
        provider = self.provider(draft={'items': [artifact]})
        result = queue.run(self.workspace, 'unused', True, Mock(return_value=provider))
        self.assertEqual(result['counts'], {'needs_review': 1})
        self.assertIn('citation_not_exact_or_visible:cap-value', result['items']['cap']['local_errors'])

    def test_review_issues_and_missing_fact_coverage_queue_human_review(self):
        self.prepare()
        review = {'items': [{'id': 'cap', 'verdict': 'supported', 'issues': ['Unclear claim.'],
                            'checked_fact_ids': []}]}
        result = queue.run(self.workspace, 'unused', True, Mock(return_value=self.provider(review=review)))
        self.assertEqual(result['counts'], {'needs_review': 1})
        self.assertIn('review_fact_coverage_incomplete', result['items']['cap']['local_errors'])

    def test_duplicate_generated_ids_stop_before_review_and_do_not_retry(self):
        self.prepare()
        provider = self.provider(draft={'items': [self.wire(), self.wire()]})
        factory = Mock(return_value=provider)
        with self.assertRaisesRegex(ValueError, 'Generated task set mismatch'):
            queue.run(self.workspace, 'unused', True, factory)
        self.assertEqual(provider.generate.call_count, 1)
        with self.assertRaisesRegex(ValueError, 'Generated task set mismatch'):
            queue.run(self.workspace, 'unused', True, factory)
        self.assertEqual(provider.generate.call_count, 1)

    def test_missing_generated_id_stops_before_review(self):
        self.prepare()
        provider = self.provider(draft={'items': []})
        with self.assertRaisesRegex(ValueError, 'Generated task set mismatch'):
            queue.run(self.workspace, 'unused', True, Mock(return_value=provider))
        self.assertEqual(provider.generate.call_count, 1)

    def test_phase_error_persists_and_retry_is_blocked(self):
        root = self.prepare()
        state = queue.read_json(root / 'state.json')
        provider = Mock()
        provider.generate.side_effect = TimeoutError('synthetic timeout')
        with self.assertRaises(TimeoutError):
            queue.execute_phase(root, state, 'generation', provider, [], queue.GEN_SCHEMA)
        saved = queue.read_json(root / 'state.json')
        self.assertEqual(saved['phases']['generation']['status'], 'blocked')
        with self.assertRaisesRegex(ValueError, 'no automatic retry'):
            queue.execute_phase(root, saved, 'generation', provider, [], queue.GEN_SCHEMA)
        self.assertEqual(provider.generate.call_count, 1)

    def test_crash_after_checkpoint_does_not_repeat_network_request(self):
        root = self.prepare()
        state = queue.read_json(root / 'state.json')
        provider = Mock()
        provider.generate.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            queue.execute_phase(root, state, 'generation', provider, [], queue.GEN_SCHEMA)
        saved = queue.read_json(root / 'state.json')
        self.assertEqual(saved['phases']['generation']['status'], 'in_flight')
        with self.assertRaisesRegex(ValueError, 'uncertain outcome'):
            queue.execute_phase(root, saved, 'generation', provider, [], queue.GEN_SCHEMA)
        self.assertEqual(provider.generate.call_count, 1)

    def test_completed_phase_cache_is_reused_and_tampering_is_rejected(self):
        root = self.prepare()
        state = queue.read_json(root / 'state.json')
        provider = self.provider()
        result = queue.execute_phase(root, state, 'generation', provider, [], queue.GEN_SCHEMA)
        saved = queue.read_json(root / 'state.json')
        self.assertEqual(queue.execute_phase(root, saved, 'generation', provider, [], queue.GEN_SCHEMA), result)
        self.assertEqual(provider.generate.call_count, 1)
        (root / 'generation.json').write_text('{"items": []}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Cached output changed'):
            queue.execute_phase(root, saved, 'generation', provider, [], queue.GEN_SCHEMA)
        self.assertEqual(provider.generate.call_count, 1)

    def test_completed_phase_rejects_changed_prompt_without_new_call(self):
        root = self.prepare()
        state = queue.read_json(root / 'state.json')
        provider = self.provider()
        queue.execute_phase(root, state, 'generation', provider, [], queue.GEN_SCHEMA)
        saved = queue.read_json(root / 'state.json')
        with self.assertRaisesRegex(ValueError, 'request identity changed'):
            queue.execute_phase(root, saved, 'generation', provider,
                                [{'role': 'user', 'content': 'changed prompt'}], queue.GEN_SCHEMA)
        self.assertEqual(provider.generate.call_count, 1)

    def test_cached_phase_cannot_redirect_to_another_file(self):
        root = self.prepare()
        state = queue.read_json(root / 'state.json')
        provider = self.provider()
        queue.execute_phase(root, state, 'generation', provider, [], queue.GEN_SCHEMA)
        saved = queue.read_json(root / 'state.json')
        saved['phases']['generation']['file'] = '../elsewhere.json'
        with self.assertRaisesRegex(ValueError, 'Invalid cached phase file'):
            queue.execute_phase(root, saved, 'generation', provider, [], queue.GEN_SCHEMA)
        self.assertEqual(provider.generate.call_count, 1)

    def test_deleted_ledger_is_not_silently_recreated(self):
        root = self.prepare()
        ledger = root / 'spend.sqlite3'
        ledger.unlink()
        factory = Mock(side_effect=AssertionError('No remote provider'))
        with self.assertRaisesRegex(ValueError, 'original ledger'):
            queue.run(self.workspace, 'unused', True, factory)
        with self.assertRaisesRegex(ValueError, 'original ledger'):
            self.prepare()
        with self.assertRaisesRegex(ValueError, 'original ledger'):
            queue.report(root)
        self.assertFalse(ledger.exists())
        factory.assert_not_called()

    def test_plan_tampering_blocks_before_provider_creation(self):
        root = self.prepare()
        plan = queue.read_json(root / 'plan.json')
        plan['credit_stop'] = '2099-01-01T00:00:00+00:00'
        (root / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
        factory = Mock(side_effect=AssertionError('No remote provider'))
        with self.assertRaisesRegex(ValueError, 'Frozen plan'):
            queue.run(self.workspace, 'unused', True, factory)
        factory.assert_not_called()

    def test_engine_version_change_invalidates_plan(self):
        root = self.prepare()
        with patch.object(queue, 'ENGINE_VERSION', 'new-version'):
            with self.assertRaisesRegex(ValueError, 'engine version'):
                queue.load_plan(root)

    def test_duplicate_review_ids_stop_without_repeating_calls(self):
        root = self.prepare()
        duplicate = {'id': 'cap', 'verdict': 'supported', 'issues': [], 'checked_fact_ids': ['cap-value']}
        provider = self.provider(review={'items': [duplicate, duplicate]})
        factory = Mock(return_value=provider)
        with self.assertRaisesRegex(ValueError, 'Review task set mismatch'):
            queue.run(self.workspace, 'unused', True, factory)
        self.assertEqual(provider.generate.call_count, 2)
        with self.assertRaisesRegex(ValueError, 'Review task set mismatch'):
            queue.run(self.workspace, 'unused', True, factory)
        self.assertEqual(provider.generate.call_count, 2)
        self.assertNotIn('finished', queue.read_json(root / 'state.json'))

    def test_citation_cannot_hide_quote_in_a_broad_excerpt(self):
        task = self.snapshot()[0]
        artifact = self.artifact(task)
        artifact['facts'][0]['citation'].update(start_line=1, end_line=3)
        self.assertIn('citation_not_exact_or_visible:cap-value', queue.validate_artifact(task, artifact))

    def test_materialize_preserves_immutable_handwritten_acceptance_anchors(self):
        task = self.snapshot()[0]
        original = copy.deepcopy(task)
        wire = self.wire(task)
        original_wire = copy.deepcopy(wire)
        full = queue.materialize(task, wire)
        self.assertEqual(full, self.artifact(task))
        self.assertEqual(queue.validate_artifact(task, full), [])
        self.assertEqual(wire, original_wire)
        full['setup_requirements'].append('later artifact change')
        full['behavior_assertions'].clear()
        full['facts'][0]['exact_values'].append('9')
        self.assertEqual(task, original)

    def test_materialize_missing_extra_duplicate_facts_remain_invalid(self):
        task = self.snapshot()[0]
        for mode in ('missing', 'extra', 'duplicate'):
            with self.subTest(mode=mode):
                wire = self.wire(task)
                if mode == 'missing':
                    wire['facts'] = []
                elif mode == 'extra':
                    invented = copy.deepcopy(wire['facts'][0])
                    invented['id'] = 'invented-fact'
                    wire['facts'].append(invented)
                else:
                    wire['facts'].append(copy.deepcopy(wire['facts'][0]))
                full = queue.materialize(task, wire)
                self.assertIn('required_fact_set_changed', queue.validate_artifact(task, full))

    def test_materialize_does_not_repair_generated_bad_citation(self):
        task = self.snapshot()[0]
        for field, value in [('path', 'Assets/Unrelated.cs'), ('start_line', 1), ('end_line', 3)]:
            with self.subTest(field=field):
                wire = self.wire(task)
                wire['facts'][0]['citation'][field] = value
                full = queue.materialize(task, wire)
                self.assertEqual(full['facts'][0]['citation'][field], value)
                self.assertIn('citation_not_exact_or_visible:cap-value', queue.validate_artifact(task, full))

    def test_wire_schema_prevents_model_from_replacing_expected_values(self):
        wire = self.wire()
        wire['facts'][0]['exact_values'] = ['9']
        with self.assertRaisesRegex(ValueError, 'unexpected fields'):
            queue.check_schema(wire, queue.WIRE_ARTIFACT)

    def test_source_change_blocks_before_provider_creation(self):
        self.prepare()
        self.source.write_text('changed source', encoding='utf-8')
        factory = Mock(side_effect=AssertionError('No remote provider'))
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            queue.run(self.workspace, 'unused', True, factory)
        factory.assert_not_called()

    def test_reprepare_cannot_replace_frozen_tasks_or_budget(self):
        root = self.prepare()
        original = (root / 'plan.json').read_bytes()
        self.manifest['task_version'] = 'synthetic-v2'
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'Frozen pilot differs'):
            self.prepare()
        self.assertEqual((root / 'plan.json').read_bytes(), original)
        self.assertEqual(queue.SpendLedger(root / 'spend.sqlite3', '1').summary()['budget_usd'], 1)

    def test_credit_deadline_blocks_before_provider_creation(self):
        self.prepare()
        class Expired(FixedTime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 17, 0, tzinfo=timezone.utc)
        factory = Mock(side_effect=AssertionError('No remote provider'))
        with patch.object(queue, 'datetime', Expired):
            with self.assertRaisesRegex(ValueError, 'deadline'):
                queue.run(self.workspace, 'unused', True, factory)
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
