import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from unity_rag.discovery import scan
from unity_rag.code_map import map_code
from unity_rag.asset_map import map_assets
from unity_rag.retrieval import build_retrieval, retrieve, retrieval_status, terms
from unity_rag.evaluation import evaluate


@unittest.skipUnless(shutil.which("dotnet"), "Requires .NET SDK")
class StructuredRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.project = cls.base / "Game"
        cls.workspace = cls.base / "Knowledge"
        shutil.copytree(Path(__file__).parent / "fixtures/retrieval_project", cls.project)
        cls.before = {p: p.read_bytes() for p in cls.project.rglob("*") if p.is_file()}
        scan(cls.project, cls.workspace)
        map_code(cls.workspace)
        map_assets(cls.workspace)
        cls.report = build_retrieval(cls.workspace)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_keyword_identifier_splitting_and_citations(self):
        results = retrieve(self.workspace, "attack cooldown", kind="code_member")["results"]
        self.assertTrue(results)
        self.assertEqual(results[0]["entity_id"], "Assembly-CSharp::M:Game.Enemy.CanAttack(System.Single)")
        self.assertEqual(results[0]["evidence_type"], "original_source")
        cite = results[0]["sources"][0]
        source = (self.project / cite["path"]).read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(results[0]["text"], "\n".join(source[cite["start_line"]-1:cite["end_line"]]))
        movement = retrieve(self.workspace, "movement speed", kind="asset_object")["results"]
        self.assertEqual(movement[0]["entity_id"], "asset:Assets/Goblin.prefab#3")

    def test_exact_guid_path_and_symbol(self):
        guid = retrieve(self.workspace, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", mode="exact", kind="asset")["results"]
        self.assertEqual(guid[0]["entity_id"], "asset:Assets/Goblin.prefab")
        path = retrieve(self.workspace, "Assets/Enemy.cs", mode="exact")["results"]
        self.assertTrue(path)
        method = retrieve(self.workspace, "CanAttack", mode="exact")["results"]
        self.assertEqual(len(method), 1)
        self.assertEqual(method[0]["match"], "exact_alias")

    def test_structured_relations_preserve_uncertainty(self):
        result = retrieve(self.workspace, "Assets/Enemy.cs", mode="used-by")
        self.assertTrue(any(e["kind"] == "uses_script" for e in result["results"]))
        inferred = retrieve(self.workspace, "Assembly-CSharp::T:Game.Enemy", mode="used-by")
        self.assertTrue(any(e["resolution"] == "inferred_filename" and e["evidence_type"] == "derived_interpretation"
                            for e in inferred["results"]))
        inherited = retrieve(self.workspace, "Game.Enemy", mode="inherits")["results"]
        self.assertTrue(inherited)
        self.assertEqual(inherited[0]["target"], "Assembly-CSharp::T:Game.CombatBase")
        derived = retrieve(self.workspace, "Game.CombatBase", mode="derived-types")["results"]
        self.assertTrue(any(e["source"] == "Assembly-CSharp::T:Game.Enemy" for e in derived))

    def test_bounded_context_and_no_duplicates(self):
        result = retrieve(self.workspace, "CanAttack", mode="exact", expand=2)
        self.assertGreater(len(result["context"]), 0)
        self.assertLessEqual(len(result["context"]), 2)
        self.assertTrue(any(c["entity_id"] == "Assembly-CSharp::T:Game.Enemy" for c in result["context"]))
        ids = [r["id"] for r in result["results"] + result["context"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_overloads_and_override_evidence(self):
        matches = retrieve(self.workspace, "FindTarget", mode="exact")["results"]
        self.assertEqual(len({m["entity_id"] for m in matches}), 2)
        overrides = retrieve(self.workspace, "damage", kind="prefab_override")["results"]
        self.assertEqual(overrides[0]["evidence_type"], "derived_interpretation")
        self.assertEqual(overrides[0]["detail"]["effective_value"], "40")
        self.assertEqual(len(overrides[0]["sources"]), 2)

    def test_query_safety_limits_and_empty_results(self):
        for query in ('"', "!!!", "qzxnonexistenttoken"):
            self.assertEqual(retrieve(self.workspace, query)["results"], [])
        retrieve(self.workspace, 'CanAttack" OR *')
        for kwargs in ({"limit": 0}, {"expand": 21}, {"mode": "unknown"}):
            with self.assertRaises(ValueError):
                retrieve(self.workspace, "Enemy", **kwargs)

    def test_stale_maps_and_source_changes(self):
        clone = self.base / "Stale"
        clone.mkdir(exist_ok=True)
        shutil.copyfile(self.workspace / "inventory.sqlite3", clone / "inventory.sqlite3")
        with sqlite3.connect(clone / "inventory.sqlite3") as db:
            value = json.loads(db.execute("SELECT value FROM metadata WHERE key='report'").fetchone()[0])
            value["scanned_at"] = "changed"
            db.execute("UPDATE metadata SET value=? WHERE key='report'", (json.dumps(value),))
        with self.assertRaisesRegex(ValueError, "stale"):
            retrieve(clone, "Enemy")
        p = self.project / "Assets/Enemy.cs"
        try:
            p.write_text("class Changed {}")
            with self.assertRaisesRegex(ValueError, "changed since discovery"):
                build_retrieval(self.workspace)
            self.assertEqual(retrieval_status(self.workspace), self.report)
        finally:
            p.write_bytes(self.before[p])

    def test_evaluation_baseline(self):
        result = evaluate(self.workspace, Path(__file__).parent / "evaluation/retrieval_v1.json", limit=5)
        self.assertEqual(result["questions"], 8)
        self.assertEqual(result["recall_at_k"], 1.0)
        self.assertGreaterEqual(result["mean_reciprocal_rank"], 0.75)
        print("\nRetrieval baseline: " + json.dumps({k: v for k, v in result.items() if k != "results"}))

    def test_cli_and_read_only_sources(self):
        result = subprocess.run([sys.executable, "-m", "unity_rag", "retrieve", "CanAttack",
                                 "--workspace", str(self.workspace), "--json"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["results"])
        self.assertTrue(all(p.read_bytes() == data for p, data in self.before.items()))


class TokenizationTests(unittest.TestCase):
    def test_identifier_parts(self):
        self.assertTrue({"http", "request", "damage", "value"} <= set(terms("HTTPRequest damage_value")))
        self.assertEqual(terms("damage damage", unique=False).count("damage"), 4)


if __name__ == "__main__":
    unittest.main()
