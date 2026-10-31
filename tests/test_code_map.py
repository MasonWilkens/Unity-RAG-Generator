import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from unity_rag.discovery import scan
from unity_rag.code_map import map_code, find_symbols, find_references, code_report

SOURCE = """using System;
namespace Game {
    public interface IDamageable { void Hit(int amount); }
    public class Base {}
    /// <summary>Player combat.</summary>
    public partial class Player : Base, IDamageable {
        [Obsolete("fixture")] public int health = 10;
        public string Name { get; set; }
        public void Hit(int amount) { health -= amount; }
        public void Hit(string text) {}
        public void Run() { Hit(1); MissingApi.DoThing(); }
#if UNITY_EDITOR
        public void EditorOnly() {}
#else
        public void RuntimeOnly() {}
#endif
    }
    public class A {}
    public class B {}
    public class Ambiguous {
        void Pick(A value) {}
        void Pick(B value) {}
        void Run() { Pick(null); }
    }
}
"""
UNITY = """namespace UnityEngine {
    public class MonoBehaviour {}
    public class SerializeField : System.Attribute {}
}
namespace Game {
    public class Enemy : UnityEngine.MonoBehaviour {
        [UnityEngine.SerializeField] private int damage;
    }
}
"""


@unittest.skipUnless(shutil.which("dotnet"), "Requires .NET SDK")
class CodeMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.project = cls.base / "Game"
        cls.workspace = cls.base / "Map"
        (cls.project / "Assets").mkdir(parents=True)
        (cls.project / "ProjectSettings").mkdir()
        (cls.project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.1f1")
        (cls.project / "Assets/Player.cs").write_text(SOURCE)
        (cls.project / "Assets/Partial.cs").write_text("namespace Game { public partial class Player { public void Extra() { Hit(2); } } }")
        (cls.project / "Assets/UnityFixture.cs").write_text(UNITY)
        (cls.project / "Assets/Missing.cs").write_text("public class Unknown : MissingBase { [SerializeField] private int value; }")
        (cls.project / "Assets/Separate").mkdir()
        (cls.project / "Assets/Separate/Other.asmdef").write_text('{"name":"Other","references":["Assembly-CSharp"]}')
        (cls.project / "Assets/Separate/Other.cs").write_text("public class Other { Game.Player player; }")
        cls.before = {p: p.read_bytes() for p in cls.project.rglob("*") if p.is_file()}
        scan(cls.project, cls.workspace)
        cls.report = map_code(cls.workspace)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_declarations_overloads_partial_and_locations(self):
        matches = find_symbols(self.workspace, "Game.Player.Hit")
        self.assertEqual(len(matches), 2)
        self.assertEqual(len({m["id"] for m in matches}), 2)
        players = [r for r in self.report["declarations"] if r["qualified_name"] == "Game.Player"]
        self.assertEqual(len(players), 2)
        self.assertEqual(len({r["id"] for r in players}), 1)
        self.assertTrue(all(r["partial"] for r in players))
        health = find_symbols(self.workspace, "Game.Player.health")[0]
        self.assertEqual(health["location"]["start_line"], 7)
        self.assertIn('Obsolete("fixture")', health["attributes"])
        self.assertEqual(health["serialized_field"], "candidate_type_rules_not_checked")
        player = next(r for r in players if r["location"]["path"] == "Assets/Player.cs")
        self.assertIn("Player combat", player["comments"])
        self.assertEqual(player["bases"], ["Base", "IDamageable"])

    def test_calls_and_ambiguity(self):
        hit = next(r for r in self.report["declarations"] if r["id"].endswith("M:Game.Player.Hit(System.Int32)"))
        calls = [r for r in find_references(self.workspace, hit["id"]) if r["kind"] == "calls"]
        self.assertEqual(len(calls), 2)
        missing = next(r for r in self.report["edges"] if r["text"] == "MissingApi.DoThing()")
        self.assertEqual(missing["resolution"], "unresolved")
        self.assertIsNone(missing["target"])
        ambiguous = next(r for r in self.report["edges"] if r["text"] == "Pick(null)")
        self.assertEqual(ambiguous["resolution"], "ambiguous_or_invalid")
        self.assertEqual(len(ambiguous["candidates"]), 2)
        self.assertIsNone(ambiguous["target"])

    def test_unity_and_assembly_scope(self):
        enemy = find_symbols(self.workspace, "Game.Enemy")[0]
        self.assertEqual(enemy["unity_type"], "UnityEngine.MonoBehaviour")
        unknown = find_symbols(self.workspace, "Unknown")[0]
        self.assertEqual(unknown["unity_type"], "unresolved")
        field = find_symbols(self.workspace, "Unknown.value")[0]
        self.assertEqual(field["serialized_field"], "unresolved_attribute_candidate")
        other = next(f for f in self.report["files"] if f["path"].endswith("Other.cs"))
        self.assertEqual(other["assembly"], "Other")
        self.assertTrue(any(d["code"] == "CS0246" for d in self.report["diagnostics"]))
        self.assertTrue(all(p.read_bytes() == content for p, content in self.before.items()))

    def test_conditions_and_cli(self):
        self.assertTrue(find_symbols(self.workspace, "RuntimeOnly"))
        self.assertFalse(find_symbols(self.workspace, "EditorOnly"))
        self.assertTrue(any(c["kind"] == "DisabledTextTrivia" for c in self.report["conditions"]))
        result = subprocess.run([sys.executable, "-m", "unity_rag", "symbols", "Game.Player.Hit",
                                 "--workspace", str(self.workspace), "--json"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 2)

    def test_changed_source_is_rejected(self):
        source = self.project / "Assets/Player.cs"
        try:
            source.write_text(SOURCE + "\n// changed")
            with self.assertRaisesRegex(ValueError, "changed since discovery"):
                map_code(self.workspace)
            self.assertEqual(code_report(self.workspace)["declarations"], self.report["declarations"])
        finally:
            source.write_text(SOURCE)

    def test_define_selection_and_stale_map(self):
        # Separate workspace avoids modifying the shared baseline map.
        work = self.base / "EditorMap"
        scan(self.project, work)
        report = map_code(work, defines=["UNITY_EDITOR"])
        self.assertTrue(any(r["name"] == "EditorOnly" for r in report["declarations"]))
        self.assertFalse(any(r["name"] == "RuntimeOnly" for r in report["declarations"]))
        scan(self.project, work)
        with self.assertRaisesRegex(ValueError, "predates"):
            code_report(work)


if __name__ == "__main__":
    unittest.main()
