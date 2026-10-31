from pathlib import Path
import tempfile
import unittest

from unity_rag.cli import index, search


class RetrievalTests(unittest.TestCase):
    def test_sources_exclusions_and_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Assets").mkdir()
            (root / "Library").mkdir()
            source = root / "Assets" / "Movement.cs"
            source.write_text("class Movement {\n    float speed = 5;\n}\n", encoding="utf-8")
            (root / "Library" / "Ignored.cs").write_text("secretcachedtoken", encoding="utf-8")
            (root / "Assets" / "binary.asset").write_bytes(b"\x00binary")
            database = root / "index.sqlite3"
            summary = index(root, database)
            self.assertEqual(summary["files"], 1)
            hits = search(database, "speed")
            self.assertEqual(hits[0]["path"], "Assets/Movement.cs")
            self.assertEqual(hits[0]["start_line"], 1)
            self.assertEqual(hits[0]["end_line"], 3)
            self.assertEqual(search(database, "secretcachedtoken"), [])
            source.unlink()
            index(root, database)
            self.assertEqual(search(database, "speed"), [])

    def test_missing_index(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                search(Path(directory) / "missing.sqlite3", "test")


if __name__ == "__main__":
    unittest.main()
