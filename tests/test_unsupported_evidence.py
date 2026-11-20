import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from unity_rag.answers import ask, verify_sources
from unity_rag.asset_map import map_assets
from unity_rag.code_map import map_code
from unity_rag.discovery import scan, status
from unity_rag.retrieval import build_retrieval, retrieve


class ScriptedProvider:
    config = {'provider': 'fixture'}

    def __init__(self):
        self.calls = 0

    def generate(self, messages, schema):
        self.calls += 1
        evidence = json.loads(messages[1]['content'])['evidence']
        key = next(iter(evidence))
        return {'claims': [{'kind': 'fact', 'text': 'A test claim.', 'evidence_ids': [key]}],
                'missing_evidence': [], 'followup_queries': []}


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class UnsupportedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project, self.work = root / 'Game', root / 'Knowledge'
        shutil.copytree(Path(__file__).parent / 'fixtures/retrieval_project', self.project)
        self.binary = self.project / 'Assets/BinaryLighting.asset'
        self.binary.write_bytes(b'\x00\xff\xa1binary asset')
        scan(self.project, self.work)
        map_code(self.work)
        map_assets(self.work)
        build_retrieval(self.work)
        inventory = status(self.work)
        entry = next(row for row in inventory['entries'] if row['path'] == 'Assets/BinaryLighting.asset')
        self.assertNotEqual(entry['status'], 'supported')
        self.record = {'id': 'binary-fixture', 'kind': 'asset', 'entity_id': 'asset:Assets/BinaryLighting.asset',
                       'title': 'Binary lighting', 'text': 'Binary asset', 'evidence_type': 'asset',
                       'sources': [{'path': entry['path'], 'sha256': entry['sha256'],
                                    'start_line': None, 'end_line': None}]}
        self.result = retrieve(self.work, 'CanAttack', mode='exact', limit=5)

    def test_unsupported_binary_is_omitted_and_disclosed(self):
        self.result['results'].append(self.record)
        provider = ScriptedProvider()
        with patch('unity_rag.answers.retrieve', return_value=self.result):
            answer = ask(self.work, 'CanAttack', provider, mode='exact', followups=0)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(answer['status'], 'partial')
        self.assertEqual(answer['context_budget']['unsupported_records_omitted'], 1)
        self.assertTrue(any('unsupported source files' in gap for gap in answer['missing_evidence']))
        self.assertFalse(any(s['path'] == 'Assets/BinaryLighting.asset'
                             for row in answer['evidence'].values() for s in row['sources']))

    def test_only_unsupported_evidence_does_not_call_provider(self):
        self.result['results'] = [self.record]
        self.result['context'] = []
        provider = ScriptedProvider()
        with patch('unity_rag.answers.retrieve', return_value=self.result):
            answer = ask(self.work, 'CanAttack', provider, mode='exact', followups=0)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(answer['status'], 'insufficient_evidence')
        self.assertEqual(answer['context_budget']['unsupported_records_omitted'], 1)

    def test_extractive_mode_discloses_omitted_binary(self):
        self.result['results'].append(self.record)
        with patch('unity_rag.answers.retrieve', return_value=self.result):
            answer = ask(self.work, 'CanAttack', mode='exact', style='extractive', followups=0)
        self.assertEqual(answer['context_budget']['unsupported_records_omitted'], 1)
        self.assertTrue(any('unsupported source files' in gap for gap in answer['missing_evidence']))

    def test_direct_verification_rejects_unsupported_source(self):
        with self.assertRaisesRegex(ValueError, 'unsupported snapshot source'):
            verify_sources(status(self.work), [self.record])

    def test_stale_supported_source_still_blocks_model(self):
        self.result['results'].append(self.record)
        source = self.project / 'Assets/Enemy.cs'
        source.write_bytes(source.read_bytes() + b'\n// modified')
        provider = ScriptedProvider()
        with patch('unity_rag.answers.retrieve', return_value=self.result):
            with self.assertRaisesRegex(ValueError, 'changed since discovery'):
                ask(self.work, 'CanAttack', provider, mode='exact', followups=0)
        self.assertEqual(provider.calls, 0)


if __name__ == '__main__':
    unittest.main()
