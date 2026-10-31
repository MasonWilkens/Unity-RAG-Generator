"""Structured evidence retrieval over versioned code and Unity asset maps."""

from collections import defaultdict

import json

from pathlib import Path

import re

import sqlite3



from .asset_map import asset_report, fingerprint

from .code_map import code_report, _read_snapshot_file

from .discovery import status



MODES = ("search", "exact", "references", "used-by", "inherits", "derived-types")

STOP = {"a", "an", "the", "is", "are", "of", "to", "in", "for", "how", "what", "where", "which", "does"}





def terms(text, unique=True):

    """Keep whole identifiers and add camelCase/PascalCase/snake_case parts."""

    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)

    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", split).replace("_", " ")

    tokens = [t.casefold() for t in re.findall(r"\w+", text + " " + split) if t.casefold() not in STOP]

    return list(dict.fromkeys(tokens)) if unique else tokens





def _inputs(workspace):

    inventory, code, assets = status(workspace), code_report(workspace), asset_report(workspace)

    stamp = {"discovery": inventory["scanned_at"], "code": fingerprint(code), "assets": fingerprint(assets)}

    if assets["code_fingerprint"] != stamp["code"]:

        raise ValueError("Asset map is not linked to this code map. Run map-assets first.")

    return inventory, code, assets, stamp





