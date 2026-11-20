"""Sensitive discovery paths must be rejected before any content access."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unity_rag.discovery import scan


class SensitiveDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Resolve Windows short temp paths so interception compares the same
        # canonical paths as discovery.scan().
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "Game"
        self.workspace = self.base / "Knowledge"
        (self.project / "Assets").mkdir(parents=True)
        (self.project / "ProjectSettings").mkdir()
        self.config = self.base / "discovery.json"

    def write(self, relative, text):
        target = self.project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def guarded_scan(self, *, protected_files=(), protected_directories=(), include=None):
        self.config.write_text(json.dumps({
            "include": include or ["Assets", "ProjectSettings"], "exclude": []
        }), encoding="utf-8")
        forbidden_files = set(protected_files)
        forbidden_directories = set(protected_directories)
        original_open, original_iterdir = Path.open, Path.iterdir
        opened, enumerated = [], []

        def guarded_open(path, *args, **kwargs):
            if path in forbidden_files or any(parent in forbidden_directories for parent in path.parents):
                raise AssertionError("Excluded fixture content must never be opened")
            opened.append(path)
            return original_open(path, *args, **kwargs)

        def guarded_iterdir(path):
            if path in forbidden_directories or any(parent in forbidden_directories for parent in path.parents):
                raise AssertionError("Excluded fixture directories must never be enumerated")
            enumerated.append(path)
            return original_iterdir(path)

        original_hash = hashlib.sha256
        marker = b"SYNTHETIC_SENSITIVE_FIXTURE"

        def guarded_hash(data=b"", *args, **kwargs):
            if marker in data:
                raise AssertionError("Excluded fixture content must never be hashed")
            return original_hash(data, *args, **kwargs)

        with patch.object(Path, "open", guarded_open), patch.object(Path, "iterdir", guarded_iterdir), \
             patch("unity_rag.discovery.hashlib.sha256", guarded_hash), \
             patch("unity_rag.discovery.git_state", return_value={"available": False}):
            report = scan(self.project, self.workspace, self.config)
        return report, opened, enumerated

    def test_sensitive_files_are_never_opened_or_hashed_with_empty_excludes(self):
        names = [".env", ".env.local", ".ENV.Production", "production.env", "app.env.local",
                 "config.secret", "client.PEM", "private.key", "identity.p12", "identity.pfx",
                 "android.jks", "android.keystore", "credentials.json", "credential.txt",
                 "google-credentials.json", "api_secret.json", "secrets.yaml", "creds.json"]
        forbidden = [self.write("Assets/" + name, "SYNTHETIC_SENSITIVE_FIXTURE") for name in names]
        safe = self.write("Assets/KeyBindings.cs", "public class KeyBindings {}")
        self.write("Assets/SecretDoor.cs", "public class SecretDoor {}")
        report, opened, _ = self.guarded_scan(protected_files=forbidden)
        rows = {row["path"]: row for row in report["entries"]}
        for name in names:
            row = rows["Assets/" + name]
            self.assertEqual((row["status"], row["reason"]), ("excluded", "sensitive_path"))
            self.assertIsNone(row["size"])
            self.assertIsNone(row["sha256"])
        self.assertIn(safe, opened)
        self.assertEqual(rows["Assets/KeyBindings.cs"]["status"], "supported")
        self.assertEqual(rows["Assets/SecretDoor.cs"]["status"], "supported")

    def test_sensitive_and_hidden_directories_are_never_entered(self):
        names = [".env", ".private", "Secrets", "Credentials", "creds", "api-secrets"]
        directories = []
        for name in names:
            self.write("Assets/" + name + "/Nested/ordinary.json", "SYNTHETIC_SENSITIVE_FIXTURE")
            directories.append(self.project / "Assets" / name)
        report, _, enumerated = self.guarded_scan(protected_directories=directories)
        rows = {row["path"]: row for row in report["entries"]}
        for name, path in zip(names, directories):
            self.assertEqual(rows["Assets/" + name]["reason"], "sensitive_path")
            self.assertNotIn(path, enumerated)
            self.assertFalse(any(key.startswith("Assets/" + name + "/") for key in rows))

    def test_explicit_include_cannot_bypass_sensitive_ancestor(self):
        for name in [".env", ".hidden", "Secrets", "Credentials", "creds", "private.key"]:
            with self.subTest(name=name):
                path = self.write("Assets/" + name + "/Nested/config.json", "SYNTHETIC_SENSITIVE_FIXTURE")
                relative = "Assets/" + name + "/Nested"
                report, _, _ = self.guarded_scan(
                    protected_files=[path], protected_directories=[path.parent.parent], include=[relative])
                self.assertEqual(len(report["entries"]), 1)
                row = report["entries"][0]
                self.assertEqual((row["path"], row["reason"]), (relative, "excluded_ancestor"))
                self.assertIsNone(row["sha256"])

    def test_native_hidden_directory_and_ancestor_are_not_entered(self):
        hidden = self.project / "Assets" / "PrivateData"
        self.write("Assets/PrivateData/Nested/ordinary.json", "SYNTHETIC_SENSITIVE_FIXTURE")
        original_lstat = Path.lstat

        def hidden_attributes(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path == hidden:
                return SimpleNamespace(st_mode=info.st_mode,
                                       st_file_attributes=getattr(info, "st_file_attributes", 0) | 0x2)
            return info

        with patch.object(Path, "lstat", hidden_attributes):
            report, _, _ = self.guarded_scan(protected_directories=[hidden])
            row = next(item for item in report["entries"] if item["path"] == "Assets/PrivateData")
            self.assertEqual(row["reason"], "hidden_directory")
            report, _, _ = self.guarded_scan(protected_directories=[hidden],
                                            include=["Assets/PrivateData/Nested"])
            self.assertEqual(len(report["entries"]), 1)
            self.assertEqual(report["entries"][0]["reason"], "excluded_ancestor")

    def test_default_env_reason_stays_compatible(self):
        secret = self.write("Assets/.env", "SYNTHETIC_SENSITIVE_FIXTURE")
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path == secret:
                raise AssertionError("Excluded fixture content must never be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open), \
             patch("unity_rag.discovery.git_state", return_value={"available": False}):
            report = scan(self.project, self.workspace)
        row = next(item for item in report["entries"] if item["path"] == "Assets/.env")
        self.assertEqual(row["reason"], "configured_exclusion")


if __name__ == "__main__":
    unittest.main()
