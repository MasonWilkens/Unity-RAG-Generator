import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unity_rag.source_checks import inspect_source, normalize_quote
from unity_rag.retrieval import format_results


class SourceCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / 'Assets' / 'Code.cs'
        self.path.parent.mkdir()
        self.path.write_text('line one\nunique target\nline three\n', encoding='utf-8')
        raw = self.path.read_bytes()
        self.entry = {'path': 'Assets/Code.cs', 'status': 'supported', 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        self.entries = {self.entry['path']: self.entry}
        self.cite = {'path': self.entry['path'], 'sha256': self.entry['sha256'], 'start_line': 1, 'end_line': 3}

    def test_current_stale_missing_and_snapshot_mismatch(self):
        self.assertEqual(inspect_source(self.root, self.entries, self.cite)['status'], 'current')
        self.assertEqual(inspect_source(self.root, self.entries, {**self.cite, 'sha256': 'bad'})['status'], 'snapshot_mismatch')
        self.path.write_text('changed', encoding='utf-8')
        self.assertEqual(inspect_source(self.root, self.entries, self.cite)['status'], 'stale')
        self.path.unlink()
        self.assertEqual(inspect_source(self.root, self.entries, self.cite)['status'], 'missing')

    def test_sensitive_path_is_excluded_before_access(self):
        with patch.object(Path, 'open', side_effect=AssertionError('secret read')):
            self.assertEqual(inspect_source(self.root, {}, {'path':'Assets/.env/creds.secret'})['status'], 'excluded')

    def test_default_text_output_surfaces_stale_citation(self):
        row={'sources':[self.cite], 'title':'example', 'evidence_type':'code', 'text':'example'}
        result={'results':[row], 'context':[], 'source_freshness':[{'path':self.cite['path'],'status':'stale'}]}
        self.assertIn('source: stale', format_results(result))

    def test_exact_quote_unique_and_no_automatic_acceptance(self):
        proposal = normalize_quote(self.root, self.entries, self.cite, 'unique target')
        self.assertEqual((proposal['citation']['start_line'], proposal['citation']['end_line']), (2, 2))
        self.assertFalse(proposal['automatic_acceptance'])
        self.assertEqual(normalize_quote(self.root, self.entries, self.cite, 'line')['status'], 'ambiguous')
        self.assertEqual(normalize_quote(self.root, self.entries, self.cite, 'not present')['status'], 'not_found')
        self.path.write_text('changed', encoding='utf-8')
        self.assertEqual(normalize_quote(self.root, self.entries, self.cite, 'unique target')['status'], 'stale')