def build_retrieval(workspace):

    inventory, code, assets, stamp = _inputs(workspace)

    root = Path(inventory["project"])

    entries = {r["path"]: r for r in inventory["entries"]}

    texts = {}

    # Check source hashes before using line ranges or parsed facts.

    for path, row in entries.items():

        if row["status"] == "supported":

            texts[path] = _read_snapshot_file(root, row).splitlines()

    chunks, aliases, relations = [], set(), []

    entities = defaultdict(list)

    def citation(path, start=None, end=None):

        return {"path": path, "start_line": start, "end_line": end,

                "sha256": entries.get(path, {}).get("sha256")}

    def add(entity, kind, evidence, title, body, sources, names=(), parent=None, detail=None):

        chunk_id = "chunk:" + fingerprint([entity, kind, sources, body])[:32]

        item = {"id": chunk_id, "entity_id": entity, "kind": kind, "evidence_type": evidence,

                "title": title, "text": body, "sources": sources, "parent": parent, "detail": detail}

        chunks.append(item)

        entities[entity].append(item)

        for name in (entity, title, *names):

            if name:

                aliases.add((str(name).casefold(), entity))

        return item

    def source_chunks(entity, kind, title, path, start, end, names=(), parent=None):

        lines = texts[path]

        start, end = max(1, start), min(end, len(lines))

        for first in range(start, end + 1, 100):

            last = min(first + 119, end)

            add(entity, kind, "original_source", title, "\n".join(lines[first - 1:last]),

                [citation(path, first, last)], names, parent)

            if last == end:

                break

    declarations = code["declarations"]

    children = defaultdict(list)

    for d in declarations:

        children[d["container"]].append(d)

    type_kinds = {"ClassDeclaration", "StructDeclaration", "InterfaceDeclaration", "RecordDeclaration",

                  "EnumDeclaration", "NamespaceDeclaration", "FileScopedNamespaceDeclaration"}

    for d in declarations:

        loc, path = d["location"], d["location"]["path"]

        title = d["qualified_name"] or d["name"]

        names = [d["name"], path]

        if d["kind"] in type_kinds:

            members = children[d["id"]]

            lines = [d["signature"], "Bases: " + ", ".join(d["bases"])]

            lines += [m["signature"] for m in members]

            add(d["id"], "code_type", "parsed_fact", title, "\n".join(lines),

                [citation(path, loc["start_line"], loc["end_line"])], names, d["container"],

                {"unity_type": d["unity_type"], "assembly": d["assembly"]})

        else:

            source_chunks(d["id"], "code_member", title, path,

                          max(1, loc["start_line"] - d["comments"].count("\n")),

                          loc["end_line"], names, d["container"])

    for asset in assets["assets"]:

        path = asset["path"]

        meta = asset["metadata_path"]

        sources = [citation(path)]

        if meta in texts:

            line = next((i + 1 for i, t in enumerate(texts[meta]) if t.startswith("guid:")), None)

            sources.append(citation(meta, line, line))

        body = (f"Asset: {path}\nGUID: {asset['guid']}\nKind: {asset['kind']}\n"

                f"Inventory: {asset['inventory_status']}\nParsing: {asset['parse_status']}")

        add(asset["id"], "asset", "parsed_fact", path, body, sources,

            [path, Path(path).name, asset["guid"]], detail=asset)

    edges_by_source = defaultdict(list)

    for edge in assets["edges"]:

        edges_by_source[edge["source"]].append(edge)

    for obj in assets["objects"]:

        loc = obj["location"]

        title = f"{obj['path']} / {obj['name'] or obj['type']} #{obj['file_id']}"

        rows = [f"Object: {obj['type']}; role: {obj['role']}; stripped: {obj['stripped']}"]

        rows += [f"{v['property']}: {v['value']}" for v in obj["values"]]

        rows += [f"{e['property']} -> {e.get('target') or e.get('target_asset') or e.get('reference')} [{e['resolution']}]"

                 for e in edges_by_source[obj["id"]]]

        # Group large records while preserving the object identity and raw source location.

        for start in range(0, len(rows), 50):

            add(obj["id"], "asset_object", "parsed_fact", title, "\n".join(rows[start:start + 50]),

                [citation(obj["path"], loc["start_line"], loc["end_line"])],

                [obj["name"], obj["path"], obj["guid"]], obj["asset"],

                {"script": obj.get("script"), "stripped": obj["stripped"]})

    for override in assets["overrides"]:

        loc = override["location"]

        sources = [citation(loc["path"], loc["start_line"], loc["end_line"])]

        if override.get("base_location"):

            base = override["base_location"]

            sources.append(citation(base["path"], base["start_line"], base["end_line"]))

        add(override["instance"] + ":override:" + str(override["index"]), "prefab_override",

            "derived_interpretation" if override["resolution"] == "resolved_scalar_override" else "parsed_fact",

            f"Prefab override {override['record'].get('propertyPath', '')}" if isinstance(override["record"], dict) else "Malformed prefab override",

            json.dumps(override, indent=2), sources, parent=override["instance"], detail=override)

    for path, row in entries.items():

        if row["status"] == "supported" and row["kind"] == "documentation":

            source_chunks("document:" + path, "documentation", path, path, 1, len(texts[path]), [path, Path(path).name])

    for origin, edges in (("code", code["edges"]), ("asset", assets["edges"])):

        for edge in edges:

            if edge["resolution"] == "null":

                continue

            loc = edge["location"]

            relations.append({**edge, "origin": origin, "evidence_type":

                              "derived_interpretation" if edge["resolution"] == "inferred_filename" else "parsed_fact",

                              "sources": [citation(loc["path"], loc["start_line"], loc["end_line"])]})

    report = {"schema_version": 1, "project": str(root), "git": inventory["git"], "inputs": stamp,

              "chunks": len(chunks), "entities": len(entities), "relationships": len(relations),

              "kinds": {kind: sum(c["kind"] == kind for c in chunks) for kind in sorted({c["kind"] for c in chunks})},

              "limits": ["Saved snapshot, not live source.", code["semantic_scope"], assets["coverage_note"]]}

    if _inputs(workspace)[3] != stamp:

        raise ValueError("Maps changed during retrieval build. Retry.")

    with sqlite3.connect(Path(workspace) / "inventory.sqlite3") as db:

        db.execute("BEGIN IMMEDIATE")

        if _database_stamp(db) != stamp:

            raise ValueError("Maps changed during retrieval build. Retry.")

        db.execute("CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, entity TEXT, kind TEXT, data TEXT)")

        db.execute("CREATE INDEX IF NOT EXISTS evidence_entities ON evidence(entity)")

        db.execute("CREATE TABLE IF NOT EXISTS evidence_aliases (alias TEXT, entity TEXT, PRIMARY KEY(alias,entity))")

        db.execute("CREATE TABLE IF NOT EXISTS evidence_relations (source TEXT, target TEXT, target_asset TEXT, kind TEXT, data TEXT)")

        db.execute("CREATE INDEX IF NOT EXISTS evidence_rel_sources ON evidence_relations(source)")

        db.execute("CREATE INDEX IF NOT EXISTS evidence_rel_targets ON evidence_relations(target)")

        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(id UNINDEXED, title, body)")

        for table in ("evidence", "evidence_aliases", "evidence_relations", "evidence_fts"):

            db.execute(f"DELETE FROM {table}")

        db.executemany("INSERT INTO evidence VALUES (?,?,?,?)",

                       [(c["id"], c["entity_id"], c["kind"], json.dumps(c)) for c in chunks])

        db.executemany("INSERT INTO evidence_aliases VALUES (?,?)", sorted(aliases))

        db.executemany("INSERT INTO evidence_relations VALUES (?,?,?,?,?)",

                       [(e.get("source"), e.get("target"), e.get("target_asset"), e["kind"], json.dumps(e)) for e in relations])

        db.executemany("INSERT INTO evidence_fts VALUES (?,?,?)",

                       [(c["id"], " ".join(terms(c["title"], False)), " ".join(terms(c["text"], False))) for c in chunks])

        db.execute("INSERT OR REPLACE INTO metadata VALUES ('retrieval_report', ?)", (json.dumps(report),))

    return report





