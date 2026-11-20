import io
import json
import unittest
from unittest.mock import patch, Mock
from unity_rag.generation import OllamaProvider


class GenerationControlsTests(unittest.TestCase):
    def generate(self, provider, envelope):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(envelope).encode())
        with patch('unity_rag.generation.build_opener', return_value=opener):
            result = provider.generate([], {'type': 'object'})
        request = opener.open.call_args.args[0]
        return result, json.loads(request.data)

    def test_thinking_and_budget_are_sent_and_recorded(self):
        provider = OllamaProvider('local-model', thinking=True, max_output_tokens=8192)
        result, body = self.generate(provider, {'message': {'content': '{"ok": true}',
                                                        'thinking': 'Not part of the answer.'}})
        self.assertEqual(result, {'ok': True})
        self.assertTrue(body['think'])
        self.assertEqual(body['options']['seed'], 42)
        self.assertEqual(provider.config['seed'], 42)
        self.assertEqual(body['options']['num_predict'], 8192)
        self.assertEqual(provider.config['max_output_tokens'], 8192)
        self.assertTrue(provider.config['thinking'])

    def test_auto_omits_thinking_and_false_is_explicit(self):
        for thinking in (None, False):
            _, body = self.generate(OllamaProvider('local-model', thinking=thinking),
                                    {'message': {'content': '{}'}})
            if thinking is None:
                self.assertNotIn('think', body)
            else:
                self.assertIs(body['think'], False)
            self.assertEqual(body['options']['num_predict'], 2048)

    def test_invalid_controls_are_rejected(self):
        for budget in (True, 255, 16385, 1024.5, '8192'):
            with self.assertRaises(ValueError):
                OllamaProvider('local-model', max_output_tokens=budget)
        for thinking in (1, 'true', [], {}):
            with self.assertRaises(ValueError):
                OllamaProvider('local-model', thinking=thinking)

    def test_truncated_output_is_rejected_even_when_json_parses(self):
        with self.assertRaisesRegex(ValueError, 'Thinking can consume'):
            self.generate(OllamaProvider('local-model'),
                          {'done_reason': 'length', 'message': {'content': '{}'}})

    def test_thinking_is_never_used_as_answer_fallback(self):
        with self.assertRaisesRegex(ValueError, 'malformed JSON'):
            self.generate(OllamaProvider('local-model'),
                          {'message': {'content': '', 'thinking': '{"ok": true}'}})

    def test_seed_validation_and_explicit_value(self):
        for seed in (True, -1, 2147483648, '42', None):
            with self.assertRaises(ValueError):
                OllamaProvider('local-model', seed=seed)
        _, body = self.generate(OllamaProvider('local-model', seed=123), {'message': {'content': '{}'}})
        self.assertEqual(body['options']['seed'], 123)
