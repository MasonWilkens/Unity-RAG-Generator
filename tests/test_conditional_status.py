"""Human-readable code status does not expose source content."""
import unittest
from unity_rag.cli import format_code_status


class ConditionalStatusTests(unittest.TestCase):
    def report(self):
        return {'files': [{}, {}], 'declarations': [], 'edges': [], 'diagnostics': [],
                'mapping_diagnostics': [], 'semantic_scope': 'Static mapping only.',
                'defines': ['UNITY_IOS'], 'conditions': [
                    {'kind': 'IfDirectiveTrivia', 'path': 'Assets/A.cs', 'text': '#if UNITY_IOS'},
                    {'kind': 'DisabledTextTrivia', 'path': 'Assets/A.cs', 'text': 'PRIVATE_MARKER'},
                    {'kind': 'DisabledTextTrivia', 'path': 'Assets/A.cs', 'text': 'OTHER_MARKER'},
                    {'kind': 'DisabledTextTrivia', 'path': 'Assets/B.cs', 'text': 'PRIVATE_MARKER'}]}

    def test_counts_regions_and_files_without_source_contents(self):
        output = format_code_status(self.report())
        self.assertIn('Supplied defines: UNITY_IOS.', output)
        self.assertIn('3 region(s) across 2 file(s)', output)
        self.assertNotIn('PRIVATE_MARKER', output)
        self.assertNotIn('Assets/A.cs', output)

    def test_empty_defines_and_conditions(self):
        report = self.report()
        report['defines'], report['conditions'] = [], []
        output = format_code_status(report)
        self.assertIn('Supplied defines: (none).', output)
        self.assertIn('0 region(s) across 0 file(s)', output)

    def test_missing_or_invalid_metadata_is_unavailable_not_zero(self):
        report = self.report(); del report['conditions']; del report['defines']
        output = format_code_status(report)
        self.assertIn('Supplied defines: (unavailable).', output)
        self.assertIn('Inactive preprocessor text: unavailable', output)
        report = self.report(); report['conditions'] = ['bad']; report['defines'] = [1]
        output = format_code_status(report)
        self.assertIn('Supplied defines: (unavailable).', output)
        self.assertIn('Inactive preprocessor text: unavailable', output)