def _database_stamp(db):

    data = {key: json.loads(value) for key, value in db.execute(

        "SELECT key,value FROM metadata WHERE key IN ('report','code_report','asset_report')")}

    if set(data) != {"report", "code_report", "asset_report"}:

        raise ValueError("Run scan, map-code, and map-assets before building retrieval.")

    inventory, code, assets = data["report"], data["code_report"], data["asset_report"]

    stamp = {"discovery": inventory["scanned_at"], "code": fingerprint(code), "assets": fingerprint(assets)}

    if code["discovery_scanned_at"] != stamp["discovery"] or assets["discovery_scanned_at"] != stamp["discovery"]:

        raise ValueError("Maps are stale. Rebuild code and asset maps before retrieval.")

    if assets["code_fingerprint"] != stamp["code"]:

        raise ValueError("Asset links are stale. Rebuild map-assets before retrieval.")

    return stamp





def _open(workspace):

    path = Path(workspace).resolve() / "inventory.sqlite3"

    if not path.is_file():

        raise ValueError("No inventory. Run scan first.")

    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)

    db.execute("BEGIN")

    try:

        stamp = _database_stamp(db)

    except Exception:

        db.close()

        raise

    row = db.execute("SELECT value FROM metadata WHERE key='retrieval_report'").fetchone()

    if row is None:

        db.close()

        raise ValueError("No retrieval index. Run build-retrieval first.")

    report = json.loads(row[0])

    if report["inputs"] != stamp:

        db.close()

        raise ValueError("Retrieval index is stale. Run build-retrieval again.")

    return db, report





def retrieval_status(workspace):

    db, report = _open(workspace)

    db.close()

    return report





