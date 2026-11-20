"""Local Unity discovery, code and asset mapping, and source retrieval."""
import argparse
from .workspace import refresh, workspace_status, watch, seal_workspace
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from .asset_map import map_assets, asset_report, find_assets, asset_users, asset_summary, inspect_asset
from .retrieval import build_retrieval, retrieval_status, retrieve, format_results, MODES
from .answer_evaluation import evaluate_answers
from .answer_review import review_template, review_answers
from .answers import ask, format_answer
from .openai_generation import provider_from_args
from .evaluation import evaluate
from .semantic import build_semantic, semantic_status, SEMANTIC_KINDS
from .embeddings import DEFAULT_MODEL
from .discovery import scan, status, format_report, is_git_lfs_pointer
from .code_map import map_code, code_report, find_symbols, find_references, summary

EXTENSIONS = {".cs", ".md", ".txt", ".shader", ".compute", ".cginc", ".hlsl",
              ".uxml", ".uss", ".asmdef", ".asmref", ".unity", ".prefab",
              ".asset", ".mat", ".json"}
EXCLUDED = {"library", "temp", "obj", "logs", "build", "builds", "bin",
            ".git", ".vs", ".idea", "node_modules", ".unity-rag", ".venv"}
MAX_BYTES = 2_000_000


def _print_human(text):
    """Keep text output usable on consoles that cannot encode every source character."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, 'encoding', None) or 'ascii'
        print(text.encode(encoding, errors='backslashreplace').decode(encoding))


def format_code_status(report):
    """Describe supplied defines and inactive text without displaying source."""
    defines = report.get('defines')
    if not isinstance(defines, list) or not all(isinstance(value, str) for value in defines):
        chosen = '(unavailable)'
    else:
        chosen = ', '.join(defines) if defines else '(none)'
    conditions = report.get('conditions')
    if (not isinstance(conditions, list) or any(not isinstance(row, dict)
            or not isinstance(row.get('kind'), str) or not isinstance(row.get('path'), str) for row in conditions)):
        inactive = 'Inactive preprocessor text: unavailable in this code map.'
    else:
        count, paths = 0, set()
        for row in conditions:
            if row['kind'] == 'DisabledTextTrivia':
                count += 1
                paths.add(row['path'])
        inactive = (f"Inactive preprocessor text: {count} region(s) across {len(paths)} file(s)."
                    + " These are not active declarations or a measure of runtime coverage;"
                    + " use coverage --conditional for bounded per-file counts.")
    return summary(report) + f"\nSupplied defines: {chosen}.\n" + inactive


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
                    if "\x00" in content or is_git_lfs_pointer(content):
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
    from . import __version__
    parser.add_argument("--version", action="version", version=f"unity-rag {__version__}")
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
        if name == "map-assets":
            asset_command.add_argument('--compress-asset-report', action='store_true',
                                       help='Use a versioned compressed asset report for this standalone map')
        if name in ("assets", "asset-users", "asset-show"):
            asset_command.add_argument("query", help="Path/GUID/ID; assets also accepts a path substring")
    for name in ("build-retrieval", "retrieval-status"):
        cmd = commands.add_parser(name, help="Build or inspect map-backed evidence retrieval")
        cmd.add_argument("--workspace", required=True)
        if name == 'build-retrieval':
            cmd.add_argument('--chunk-budget', type=int, default=4096)
            cmd.add_argument('--chunk-encoding', default='utf8-bytes', help='utf8-bytes or an installed tiktoken encoding')
    semantic = commands.add_parser("build-semantic", help="Build cached local embeddings (first use downloads model weights)")
    semantic.add_argument("--workspace", required=True)
    semantic.add_argument("--model", default=DEFAULT_MODEL)
    semantic.add_argument("--batch-size", type=int, default=4)
    semantic.add_argument("--kind", action="append", choices=SEMANTIC_KINDS,
                          help="Embed only these evidence kinds; lexical retrieval keeps all kinds")
    semantic_status_cmd = commands.add_parser("semantic-status")
    semantic_status_cmd.add_argument("--workspace", required=True)
    query = commands.add_parser("retrieve", help="Search evidence or query mapped relationships")
    query.add_argument("query")
    query.add_argument("--workspace", required=True)
    query.add_argument("--mode", choices=MODES, default="search")
    query.add_argument("--limit", type=int, default=5)
    query.add_argument("--expand", type=int, default=0, help="Maximum additional related evidence records")
    query.add_argument("--kind", choices=(*SEMANTIC_KINDS, "rig_asset"))
    query.add_argument("--semantic-weight", type=float, default=0.8)
    query.add_argument("--context-chars", type=int, default=24000)
    query.add_argument('--reserve-related-chars', type=int, default=0,
                       help='Opt in to reserving part of the context budget for related evidence')
    query.add_argument('--related-rank', action='store_true',
                       help='Experimental: promote related same-class methods for two-action questions')
    query.add_argument('--path-refs', type=int, default=0, help='Additional indexed paths mentioned by current documentation (0–20)')
    query.add_argument('--incoming-expand', type=int, default=0, help='Additional resolved incoming references (0–20)')
    query.add_argument('--source-aware', action='store_true', help='Hybrid: reserve lexical matches and include bounded current-source excerpts')
    query.add_argument("--json", action="store_true")
    evaluation = commands.add_parser("evaluate-retrieval", help="Measure a versioned question set")
    evaluation.add_argument("dataset")
    evaluation.add_argument("--workspace", required=True)
    evaluation.add_argument("--semantic-weight", type=float, default=0.8)
    evaluation.add_argument("--expand", type=int, default=0)
    evaluation.add_argument("--mode", choices=("search", "semantic", "hybrid"), default="search")
    evaluation.add_argument("--limit", type=int, default=5)
    benchmark = commands.add_parser('benchmark', help='Run a multi-project retrieval benchmark manifest')
    benchmark.add_argument('manifest')
    benchmark.add_argument('--mode', choices=('search', 'semantic', 'hybrid'), default='search')
    benchmark.add_argument('--limit', type=int, default=10)
    benchmark.add_argument('--expand', type=int, default=0)
    freeze = commands.add_parser('freeze-benchmark', help='Pin datasets, generation files and an existing baseline')
    freeze.add_argument('manifest')
    freeze.add_argument('--baseline', required=True)
    freeze.add_argument('--output', required=True)
    answer = commands.add_parser("ask", help="Answer from verified project evidence using a selected provider")
    answer.add_argument("question")
    answer.add_argument("--workspace", required=True)
    answer.add_argument("--style", choices=("explain", "extractive"), default="explain")
    answer.add_argument("--model", help="Ollama model name or price-verified OpenAI model")
    answer.add_argument("--endpoint", help="Ollama origin only; defaults to localhost")
    answer.add_argument("--allow-remote", action="store_true", help="Explicitly permit sending project evidence to a remote model")
    answer.add_argument("--seed", type=int, default=42, help="Ollama sampling seed; does not guarantee correctness or determinism")
    answer.add_argument("--thinking", choices=("auto", "on", "off"), default="auto", help="Ollama thinking mode; model must support the requested setting")
    answer.add_argument("--max-output-tokens", type=int, default=2048, help="Generation budget, including thinking; 256-16384")
    answer.add_argument("--timeout", type=int, default=120)
    answer.add_argument("--mode", choices=("search", "semantic", "hybrid", "exact"), default="semantic")
    answer.add_argument("--limit", type=int, default=5)
    answer.add_argument("--expand", type=int, default=3)
    answer.add_argument("--context-chars", type=int, default=24000)
    answer.add_argument('--context-tokens', type=int, help='Token budget for serialized evidence, replacing --context-chars for evidence packing')
    answer.add_argument('--token-encoding', default='utf8-bytes', help='utf8-bytes estimate or installed tiktoken encoding name')
    answer.add_argument("--followups", type=int, default=2)
    answer.add_argument("--review-claims", action="store_true", help="Experimental claim review with verified quotes; not a correctness guarantee")
    answer.add_argument("--show-sources", action="store_true", help="Display verified source lines beside each generated claim")
    answer.add_argument("--json", action="store_true")
    answer_eval = commands.add_parser("evaluate-answers", help="Generate answer review artifacts and citation contract checks")
    answer_eval.add_argument("dataset")
    answer_eval.add_argument("--workspace", required=True)
    answer_eval.add_argument("--style", choices=("explain", "extractive"), default="explain")
    answer_eval.add_argument("--model")
    answer_eval.add_argument("--endpoint", help="Ollama origin only; defaults to localhost")
    answer_eval.add_argument("--allow-remote", action="store_true")
    answer_eval.add_argument("--review-claims", action="store_true")
    answer_eval.add_argument("--limit", type=int, default=5)
    answer_eval.add_argument("--expand", type=int, default=3)
    answer_eval.add_argument("--context-chars", type=int, default=24000)
    answer_eval.add_argument('--context-tokens', type=int)
    answer_eval.add_argument('--token-encoding', default='utf8-bytes')
    answer_eval.add_argument("--followups", type=int, default=2)
    answer_eval.add_argument("--seed", type=int, default=42, help="Ollama sampling seed; does not guarantee correctness or determinism")
    answer_eval.add_argument("--thinking", choices=("auto", "on", "off"), default="auto", help="Ollama thinking mode; model must support the requested setting")
    answer_eval.add_argument("--max-output-tokens", type=int, default=2048, help="Generation budget, including thinking; 256-16384")
    answer_eval.add_argument("--timeout", type=int, default=120)
    answer_eval.add_argument("--mode", choices=("search", "semantic", "hybrid", "exact"), default="search")
    for provider_command in (answer, answer_eval):
        provider_command.add_argument('--model-context-tokens', type=int,
            help='Optional complete-input estimate plus output reserve; requires --context-tokens and a tiktoken encoding')
        provider_command.add_argument("--provider", choices=("ollama", "openai"), default="ollama")
        provider_command.add_argument("--api-key-file", help="Explicit local credential file, loaded only at request time")
        provider_command.add_argument("--spend-ledger", help="Persistent SQLite ledger shared by all requests for this budget")
        provider_command.add_argument("--budget-usd", help="Fixed ledger cap, at most $56; required for OpenAI")
        provider_command.add_argument("--max-api-calls", type=int, default=3, help="OpenAI calls per invocation; no automatic retries")
    review_template_parser = commands.add_parser("answer-review-template", help="Create an explicit quality review template for an evaluation report")
    review_template_parser.add_argument("report")
    review_parser = commands.add_parser("review-answers", help="Check a completed quality review against the exact evaluated outputs")
    review_parser.add_argument("report")
    review_parser.add_argument("judgments")
    ingest = commands.add_parser("index", help="Rebuild the index for a Unity project")
    ingest.add_argument("project")
    legacy_search = commands.add_parser("search", help="Find relevant source chunks")
    legacy_search.add_argument("query")
    legacy_search.add_argument("--limit", type=int, default=5)
    legacy_search.add_argument("--json", action="store_true", help="Emit context for downstream tools")
    for name in ('refresh', 'rebuild', 'watch'):
        command = commands.add_parser(name, help='Publish a complete, consistent workspace snapshot')
        command.add_argument('--workspace', required=True)
        command.add_argument('--project')
        command.add_argument('--config')
        command.add_argument('--semantic', action=argparse.BooleanOptionalAction, default=None)
        command.add_argument('--semantic-batch-size', type=int)
        command.add_argument('--chunk-budget', type=int, help='Evidence text limit in the selected counter units; omitted preserves saved policy')
        command.add_argument('--chunk-encoding', help='utf8-bytes or an installed tiktoken encoding; omitted preserves saved policy')
        command.add_argument('--compress-asset-report', action=argparse.BooleanOptionalAction, default=None,
                             help='Opt in to versioned asset-report compression; omitted preserves saved setting')
        command.add_argument('--semantic-kind', action='append', choices=(*SEMANTIC_KINDS, 'all'),
                             help='Limit semantic corpus; omitted preserves prior scope, all resets it')
        command.add_argument('--model')
        command.add_argument('--define', action='append', default=None)
        command.add_argument('--reference', action='append', default=None)
        command.add_argument('--language-version')
        if name != 'watch':
            command.add_argument('--require-unity-closed', action='store_true',
                                 help='Fail closed if Unity is open or cannot be checked at refresh stage boundaries (Windows)')
        if name == 'watch':
            command.add_argument('--interval', type=int, default=None, help='Seconds between polls (legacy 5; ticket mode 300)')
            command.add_argument('--iterations', type=int, default=0)
            command.add_argument('--ticket-mode', action='store_true', help='Use the threshold, quiet period and idle ticket instead of refreshing every interval')
            command.add_argument('--threshold', type=int, default=None, help='Ticket mode: changed indexed files (default 50)')
            command.add_argument('--quiet-seconds', type=int, default=None, help='Ticket mode: unchanged observations (default 120 seconds)')
            command.add_argument('--idle-policy', choices=('unity-closed',), default=None,
                                 help='Ticket mode: require all processes named Unity to be closed on Windows')
    preflight_command = commands.add_parser('creation-preflight', help='Check local workspace creation prerequisites without writing or downloading')
    preflight_command.add_argument('--project', required=True)
    preflight_command.add_argument('--workspace', required=True)
    preflight_command.add_argument('--semantic', action='store_true', help='Check the default local semantic provider dependency')
    coverage_command = commands.add_parser('coverage', help='Summarize saved inventory and retrieval evidence coverage')
    coverage_command.add_argument('--workspace', required=True)
    coverage_command.add_argument('--path', action='append', default=[], help='Check one canonical project-relative source path (repeatable; max 50)')
    coverage_command.add_argument('--conditional', action='store_true', help='Include bounded inactive-preprocessor counts and supplied defines')
    source_eval = commands.add_parser('evaluate-sources', help='Evaluate labeled source paths without a model judge')
    source_eval.add_argument('--workspace', required=True)
    source_eval.add_argument('--dataset', required=True)
    source_eval.add_argument('--mode', choices=('search', 'semantic', 'hybrid'), default='search')
    source_eval.add_argument('--limit', type=int, default=5)
    source_eval.add_argument('--expand', type=int, default=2)
    source_eval.add_argument('--path-refs', type=int, default=0)
    source_eval.add_argument('--incoming-expand', type=int, default=0)
    source_eval.add_argument('--source-aware', action='store_true')
    source_eval.add_argument('--context-chars', type=int, default=24000)
    source_eval.add_argument('--reserve-related-chars', type=int, default=0)
    source_eval.add_argument('--related-rank', action='store_true')
    passage_eval = commands.add_parser('evaluate-passages', help='Check reviewed passages through retrieval and actual answer packing')
    passage_eval.add_argument('--workspace', required=True)
    passage_eval.add_argument('--dataset', required=True)
    passage_eval.add_argument('--mode', choices=('search', 'semantic', 'hybrid', 'exact'), default='search')
    passage_eval.add_argument('--limit', type=int, default=5)
    passage_eval.add_argument('--expand', type=int, default=0)
    passage_eval.add_argument('--context-chars', type=int, default=24000)
    passage_eval.add_argument('--context-tokens', type=int)
    passage_eval.add_argument('--token-encoding', default='utf8-bytes')
    inventory_command = commands.add_parser('generation-inventory', help='Preview generation retention; never deletes')
    inventory_command.add_argument('--workspace', required=True)
    inventory_command.add_argument('--retain', type=int, default=2)
    ticket_command = commands.add_parser('refresh-ticket', help='Explicitly observe or run a checkpointed idle refresh')
    ticket_command.add_argument('--workspace', required=True)
    ticket_command.add_argument('action', choices=('observe', 'run'))
    ticket_command.add_argument('--threshold', type=int, default=50)
    ticket_command.add_argument('--quiet-seconds', type=int, default=120)
    ticket_command.add_argument('--confirm-idle', action='store_true')
    seal_command = commands.add_parser('seal-workspace', help='Stage a verified metadata seal without rescanning or re-embedding')
    seal_command.add_argument('--workspace', required=True)
    for name in ('workspace-status', 'diagnostics'):
        command = commands.add_parser(name, help='Check freshness, health and interrupted refreshes')
        command.add_argument('--workspace', required=True)
        command.add_argument('--threshold', type=int, default=50,
                             help='Indexed file changes needed for a pending recommendation (no automatic refresh)')
    args = parser.parse_args()
    try:
        if args.command == 'watch':
            if args.ticket_mode:
                if any(x is not None for x in (args.project, args.config, args.semantic, args.model,
                                                args.define, args.reference, args.language_version, args.semantic_kind, args.compress_asset_report, args.semantic_batch_size,
                                                args.chunk_budget, args.chunk_encoding)):
                    raise ValueError('Ticket watch inherits the active workspace settings; refresh overrides are not supported.')
                from .idle_watch import watch_tickets
                results = watch_tickets(args.workspace, threshold=args.threshold if args.threshold is not None else 50,
                                        quiet_seconds=args.quiet_seconds if args.quiet_seconds is not None else 120,
                                        interval=args.interval if args.interval is not None else 300,
                                        iterations=args.iterations)
            else:
                if any(x is not None for x in (args.threshold, args.quiet_seconds, args.idle_policy)):
                    raise ValueError('Threshold, quiet period and idle policy require --ticket-mode.')
                results = watch(args.workspace, project=args.project, config_path=args.config,
                        interval=args.interval if args.interval is not None else 5, iterations=args.iterations,
                        semantic=args.semantic, model=args.model, defines=args.define, references=args.reference,
                        language_version=args.language_version, semantic_kinds=args.semantic_kind, semantic_batch_size=args.semantic_batch_size,
                        chunk_budget=args.chunk_budget, chunk_encoding=args.chunk_encoding,
                        compress_asset_report=args.compress_asset_report)
            for result in results:
                print(json.dumps(result, indent=2), flush=True)
        elif args.command in ('refresh', 'rebuild'):
            stage_guard = None
            if args.require_unity_closed:
                from .idle_watch import unity_editors_closed
                def stage_guard(stage):
                    if not unity_editors_closed():
                        raise ValueError(f'Unity is running at refresh stage {stage}; previous snapshot preserved.')
            print(json.dumps(refresh(args.workspace, args.project, args.config,
                rebuild=args.command == 'rebuild', semantic=args.semantic, model=args.model,
                defines=args.define, references=args.reference, language_version=args.language_version,
                semantic_kinds=args.semantic_kind, semantic_batch_size=args.semantic_batch_size, compress_asset_report=args.compress_asset_report,
                chunk_budget=args.chunk_budget, chunk_encoding=args.chunk_encoding, **({'stage_guard': stage_guard} if stage_guard else {})), indent=2))
        elif args.command == 'creation-preflight':
            from .preflight import creation_preflight
            result = creation_preflight(args.project, args.workspace, semantic=args.semantic)
            print(json.dumps(result, indent=2))
            return 0 if result['ready_for_attempt'] else 1
        elif args.command == 'coverage':
            from .coverage import coverage
            print(json.dumps(coverage(args.workspace, args.path, include_conditional=args.conditional), indent=2))
        elif args.command == 'evaluate-sources':
            from .source_evaluation import evaluate_sources
            print(json.dumps(evaluate_sources(args.workspace, args.dataset, mode=args.mode, limit=args.limit, expand=args.expand, path_refs=args.path_refs, incoming_expand=args.incoming_expand, context_chars=args.context_chars, source_aware=args.source_aware, reserve_related_chars=args.reserve_related_chars, related_rank=args.related_rank), indent=2))
        elif args.command == 'evaluate-passages':
            from .passage_evaluation import evaluate_passages
            print(json.dumps(evaluate_passages(args.workspace, args.dataset, mode=args.mode, limit=args.limit,
                expand=args.expand, context_chars=args.context_chars, context_tokens=args.context_tokens,
                token_encoding=args.token_encoding), indent=2))
        elif args.command == 'generation-inventory':
            from .maintenance import generation_inventory
            print(json.dumps(generation_inventory(args.workspace, retain=args.retain), indent=2))
        elif args.command == 'refresh-ticket':
            from .maintenance import observe_ticket, run_ticket
            result = (observe_ticket(args.workspace, threshold=args.threshold) if args.action == 'observe'
                      else run_ticket(args.workspace, confirm_idle=args.confirm_idle, quiet_seconds=args.quiet_seconds))
            print(json.dumps(result, indent=2))
        elif args.command == 'seal-workspace':
            print(json.dumps(seal_workspace(args.workspace), indent=2))
        elif args.command in ('workspace-status', 'diagnostics'):
            result = workspace_status(args.workspace, threshold=args.threshold)
            print(json.dumps(result, indent=2))
            return 0 if result['healthy'] else 1
        elif args.command == "answer-review-template":
            print(json.dumps(review_template(args.report), indent=2))
        elif args.command == "review-answers":
            result = review_answers(args.report, args.judgments)
            print(json.dumps(result, indent=2))
            return 0 if result['accepted'] else 1
        elif args.command in ("scan", "status"):
            report = (scan(args.project, args.workspace, args.config)
                      if args.command == "scan" else status(args.workspace))
            print(json.dumps(report, indent=2) if args.json else format_report(report))
        elif args.command in ("map-code", "code-status"):
            report = (map_code(args.workspace, args.define, args.reference, args.language_version)
                      if args.command == "map-code" else code_report(args.workspace))
            print(json.dumps(report, indent=2) if args.json else format_code_status(report))
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
            report = map_assets(args.workspace, compress_report=args.compress_asset_report) if args.command == "map-assets" else asset_report(args.workspace)
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
            result = build_retrieval(args.workspace, chunk_budget=args.chunk_budget, chunk_encoding=args.chunk_encoding) if args.command == "build-retrieval" else retrieval_status(args.workspace)
            print(json.dumps(result, indent=2))
        elif args.command == "build-semantic":
            def progress(done, total):
                print(f"Embedded {done}/{total} uncached passages", file=sys.stderr, flush=True)
            print(json.dumps(build_semantic(args.workspace, model=args.model, progress=progress,
                                            include_kinds=args.kind, batch_size=args.batch_size), indent=2))
        elif args.command == "semantic-status":
            print(json.dumps(semantic_status(args.workspace), indent=2))
        elif args.command == "retrieve":
            result = retrieve(args.workspace, args.query, args.mode, args.limit, args.expand, args.kind, args.context_chars, semantic_weight=args.semantic_weight, path_refs=args.path_refs, incoming_expand=args.incoming_expand, source_aware=args.source_aware, reserve_related_chars=args.reserve_related_chars, related_rank=args.related_rank)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                _print_human(format_results(result))
        elif args.command == "evaluate-retrieval":
            print(json.dumps(evaluate(args.workspace, args.dataset, args.limit, mode=args.mode, expand=args.expand, semantic_weight=args.semantic_weight), indent=2))
        elif args.command == 'freeze-benchmark':
            from .benchmark import freeze_benchmark
            print(json.dumps(freeze_benchmark(args.manifest, args.output, args.baseline), indent=2))
        elif args.command == 'benchmark':
            from .benchmark import benchmark as run_benchmark
            print(json.dumps(run_benchmark(args.manifest, mode=args.mode, limit=args.limit, expand=args.expand), indent=2))
        elif args.command == "evaluate-answers":
            provider = provider_from_args(args)
            print(json.dumps(evaluate_answers(args.workspace, args.dataset, provider, args.mode, review=args.review_claims, style=args.style, limit=args.limit, expand=args.expand, context_chars=args.context_chars, followups=args.followups, context_tokens=args.context_tokens, token_encoding=args.token_encoding, model_context_tokens=args.model_context_tokens), indent=2))
        elif args.command == "ask":
            provider = provider_from_args(args)
            result = ask(args.workspace, args.question, provider, args.mode, args.limit,
                         args.expand, args.context_chars, args.followups, review=args.review_claims, style=args.style, show_sources=args.show_sources, context_tokens=args.context_tokens, token_encoding=args.token_encoding, model_context_tokens=args.model_context_tokens)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                _print_human(format_answer(result))
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
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
