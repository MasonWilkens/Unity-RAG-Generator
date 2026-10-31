import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from unity_rag.discovery import scan
from unity_rag.asset_map import map_assets, asset_report, asset_users, find_assets, inspect_asset
from unity_rag.code_map import map_code
from unity_rag.unity_yaml import parse_unity

SCRIPT = "a" * 32
PREFAB = "b" * 32
STATS = "c" * 32
TEXTURE = "d" * 32
VARIANT = "e" * 32


def document(class_id, file_id, name, body, stripped=False):
    return f"--- !u!{class_id} &{file_id}{' stripped' if stripped else ''}\n{name}:\n{body}"


BASE = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n" + document(1, 1, "GameObject", """  m_Name: Ogre
  m_Component:
  - component: {fileID: 2}
  - component: {fileID: 3}
""") + document(4, 2, "Transform", """  m_GameObject: {fileID: 1}
  m_Father: {fileID: 0}
  m_LocalPosition: {x: 0, y: 1, z: 0}
""") + document(114, 3, "MonoBehaviour", f"""  m_GameObject: {{fileID: 1}}
  m_Script: {{fileID: 11500000, guid: {SCRIPT}, type: 3}}
  m_EditorClassIdentifier:
  damage: 10
  stats: {{fileID: 11400000, guid: {STATS}, type: 2}}
  texture: {{fileID: 2800000, guid: {TEXTURE}, type: 3}}
  missing: {{fileID: 1, guid: {'f' * 32}, type: 3}}
""")


def instance(source=PREFAB, target=PREFAB, prop="damage", value="40", structural=False):
    return document(1001, 100, "PrefabInstance", f"""  m_SourcePrefab: {{fileID: 100100000, guid: {source}, type: 3}}
  m_Modification:
    m_TransformParent: {{fileID: 0}}
    m_Modifications:
    - target: {{fileID: 3, guid: {target}, type: 3}}
      propertyPath: {prop}
      value: {value}
      objectReference: {{fileID: 0}}
    m_RemovedComponents: {'[{fileID: 3}]' if structural else '[]'}
    m_AddedComponents: []
""")


class AssetMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "Game"
        self.workspace = self.base / "Knowledge"
        self.write("ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.0.1f1")
        self.write("Assets/Ogre.cs", """namespace UnityEngine { public class MonoBehaviour {} }
