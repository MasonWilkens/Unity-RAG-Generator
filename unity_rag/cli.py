"""Local Unity discovery, code and asset mapping, and source retrieval."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3

from .asset_map import map_assets, asset_report, find_assets, asset_users, asset_summary, inspect_asset
from .retrieval import build_retrieval, retrieval_status, retrieve, format_results, MODES
from .evaluation import evaluate
from .discovery import scan, status, format_report
from .code_map import map_code, code_report, find_symbols, find_references, summary

EXTENSIONS = {".cs", ".md", ".txt", ".shader", ".compute", ".cginc", ".hlsl",
              ".uxml", ".uss", ".asmdef", ".asmref", ".unity", ".prefab",
              ".asset", ".mat", ".json"}
EXCLUDED = {"library", "temp", "obj", "logs", "build", "builds", "bin",
            ".git", ".vs", ".idea", "node_modules", ".unity-rag", ".venv"}
MAX_BYTES = 2_000_000


def connect(database):
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, start UNINDEXED, end UNINDEXED, body, tokenize='unicode61')")
    connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
    return connection


def index(project, database):
    root = Path(project).resolve(strict=True)
    if not root.is_dir() or not (root / "Assets").is_dir():
        raise ValueError("Choose a Unity project directory containing Assets.")
    rows = []
    files = 0
    skipped = 0
    # Limit scanning to Unity content roots, avoiding unrelated files and secrets.
    for folder in ("Assets", "Packages", "ProjectSettings"):
        base = root / folder
        if not base.is_dir() or base.is_symlink():
            continue
        for directory, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d.lower() not in EXCLUDED
                             and not d.startswith(".")
                             and not (Path(directory) / d).is_symlink())
            for name in sorted(names):
                path = Path(directory) / name
                if path.suffix.lower() not in EXTENSIONS or name.startswith("."):
                    continue
                try:
                    if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > MAX_BYTES:
                        skipped += 1
                        continue
                    content = path.read_text(encoding="utf-8-sig")
                    if "\x00" in content:
                        skipped += 1
                        continue
                except (OSError, UnicodeError):
                    skipped += 1
                    continue
                lines = content.splitlines()
                for start in range(0, len(lines), 64):
                    end = min(start + 80, len(lines))
                    rows.append((path.relative_to(root).as_posix(), start + 1, end,
                                 "\n".join(lines[start:end])))
                    if end == len(lines):
                        break
                files += 1
    connection = connect(database)
    try:
        with connection:
            connection.execute("DELETE FROM chunks")
            connection.executemany("INSERT INTO chunks(path,start,end,body) VALUES (?,?,?,?)", rows)
            connection.execute("INSERT OR REPLACE INTO metadata VALUES ('project', ?)", (str(root),))
    finally:
        connection.close()
    return {"project": str(root), "files": files, "chunks": len(rows), "skipped": skipped}


def search(database, query, limit=5):
    if not Path(database).is_file():
        raise ValueError("Index a Unity project first.")
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    if not tokens:
        return []
    expression = " OR ".join('"' + token + '"' for token in tokens)
    connection = connect(database)
    try:
        project = connection.execute("SELECT value FROM metadata WHERE key='project'").fetchone()
        rows = connection.execute(
            "SELECT path,start,end,body FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks), path, start LIMIT ?",
            (expression, limit)).fetchall()
        return [{"project": project[0] if project else None, "path": row[0],
                 "start_line": int(row[1]), "end_line": int(row[2]), "text": row[3]}
                for row in rows]
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=".unity-rag/index.sqlite3", help="Local index location")
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("scan", help="Inventory a Unity project without modifying it")
    discover.add_argument("project")
    discover.add_argument("--workspace", required=True, help="Directory outside the target project")
    discover.add_argument("--config", help="JSON discovery configuration")
    discover.add_argument("--json", action="store_true")
    saved = commands.add_parser("status", help="Read the last saved discovery report")
    saved.add_argument("--workspace", required=True)
    saved.add_argument("--json", action="store_true")
    mapping = commands.add_parser("map-code", help="Map C# from a saved discovery inventory")
    mapping.add_argument("--workspace", required=True)
    mapping.add_argument("--define", action="append", default=[])
    mapping.add_argument("--reference", action="append", default=[], help="Explicit reference DLL, metadata only")
    mapping.add_argument("--language-version", default="10")
    mapping.add_argument("--json", action="store_true")
    for name in ("symbols", "references"):
        lookup = commands.add_parser(name, help="Query saved C# declarations or resolved references")
        lookup.add_argument("query", help="Symbol name substring, or exact symbol ID for references")
        lookup.add_argument("--workspace", required=True)
        lookup.add_argument("--json", action="store_true")
    code_status = commands.add_parser("code-status", help="Read saved code-map coverage and diagnostics")
    code_status.add_argument("--workspace", required=True)
    code_status.add_argument("--json", action="store_true")
    for name in ("map-assets", "asset-status", "assets", "asset-users", "asset-show"):
        asset_command = commands.add_parser(name, help="Build or query the saved Unity asset map")
        asset_command.add_argument("--workspace", required=True)
        asset_command.add_argument("--json", action="store_true")
        if name in ("assets", "asset-users", "asset-show"):
            asset_command.add_argument("query", help="Path/GUID/ID; assets also accepts a path substring")
    for name in ("build-retrieval", "retrieval-status"):
        cmd = commands.add_parser(name, help="Build or inspect map-backed evidence retrieval")
        cmd.add_argument("--workspace", required=True)
    query = commands.add_parser("retrieve", help="Search evidence or query mapped relationships")
    query.add_argument("query")
    query.add_argument("--workspace", required=True)
    query.add_argument("--mode", choices=MODES, default="search")
    query.add_argument("--limit", type=int, default=5)
    query.add_argument("--expand", type=int, default=0, help="Maximum additional related evidence records")
    query.add_argument("--kind", choices=["code_type", "code_member", "asset", "asset_object", "prefab_override", "documentation"])
    query.add_argument("--json", action="store_true")
    evaluation = commands.add_parser("evaluate-retrieval", help="Measure a versioned question set")
    evaluation.add_argument("dataset")
    evaluation.add_argument("--workspace", required=True)
    evaluation.add_argument("--limit", type=int, default=5)
    ingest = commands.add_parser("index", help="Rebuild the index for a Unity project")
    ingest.add_argument("project")
    legacy_search = commands.add_parser("search", help="Find relevant source chunks")
    legacy_search.add_argument("query")
    legacy_search.add_argument("--limit", type=int, default=5)
    legacy_search.add_argument("--json", action="store_true", help="Emit context for downstream tools")
    args = parser.parse_args()
    try:
        if args.command in ("scan", "status"):
            report = (scan(args.project, args.workspace, args.config)
                      if args.command == "scan" else status(args.workspace))
            print(json.dumps(report, indent=2) if args.json else format_report(report))
        elif args.command in ("map-code", "code-status"):
            report = (map_code(args.workspace, args.define, args.reference, args.language_version)
                      if args.command == "map-code" else code_report(args.workspace))
            print(json.dumps(report, indent=2) if args.json else summary(report))
        elif args.command in ("symbols", "references"):
            results = (find_symbols(args.workspace, args.query) if args.command == "symbols"
                       else find_references(args.workspace, args.query))
            if args.json:
                print(json.dumps(results, indent=2))
            else:
                for row in results:
                    loc = row["location"]
                    print(f"{loc['path']}:{loc['start_line']}  {row.get('id', row.get('text'))}")
                if not results:
                    print("No matching symbols or resolved references.")
        elif args.command in ("map-assets", "asset-status"):
            report = map_assets(args.workspace) if args.command == "map-assets" else asset_report(args.workspace)
            print(json.dumps(report, indent=2) if args.json else asset_summary(report))
        elif args.command == "asset-show":
            print(json.dumps(inspect_asset(args.workspace, args.query), indent=2))
        elif args.command in ("assets", "asset-users"):
            rows = find_assets(args.workspace, args.query) if args.command == "assets" else asset_users(args.workspace, args.query)
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for row in rows:
                    print(f"{row.get('path', row.get('source'))}  {row.get('guid', row.get('kind'))}  {row.get('parse_status', row.get('resolution'))}")
                if not rows:
                    print("No matching assets or references.")
        elif args.command in ("build-retrieval", "retrieval-status"):
            result = build_retrieval(args.workspace) if args.command == "build-retrieval" else retrieval_status(args.workspace)
            print(json.dumps(result, indent=2))
        elif args.command == "retrieve":
            result = retrieve(args.workspace, args.query, args.mode, args.limit, args.expand, args.kind)
            print(json.dumps(result, indent=2) if args.json else format_results(result))
        elif args.command == "evaluate-retrieval":
            print(json.dumps(evaluate(args.workspace, args.dataset, args.limit), indent=2))
        elif args.command == "index":
            print(json.dumps(index(args.project, args.db), indent=2))
        else:
            if not 1 <= args.limit <= 100:
                parser.error("--limit must be between 1 and 100")
            results = search(args.db, args.query, args.limit)
            if args.json:
                print(json.dumps(results, indent=2))
            else:
                for result in results:
                    print(f"\n{result['path']}:{result['start_line']}-{result['end_line']}\n{result['text']}")
                if not results:
                    print("No matching source chunks.")
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
