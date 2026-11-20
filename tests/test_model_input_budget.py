import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from unity_rag.context_packing import InputBudget, fit_record
from unity_rag.answers import ask
from unity_rag.workspace import refresh


class InputBudgetTests(unittest.TestCase):
    def test_counts_prompt_schema_framing_and_output_before_provider(self):
        calls = []
        class Provider:
            def generate(self, messages, schema):
                calls.append(messages)
                return {}
        messages, schema = [{'role': 'user', 'content': 'hello'}], {'type': 'object'}
        budget = InputBudget(1000, 200, len)
        budget.generate(Provider(), messages, schema)
        self.assertEqual(len(calls), 1)
        self.assertGreater(budget.report()['peak_estimated_input_tokens'], len('hello'))
        with self.assertRaisesRegex(ValueError, 'no request sent'):
            budget.generate(Provider(), [{'role': 'user', 'content': 'x' * 800}], schema)
        self.assertEqual(len(calls), 1)

    def test_extra_input_limit_applies_while_selecting_a_middle_window(self):
        lines = ['setup'] * 80 + ['decisiveCoolingFormula'] + ['tail'] * 10
        record = {'text': '\n'.join(lines), 'evidence_type': 'original_source',
                  'sources': [{'path': 'Assets/Long.cs', 'start_line': 1, 'end_line': len(lines)}]}
        packed, clipped = fit_record({}, 'E1', record, 10000, len,
            query='decisiveCoolingFormula', extra_fits=lambda evidence: len(json.dumps(evidence)) <= 400)
        self.assertTrue(clipped)
        self.assertIn('decisiveCoolingFormula', packed['text'])
        self.assertLessEqual(len(json.dumps({'E1': packed})), 400)


@unittest.skipUnless(shutil.which('dotnet') and importlib.util.find_spec('tiktoken'), 'Requires .NET SDK and optional tokenizer')
class ModelInputIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.project, cls.work = root/'project', root/'workspace'
        shutil.copytree(Path(__file__).parent/'fixtures/passage_project', cls.project)
        refresh(cls.work, cls.project, semantic=False, chunk_budget=8192)

    def provider(self):
        class Provider:
            config = {'max_output_tokens': 256, 'context_tokens': 4096}
            def __init__(self):
                self.calls = []
            def generate(self, messages, schema):
                self.calls.append((messages, schema))
                return {'claims': [], 'missing_evidence': [{'kind': 'repository_evidence',
                        'description': 'This scripted test checks context packing only.'}], 'followup_queries': []}
        return Provider()

    def test_real_tokenizer_fits_complete_request_and_keeps_decisive_lines(self):
        provider = self.provider()
        answer = ask(self.work, 'How does ThermalController AdvanceHeat reduce heat using CoolingPerSecond?',
                     provider, mode='search', limit=1, expand=0, followups=0,
                     context_tokens=3500, token_encoding='cl100k_base', model_context_tokens=2200)
        self.assertEqual(len(provider.calls), 1)
        budget = answer['context_budget']['model_input']
        self.assertLessEqual(budget['peak_estimated_input_tokens'] + budget['reserved_output_tokens'], 2200)
        self.assertIn('CoolingPerSecond * elapsed', '\n'.join(r['text'] for r in answer['evidence'].values()))

    def test_impossible_budget_and_wrong_units_fail_without_generation(self):
        provider = self.provider()
        with self.assertRaisesRegex(ValueError, 'cannot fit'):
            ask(self.work, 'AdvanceHeat', provider, mode='search', context_tokens=1000,
                token_encoding='cl100k_base', model_context_tokens=100)
        with self.assertRaisesRegex(ValueError, 'tiktoken encoding'):
            ask(self.work, 'AdvanceHeat', provider, mode='search', context_tokens=1000, model_context_tokens=4096)
        self.assertEqual(provider.calls, [])
