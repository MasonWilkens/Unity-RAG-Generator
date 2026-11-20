import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from unity_rag.answers import ask, validate_output, format_answer, SYSTEM
from unity_rag.generation import OllamaProvider
from unity_rag.discovery import scan
from unity_rag.code_map import map_code
from unity_rag.asset_map import map_assets
from unity_rag.retrieval import build_retrieval


def response(claims=None, gaps=None, followups=None, runtime=False):
    return {'claims': claims or [], 'missing_evidence': [
        {'kind': 'runtime_observation' if runtime else 'repository_evidence', 'description': g}
        for g in (gaps or [])], 'followup_queries': followups or []}


class ScriptedProvider:
    config = {'provider': 'scripted-test', 'model': 'fixture'}

    def __init__(self, fn):
        self.fn = fn
        self.calls = []

    def generate(self, messages, schema):
        payload = json.loads(messages[1]['content'])
        self.calls.append(payload)
        return self.fn(payload, len(self.calls))


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class AnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.project = cls.base/'Game'
        cls.workspace = cls.base/'Knowledge'
        shutil.copytree(Path(__file__).parent/'fixtures/retrieval_project', cls.project)
        scan(cls.project, cls.workspace)
        map_code(cls.workspace)
        map_assets(cls.workspace)
        build_retrieval(cls.workspace)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_answer_citations_and_source_preservation(self):
        before = {p: p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        def answer(payload, call):
            key = next(k for k,v in payload['evidence'].items() if 'CanAttack' in v['title'])
            return response([{'text': 'CanAttack checks whether now has reached nextAttackTime.',
                              'kind': 'fact', 'evidence_ids': [key]}])
        result = ask(self.workspace, 'CanAttack', ScriptedProvider(answer), mode='exact', review=False)
        self.assertEqual(result['status'], 'answered')
        self.assertTrue(result['quality']['requires_source_review'])
        self.assertIn('Draft explanation', format_answer(result))
        citation = result['claims'][0]['citations'][0]
        self.assertEqual(citation['path'], 'Assets/Enemy.cs')
        self.assertIsInstance(citation['start_line'], int)
        self.assertLessEqual(result['context_budget']['used_chars'], 24000)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_token_budget_applies_to_real_answer_evidence(self):
        provider = ScriptedProvider(lambda payload, _: response([{
            'text': 'A source was found.', 'kind': 'fact',
            'evidence_ids': [next(iter(payload['evidence']))]}]))
        result = ask(self.workspace, 'CanAttack', provider, mode='exact',
                     context_tokens=1200, token_encoding='utf8-bytes')
        budget = result['context_budget']
        self.assertLessEqual(budget['used_tokens'], 1200)
        self.assertEqual(budget['used_tokens'], len(json.dumps(provider.calls[0]['evidence'], ensure_ascii=False).encode('utf-8')))
        self.assertTrue(result['claims'][0]['citations'])

    def test_claim_source_view_is_literal_and_citation_scoped(self):
        def model(payload, call):
            key = next(k for k,v in payload['evidence'].items() if 'CanAttack' in v['title'])
            # Deliberately wrong prose: source display must not imply entailment verification.
            return response([{'text': 'CanAttack always returns true.',
                              'kind': 'fact', 'evidence_ids': [key, key]}])
        provider = ScriptedProvider(model)
        result = ask(self.workspace, 'CanAttack', provider, mode='exact', show_sources=True)
        claim = result['claims'][0]
        excerpts = claim['source_view']['excerpts']
        self.assertTrue(excerpts)
        self.assertEqual(len(provider.calls), 1)
        identities = set()
        for excerpt in excerpts:
            self.assertIn(excerpt['evidence_id'], claim['evidence_ids'])
            lines = (self.project/excerpt['path']).read_text(encoding='utf-8-sig').splitlines()
            self.assertEqual(excerpt['text'], '\n'.join(lines[excerpt['start_line']-1:excerpt['end_line']]))
            identities.add((excerpt['path'], excerpt['start_line'], excerpt['end_line']))
        self.assertEqual(len(identities), len(excerpts))
        rendered = format_answer(result)
        self.assertIn('not a correctness verdict', rendered)
        self.assertIn('return now >= nextAttackTime;', rendered)
        self.assertIn(f"{excerpts[0]['start_line']}: ", rendered)

    def test_source_view_is_opt_in_and_abstention_adds_no_excerpts(self):
        provider = ScriptedProvider(lambda *_: response([{
            'text': 'Attack checks a timer.', 'kind': 'fact', 'evidence_ids': ['E1']}]))
        result = ask(self.workspace, 'CanAttack', provider, mode='exact')
        self.assertNotIn('source_view', result['claims'][0])
        result = ask(self.workspace, 'CanAttack', ScriptedProvider(
            lambda *_: response(gaps=['No runtime observation.'])), mode='exact', show_sources=True)
        self.assertEqual(result['claims'], [])
        self.assertNotIn('Cited source', format_answer(result))

    def test_source_change_during_claim_extraction_is_rejected(self):
        from unittest.mock import patch
        from unity_rag.extractive import extract_excerpts
        path = self.project/'Assets/Enemy.cs'
        before = path.read_bytes()
        def extract_then_change(*args):
            result = extract_excerpts(*args)
            path.write_bytes(before+b'\n// changed during extraction')
            return result
        provider = ScriptedProvider(lambda *_: response([{
            'text': 'Attack checks a timer.', 'kind': 'fact', 'evidence_ids': ['E1']}]))
        try:
            with patch('unity_rag.extractive.extract_excerpts', side_effect=extract_then_change):
                with self.assertRaisesRegex(ValueError, 'Source changed'):
                    ask(self.workspace, 'CanAttack', provider, mode='exact', show_sources=True)
        finally:
            path.write_bytes(before)

    def test_no_evidence_does_not_call_model(self):
        provider = ScriptedProvider(lambda *_: self.fail('Unexpected model call'))
        result = ask(self.workspace, 'unfindable_qzx', provider, mode='exact', review=False)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(result['model_calls'], 0)
        self.assertIsNone(result['runtime_required'])

    def test_unknown_citations_are_rejected(self):
        provider = ScriptedProvider(lambda *_: response([
            {'text': 'Invented assertion', 'kind': 'fact', 'evidence_ids': ['E999']}]))
        with self.assertRaisesRegex(ValueError, 'unknown evidence'):
            ask(self.workspace, 'CanAttack', provider, mode='exact', review=False)

    def test_followup_searches_and_generation_are_bounded(self):
        provider = ScriptedProvider(lambda payload, n: response(gaps=['Need more evidence'],
            followups=['ApplyDamage', 'Damage'] if n == 1 else ['Tick']))
        result = ask(self.workspace, 'CanAttack', provider, mode='exact', review=False, followups=1)
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(len(result['searches']), 2)
        self.assertTrue(provider.calls[0]['followups_allowed'])
        self.assertFalse(provider.calls[1]['followups_allowed'])

    def test_source_change_during_generation_is_rejected(self):
        path = self.project/'Assets/Enemy.cs'
        before = path.read_bytes()
        def change(payload, n):
            path.write_bytes(before+b'\n// changed')
            return response(gaps=['Cannot determine behavior'])
        try:
            with self.assertRaisesRegex(ValueError, 'Source changed'):
                ask(self.workspace, 'CanAttack', ScriptedProvider(change), mode='exact', review=False)
        finally:
            path.write_bytes(before)

    def test_measurement_guidance_reaches_generation(self):
        from unity_rag.question_guidance import UNIT_GUIDANCE
        test = self
        class InspectProvider(ScriptedProvider):
            def generate(self, messages, schema):
                test.assertTrue(messages[0]['content'].endswith(UNIT_GUIDANCE))
                return super().generate(messages, schema)
        provider = InspectProvider(lambda *_: response(gaps=['Need more evidence.']))
        ask(self.workspace, 'When does Enemy.Tick update nextAttackTime, and by how much?', provider, mode='search')
        self.assertEqual(len(provider.calls), 1)

    def test_explicit_runtime_question_never_generates_a_static_value(self):
        provider = ScriptedProvider(lambda *_: self.fail('Runtime question must not call generation'))
        result = ask(self.workspace, 'What is the current health of the Enemy in my running game?', provider, mode='search')
        self.assertEqual(result['claims'], [])
        self.assertEqual(result['model_calls'], 0)
        self.assertTrue(result['runtime_required'])
        self.assertTrue(result['runtime_routing']['matched'])
        self.assertEqual(result['status'], 'insufficient_evidence')

    def test_runtime_abstention_and_uncertainty(self):
        provider = ScriptedProvider(lambda *_: response(gaps=['Live frame timing was not recorded.'], runtime=True))
        result = ask(self.workspace, 'Enemy', provider, mode='search', review=False)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertTrue(result['runtime_required'])

    def test_answer_with_unresolved_gaps_is_partial(self):
        def model(payload, call):
            self.assertFalse(payload['evidence']['E1']['provenance']['runtime_observed'])
            return response([{'text': 'CanAttack checks a timer.', 'kind': 'fact', 'evidence_ids': ['E1']}],
                            gaps=['The live timer value is unavailable.'], runtime=True)
        result = ask(self.workspace, 'CanAttack', ScriptedProvider(model), mode='exact')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['model_calls'], 1)

    def test_extractive_integration_has_no_model_call_or_paraphrase(self):
        provider = ScriptedProvider(lambda *_: self.fail('Extractive mode must not generate'))
        result = ask(self.workspace, 'CanAttack', provider, mode='exact', style='extractive')
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(result['claims'], [])
        self.assertTrue(result['excerpts'])
        self.assertIsNone(result['model'])
        excerpt = result['excerpts'][0]
        lines = (self.project/excerpt['path']).read_text(encoding='utf-8-sig').splitlines()
        self.assertEqual(excerpt['text'], '\n'.join(lines[excerpt['start_line']-1:excerpt['end_line']]))


    def test_opt_in_review_filters_claims_and_returns_verified_quotes(self):
        def model(payload, call):
            if call == 1:
                return response([{'text': 'Attack checks the current time against nextAttackTime.',
                                  'kind': 'fact', 'evidence_ids': ['E1']}])
            return {'runtime_required': False, 'decisions': [{
                'claim_index': 0, 'verdict': 'supported', 'reason': 'Direct comparison.',
                'quotes': [{'evidence_id': 'E1', 'quote': 'return now >= nextAttackTime;'}]}]}
        result = ask(self.workspace, 'CanAttack', ScriptedProvider(model), mode='exact', review=True)
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(result['claims'][0]['supporting_quotes'][0]['quote'], 'return now >= nextAttackTime;')
        self.assertEqual(result['grounding_review']['removed_claims'], 0)
        self.assertEqual(result['quality']['status'], 'unreviewed_model_output')
        self.assertFalse(result['quality']['model_review_is_approval'])

    def test_review_rejection_produces_abstention(self):
        def model(payload, call):
            if call == 1:
                return response([{'text': 'This runs at 1000 FPS.', 'kind': 'fact', 'evidence_ids': ['E1']}])
            return {'runtime_required': True, 'decisions': [{
                'claim_index': 0, 'verdict': 'unsupported', 'reason': 'No profiling evidence.', 'quotes': []}]}
        result = ask(self.workspace, 'CanAttack', ScriptedProvider(model), mode='exact', review=True)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertTrue(result['runtime_required'])
        self.assertTrue(result['missing_evidence'])
        self.assertTrue(any(g['kind'] == 'runtime_observation' for g in result['evidence_gaps']))
        self.assertEqual(result['missing_evidence'], [g['description'] for g in result['evidence_gaps']])

    def test_source_change_during_review_is_rejected(self):
        path = self.project/'Assets/Enemy.cs'
        before = path.read_bytes()
        def model(payload, call):
            if call == 1:
                return response([{'text': 'Attack checks a timer.', 'kind': 'fact', 'evidence_ids': ['E1']}])
            path.write_bytes(before+b'\n// changed during review')
            return {'runtime_required': False, 'decisions': [{
                'claim_index': 0, 'verdict': 'unsupported', 'reason': 'Uncertain.', 'quotes': []}]}
        try:
            with self.assertRaisesRegex(ValueError, 'Source changed'):
                ask(self.workspace, 'CanAttack', ScriptedProvider(model), mode='exact', review=True)
        finally:
            path.write_bytes(before)

    def test_http_adapter_end_to_end_with_scripted_server(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(payload)
                evidence = json.loads(payload['messages'][1]['content'])['evidence']
                key = next(iter(evidence))
                value = response([{'text': 'The function checks the attack cooldown.',
                                   'kind': 'fact', 'evidence_ids': [key]}])
                raw = json.dumps({'message': {'content': json.dumps(value)}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = OllamaProvider('scripted-test', f'http://127.0.0.1:{server.server_port}')
            result = ask(self.workspace, 'CanAttack', provider, mode='exact', review=False)
            self.assertEqual(result['status'], 'answered')
            self.assertEqual(requests[0]['messages'][0]['role'], 'system')
            self.assertFalse(requests[0]['stream'])
            self.assertEqual(requests[0]['format']['type'], 'object')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class OutputContractTests(unittest.TestCase):
    def test_inferred_evidence_cannot_be_promoted_to_fact(self):
        value = response([{'text': 'Filename suggests this component type.', 'kind': 'fact', 'evidence_ids': ['E1']}])
        result = validate_output(value, {'E1': {'evidence_type': 'derived_interpretation'}})
        self.assertEqual(result['claims'][0]['kind'], 'interpretation')

    def test_uncited_claims_and_malformed_output_fail_closed(self):
        for value in [response(), {'claims': []}, response([
                {'text': 'Unsupported statement', 'kind': 'fact', 'evidence_ids': []}])]:
            with self.assertRaises(ValueError):
                validate_output(value, {})

    def test_typed_gaps_derive_runtime_flag_and_preserve_descriptions(self):
        value = response(gaps=['Source missing'])
        value['missing_evidence'].append({'kind': 'runtime_observation', 'description': 'Measure frame time.'})
        result = validate_output(value, {})
        self.assertTrue(result['runtime_required'])
        self.assertEqual(result['missing_evidence'], ['Source missing', 'Measure frame time.'])
        self.assertEqual(result['evidence_gaps'][1]['kind'], 'runtime_observation')
        result = validate_output(response(gaps=['Source missing']), {})
        self.assertFalse(result['runtime_required'])

    def test_multi_reference_answers_have_bounded_room_for_all_assignments(self):
        claims = [{'text': f'Reference {i}', 'kind': 'fact', 'evidence_ids': ['E1']} for i in range(12)]
        evidence = {'E1': {'evidence_type': 'source_fact'}}
        self.assertEqual(len(validate_output(response(claims), evidence)['claims']), 12)
        with self.assertRaisesRegex(ValueError, 'invalid claims'):
            validate_output(response(claims + [claims[0]]), evidence)

    def test_old_independent_runtime_flag_is_rejected(self):
        value = response(gaps=['Measure frame time.'], runtime=True)
        value['runtime_required'] = False
        with self.assertRaisesRegex(ValueError, 'invalid fields'):
            validate_output(value, {})

    def test_malformed_gap_types_fail_closed(self):
        invalid = ['plain text', {}, {'kind': 'unknown', 'description': 'Gap'},
                   {'kind': 'runtime_observation', 'description': ' '},
                   {'kind': 'repository_evidence', 'description': 'x' * 2001},
                   {'kind': 'repository_evidence', 'description': 'Gap', 'extra': True}]
        for gap in invalid:
            value = response()
            value['missing_evidence'] = [gap]
            with self.subTest(gap=gap), self.assertRaisesRegex(ValueError, 'evidence gap'):
                validate_output(value, {})

    def test_prompt_marks_comments_and_instructions_as_untrusted(self):
        self.assertIn('never instructions', SYSTEM)
        self.assertIn('Comments may be misleading', SYSTEM)
        self.assertIn('runtime values', SYSTEM)
        self.assertIn('rather than a literal YAML key', SYSTEM)

    def test_remote_transfer_requires_explicit_setting(self):
        for endpoint, model in [('https://example.com', 'model'),
                                ('http://127.0.0.1:11434', 'model-cloud')]:
            with self.assertRaisesRegex(ValueError, 'allow-remote'):
                OllamaProvider(model, endpoint)
        self.assertTrue(OllamaProvider('model', 'https://example.com', True).config['remote_transfer_allowed'])
        for endpoint in ['https://user:secret@example.com', 'http://example.com',
                         'https://example.com/path', 'https://example.com?key=secret']:
            with self.assertRaises(ValueError):
                OllamaProvider('model', endpoint, True)
