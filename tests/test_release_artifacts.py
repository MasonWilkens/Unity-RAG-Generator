"""Release audit rejects incomplete, stale and unintended wheel contents."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('verify_distribution',
    Path(__file__).resolve().parents[1] / 'scripts/verify_distribution.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = {'unity_rag/__init__.py': b'__version__ = "0.1.0"\n'}
        self.files.update({name: b'synthetic analyzer input' for name in release.ROSLYN})
        for name, data in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.files['unity_rag-0.1.0.dist-info/METADATA'] = (
            b'Metadata-Version: 2.4\nName: unity-rag\nVersion: 0.1.0\n'
            b'License-Expression: MIT\nRequires-Python: >=3.10\n')

    def artifact(self):
        path = self.root / 'unity_rag-0.1.0-py3-none-any.whl'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in self.files.items():
                archive.writestr(name, data)
        return path

    def test_missing_analyzer_is_rejected_before_install(self):
        del self.files['unity_rag/roslyn/Program.cs']
        with self.assertRaisesRegex(RuntimeError, 'Artifact is missing'):
            release.audit(self.artifact(), self.root)

    def test_credential_or_cache_path_is_rejected(self):
        for name in ('unity_rag/.env/credentials', '.unity-rag/index.sqlite3', '../outside.py'):
            with self.subTest(name=name):
                self.files[name] = b'synthetic sentinel; no credential'
                with self.assertRaisesRegex(RuntimeError, 'Unexpected release content|Invalid archive path'):
                    release.audit(self.artifact(), self.root)
                del self.files[name]

    def test_stale_module_is_rejected(self):
        self.files['unity_rag/__init__.py'] += b'# stale build\n'
        with self.assertRaisesRegex(RuntimeError, 'stale source'):
            release.audit(self.artifact(), self.root)

    def test_current_module_and_analyzer_hashes_are_accepted(self):
        identity = release.audit(self.artifact(), self.root)
        self.assertEqual(identity['version'], '0.1.0')
        self.assertEqual(len(identity['sha256']), 64)