def retrieve(workspace, query, mode="search", limit=5, expand=0, kind=None):

    if mode not in MODES or not 1 <= limit <= 100 or not 0 <= expand <= 20:

        raise ValueError("Use a supported mode, limit 1–100, and expansion count 0–20.")

    if kind and mode not in ("search", "exact"):
        raise ValueError("Kind filters apply to search and exact modes only.")
    if len(query) > 2000:

        raise ValueError("Query is too long (maximum 2000 characters).")

    db, report = _open(workspace)

    try:

        matched = [r[0] for r in db.execute("SELECT entity FROM evidence_aliases WHERE alias=? ORDER BY entity", (query.casefold(),))]

        results = []

        if mode in ("references", "used-by", "inherits", "derived-types"):

            incoming = mode in ("used-by", "derived-types")

            # Exact entity identities only: ambiguous names retain every matching identity.

            selected = set(matched)

            asset_matches = {e for e in selected if e.startswith("asset:") and "#" not in e}

            if incoming and asset_matches:

                selected = asset_matches

            if query:

                selected.add(query)  # Allows external symbol IDs without a declaration.

            seen = set()

            for entity in sorted(selected):

                if incoming:

                    rows = db.execute("SELECT data FROM evidence_relations WHERE target=? OR target_asset=?", (entity, entity))

                else:

                    rows = db.execute("SELECT data FROM evidence_relations WHERE source=?", (entity,))

                for (raw,) in rows:

                    edge = json.loads(raw)

                    if mode in ("inherits", "derived-types") and edge["kind"] != "inherits_or_implements":

                        continue

                    key = fingerprint(edge)

                    if key not in seen:

                        results.append(edge)

                        seen.add(key)

            results.sort(key=lambda e: (e["sources"][0]["path"], e["sources"][0]["start_line"], e["kind"], e.get("target") or ""))

            total = len(results)

            results = results[:limit]

        else:

            candidates = {}

            if matched:

                for entity in matched:

                    for (raw,) in db.execute("SELECT data FROM evidence WHERE entity=? ORDER BY id", (entity,)):

                        item = json.loads(raw)

                        if kind and item["kind"] != kind:

                            continue

                        candidates[item["id"]] = {**item, "match": "exact_alias", "score": None}

            tokens = terms(query)

            if mode == "search" and tokens:

                expression = " OR ".join('"' + t + '"' for t in tokens[:64])

                rows = db.execute(

                    "SELECT e.data, bm25(evidence_fts,0,5,1) FROM evidence_fts JOIN evidence e ON e.id=evidence_fts.id "

                    "WHERE evidence_fts MATCH ? AND (? IS NULL OR e.kind=?) ORDER BY bm25(evidence_fts,0,5,1),e.id LIMIT ?",

                    (expression, kind, kind, max(100, limit * 10)))

                for raw, score in rows:

                    item = json.loads(raw)

                    candidates.setdefault(item["id"], {**item, "match": "keyword", "score": score})

            ordered = sorted(candidates.values(), key=lambda r: (

                r["match"] != "exact_alias", r["score"] if r["score"] is not None else 0,

                r["sources"][0]["path"], r["sources"][0]["start_line"] or 0, r["id"]))

            # Identical overlapping source ranges should not consume separate result slots.

            seen = set()

            for item in ordered:

                key = (item["entity_id"], fingerprint(item["sources"]), item["text"])

                if key not in seen:

                    results.append(item)

                    seen.add(key)

            total = len(results)

            results = results[:limit]

        context, seen_context = [], {r.get("id") for r in results}

        for hit in results:

            if len(context) >= expand:

                break

            entity = hit.get("entity_id") or hit.get("source")

            related = []

            if hit.get("parent"):

                related.append((hit["parent"], "containing_entity", None))

            for raw, in db.execute("SELECT data FROM evidence_relations WHERE source=? ORDER BY kind,target", (entity,)):

                edge = json.loads(raw)

                target = edge.get("target") or edge.get("target_asset")

                if target:

                    related.append((target, edge["kind"], edge))

            for target, reason, edge in related:

                for raw, in db.execute("SELECT data FROM evidence WHERE entity=? ORDER BY id", (target,)):

                    item = json.loads(raw)

                    if item["id"] not in seen_context and len(context) < expand:

                        context.append({**item, "expansion": {"from": entity, "reason": reason, "relationship": edge}})

                        seen_context.add(item["id"])

        return {"schema_version": 1, "project": report["project"], "snapshot": report["inputs"],

                "git": report["git"], "query": query, "mode": mode, "matched_entities": matched,

                "results": results, "context": context, "candidate_count": total,

                "truncated": total > limit, "limits": report["limits"],

                "ranking": "Exact aliases first, then SQLite BM25 (lower is better); keyword candidates capped at max(100,10*limit)."}

    finally:

        db.close()





def format_results(result):

    lines = [f"{len(result['results'])} results; {len(result['context'])} context records."]

    for row in result["results"] + result["context"]:

        source = row["sources"][0]

        where = source["path"] + (f":{source['start_line']}-{source['end_line']}" if source["start_line"] else "")

        label = row.get("title") or f"{row.get('source')} -> {row.get('target') or row.get('target_asset')}"

        lines.append(f"\n{label}\n{where} [{row['evidence_type']}]")

        lines.append(row.get("text") or f"{row['kind']} [{row['resolution']}]")

    if not result["results"]:

        lines.append("No matching evidence.")

    return "\n".join(lines)
