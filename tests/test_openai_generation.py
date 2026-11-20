"""OpenAI transport and spend controls; all credentials and HTTP are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from unity_rag import openai_generation as api


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 13)


SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
          'required': ['ok'], 'additionalProperties': False}
MESSAGES = [{'role': 'user', 'content': 'Return ok=true.'}]


def completed(**updates):
    result = {'status': 'completed', 'service_tier': 'flex', 'model': api.MODEL,
              'usage': {'input_tokens': 100, 'output_tokens': 20},
              'output': [{'type': 'message', 'content': [
                  {'type': 'output_text', 'text': '{"ok": true}'}]}]}
    result.update(updates)
    return result


class OpenAIProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = 'sk-' + 'synthetic_test_only_' * 3
        self.keyfile = self.root / 'synthetic.secret'
        self.keyfile.write_text(self.key, encoding='utf-8')
        self.date_patch = patch.object(api, 'date', FixedDate)
        self.date_patch.start()
        self.addCleanup(self.date_patch.stop)
        self.opener = Mock()
        self.opener_patch = patch.object(api, 'build_opener', return_value=self.opener)
        self.factory = self.opener_patch.start()
        self.addCleanup(self.opener_patch.stop)
        self.reply(completed())
        self.serial = 0

    def reply(self, envelope):
        self.opener.open.side_effect = None
        self.opener.open.return_value = io.BytesIO(json.dumps(envelope).encode())

    def provider(self, **kwargs):
        self.serial += 1
        options = dict(api_key_file=self.keyfile,
                       ledger_path=self.root / f'spend-{self.serial}.sqlite3',
                       budget_usd='0.25', allow_remote=True)
        options.update(kwargs)
        return api.OpenAIProvider(**options)

    def assert_no_request(self, provider):
        self.opener.open.assert_not_called()
        self.assertEqual(provider.ledger.summary()['attempts'], 0)

    def test_official_transport_schema_usage_and_redacted_metadata(self):
        provider = self.provider(max_calls=1)
        self.assertEqual(provider.generate(MESSAGES, SCHEMA), {'ok': True})
        request = self.opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, api.ENDPOINT)
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.key)
        self.assertFalse(body['store'])
        self.assertEqual(body['model'], api.MODEL)
        self.assertEqual(body['service_tier'], 'flex')
        self.assertEqual(body['prompt_cache_options'], {'mode': 'implicit', 'ttl': '30m'})
        self.assertEqual(body['reasoning'], {'effort': 'none'})
        self.assertEqual(body['max_output_tokens'], 2048)
        self.assertEqual(body['text']['format']['schema'], SCHEMA)
        self.assertTrue(body['text']['format']['strict'])
        self.assertNotIn('tools', body)
        self.assertNotIn('previous_response_id', body)
        handlers = self.factory.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], api.NoRedirect)
        self.assertEqual(provider.last_usage['input_tokens'], 100)
        self.assertEqual(provider.last_usage['output_tokens'], 20)
        self.assertEqual(provider.ledger.summary()['measured_responses'], 1)
        self.assertNotIn(self.key, json.dumps(provider.config))
        self.assertNotIn(str(self.keyfile), json.dumps(provider.config))
        self.assertEqual(self.keyfile.read_text(), self.key)

    def test_call_limit_blocks_before_second_request(self):
        provider = self.provider(max_calls=1)
        provider.generate(MESSAGES, SCHEMA)
        with self.assertRaisesRegex(ValueError, 'call limit'):
            provider.generate(MESSAGES, SCHEMA)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_standard_tier_price_reservation_and_measured_metadata(self):
        provider = self.provider(service_tier='default', budget_usd='1', max_output_tokens=8192)
        self.reply(completed(service_tier='default'))
        provider.generate(MESSAGES, SCHEMA)
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(body['service_tier'], 'default')
        self.assertEqual(provider.config['service_tier'], 'default')
        self.assertEqual(provider.ledger.summary()['reserved_upper_usd'], 0.268644)
        self.assertEqual(provider.last_usage['estimated_cost_upper_usd'], 0.00004)
        self.assertEqual(provider.last_usage['attempt_id'], 1)
        self.assertEqual(provider.last_usage['model'], api.MODEL)
        self.assertNotIn('estimated_flex_cost_upper_usd', provider.last_usage)
        self.reply(completed(service_tier='default'))
        provider.generate(MESSAGES, SCHEMA)
        self.assertEqual(provider.ledger.summary()['reserved_upper_usd'], 0.537288)

    def test_standard_rejects_wrong_tier_and_unverified_model(self):
        for update in ({'service_tier': 'flex'}, {'model': None}, {'model': 'other-model'},
                       {'model': api.MODEL + '-invalid-suffix'}):
            with self.subTest(update=update):
                provider = self.provider(service_tier='default', budget_usd='1')
                envelope = completed(service_tier='default')
                envelope.update(update)
                self.reply(envelope)
                with self.assertRaisesRegex(ValueError, 'tier|model'):
                    provider.generate(MESSAGES, SCHEMA)
                self.assertTrue(provider.ledger.summary()['blocked'])
                self.assertEqual(provider.ledger.summary()['measured_responses'], 0)

    def test_timeout_blocks_reopened_provider_before_another_request(self):
        provider = self.provider(budget_usd='1')
        self.opener.open.side_effect = TimeoutError('private')
        with self.assertRaisesRegex(ValueError, 'outcome uncertain'):
            provider.generate(MESSAGES, SCHEMA)
        self.assertTrue(provider.ledger.summary()['blocked'])
        reopened = self.provider(ledger_path=provider.ledger.path, budget_usd='1')
        with self.assertRaisesRegex(ValueError, 'ledger blocked'):
            reopened.generate(MESSAGES, SCHEMA)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_reservation_precedes_network_and_survives_http_error(self):
        provider = self.provider()
        def denied(*args, **kwargs):
            self.assertEqual(provider.ledger.summary()['attempts'], 1)
            raise HTTPError(api.ENDPOINT, 401, 'private detail', {}, io.BytesIO(self.key.encode()))
        self.opener.open.side_effect = denied
        with self.assertRaisesRegex(ValueError, 'HTTP 401') as caught:
            provider.generate(MESSAGES, SCHEMA)
        self.assertNotIn(self.key, str(caught.exception))
        self.assertNotIn('private detail', str(caught.exception))
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertGreater(provider.ledger.summary()['reserved_upper_usd'], 0)
        self.assertEqual(provider.ledger.summary()['measured_responses'], 0)

    def test_timeout_and_network_failure_do_not_retry_or_refund(self):
        for error in (TimeoutError('private'), URLError('private')):
            with self.subTest(error=type(error).__name__):
                self.opener.open.reset_mock()
                provider = self.provider()
                self.opener.open.side_effect = error
                with self.assertRaisesRegex(ValueError, 'outcome uncertain'):
                    provider.generate(MESSAGES, SCHEMA)
                self.assertEqual(self.opener.open.call_count, 1)
                self.assertEqual(provider.ledger.summary()['attempts'], 1)
                self.assertGreater(provider.ledger.summary()['reserved_upper_usd'], 0)

    def test_missing_or_out_of_bound_usage_blocks_ledger(self):
        usages = ({}, {'input_tokens': -1, 'output_tokens': 0},
                  {'input_tokens': api.MAX_INPUT_TOKENS + 1, 'output_tokens': 1},
                  {'input_tokens': 2, 'output_tokens': 2049},
                  {'input_tokens': True, 'output_tokens': 1})
        for usage in usages:
            with self.subTest(usage=usage):
                provider = self.provider()
                self.reply(completed(usage=usage))
                with self.assertRaisesRegex(ValueError, 'ledger blocked'):
                    provider.generate(MESSAGES, SCHEMA)
                self.assertTrue(provider.ledger.summary()['blocked'])
                with self.assertRaisesRegex(ValueError, 'ledger blocked'):
                    provider.ledger.reserve(1)

    def test_unconfirmed_flex_tier_blocks_ledger_without_fallback(self):
        for tier in (None, 'default', 'priority'):
            with self.subTest(tier=tier):
                self.opener.open.reset_mock()
                provider = self.provider()
                self.reply(completed(service_tier=tier))
                with self.assertRaisesRegex(ValueError, 'tier|Flex|flex'):
                    provider.generate(MESSAGES, SCHEMA)
                self.assertTrue(provider.ledger.summary()['blocked'])
                self.assertEqual(self.opener.open.call_count, 1)

    def test_single_full_context_reservation_stays_below_validation_cap(self):
        provider = self.provider(max_output_tokens=2048)
        provider.generate(MESSAGES, SCHEMA)
        expected = (api.MAX_INPUT_TOKENS * api.INPUT_NANODOLLARS
                    + provider.max_output_tokens * api.OUTPUT_NANODOLLARS) / 1e9
        self.assertEqual(api.MAX_INPUT_TOKENS, 1_050_000)
        self.assertEqual(provider.ledger.summary()['reserved_upper_usd'], expected)
        self.assertLess(expected, 0.25)

    def test_incomplete_and_refusal_fail_without_retry_but_record_usage(self):
        envelopes = [completed(status='incomplete'),
                     completed(output=[{'type': 'message', 'content': [
                         {'type': 'refusal', 'refusal': 'private response'}]}])]
        for envelope in envelopes:
            with self.subTest(status=envelope['status']):
                self.opener.open.reset_mock()
                provider = self.provider()
                self.reply(envelope)
                with self.assertRaises(ValueError) as caught:
                    provider.generate(MESSAGES, SCHEMA)
                self.assertNotIn('private response', str(caught.exception))
                self.assertEqual(self.opener.open.call_count, 1)
                self.assertEqual(provider.ledger.summary()['measured_responses'], 1)

    def test_malformed_response_is_suppressed_and_reserved(self):
        for raw in (b'not json', b'[]', json.dumps(completed(output=[])).encode()):
            with self.subTest(raw_type=len(raw)):
                provider = self.provider()
                self.opener.open.return_value = io.BytesIO(raw)
                with self.assertRaisesRegex(ValueError, 'malformed output'):
                    provider.generate(MESSAGES, SCHEMA)
                self.assertEqual(provider.ledger.summary()['attempts'], 1)

    def test_bad_provider_configuration_prevents_network(self):
        bad = [dict(model='unknown-model'), dict(allow_remote=False),
               dict(timeout=0), dict(timeout=True), dict(timeout=121),
               dict(max_output_tokens=255), dict(max_output_tokens=8193),
               dict(max_output_tokens=True), dict(max_calls=0), dict(max_calls=True),
               dict(api_key_file=None), dict(ledger_path=None),
               dict(service_tier='priority'), dict(service_tier=[])]
        for options in bad:
            with self.subTest(options=list(options)):
                with self.assertRaises(ValueError):
                    self.provider(**options)
        self.opener.open.assert_not_called()

    def test_oversized_body_and_multibyte_body_rejected_before_credentials(self):
        provider = self.provider()
        with patch.object(api, 'load_credential') as loader:
            for text in ('x' * api.MAX_REQUEST_BYTES, '\u754c' * 24000):
                with self.assertRaisesRegex(ValueError, '64 KiB'):
                    provider.generate([{'role': 'user', 'content': text}], SCHEMA)
            loader.assert_not_called()
        self.assert_no_request(provider)

    def test_embedded_credentials_are_rejected_before_network(self):
        provider = self.provider()
        for text in (self.key, '-----BEGIN PRIVATE KEY-----'):
            with self.assertRaisesRegex(ValueError, 'credential material'):
                provider.generate([{'role': 'user', 'content': text}], SCHEMA)
        self.assert_no_request(provider)

    def test_nontext_inputs_and_implicit_context_are_rejected(self):
        provider = self.provider()
        bad = [[], MESSAGES * 9,
               [{'role': 'user', 'content': [{'type': 'input_file', 'file_id': 'file-123'}]}],
               [{'role': 'user', 'content': 'text', 'previous_response_id': 'response-123'}],
               [{'role': 'tool', 'content': 'text'}],
               [{'role': 'user', 'content': 'text', 'tools': []}]]
        for messages in bad:
            with self.assertRaises(ValueError):
                provider.generate(messages, SCHEMA)
        self.assert_no_request(provider)

    def test_stale_pricing_stops_before_request(self):
        provider = self.provider()
        with patch.object(api, 'PRICING_VALID_THROUGH', '2026-10-31'):
            with self.assertRaisesRegex(ValueError, 'Reverify'):
                provider.generate(MESSAGES, SCHEMA)
        self.assert_no_request(provider)

    def test_insufficient_budget_stops_before_request(self):
        provider = self.provider(budget_usd='0.001')
        with self.assertRaisesRegex(ValueError, 'spending limit'):
            provider.generate(MESSAGES, SCHEMA)
        self.assert_no_request(provider)

    def test_credential_load_supports_plain_assignment_and_json_without_writes(self):
        for source in (self.key, 'OPENAI_API_KEY="' + self.key + '"',
                       json.dumps({'api_key': self.key})):
            self.keyfile.write_text(source, encoding='utf-8')
            before = self.keyfile.read_bytes()
            self.assertEqual(api.load_credential(self.keyfile), self.key)
            self.assertEqual(self.keyfile.read_bytes(), before)

    def test_credential_load_rejects_missing_multiple_and_large_files_safely(self):
        for source in ('missing key', self.key + '\n' + self.key + 'different', 'x' * 16385):
            self.keyfile.write_text(source, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'content suppressed') as caught:
                api.load_credential(self.keyfile)
            self.assertNotIn(self.key, str(caught.exception))
        with self.assertRaisesRegex(ValueError, 'content suppressed'):
            api.load_credential(self.root / 'absent.secret')

    def test_symlink_credential_component_is_rejected(self):
        original = Path.is_symlink
        def synthetic_link(path):
            return path == self.keyfile or original(path)
        with patch.object(Path, 'is_symlink', synthetic_link):
            with self.assertRaisesRegex(ValueError, 'content suppressed'):
                api.load_credential(self.keyfile)

    def test_provider_factory_does_not_load_credentials_for_extractive_mode(self):
        with patch.object(api, 'load_credential') as loader:
            self.assertIsNone(api.provider_from_args(SimpleNamespace(style='extractive')))
            loader.assert_not_called()

    def test_factory_preserves_ollama_defaults_and_explicit_controls(self):
        args = SimpleNamespace(style='explain', provider='ollama', model='local-model',
                               endpoint=None, allow_remote=False, timeout=120,
                               thinking='auto', max_output_tokens=2048, seed=42)
        with patch.object(api, 'load_credential') as loader:
            default = api.provider_from_args(args)
            self.assertEqual(default.config['provider'], 'ollama')
            self.assertEqual(default.config['endpoint'], 'http://127.0.0.1:11434')
            self.assertEqual(default.config['seed'], 42)
            self.assertIsNone(default.config['thinking'])
            self.assertEqual(default.config['max_output_tokens'], 2048)
            self.assertEqual(default.timeout, 120)
            args.endpoint = 'http://127.0.0.1:11435'
            args.seed = 123
            args.thinking = 'off'
            args.max_output_tokens = 4096
            args.timeout = 300
            configured = api.provider_from_args(args)
            self.assertEqual(configured.config['endpoint'], args.endpoint)
            self.assertEqual(configured.config['seed'], 123)
            self.assertFalse(configured.config['thinking'])
            self.assertEqual(configured.config['max_output_tokens'], 4096)
            self.assertEqual(configured.timeout, 300)
            loader.assert_not_called()
        self.opener.open.assert_not_called()

    def test_factory_routes_openai_options_and_rejects_ollama_controls(self):
        args = SimpleNamespace(style='explain', provider='openai', model=None,
                               endpoint=None, thinking='auto', api_key_file=self.keyfile,
                               spend_ledger=self.root / 'factory.sqlite3', budget_usd='0.25',
                               allow_remote=True, timeout=90, max_output_tokens=1024,
                               max_api_calls=1, seed=42)
        with patch.object(api, 'OpenAIProvider') as constructor:
            self.assertIs(api.provider_from_args(args), constructor.return_value)
            constructor.assert_called_once_with(
                api.MODEL, api_key_file=args.api_key_file, ledger_path=args.spend_ledger,
                budget_usd='0.25', allow_remote=True, timeout=90,
                max_output_tokens=1024, max_calls=1)
            constructor.reset_mock()
            for endpoint, thinking in (('https://other.example', 'auto'),
                                       (None, 'on'), (None, 'off')):
                args.endpoint, args.thinking = endpoint, thinking
                with self.assertRaisesRegex(ValueError, 'Ollama controls'):
                    api.provider_from_args(args)
            constructor.assert_not_called()
        self.opener.open.assert_not_called()


class SpendLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'budget.sqlite3'

    def test_cap_and_usage_persist_and_cannot_be_raised_on_reopen(self):
        ledger = api.SpendLedger(self.path, '0.25')
        ledger.reserve(200_000_000)
        reopened = api.SpendLedger(self.path, '0.25')
        self.assertEqual(reopened.summary()['reserved_upper_usd'], 0.2)
        with self.assertRaisesRegex(ValueError, 'spending limit'):
            reopened.reserve(50_000_001)
        with self.assertRaisesRegex(ValueError, 'fixed'):
            api.SpendLedger(self.path, '56')
        self.assertEqual(reopened.summary()['attempts'], 1)

    def test_atomic_reservations_cannot_overspend_concurrently(self):
        api.SpendLedger(self.path, '0.25')
        def reserve(_):
            ledger = api.SpendLedger(self.path, '0.25')
            try:
                ledger.reserve(100_000_000)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(8)))
        # One unmeasured in-flight attempt freezes other workers until settled.
        self.assertEqual(sum(results), 1)
        summary = api.SpendLedger(self.path, '0.25').summary()
        self.assertEqual(summary['reserved_upper_usd'], 0.1)
        self.assertEqual(summary['attempts'], 1)

    def test_crash_after_reservation_blocks_reopen_without_refund(self):
        ledger = api.SpendLedger(self.path, '1')
        ledger.reserve(100_000_000)
        reopened = api.SpendLedger(self.path, '1')
        self.assertTrue(reopened.summary()['blocked'])
        self.assertEqual(reopened.summary()['unmeasured_attempts'], 1)
        with self.assertRaisesRegex(ValueError, 'ledger blocked'):
            reopened.reserve(1)
        self.assertEqual(reopened.summary()['reserved_upper_usd'], 0.1)

    def test_measured_reservation_allows_next_call_but_never_refunds(self):
        ledger = api.SpendLedger(self.path, '0.25')
        first = ledger.reserve(100_000_000)
        ledger.record(first, 100, 20)
        second = ledger.reserve(100_000_000)
        ledger.record(second, 100, 20)
        self.assertFalse(ledger.summary()['blocked'])
        with self.assertRaisesRegex(ValueError, 'spending limit'):
            ledger.reserve(50_000_001)
        self.assertEqual(ledger.summary()['reserved_upper_usd'], 0.2)

    def test_usage_cannot_be_rewritten_or_invented(self):
        ledger = api.SpendLedger(self.path, '1')
        attempt = ledger.reserve(100_000_000)
        for inputs, outputs in ((True, 0), (-1, 0), (1, None)):
            with self.assertRaises(ValueError):
                ledger.record(attempt, inputs, outputs)
        ledger.record(attempt, 100, 20)
        with self.assertRaises(ValueError):
            ledger.record(attempt, 0, 0)
        with self.assertRaises(ValueError):
            ledger.record(999, 0, 0)
        self.assertEqual(ledger.summary()['input_tokens'], 100)

    def test_bad_budgets_are_rejected(self):
        for value in (0, -1, 57, True, 'nan', 'Infinity', '0.0000000001', None):
            with self.subTest(value=str(value)):
                with self.assertRaises(ValueError):
                    api.SpendLedger(self.path, value)

    def test_invalid_reservation_cannot_reduce_or_bypass_cap(self):
        ledger = api.SpendLedger(self.path, '0.25')
        for amount in (0, -1, True, 0.5, '100', None):
            with self.subTest(amount=str(amount)):
                with self.assertRaises(ValueError):
                    ledger.reserve(amount)
        self.assertEqual(ledger.summary()['attempts'], 0)
        self.assertEqual(ledger.summary()['reserved_upper_usd'], 0)


if __name__ == '__main__':
    unittest.main()