public class Ogre : UnityEngine.MonoBehaviour { public int damage = 10; }
""", SCRIPT)
        self.write("Assets/Ogre.prefab", BASE, PREFAB)
        self.write("Assets/Other/Ogre.prefab", document(1, 1, "GameObject", "  m_Name: Ogre\n"), "1" * 32)
        self.write("Assets/Stats.asset", document(114, 11400000, "MonoBehaviour", "  m_Name: Stats\n  armor: 5\n"), STATS)
        self.write("Assets/Texture.png", b"\x00\xffPNG", TEXTURE)
        self.write("Assets/Variant.prefab", instance(), VARIANT)
        self.write("Assets/Nested.prefab", instance(source=VARIANT), "2" * 32)
        self.write("Assets/Scene.unity", document(4, 200, "Transform",
                   f"  m_CorrespondingSourceObject: {{fileID: 2, guid: {PREFAB}, type: 3}}\n  m_PrefabInstance: {{fileID: 100}}\n", True) + instance())
        self.write("Assets/Broken.prefab", "--- !u!1 &1\nGameObject: [broken")
        self.before = {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        scan(self.project, self.workspace)

    def write(self, path, content, guid=None):
        p = self.project / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
        if guid:
            Path(str(p) + ".meta").write_text("fileFormatVersion: 2\nguid: " + guid + "\n")
        return p

    def test_graph_and_provenance(self):
        report = map_assets(self.workspace)
        obj = next(o for o in report["objects"] if o["id"] == "asset:Assets/Ogre.prefab#3")
        damage = next(v for v in obj["values"] if v["property"] == "damage")
        self.assertEqual(damage["value"], "10")
        self.assertEqual(damage["origin"], "serialized")
        line = damage["location"]["start_line"]
        self.assertEqual(BASE.splitlines()[line - 1].strip(), "damage: 10")
        script = next(e for e in report["edges"] if e["source"] == obj["id"] and e["kind"] == "uses_script")
        self.assertEqual(script["target_asset"], "asset:Assets/Ogre.cs")
        self.assertEqual(script["resolution"], "resolved_asset_only")
        component = next(e for e in report["edges"] if e["kind"] == "has_component")
        self.assertEqual(component["resolution"], "resolved_object")
        self.assertTrue(any(e["resolution"] == "external_or_missing_guid" for e in report["edges"]))
        self.assertEqual(len(find_assets(self.workspace, "Ogre.prefab")), 2)
        self.assertTrue(asset_users(self.workspace, STATS))
        texture = find_assets(self.workspace, TEXTURE)[0]
        self.assertEqual(texture["parse_status"], "metadata_only")
        self.assertEqual(texture["inventory_status"], "binary")
        self.assertEqual(asset_report(self.workspace), report)
        self.assertTrue(all(p.read_bytes() == data for p, data in self.before.items()))

    def test_prefab_subset_and_stripped_objects(self):
        report = map_assets(self.workspace)
        variant = next(o for o in report["overrides"] if o["instance"] == "asset:Assets/Variant.prefab#100")
        self.assertEqual(variant["resolution"], "resolved_scalar_override")
        self.assertEqual(variant["base_value"], "10")
        self.assertEqual(variant["effective_value"], "40")
        nested = next(o for o in report["overrides"] if o["instance"] == "asset:Assets/Nested.prefab#100")
        self.assertEqual(nested["resolution"], "unsupported_override")
        stripped = next(o for o in report["objects"] if o["id"] == "asset:Assets/Scene.unity#200")
        self.assertTrue(stripped["stripped"])
        self.assertTrue(any(e["source"] == stripped["id"] and e["kind"] == "corresponding_source"
                            and e["resolution"] == "resolved_object" for e in report["edges"]))

    def test_duplicates_and_missing_fileids(self):
        self.write("Assets/Duplicate.cs", "class Duplicate {}", SCRIPT)
        self.write("Assets/BadRef.prefab", document(114, 99, "MonoBehaviour",
                   f"  target: {{fileID: 999, guid: {PREFAB}, type: 3}}\n"))
        scan(self.project, self.workspace)
        report = map_assets(self.workspace)
        self.assertTrue(any(d["reason"] == "duplicate_guid" for d in report["diagnostics"]))
        self.assertTrue(any(e["resolution"] == "ambiguous_guid" for e in report["edges"]))
        self.assertTrue(any(e["resolution"] == "missing_file_id" for e in report["edges"]))

    def test_malformed_and_structural_overrides(self):
        self.write("Assets/Structural.prefab", instance(structural=True))
        scan(self.project, self.workspace)
        report = map_assets(self.workspace)
        self.assertTrue(any(d.get("path") == "Assets/Broken.prefab" for d in report["diagnostics"]))
        entry = next(o for o in report["overrides"] if o["instance"] == "asset:Assets/Structural.prefab#100")
        self.assertEqual(entry["resolution"], "unsupported_override")

    def test_stale_inputs_and_atomic_preservation(self):
        original = map_assets(self.workspace)
        self.write("Assets/Ogre.prefab", BASE + "\n")
        with self.assertRaisesRegex(ValueError, "changed since discovery"):
            map_assets(self.workspace)
        self.assertEqual(asset_report(self.workspace), original)
        scan(self.project, self.workspace)
        with self.assertRaisesRegex(ValueError, "predates"):
            asset_report(self.workspace)

    def test_duplicate_overrides_and_inspection(self):
        duplicated = instance().replace("    m_RemovedComponents: []", f"""    - target: {{fileID: 3, guid: {PREFAB}, type: 3}}
      propertyPath: damage
      value: 90
      objectReference: {{fileID: 0}}
    m_RemovedComponents: []""")
        self.write("Assets/Variant.prefab", duplicated)
        scan(self.project, self.workspace)
        map_assets(self.workspace)
        details = inspect_asset(self.workspace, VARIANT)
        self.assertEqual(len(details["overrides"]), 2)
        self.assertTrue(all(o["resolution"] == "ambiguous_duplicate_override" for o in details["overrides"]))
        self.assertTrue(all("effective_value" not in o for o in details["overrides"]))

    def test_cli(self):
        result = subprocess.run([sys.executable, "-m", "unity_rag", "map-assets",
                                "--workspace", str(self.workspace), "--json"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(json.loads(result.stdout)["counts"]["objects"], 0)

    @unittest.skipUnless(shutil.which("dotnet"), "Requires .NET SDK")
    def test_real_code_map_link_and_invalidation(self):
        self.write("Assets/Stats.cs", "namespace UnityEngine { public class ScriptableObject {} } public class Stats : UnityEngine.ScriptableObject {}", "3" * 32)
        self.write("Assets/Stats.asset", document(114, 11400000, "MonoBehaviour",
                   "  m_Name: Stats\n  m_Script: {fileID: 11500000, guid: " + "3" * 32 + ", type: 3}\n  armor: 5\n"), STATS)
        scan(self.project, self.workspace)
        map_assets(self.workspace)
        map_code(self.workspace)
        with self.assertRaisesRegex(ValueError, "Code map changed"):
            asset_report(self.workspace)
        report = map_assets(self.workspace)
        link = next(e for e in report["edges"] if e["kind"] == "script_type")
        self.assertEqual(link["target"], "Assembly-CSharp::T:Ogre")
        self.assertEqual(link["resolution"], "inferred_filename")
        self.assertTrue(asset_users(self.workspace, link["target"]))
        stats = next(o for o in report["objects"] if o["path"] == "Assets/Stats.asset")
        self.assertEqual(stats["role"], "scriptable_object_candidate")


class UnityYamlTests(unittest.TestCase):
    def test_strings_guids_and_large_ids(self):
        text = document(1, -9223372036854775807, "GameObject", "  m_Name: on\n  value: 00123\n  note: |\n    hello\n    world\n")
        obj = parse_unity(text, "test.prefab")[0]
        self.assertEqual(obj["file_id"], "-9223372036854775807")
        self.assertEqual(obj["data"]["m_Name"], "on")
        self.assertEqual(obj["data"]["value"], "00123")
        self.assertEqual(obj["data"]["note"], "hello\nworld\n")

    def test_unsupported_yaml_is_rejected(self):
        for body in ("  x: 1\n  x: 2\n", "  x: &a [1]\n  y: *a\n"):
            with self.assertRaises(ValueError):
                parse_unity(document(1, 1, "GameObject", body), "bad.prefab")
        with self.assertRaisesRegex(ValueError, "duplicate_file_id"):
            parse_unity(document(1, 1, "GameObject", "  x: 1\n") * 2, "bad.prefab")


if __name__ == "__main__":
    unittest.main()
