import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from unity_rag.discovery import scan, status


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "Game"
        self.workspace = self.base / "Knowledge"
        self.write("Assets/Player.cs", "public class Player {}")
        self.write("Assets/Player.cs.meta", "guid: 0123456789abcdef0123456789abcdef")
        self.write("Assets/Main.unity", "%YAML 1.1\n--- !u!1 &1\nGameObject: {}")
        self.write("Assets/Ogre.prefab", "%YAML 1.1\nGameObject: {}")
        self.write("Assets/Stats.asset", "%YAML 1.1\nMonoBehaviour: {}")
        self.write("Assets/Body.mat", "%YAML 1.1\nMaterial: {}")
        self.write("Assets/Effect.shader", 'Shader "Example" {}')
        self.write("Assets/Notes.md", "# Notes")
        self.write("Assets/Game.asmdef", '{"name":"Game"}')
        self.write("ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.0.1f1\n")
        self.write("Packages/manifest.json", '{"dependencies":{"com.example.local":"file:../../Local"}}')
        self.write("Packages/packages-lock.json", '{"dependencies":{}}')
        self.write("Library/generated.cs", "do not scan")
        self.write("Assets/.env", "SECRET=example")
        self.write("Assets/image.png", b"\x89PNG\0\xff")
        self.write("Assets/mystery.xyz", "unknown text")

    def write(self, path, content):
        target = self.project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
        return target

    def snapshot(self):
        return {str(p.relative_to(self.project)): p.read_bytes()
                for p in self.project.rglob("*") if p.is_file()}

    def test_inventory_and_source_unchanged(self):
        before = self.snapshot()
        report = scan(self.project, self.workspace)
        rows = {r["path"]: r for r in report["entries"]}
        self.assertEqual(report["unity_version"], "6000.0.1f1")
        self.assertEqual(report["counts"], {"binary": 1, "excluded": 1, "supported": 12, "unsupported": 1})
        self.assertNotIn("Library/generated.cs", rows)
        self.assertEqual(rows["Assets/.env"]["reason"], "configured_exclusion")
        self.assertEqual(rows["Assets/Player.cs"]["sha256"],
                         hashlib.sha256(b"public class Player {}").hexdigest())
        self.assertEqual(report["assemblies"][0]["declaration"]["name"], "Game")
        self.assertEqual(report["packages"][0]["handling"], "declared_only_not_resolved")
        self.assertEqual(status(self.workspace), report)
        self.assertEqual(before, self.snapshot())

    def test_configuration_missing_size_and_overlap(self):
        self.write("Assets/ThirdParty/Test.cs", "class Test {}")
        config = self.base / "config.json"
        config.write_text(json.dumps({"include": ["Assets", "Assets/ThirdParty", "Missing"],
                                     "exclude": ["Assets/ThirdParty"], "max_file_bytes": 30}))
        report = scan(self.project, self.workspace, config)
        rows = {r["path"]: r for r in report["entries"]}
        self.assertEqual(len(rows), len(report["entries"]))
        self.assertEqual(rows["Assets/ThirdParty"]["status"], "excluded")
        self.assertEqual(rows["Missing"]["status"], "missing")
        self.assertEqual(rows["Assets/Main.unity"]["reason"], "size_limit")

    def test_refresh_removes_stale_files(self):
        scan(self.project, self.workspace)
        (self.project / "Assets/Stats.asset").unlink()
        self.write("Assets/Player.cs", "class Changed {}")
        scan(self.project, self.workspace)
        with sqlite3.connect(self.workspace / "inventory.sqlite3") as db:
            self.assertIsNone(db.execute("SELECT path FROM files WHERE path='Assets/Stats.asset'").fetchone())
            digest = db.execute("SELECT sha256 FROM files WHERE path='Assets/Player.cs'").fetchone()[0]
        self.assertEqual(digest, hashlib.sha256(b"class Changed {}").hexdigest())

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            scan(self.base, self.workspace)
        with self.assertRaises(ValueError):
            scan(self.project, self.project / "output")
        for value in ({"include": ["../escape"]}, {"max_file_bytes": 0},
                      {"external_packages": "follow"}, {"typo": True}):
            config = self.base / "invalid.json"
            config.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                scan(self.project, self.workspace, config)
        self.assertFalse(self.workspace.exists())

    def test_diagnostics(self):
        self.write("Assets/Game.asmdef", "{")
        with patch("unity_rag.discovery.subprocess.run", side_effect=FileNotFoundError):
            report = scan(self.project, self.workspace)
        self.assertFalse(report["git"]["available"])
        self.assertTrue(any(d["reason"] == "invalid_json" for d in report["diagnostics"]))

    def test_unreadable(self):
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path.name == "Player.cs":
                raise PermissionError("fixture")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            report = scan(self.project, self.workspace)
        row = next(r for r in report["entries"] if r["path"] == "Assets/Player.cs")
        self.assertEqual(row["status"], "unreadable")

    def test_workspace_binding(self):
        scan(self.project, self.workspace)
        other = self.base / "Other"
        (other / "Assets").mkdir(parents=True)
        (other / "ProjectSettings").mkdir()
        with self.assertRaises(ValueError):
            scan(other, self.workspace)

    def test_excluded_ancestor_cannot_be_bypassed(self):
        self.write("Assets/ThirdParty/Nested/Test.cs", "class Test {}")
        config = self.base / "config.json"
        config.write_text(json.dumps({"include": ["Assets/ThirdParty/Nested"],
                                     "exclude": ["ThirdParty"]}))
        report = scan(self.project, self.workspace, config)
        self.assertEqual(report["entries"][0]["reason"], "excluded_ancestor")
        self.assertEqual(len(report["entries"]), 1)

    def test_linked_directory_is_pruned(self):
        self.write("Assets/Linked/Outside.cs", "class Outside {}")
        from unity_rag.discovery import is_link
        with patch("unity_rag.discovery.is_link",
                   side_effect=lambda p: p.name == "Linked" or is_link(p)):
            report = scan(self.project, self.workspace)
        rows = {r["path"]: r for r in report["entries"]}
        self.assertEqual(rows["Assets/Linked"]["reason"], "link_or_external_path")
        self.assertNotIn("Assets/Linked/Outside.cs", rows)

    def test_git_metadata(self):
        subprocess.run(["git", "init", str(self.project)], check=True, capture_output=True)
        report = scan(self.project, self.workspace)
        self.assertTrue(report["git"]["available"])
        self.assertTrue(report["git"]["dirty"])
        self.assertIsNone(report["git"]["commit"])

    def test_cli(self):
        command = [sys.executable, "-m", "unity_rag"]
        result = subprocess.run(command + ["scan", str(self.project), "--workspace",
                                str(self.workspace), "--json"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["unity_version"], "6000.0.1f1")
        result = subprocess.run(command + ["status", "--workspace", str(self.workspace)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Coverage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
