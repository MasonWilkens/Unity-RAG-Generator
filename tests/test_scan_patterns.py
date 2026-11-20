import fnmatch
import unittest
from unity_rag.discovery import _compile_excludes


class ExclusionPatternTests(unittest.TestCase):
    def test_combined_patterns_equal_original_case_normalized_fnmatch(self):
        groups = [[], ['Library', 'Temp', '*.secret'], ['**/obj/*', '[a-z]*.txt', 'Assets/X?/*.CS'],
                  ['a[!b]c', 'x[[]1]', 'foo**bar', 'Docs/README.md']]
        values = ['library', 'assets/obj/a.cs', 'readme.TXT', 'Assets/X1/file.cs',
                  'abc', 'acc', 'x[1]', 'food/bar', 'docs/readme.MD', '.env/key']
        for patterns in groups:
            match = _compile_excludes(patterns)
            for value in values:
                expected = any(fnmatch.fnmatchcase(value.lower(), p.lower()) for p in patterns)
                self.assertEqual(bool(match(value.lower())), expected, (patterns, value))


if __name__ == '__main__':
    unittest.main()
