import hashlib
from pathlib import Path
import tempfile
import unittest
from unity_rag.extractive import extract_excerpts

class ExtractiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root/'Example.cs'
        self.configure(b'// Adds health.\r\nint Apply(int hp, int damage) {\r\n    return hp - damage;\r\n}\r\n')
    def configure(self, raw):
        self.raw = raw
        self.path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.inventory = {'project': str(self.root), 'entries': [{'path': 'Example.cs', 'sha256': digest, 'size': len(raw)}]}
        self.evidence = {'E1': {'sources': [{'path': 'Example.cs', 'sha256': digest, 'start_line': 1, 'end_line': len(raw.splitlines())}]}}
    def tearDown(self):
        self.temp.cleanup()
    def test_copies_source_without_generating_text(self):
        excerpts, gaps, diagnostic = extract_excerpts(self.inventory, self.evidence, 24000)
        self.assertEqual(excerpts[0]['text'], self.raw.decode().replace('\r\n','\n').rstrip('\n'))
        self.assertEqual(excerpts[0]['sha256'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(gaps, [])
        self.assertEqual(diagnostic['model_calls'], 0)
    def test_long_sources_are_bounded(self):
        self.configure(('\n'.join(f'int field{i};' for i in range(150))).encode())
        excerpts, gaps, diagnostic = extract_excerpts(self.inventory, self.evidence, 24000)
        self.assertEqual(len(excerpts), 4)
        self.assertTrue(all(e['end_line']-e['start_line'] < 20 for e in excerpts))
        self.assertEqual(excerpts[0]['start_line'], 1)
        self.assertEqual(excerpts[-1]['end_line'], 80)
        self.assertIn('retrieval_incomplete', gaps)
    def test_duplicates_and_budget(self):
        self.evidence['E2'] = self.evidence['E1']
        self.assertEqual(len(extract_excerpts(self.inventory, self.evidence, 24000)[0]), 1)
        excerpts, gaps, diagnostic = extract_excerpts(self.inventory, self.evidence, 2)
        self.assertFalse(excerpts)
        self.assertLessEqual(diagnostic['input_chars'], 2)
        self.assertIn('retrieval_incomplete', gaps)
    def test_missing_line_ranges(self):
        self.evidence['E1']['sources'][0].update(start_line=None, end_line=None)
        excerpts, gaps, diagnostic = extract_excerpts(self.inventory, self.evidence, 1000)
        self.assertEqual(excerpts, [])
        self.assertEqual(diagnostic['model_calls'], 0)
    def test_changed_source_and_invalid_ranges(self):
        self.path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            extract_excerpts(self.inventory, self.evidence, 24000)
        self.path.write_bytes(self.raw)
        self.evidence['E1']['sources'][0]['end_line'] = 1000
        with self.assertRaisesRegex(ValueError, 'range'):
            extract_excerpts(self.inventory, self.evidence, 24000)
