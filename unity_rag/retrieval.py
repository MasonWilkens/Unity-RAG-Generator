"""Structured evidence retrieval over versioned code and Unity asset maps."""
from contextlib import closing

from .workspace import resolve_workspace, require_writable
from collections import defaultdict

import json

from pathlib import Path

import re

import sqlite3



from .asset_map import asset_report, fingerprint, decode_asset_report

from .code_map import code_report, _read_snapshot_file

from .discovery import status



MODES = ("search", "semantic", "hybrid", "exact", "references", "used-by", "inherits", "derived-types")

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





def build_retrieval(workspace, *, chunk_budget=4096, chunk_encoding='utf8-bytes'):

    workspace = require_writable(workspace)
    inventory, code, assets, stamp = _inputs(workspace)
    from .chunking import ChunkPolicy
    policy = ChunkPolicy(chunk_budget, chunk_encoding)

    root = Path(inventory["project"])

    entries = {r["path"]: r for r in inventory["entries"]}

    # Verify every supported file, but retain at most four decoded files. Large
    # projects previously retained all source lines alongside both parsed maps.
    from functools import lru_cache
    @lru_cache(maxsize=4)
    def text_lines(path):
        return _read_snapshot_file(root, entries[path]).splitlines()
    for path, row in entries.items():
        if row['status'] == 'supported':
            text_lines(path)
    text_lines.cache_clear()

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

        lines = text_lines(path)

        start, end = max(1, start), min(end, len(lines))

        selected = lines[start - 1:end]
        for a, b in policy.windows(selected):
            first, last = start + a, start + b - 1

            add(entity, kind, "original_source", title, "\n".join(lines[first - 1:last]),

                [citation(path, first, last)], names, parent)


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

            heading = d['signature'] + '\nBases: ' + ', '.join(d['bases'])
            lines = [m['signature'] for m in members] or ['']
            for first, last in policy.windows(lines, max_lines=50, overlap=0, heading=heading):
                add(d['id'], 'code_type', 'parsed_fact', title,
                    '\n'.join([heading, *lines[first:last]]),
                    [citation(path, loc['start_line'], loc['end_line'])], names, d['container'],
                    {'unity_type': d['unity_type'], 'assembly': d['assembly']})

        else:

            source_chunks(d["id"], "code_member", title, path,

                          max(1, loc["start_line"] - d["comments"].count("\n")),

                          loc["end_line"], names, d["container"])

    for asset in assets["assets"]:

        path = asset["path"]

        meta = asset["metadata_path"]

        sources = [citation(path)]

        if entries.get(meta, {}).get('status') == 'supported':

            line = next((i + 1 for i, t in enumerate(text_lines(meta)) if t.startswith("guid:")), None)

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

        for start, end in policy.windows(rows, max_lines=50, overlap=0):

            add(obj["id"], "asset_object", "parsed_fact", title, "\n".join(rows[start:end]),

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

        source_kinds = {'documentation': 'documentation', 'shader': 'shader_source',
                        'ui': 'ui_source', 'configuration': 'configuration_source',
                        'assembly_definition': 'configuration_source', 'assembly_reference': 'configuration_source'}
        if row['status'] == 'supported' and row['kind'] in source_kinds:
            kind = source_kinds[row['kind']]
            entity = ('document:' if kind == 'documentation' else 'source:') + path
            source_chunks(entity, kind, path, path, 1, len(text_lines(path)), [path, Path(path).name])

    for origin, edges in (("code", code["edges"]), ("asset", assets["edges"])):

        for edge in edges:

            if edge["resolution"] == "null":

                continue

            loc = edge["location"]

            relations.append({**edge, "origin": origin, "evidence_type":

                              "derived_interpretation" if edge["resolution"] == "inferred_filename" else "parsed_fact",

                              "sources": [citation(loc["path"], loc["start_line"], loc["end_line"])]})

    report = {"schema_version": 1, "project": str(root), "git": inventory["git"], "inputs": stamp,
              "chunking": {**policy.config, 'oversized_lines': policy.oversized_lines},

              "chunks": len(chunks), "entities": len(entities), "relationships": len(relations),

              "kinds": {kind: sum(c["kind"] == kind for c in chunks) for kind in sorted({c["kind"] for c in chunks})},

              "limits": ["Saved snapshot, not live source.", code["semantic_scope"], assets["coverage_note"]]}

    # The materialized maps and source text are no longer needed after evidence
    # construction. Recheck their stamp inside the following write transaction.
    text_lines.cache_clear()
    del source_chunks, citation, add, inventory, code, assets, text_lines, entries
    del entities, children, declarations, edges_by_source, edges

    with closing(sqlite3.connect(Path(workspace) / "inventory.sqlite3")) as db, db:

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

                       ((c["id"], c["entity_id"], c["kind"], json.dumps(c)) for c in chunks))

        db.executemany("INSERT INTO evidence_aliases VALUES (?,?)", sorted(aliases))

        db.executemany("INSERT INTO evidence_relations VALUES (?,?,?,?,?)",

                       ((e.get("source"), e.get("target"), e.get("target_asset"), e["kind"], json.dumps(e)) for e in relations))

        db.executemany("INSERT INTO evidence_fts VALUES (?,?,?)",

                       ((c["id"], " ".join(terms(c["title"], False)), " ".join(terms(c["text"], False))) for c in chunks))

        db.execute("INSERT OR REPLACE INTO metadata VALUES ('retrieval_report', ?)", (json.dumps(report),))

    return report





def _database_stamp(db, seal=None, *, include_hashes=False):

    raw = dict(db.execute(

        "SELECT key,value FROM metadata WHERE key IN ('report','code_report','asset_report')"))

    if set(raw) != {"report", "code_report", "asset_report"}:

        raise ValueError("Run scan, map-code, and map-assets before building retrieval.")

    inventory = json.loads(raw['report'])
    raw['asset_report'] = decode_asset_report(raw['asset_report'])

    code_id = _hashlib.sha256(raw['code_report'].encode('utf-8')).digest()
    asset_id = _hashlib.sha256(raw['asset_report'].encode('utf-8')).digest()
    # A published seal is produced only after full validation, and is checked
    # against both complete metadata records on every use. Invalid/missing seals
    # fall back to the original full canonical validation.
    if (isinstance(seal, dict) and seal.get('version') == 1
            and seal.get('code_raw_sha256') == code_id.hex()
            and seal.get('asset_raw_sha256') == asset_id.hex()
            and isinstance(seal.get('stamp'), dict)
            and set(seal['stamp']) == {'discovery', 'code', 'assets'}
            and all(isinstance(v, str) for v in seal['stamp'].values())
            and seal['stamp']['discovery'] == inventory['scanned_at']):
        stamp = dict(seal['stamp'])
        return (stamp, code_id.hex(), asset_id.hex()) if include_hashes else stamp
    key = (code_id, asset_id)
    with _stamp_cache_lock:
        cached = _stamp_cache.get(key)
        if cached is not None:
            _stamp_cache.move_to_end(key)
    if cached is None:
        code = json.loads(raw['code_report'])
        assets = json.loads(raw['asset_report'])
        cached = (fingerprint(code), fingerprint(assets), code['discovery_scanned_at'],
                  assets['discovery_scanned_at'], assets['code_fingerprint'])
        # Store only content digests and scalar stamps, never the 700+ MB reports.
        with _stamp_cache_lock:
            _stamp_cache[key] = cached
            _stamp_cache.move_to_end(key)
            while len(_stamp_cache) > 4:
                _stamp_cache.popitem(last=False)
    code_fingerprint, asset_fingerprint, code_time, asset_time, linked_code = cached
    stamp = {"discovery": inventory["scanned_at"], "code": code_fingerprint, "assets": asset_fingerprint}

    if code_time != stamp["discovery"] or asset_time != stamp["discovery"]:

        raise ValueError("Maps are stale. Rebuild code and asset maps before retrieval.")

    if linked_code != stamp["code"]:

        raise ValueError("Asset links are stale. Rebuild map-assets before retrieval.")

    return (stamp, code_id.hex(), asset_id.hex()) if include_hashes else stamp


import hashlib as _hashlib
from collections import OrderedDict as _OrderedDict
from threading import RLock as _RLock

# A bounded, process-local cache. Every hit still hashes both complete reports.
_stamp_cache = _OrderedDict()
_stamp_cache_lock = _RLock()


def _trusted_seal(workspace):
    """Only the active pointer can bind a seal to a published manifest."""
    workspace = Path(workspace)
    if workspace.parent.name != 'generations':
        return None
    pointer = workspace.parent.parent / 'active.json'
    if not pointer.is_file():
        return None
    reference = json.loads(pointer.read_text(encoding='utf-8'))
    if reference.get('generation') != workspace.name or 'manifest_sha256' not in reference:
        return None
    raw = (workspace / 'complete.json').read_bytes()
    if _hashlib.sha256(raw).hexdigest() != reference['manifest_sha256']:
        raise ValueError('Active generation manifest checksum does not match its pointer.')
    return json.loads(raw).get('metadata_seal')





def _open(workspace):

    workspace = resolve_workspace(workspace)
    path = Path(workspace).resolve() / "inventory.sqlite3"

    if not path.is_file():

        raise ValueError("No inventory. Run scan first.")

    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)

    try:
        db.execute("BEGIN")
        stamp = _database_stamp(db, _trusted_seal(workspace))
        row = db.execute("SELECT value FROM metadata WHERE key='retrieval_report'").fetchone()
        if row is None:
            raise ValueError("No retrieval index. Run build-retrieval first.")
        report = json.loads(row[0])
        if report["inputs"] != stamp:
            raise ValueError("Retrieval index is stale. Run build-retrieval again.")
    except BaseException:
        db.close()
        raise
    return db, report





def retrieval_status(workspace):

    db, report = _open(workspace)

    db.close()

    return report





def _search_terms(query):
    """Add bounded literal inflection variants to keyword queries only."""
    original = terms(query)
    # Preserve a standalone term even if it is normally a filler word.
    filtered = [t for t in original if t not in {"and", "when", "it", "from"}] or original
    extra = []
    for token in filtered:
        if len(token) >= 6 and token.endswith("ing"):
            extra.extend((token[:-3], token[:-3] + "e"))
        if len(token) >= 5 and token.endswith("ed"):
            extra.extend((token[:-2], token[:-1]))
        if len(token) >= 5 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
            extra.append(token[:-1])
        if token.startswith(("initializ", "initialis")):
            extra.append("init")
    return list(dict.fromkeys(filtered + extra))


def _keyword_candidates(db, tokens, kind, limit):
    """Bounded, literal FTS candidate stage; exact matches are merged separately."""
    expression = " OR ".join('"' + t + '"' for t in tokens[:64])
    rows = db.execute(
        "SELECT e.data, bm25(evidence_fts,0,5,1) FROM evidence_fts JOIN evidence e ON e.id=evidence_fts.id "
        "WHERE evidence_fts MATCH ? AND (? IS NULL OR e.kind=?) ORDER BY bm25(evidence_fts,0,5,1),e.id LIMIT ?",
        (expression, kind, kind, limit))
    return [{**json.loads(raw), "match": "keyword", "score": score} for raw, score in rows]


def _scene_name_candidates(query, fallback=()):
    """Prefer names explicitly adjacent to 'scene'; ignore generic determiners."""
    generic = STOP | {'this', 'that', 'current', 'same', 'named', 'your', 'our', 'my',
                      'each', 'every', 'specific', 'selected', 'requested', 'active', 'loaded', 'given',
                      'game', 'unity', 'editor', 'runtime', 'project', 'component',
                      'first', 'last', 'next', 'previous', 'main', 'opening'}
    leads = {'the', 'in', 'for', 'of', 'from', 'to', 'at', 'on', 'a', 'an', 'this', 'that',
             'your', 'our', 'my', 'each', 'every', 'selected', 'named', 'called'}
    # Do not silently reduce an unsupported multi-word scene name to its last word.
    pairs = re.finditer(r'\b([A-Za-z][A-Za-z0-9_]*)\s+([A-Za-z][A-Za-z0-9_]*)(?:\.unity)?\s+scene\b', query, re.I)
    if (any(m.group(1).casefold() not in leads for m in pairs)
            or re.search(r'\b[A-Za-z0-9_]+[-/][A-Za-z0-9_./-]+\s+scene\b', query, re.I)):
        return None
    mentions = [m.group(1) for m in re.finditer(r'\b([A-Za-z][A-Za-z0-9_]{2,127})(?:\.unity)?\s+scene\b', query, re.I)]
    reverse_pattern = r'\bscene\s+(?:named|called)\s+([A-Za-z][A-Za-z0-9_]{2,127})(?:\.unity)?\b'
    reverse_matches = list(re.finditer(reverse_pattern, query, re.I))
    next_word = re.compile(r'\s+([A-Za-z][A-Za-z0-9_]*)')
    grammar = STOP | {'is', 'are', 'was', 'were', 'has', 'have', 'contains', 'with', 'where', 'which',
                      'does', 'that', 'what', 'how', 'uses', 'its', 'serialized', 'component', 'and', 'or'}
    for match in reverse_matches:
        following = next_word.match(query, match.end())
        if following and following.group(1).casefold() not in grammar:
            return None
    reverse = [m.group(1) for m in reverse_matches]
    explicit = [name for name in mentions + reverse if name.casefold() not in generic]
    return list(dict.fromkeys(explicit if mentions or reverse else fallback))


def _promote_related_members(ordered, tokens, query):
    """Promote at most two same-container methods matching uncovered query terms."""
    raw = terms(query)
    inflected = {t for t in raw if len(t) >= 5 and t.endswith(("ed", "ing"))}
    if ("and" not in raw or len(inflected) < 2 or not ordered
            or ordered[0].get("kind") != "code_member" or not ordered[0].get("parent")):
        return ordered, False
    def member_terms(item):
        simple = item["title"].split("(", 1)[0].rsplit(".", 1)[-1]
        return {t for t in _search_terms(simple) if len(t) >= 4}
    first = ordered[0]
    remaining = {t for t in tokens if len(t) >= 4} - member_terms(first)
    selected, seen = [], set()
    for item in ordered[1:]:
        matched_terms = remaining & member_terms(item)
        if (item.get("kind") == "code_member" and item.get("parent") == first["parent"]
                and item.get("entity_id") not in seen and matched_terms):
            chosen = dict(item)
            chosen["ranking_diagnostics"] = {**item.get("ranking_diagnostics", {}),
                                             "related_method_terms": sorted(matched_terms)}
            selected.append(chosen)
            seen.add(item["entity_id"])
            if len(selected) == 2:
                break
    if not selected:
        return ordered, False
    selected_ids = {item["id"] for item in selected}
    return [first] + selected + [item for item in ordered[1:] if item["id"] not in selected_ids], True


def _explicit_name_in_query(value, query):
    return len(value) >= 4 and bool(re.search(r'(?<!\w)' + re.escape(value) + r'(?!\w)', query, re.I))


def _promote_named_root_position(db, ordered, query):
    """Promote only a source-validated, unique root instance in a named prefab.

    No prefab expansion, runtime evaluation, array overrides, or fuzzy name
    matching is attempted. Bounds are fail-closed, not partial uniqueness.
    """
    # A local serialized coordinate is not a world-space position.
    if (not re.search(r'\bprefab\b', query, re.I)
            or not re.search(r'\b(?:local|serialized)\b', query, re.I)):
        return ordered, False
    axes = set()
    for match in re.finditer(r'\b([xyz])\s+(?:axis\s+)?position\b|\bposition\s+(?:on\s+)?(?:the\s+)?([xyz])(?:\s+axis)?\b', query, re.I):
        axes.add((match.group(1) or match.group(2)).lower())
    if len(axes) != 1:
        return ordered, False
    prop = 'm_LocalPosition.' + next(iter(axes))
    # Only inspect prefab paths already present in the candidate set, and only
    # when their entire basename is named by the question and is globally unique.
    paths = set()
    for item in ordered:
        entity = item.get('entity_id', '') if item.get('kind') == 'asset_object' else item.get('parent', '')
        if not isinstance(entity, str) or not entity.startswith('asset:') or '#' not in entity:
            continue
        path = entity[6:].rsplit('#', 1)[0]
        name = Path(path).name
        if not name.lower().endswith('.prefab') or not _explicit_name_in_query(Path(path).stem, query):
            continue
        paths.add(path)
    if not paths or len(paths) > 8:
        return ordered, False
    options = []
    for path in sorted(paths):
        name = Path(path).name.casefold()
        assets = db.execute("SELECT DISTINCT e.entity FROM evidence_aliases a JOIN evidence e ON e.entity=a.entity WHERE a.alias=? AND e.kind='asset' LIMIT 2", (name,)).fetchall()
        if assets != [('asset:' + path,)]:
            return ordered, False
        prefix = 'asset:' + path + '#'
        object_rows = db.execute('SELECT data FROM unity_objects WHERE id>=? AND id<? LIMIT 5001', (prefix, prefix + '\uffff'))
        total_chars = 0
        for count, (raw,) in enumerate(object_rows, 1):
            total_chars += len(raw)
            if count > 5000 or len(raw) > 1000000 or total_chars > 20000000:
                return ordered, False
            obj = json.loads(raw)
            if obj.get('type') != 'PrefabInstance':
                continue
            modification = obj.get('data', {}).get('m_Modification', {})
            changes = modification.get('m_Modifications', []) if isinstance(modification, dict) else []
            if not isinstance(changes, list) or len(changes) > 500:
                return ordered, False
            for index, change in enumerate(changes):
                if not isinstance(change, dict) or change.get('propertyPath') != 'm_Name':
                    continue
                value = change.get('value')
                if isinstance(value, str) and _explicit_name_in_query(value, query):
                    options.append((len(value), obj, index))
    if not options:
        return ordered, False
    length = max(option[0] for option in options)
    longest = [option for option in options if option[0] == length]
    # Check all instances in each matched prefab, not merely top-ranked ones.
    if len(longest) != 1:
        return ordered, False
    _, instance, name_index = longest[0]
    winner = instance['data']['m_Modification']['m_Modifications'][name_index]['value']
    winning_spans = [m.span() for m in re.finditer(r'(?<!\w)' + re.escape(winner) + r'(?!\w)', query, re.I)]
    for _, other, other_index in options:
        value = other['data']['m_Modification']['m_Modifications'][other_index]['value']
        occurrences = [m.span() for m in re.finditer(r'(?<!\w)' + re.escape(value) + r'(?!\w)', query, re.I)]
        if any(not any(a <= start and end <= b for a, b in winning_spans) for start, end in occurrences):
            return ordered, False
    prefix = instance['id'] + ':override:'
    rows = db.execute('SELECT data FROM evidence WHERE entity>=? AND entity<? AND kind=? LIMIT 501',
                      (prefix, prefix + '\uffff', 'prefab_override'))
    records, total_chars = [], 0
    for count, (raw,) in enumerate(rows, 1):
        total_chars += len(raw)
        if count > 500 or len(raw) > 1000000 or total_chars > 20000000:
            return ordered, False
        records.append(json.loads(raw))
    named = [r for r in records if r['entity_id'] == prefix + str(name_index) and r['detail'].get('resolution') == 'resolved_scalar_override']
    positions = [r for r in records if r['detail'].get('resolution') == 'resolved_scalar_override' and r['detail'].get('property') == prop]
    if len(named) != 1:
        return ordered, False
    name = named[0]
    name_target = db.execute('SELECT data FROM unity_objects WHERE id=?', (name['detail'].get('target'),)).fetchone()
    if not name_target or len(name_target[0]) > 1000000 or json.loads(name_target[0]).get('type') != 'GameObject':
        return ordered, False
    found = []
    for position in positions:
        target = position['detail'].get('target')
        if not isinstance(target, str) or '#' not in target:
            continue
        row = db.execute('SELECT data FROM unity_objects WHERE id=?', (target,)).fetchone()
        if not row or len(row[0]) > 1000000:
            return ordered, False
        target_obj = json.loads(row[0]); data = target_obj.get('data', {})
        if not isinstance(data, dict):
            return ordered, False
        go = data.get('m_GameObject'); father = data.get('m_Father')
        if (target_obj.get('type') != 'Transform' or not isinstance(go, dict) or not isinstance(father, dict)
                or father.get('fileID') != '0' or go.get('guid') not in (None, '', '0' * 32)
                or name['detail'].get('target') != target.rsplit('#', 1)[0] + '#' + str(go.get('fileID', ''))):
            continue
        found.append(position)
    if len(found) != 1:
        return ordered, False
    result = found[0]
    # The root-name override is independent cited evidence for this association.
    name_source = name['sources'][0]
    result['sources'] = list(result['sources']) + [name_source]
    result['match'] = 'named_root_position'
    result['score'] = None
    result['ranking_diagnostics'] = {'named_root_position': instance['id'],
        'root_name_override': name['detail']['effective_value']}
    return [result] + [item for item in ordered if item['id'] != result['id']], True


def _tag_query_quote_regions(query):
    """Find quoted ranges without mistaking contractions for opening quotes."""
    regions = []
    opened = None
    escaped = False
    quotes = {"'", '"', '`'}
    for index, char in enumerate(query):
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if opened is not None:
            quote, start = opened
            if char == quote:
                if (char == "'" and index > start + 1 and index + 1 < len(query)
                        and query[index - 1].isalnum() and query[index + 1].isalnum()):
                    continue
                regions.append((start, index, True))
                opened = None
        elif char in quotes:
            if char == "'" and index and (query[index - 1].isalnum() or query[index - 1] == '_'):
                continue
            opened = (char, index)
    if opened is not None:
        regions.append((opened[1], len(query), False))
    return regions


def _tag_query_match_is_whole(query, span, regions):
    """Disallow a tag candidate that is only part of a quoted name."""
    start, end = span
    for opening, closing, complete in regions:
        if opening < end and start <= closing:
            return complete and start == opening + 1 and end == closing
    quotes = {"'", '"', '`'}
    if ((start and query[start - 1] in quotes)
            or (end < len(query) and query[end] in quotes)):
        return False
    return True


def _promote_declared_unity_tag(db, ordered, query):
    """Surface static declaration, assignment, and literal checks of a named tag."""
    if (not re.search(r'\btags?\b', query, re.I)
            or re.search(r'\b(?:compare|comparison|versus|vs|difference|different|differs?|both)\b', query, re.I)):
        return ordered, False
    prefix = 'asset:ProjectSettings/TagManager.asset#'
    rows = db.execute('SELECT data FROM unity_objects WHERE id>=? AND id<? LIMIT 2', (prefix, prefix + '\uffff')).fetchall()
    if len(rows) != 1 or len(rows[0][0]) > 1000000:
        return ordered, False
    manager = json.loads(rows[0][0])
    tags = manager.get('data', {}).get('tags')
    if (manager.get('type') != 'TagManager' or not isinstance(tags, list) or len(tags) > 10000
            or any(isinstance(key, str) and '.' in key for key in manager.get('data', {}))):
        return ordered, False
    matches = []
    quote_regions = _tag_query_quote_regions(query)
    for tag in tags:
        if not isinstance(tag, str) or not re.fullmatch(r'\w(?:[\w. -]{0,62}\w)?', tag):
            continue
        escaped = re.escape(tag)
        before = r'(?<![\w.-])[`\'\"]?(?P<tag>' + escaped + r')[`\'\"]?\s+(?:Unity\s+)?(?:GameObject\s+)?tags?\b'
        after = r'\btags?\s+(?:(?:named|called)\s+[`\'\"]?(?P<tag>' + escaped + r')[`\'\"]?|[`\'\"](?P<quoted>' + escaped + r')[`\'\"])(?!\w|[.-]\w)'
        candidates = ((match, name) for pattern in (before, after)
                      for match in re.finditer(pattern, query, re.I)
                      for name in ('tag', 'quoted') if name in match.re.groupindex and match.start(name) >= 0)
        if any(_tag_query_match_is_whole(query, match.span(name), quote_regions) for match, name in candidates):
            matches.append(tag)
    if len(matches) != 1:
        return ordered, False
    tag = matches[0]
    definitions, definition_chars = [], 0
    for count, (raw,) in enumerate(db.execute("SELECT data FROM evidence WHERE entity=? AND kind='asset_object' ORDER BY id LIMIT 101", (manager['id'],)), 1):
        definition_chars += len(raw)
        if count > 100 or len(raw) > 1000000 or definition_chars > 20000000:
            return ordered, False
        item = json.loads(raw)
        if re.search(r'(?m)^tags\.\d+:\s*' + re.escape(tag) + r'\s*$', item.get('text', ''), re.I):
            definitions.append(item)
    if len(definitions) != 1:
        return ordered, False
    definition = definitions[0]
    if not definition.get('sources') or not definition['sources'][0].get('sha256'):
        return ordered, False
    code_pattern = re.compile(r'\b(?:CompareTag|FindGameObjects?WithTag)\s*\(\s*@?"' + re.escape(tag) + r'"\s*\)')
    rows = db.execute("SELECT e.data FROM evidence_fts f JOIN evidence e ON e.id=f.id WHERE evidence_fts MATCH ? AND e.kind IN ('asset_object','code_member') ORDER BY bm25(evidence_fts,0,5,1),e.id LIMIT 1001", ('"' + tag + '"',))
    assigned, checked, total_chars = [], [], 0
    relation_count = relation_chars = 0
    for count, (raw,) in enumerate(rows, 1):
        total_chars += len(raw)
        if count > 1000 or len(raw) > 1000000 or total_chars > 20000000:
            return ordered, False
        item = json.loads(raw)
        if item.get('kind') == 'code_member':
            # Roslyn's invocation records exclude comments and string literals;
            # unresolved bindings are retained only as syntactic call evidence.
            relations = db.execute("SELECT data FROM evidence_relations WHERE source=? AND kind='calls' LIMIT 501", (item['entity_id'],))
            for index, (relation_raw,) in enumerate(relations, 1):
                relation_count += 1
                relation_chars += len(relation_raw)
                if (index > 500 or relation_count > 5000 or len(relation_raw) > 1000000
                        or relation_chars > 20000000):
                    return ordered, False
                relation = json.loads(relation_raw)
                loc = relation.get('location') or {}
                source = item.get('sources', [{}])[0]
                line_values = (source.get('start_line'), loc.get('start_line'),
                               loc.get('end_line'), source.get('end_line'))
                if (all(type(value) is int for value in line_values)
                        and code_pattern.search(relation.get('text', '')) and loc.get('path') == source.get('path')
                        and line_values[0] <= line_values[1] <= line_values[2] <= line_values[3]):
                    checked.append(item)
                    break
        elif item.get('kind') == 'asset_object' and re.search(r'(?m)^m_TagString:\s*' + re.escape(tag) + r'\s*$', item.get('text', ''), re.I):
            row = db.execute('SELECT data FROM unity_objects WHERE id=?', (item['entity_id'],)).fetchone()
            if row:
                if len(row[0]) > 1000000:
                    return ordered, False
                obj = json.loads(row[0])
                if obj.get('type') == 'GameObject' and obj.get('data', {}).get('m_TagString') == tag:
                    assigned.append(item)
    if not assigned and not checked:
        return ordered, False
    selected = [definition] + assigned[:1] + checked[:1]
    for item in selected:
        item['match'] = 'declared_unity_tag'
        item['score'] = None
        item['ranking_diagnostics'] = {'declared_unity_tag': tag}
        if item is not definition:
            item['sources'] = list(item['sources']) + [{**definition['sources'][0], 'role': 'reference_dependency'}]
    ids = {item['id'] for item in selected}
    return selected + [item for item in ordered if item['id'] not in ids], True


def _promote_unique_named_type_method(db, ordered, query):
    """Favor one uniquely relevant method in an explicitly named indexed type."""
    if not re.search(r'\bhow\b', query, re.I):
        return ordered, False
    names = list(dict.fromkeys(re.findall(r'\b[A-Z][A-Za-z0-9_]{2,127}\b', query)))
    if len(names) > 12:
        return ordered, False
    types = {}
    for name in names:
        rows = db.execute("SELECT DISTINCT e.entity FROM evidence_aliases a JOIN evidence e ON e.entity=a.entity WHERE a.alias=? AND e.kind='code_type' LIMIT 2", (name.casefold(),)).fetchall()
        if len(rows) == 1:
            types.setdefault(rows[0][0], name)
        elif len(rows) > 1:
            return ordered, False
    if len(types) != 1:
        return ordered, False
    owner, name = next(iter(types.items()))
    stop = STOP | {'how', 'does', 'and', 'or', 'from', 'choose', 'chooses'} | set(terms(name))
    def stem(word):
        for suffix in ('ation', 'ations', 'ing', 'ed', 'es', 's', 'e'):
            if len(word) - len(suffix) >= 4 and word.endswith(suffix):
                word = word[:-len(suffix)]
                break
        return word[:-2] if len(word) > 6 and word.endswith('at') else word
    wanted = {stem(word) for word in terms(query) if word not in stop and len(word) >= 2}
    if len(wanted) < 2:
        return ordered, False
    methods = {}
    total_relations = total_relation_chars = method_chunks = 0
    for item in ordered:
        if item.get('parent') != owner or '::M:' not in item.get('entity_id', ''):
            continue
        method_chunks += 1
        if method_chunks > 100:
            return ordered, False
        sources = item.get('sources') or []
        source = sources[0] if sources else {}
        if type(source.get('start_line')) is not int or type(source.get('end_line')) is not int:
            continue
        identifiers, relation_count, relation_chars = [], 0, 0
        rows = db.execute("SELECT data FROM evidence_relations WHERE source=? AND kind='references' LIMIT 1001", (item['entity_id'],))
        for (raw,) in rows:
            relation_count += 1
            relation_chars += len(raw)
            total_relations += 1
            total_relation_chars += len(raw)
            if (relation_count > 1000 or len(raw) > 1000000 or relation_chars > 10000000
                    or total_relations > 5000 or total_relation_chars > 20000000):
                return ordered, False
            edge = json.loads(raw)
            loc = edge.get('location') or {}
            if (loc.get('path') == source.get('path') and type(loc.get('start_line')) is int
                    and type(loc.get('end_line')) is int
                    and source['start_line'] <= loc['start_line'] <= loc['end_line'] <= source['end_line']):
                identifiers.append(edge.get('text', ''))
        covered = wanted & {stem(word) for word in terms(' '.join(identifiers))}
        if len(covered) >= 2:
            previous = methods.get(item['entity_id'])
            if previous is None or len(covered) > previous[0]:
                methods[item['entity_id']] = (len(covered), item)
    ranked = sorted(methods.values(), key=lambda x: (-x[0], x[1]['id']))
    if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
        return ordered, False
    item = dict(ranked[0][1])
    type_rows = db.execute("SELECT data FROM evidence WHERE entity=? AND kind='code_type' LIMIT 101", (owner,))
    item['sources'] = list(item.get('sources', []))
    paths = {source.get('path') for source in item['sources']}
    type_count = type_chars = 0
    for (raw,) in type_rows:
        type_count += 1
        type_chars += len(raw)
        if type_count > 100 or len(raw) > 1000000 or type_chars > 20000000:
            return ordered, False
        type_sources = json.loads(raw).get('sources') or []
        if not type_sources or not type_sources[0].get('sha256'):
            return ordered, False
        if type_sources[0].get('path') not in paths:
            item['sources'].append({**type_sources[0], 'role': 'reference_dependency'})
            paths.add(type_sources[0].get('path'))
    if not type_count:
        return ordered, False
    item['ranking_diagnostics'] = {**item.get('ranking_diagnostics', {}), 'named_type_method': owner}
    return [item] + [x for x in ordered if x['id'] != item['id']], True


def _reference_dependencies(db, entries, item):
    """Record snapshot files required by displayed serialized-link interpretations."""
    direct = {s.get("path") for s in item.get("sources", [])}
    dependencies = {}
    def add(path):
        if not path or path in direct or path in dependencies:
            return
        entry = entries.get(path, {})
        dependencies[path] = {"path": path, "start_line": None, "end_line": None,
                              "sha256": entry.get("sha256"), "role": "reference_dependency"}
    def from_edge(edge):
        target = edge.get("target_asset")
        if not isinstance(target, str) or not target.startswith("asset:"):
            return
        path = target[len("asset:"):]
        resolution = edge.get("resolution")
        if resolution not in ("resolved_object", "resolved_asset_only", "missing_file_id",
                               "unavailable_lfs_pointer", "inferred_filename", "explicit_identifier"):
            return
        reference = edge.get("reference") or {}
        guid = reference.get("guid", "") if isinstance(reference, dict) else ""
        if (guid and guid != "0" * 32) or edge.get("kind") == "script_type":
            add(path + ".meta")
        if resolution in ("resolved_object", "missing_file_id", "inferred_filename", "explicit_identifier"):
            add(path)
    if item.get("kind") == "asset_object":
        rendered_lines = set(item.get("text", "").splitlines())
        for raw, in db.execute("SELECT data FROM evidence_relations WHERE source=?", (item.get("entity_id"),)):
            edge = json.loads(raw)
            rendered = f"{edge.get('property')} -> {edge.get('target') or edge.get('target_asset') or edge.get('reference')} [{edge.get('resolution')}]"
            if rendered in rendered_lines or (('\n' in rendered or '\r' in rendered) and rendered in item.get("text", "")):
                from_edge(edge)
    elif item.get("origin") == "asset":
        from_edge(item)
    elif item.get("kind") == "prefab_override":
        detail = item.get("detail") or {}
        base = detail.get("base_location") or {}
        path = base.get("path") if isinstance(base, dict) else None
        record = detail.get("record") or {}
        reference = record.get("target") if isinstance(record, dict) else None
        if detail.get("resolution") == "resolved_scalar_override" and isinstance(path, str):
            from_edge({"target_asset": "asset:" + path, "resolution": "resolved_object", "reference": reference})
    return list(dependencies.values())


def _pack_context(results, context, excerpts, max_chars, reserve_related_chars=0, query=''):
    """Pack evidence, retaining query-relevant source lines when a record is large."""
    from .passage_selection import fit_source_record
    results, context = list(results), list(context)
    used = omitted = reserved_used = 0
    for row in excerpts:
        size = len(json.dumps(row, ensure_ascii=False))
        if used + size <= max_chars:
            used += size
        else:
            context.remove(row)
            omitted += 1
    reserved = set()
    if reserve_related_chars:
        # Preserve the first ranked primary record that fits, if there is one.
        remaining = max_chars - used
        primary_size = next((len(json.dumps(row, ensure_ascii=False)) for row in results
                             if len(json.dumps(row, ensure_ascii=False)) <= remaining), 0)
        allowance = min(reserve_related_chars, remaining - primary_size)
        for row in context:
            if row in excerpts:
                continue
            size = len(json.dumps(row, ensure_ascii=False))
            if reserved_used + size <= allowance:
                reserved.add(id(row))
                reserved_used += size
        used += reserved_used
    packed_results, packed_context = [], []
    for records, kept in ((results, packed_results), (context, packed_context)):
        for row in records:
            if row in excerpts or id(row) in reserved:
                kept.append(row)
                continue
            size = len(json.dumps(row, ensure_ascii=False))
            if used + size <= max_chars:
                kept.append(row)
                used += size
            else:
                fitted, _ = fit_source_record(row,
                    lambda candidate: used + len(json.dumps(candidate, ensure_ascii=False)) <= max_chars,
                    query=query)
                if fitted is not None:
                    kept.append(fitted)
                    used += len(json.dumps(fitted, ensure_ascii=False))
                else:
                    omitted += 1
    return packed_results, packed_context, used, omitted, reserved_used


def _exact_entities(db, query):
    """Resolve exact aliases and parameter-omitted method names in older indexes.

    A missing signature denotes every overload; an explicit signature remains
    exact. Only method evidence qualifies, never a prefix match on assets/types.
    """
    name = query.casefold()
    matches = {row[0] for row in db.execute(
        "SELECT entity FROM evidence_aliases WHERE alias=?", (name,))}
    if name and '(' not in name and ')' not in name:
        # Range lookup uses the alias index without SQL wildcard interpretation.
        for alias, entity in db.execute(
                "SELECT DISTINCT a.alias,a.entity FROM evidence_aliases a "
                "JOIN evidence e ON e.entity=a.entity "
                "WHERE a.alias>=? AND a.alias<? AND e.kind='code_member'",
                (name + '(', name + ')')):
            if '::M:' in entity and alias.startswith(name + '(') and alias.endswith(')'):
                matches.add(entity)
    return sorted(matches)


def retrieve(workspace, query, mode="search", limit=5, expand=0, kind=None, context_chars=24000, provider=None, semantic_weight=0.8, path_refs=0, incoming_expand=0, source_aware=False, reserve_related_chars=0, related_rank=False):

    workspace = resolve_workspace(workspace)
    if not 0 <= semantic_weight <= 1:
        raise ValueError("Semantic weight must be finite and between 0 and 1.")
    if type(path_refs) is not int or type(incoming_expand) is not int or not 0 <= path_refs <= 20 or not 0 <= incoming_expand <= 20:
        raise ValueError('Additional context limits must be integers from 0 to 20.')
    if mode not in MODES or not 1 <= limit <= 100 or not 0 <= expand <= 20:

        raise ValueError("Use a supported mode, limit 1–100, and expansion count 0–20.")

    if not 1 <= context_chars <= 1000000:
        raise ValueError("Context budget must be 1–1000000 characters.")
    if type(reserve_related_chars) is not int or not 0 <= reserve_related_chars <= context_chars:
        raise ValueError("Related-context reservation must be an integer from 0 to the context budget.")
    if kind and mode not in ("search", "semantic", "hybrid", "exact"):
        raise ValueError("Kind filters apply to evidence search modes only.")
    if type(source_aware) is not bool or (source_aware and mode != 'hybrid'):
        raise ValueError('Source-aware retrieval is an opt-in hybrid mode.')
    if type(related_rank) is not bool or (related_rank and mode != 'search'):
        raise ValueError('Experimental related-method ranking is available only for keyword search.')
    if len(query) > 2000:

        raise ValueError("Query is too long (maximum 2000 characters).")

    db, report = _open(workspace)

    try:

        matched = _exact_entities(db, query)

        results = []
        prefab_anchor_used = False
        named_root_position_used = False
        declared_unity_tag_used = False
        named_type_method_used = False
        scene_anchor_used = False
        related_rank_used = False
        query_terms_used = []
        named_object_used = False
        behavior_anchor_used = False
        scores = None

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

            tokens = _search_terms(query) if mode == "search" else terms(query)
            if mode in ("search", "hybrid"):
                query_terms_used = tokens[:64]

            if mode in ("search", "hybrid") and tokens:
                for item in _keyword_candidates(db, tokens, kind, max(100, limit * 10)):
                    candidates.setdefault(item["id"], item)

            # A named prefab object is a useful anchor for serialized fields in
            # that same prefab. Only act on one unambiguous explicit identifier;
            # normal searches, exact lookups and ambiguous names keep their order.
            names = list(dict.fromkeys(t for t in re.findall(r"\b[A-Za-z][A-Za-z0-9_]{3,127}\b", query)
                                       if re.search(r"[a-z0-9][A-Z]", t)))
            prefab_paths = set()
            if mode == "search" and not matched and len(names) == 1 and "prefab" in tokens:
                rows = db.execute("SELECT e.data FROM evidence_aliases a JOIN evidence e ON e.entity=a.entity WHERE a.alias=?", (names[0].casefold(),))
                for raw, in rows:
                    item = json.loads(raw)
                    if item["kind"] == "asset_object":
                        prefab_paths.update(s["path"] for s in item.get("sources", [])
                                            if s.get("path", "").lower().endswith(".prefab"))
                        if len(prefab_paths) > 1:
                            break
            if len(prefab_paths) == 1:
                for item in candidates.values():
                    if item["kind"] == "asset_object" and any(s.get("path") in prefab_paths for s in item["sources"]):
                        item["ranking_diagnostics"] = {"prefab_identifier": names[0]}
                        prefab_anchor_used = True
            # A scene-specific question can require a serialized override that
            # contradicts its script initializer. Require one exact scene name
            # and one unambiguous component whose script filename was named.
            scene_names = _scene_name_candidates(query, names)
            if mode == 'search' and not matched and re.search(r'\bscene\b', query, re.I) and scene_names and (not kind or kind == 'asset_object'):
                scene_paths = {}
                for name in scene_names:
                    for (entity,) in db.execute("SELECT DISTINCT e.entity FROM evidence_aliases a JOIN evidence e ON e.entity=a.entity WHERE a.alias=? AND e.kind='asset' LIMIT 10", (name.casefold() + '.unity',)):
                        if entity.startswith('asset:') and entity.lower().endswith('.unity'):
                            scene_paths[entity[6:]] = name
                if len(scene_paths) == 1:
                    scene_path, scene_name = next(iter(scene_paths.items()))
                    script_names = {n.casefold() for n in re.findall(r'\b[A-Za-z][A-Za-z0-9_]{3,127}\b', query)}
                    possible = {}
                    rows = db.execute("SELECT e.data FROM evidence_aliases a JOIN evidence e ON e.entity=a.entity WHERE a.alias=? AND e.kind='asset_object' ORDER BY e.id LIMIT 5001", (scene_path.casefold(),)).fetchall()
                    # Do not claim uniqueness if a large scene was only partly inspected.
                    if len(rows) > 5000:
                        rows = []
                    for (raw,) in rows:
                        candidate = json.loads(raw)
                        script = candidate.get('detail', {}).get('script') or {}
                        script_path = script.get('path') or ''
                        if Path(script_path).stem.casefold() in script_names:
                            possible.setdefault(candidate['entity_id'], []).append(candidate)
                    if len(possible) == 1:
                        chunks = next(iter(possible.values()))
                        chunks.sort(key=lambda r: (-len(set(_search_terms(r.get('text', ''))) & set(tokens)), r['id']))
                        for item in chunks[:3]:
                            candidate = candidates.setdefault(item['id'], {**item, 'match': 'scene_component_anchor', 'score': None})
                            candidate['ranking_diagnostics'] = {**candidate.get('ranking_diagnostics', {}),
                                'scene_component_anchor': scene_path,
                                'scene_anchor_script': (item.get('detail', {}).get('script') or {}).get('path')}
                            scene_anchor_used = True
                        # For explicit multi-part scene-and-behavior questions,
                        # use resolved code references to find methods that read
                        # or write this component's serialized fields.
                        if not kind and re.search(r'\band\b', query, re.I) and re.search(r'\b(?:how|why)\b', query, re.I):
                            component = chunks[0]
                            script_path = (component.get('detail', {}).get('script') or {}).get('path') or ''
                            fields = re.findall(r'(?m)^([A-Za-z_][A-Za-z0-9_]*):\s*[^\n]+', component.get('text', ''))
                            fields = [f for f in fields if f not in {'m_ObjectHideFlags', 'm_Enabled', 'm_EditorHideFlags', 'm_Name', 'm_EditorClassIdentifier'}][:32]
                            methods = {}
                            for field_name in fields:
                                field_ids = set()
                                rows = db.execute("SELECT e.data FROM evidence_aliases a JOIN evidence e ON e.entity=a.entity WHERE a.alias=? AND e.kind='code_member' LIMIT 101", (field_name.casefold(),)).fetchall()
                                if len(rows) > 100:
                                    continue
                                for (raw,) in rows:
                                    field = json.loads(raw)
                                    if ('::F:' in field.get('entity_id', '') and field['title'].rsplit('.',1)[-1] == field_name
                                            and any(src.get('path') == script_path for src in field.get('sources', []))):
                                        field_ids.add(field['entity_id'])
                                if len(field_ids) != 1:
                                    continue
                                field_id = next(iter(field_ids))
                                for (raw,) in db.execute("SELECT data FROM evidence_relations WHERE target=? ORDER BY source LIMIT 100", (field_id,)):
                                    edge = json.loads(raw)
                                    source = edge.get('source') or ''
                                    if edge.get('kind') != 'references' or edge.get('resolution') != 'resolved' or '::M:' not in source or '.get_' in source or '.set_' in source:
                                        continue
                                    for (method_raw,) in db.execute("SELECT data FROM evidence WHERE entity=? AND kind='code_member' ORDER BY id LIMIT 1", (source,)):
                                        method = json.loads(method_raw)
                                        if not any(src.get('path') == script_path for src in method.get('sources', [])):
                                            continue
                                        slot = methods.setdefault(method['id'], {'item': method, 'fields': set()})
                                        slot['fields'].add(field_name)
                            ranked_methods = sorted(methods.values(), key=lambda r: (-len(r['fields']), r['item']['title'], r['item']['id']))
                            for record in ranked_methods[:2]:
                                item = record['item']
                                candidate = candidates.setdefault(item['id'], {**item, 'match': 'scene_field_usage', 'score': None})
                                candidate['ranking_diagnostics'] = {**candidate.get('ranking_diagnostics', {}), 'scene_field_usage': {'scene': scene_path, 'component': component['entity_id'], 'script': script_path, 'fields': sorted(record['fields'])}}
                                candidate['sources'] = candidate.get('sources', []) + [{**src, 'role': 'reference_dependency'} for src in component.get('sources', [])]
            def priority(item):
                if item["match"] == "exact_alias":
                    return 0
                if "scene_component_anchor" in item.get("ranking_diagnostics", {}):
                    return 1
                if "scene_field_usage" in item.get("ranking_diagnostics", {}):
                    return 2
                if "prefab_identifier" in item.get("ranking_diagnostics", {}):
                    return 3
                return 4
            ordered = sorted(candidates.values(), key=lambda r: (

                priority(r), r["score"] if r["score"] is not None else 0,

                r["sources"][0]["path"], r["sources"][0]["start_line"] or 0, r["id"]))

            if mode == 'search' and not matched and (not kind or kind == 'prefab_override'):
                ordered, named_root_position_used = _promote_named_root_position(db, ordered, query)
            if mode == 'search' and not matched and not kind and not named_root_position_used:
                ordered, declared_unity_tag_used = _promote_declared_unity_tag(db, ordered, query)
            if (mode == 'search' and not matched and (not kind or kind == 'code_member') and not scene_anchor_used
                    and not named_root_position_used and not declared_unity_tag_used):
                ordered, named_type_method_used = _promote_unique_named_type_method(db, ordered, query)
            if related_rank and not matched:
                ordered, related_rank_used = _promote_related_members(ordered, tokens, query)

            if mode in ("semantic", "hybrid") and query.strip():
                from .semantic import rank
                scores = rank(workspace, query, provider)
                dense = []
                for raw, in db.execute("SELECT data FROM evidence ORDER BY id"):
                    item = json.loads(raw)
                    if item["id"] in scores and (not kind or item["kind"] == kind):
                        dense.append({**item, "match": "semantic", "score": scores[item["id"]]})
                dense.sort(key=lambda r: (-r["score"], r["id"]))
                from .ranking import fuse, reserve_source_matches
                keyword = ordered
                ordered = fuse(keyword, dense, scores, mode, max(100, limit * 10), semantic_weight)
                if source_aware and semantic_weight < 1:
                    ordered = reserve_source_matches(keyword, ordered)

            if mode in ('search', 'semantic', 'hybrid') and not matched:
                from .query_ranking import promote_named_gameobjects
                ordered, named_object_used = promote_named_gameobjects(db, ordered, query, kind)
                if not named_object_used:
                    from .behavior_ranking import promote_behavior_methods
                    ordered, behavior_anchor_used = promote_behavior_methods(db, ordered, query, kind)

            # Identical overlapping source ranges should not consume separate result slots.

            seen = set()

            for item in ordered:

                key = (item["entity_id"], fingerprint(item["sources"]), item["text"])

                if key not in seen:

                    results.append(item)

                    seen.add(key)

            total = len(results)

            results = results[:limit]

        from .rig_catalog import lookup as lookup_rigs
        rig_hits = lookup_rigs(workspace, query, mode, kind, limit)
        if rig_hits:
            from .rig_catalog import check_rig_snapshot
            rig_hits=[r for r in rig_hits if check_rig_snapshot(report["project"],r)["status"]=="current_source_and_meta"]
        if rig_hits:
            if results and limit>=3 and kind is None: rig_hits=rig_hits[:limit-1]
            results = (rig_hits + results)[:limit]
            total += len(rig_hits)
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

        from .source_checks import inspect_source
        inventory = status(workspace)
        entries = {r['path']: r for r in inventory['entries']}
        if incoming_expand:
            added = 0
            for hit in results:
                entity = hit.get('entity_id')
                if not entity:
                    continue
                for raw, in db.execute('SELECT data FROM evidence_relations WHERE target=? ORDER BY kind,source LIMIT 30', (entity,)):
                    edge = json.loads(raw)
                    if edge.get('resolution') != 'resolved':
                        continue
                    for item_raw, in db.execute('SELECT data FROM evidence WHERE entity=? ORDER BY id LIMIT 1', (edge.get('source'),)):
                        item = json.loads(item_raw)
                        if item['id'] not in seen_context and added < incoming_expand:
                            context.append({**item, 'expansion': {'from': entity, 'reason': 'incoming_reference', 'relationship': edge}})
                            seen_context.add(item['id'])
                            added += 1
                    if added >= incoming_expand:
                        break
                if added >= incoming_expand:
                    break
        if path_refs:
            added, seen_paths = 0, set()
            for hit in results:
                if hit.get('kind') != 'documentation' or any(inspect_source(inventory['project'], entries, s)['status'] != 'current' for s in hit.get('sources', [])):
                    continue
                for path in re.findall(r'`((?:Assets|Packages|ProjectSettings|Docs|AgentContext)/[^`\r\n]+)`', hit.get('text', '')):
                    if path in seen_paths or entries.get(path, {}).get('status') != 'supported':
                        continue
                    seen_paths.add(path)
                    for item_raw, in db.execute('SELECT data FROM evidence WHERE entity=? ORDER BY id LIMIT 1', ('asset:' + path,)):
                        item = json.loads(item_raw)
                        if item['id'] not in seen_context and added < path_refs:
                            context.append({**item, 'expansion': {'from': hit['entity_id'], 'reason': 'document_path_mention', 'relationship': None}})
                            seen_context.add(item['id'])
                            added += 1
                    if added >= path_refs:
                        break
                if added >= path_refs:
                    break
        excerpts = []
        if source_aware:
            from .source_excerpts import source_excerpts
            excerpts = source_excerpts(inventory['project'], entries, results + context, query)
            context[:0] = excerpts
        # Scene-based ranking uses the component's script association even
        # when a large component's selected chunk does not display that link.
        # Check the script and its GUID metadata wherever that association was
        # used, and drop the ranked record if its dependency was not indexed.
        def scene_usage_sources(item):
            diagnostics = item.get('ranking_diagnostics', {})
            usage = diagnostics.get('scene_field_usage')
            anchor_script = diagnostics.get('scene_anchor_script')
            paths = ([anchor_script, anchor_script + '.meta'] if anchor_script else
                     [usage['script'] + '.meta'] if usage else [])
            if not paths:
                return True
            sources = list(item.get('sources', []))
            existing = {source.get('path') for source in sources}
            for path in paths:
                entry = entries.get(path)
                if not entry or not entry.get('sha256'):
                    return False
                if path not in existing:
                    sources.append({'path': path, 'start_line': None, 'end_line': None,
                        'sha256': entry['sha256'], 'role': 'reference_dependency'})
                    existing.add(path)
            item['sources'] = sources
            return True
        results = [item for item in results if scene_usage_sources(item)]
        context = [item for item in context if scene_usage_sources(item)]
        for item in results + context:
            dependencies = _reference_dependencies(db, entries, item)
            if dependencies:
                item["sources"] = item.get("sources", []) + dependencies
            if scores is not None and hasattr(scores, 'passage'):
                passage = scores.passage(item)
                if passage is not None:
                    item['ranking_diagnostics'] = {**item.get('ranking_diagnostics', {}),
                                                   'semantic_passage': passage}
        results, context, used, omitted, reserved_used = _pack_context(
            results, context, excerpts, context_chars, reserve_related_chars, query)
        source_checks, checked = [], set()
        for item in results + context:
            if item.get("kind")=="rig_asset":
                from .rig_catalog import report_entry
                extra=report_entry(item);entries[extra["path"]]=extra
            for source in item.get('sources', []):
                key = (source.get('path'), source.get('sha256'))
                if key not in checked:
                    if item.get("kind") == "rig_asset":
                        from .rig_catalog import check_rig_snapshot
                        source_checks.append(check_rig_snapshot(inventory["project"], item))
                        source_checks.append(inspect_source(inventory["project"], entries, source))
                    else:
                        source_checks.append(inspect_source(inventory['project'], entries, source))
                    checked.add(key)
        return {"source_freshness": source_checks,
                "semantic_profile": getattr(scores, 'profile', None),
                "context_budget": {"max_chars": context_chars, "used_chars": used, "omitted_records": omitted,
                                   "truncated_records": sum(bool(r.get('packing', {}).get('truncated')) for r in results + context),
                                   "reserve_related_chars": reserve_related_chars, "reserved_used_chars": reserved_used},
                "schema_version": 1, "project": report["project"], "snapshot": report["inputs"],

                "git": report["git"], "query": query, "mode": mode, "matched_entities": matched,
                "query_terms": query_terms_used,

                "results": results, "context": context, "candidate_count": total,

                "truncated": total > limit or omitted > 0, "limits": report["limits"],

                "ranking": (("Unity rig catalog matches first; " if rig_hits else "") + ("Cosine similarity" if mode == "semantic" else ("Source-aware lexical reservation, then entity-diverse weighted reciprocal rank fusion k=60" if source_aware and semantic_weight < 1 else "Exact aliases first, entity-diverse weighted reciprocal rank fusion k=60") if mode == "hybrid" else "Exact aliases, then unambiguous named-prefab objects, then SQLite BM25 (lower is better)" if prefab_anchor_used else "Exact aliases first, then SQLite BM25 (lower is better)") + ("; unambiguous named-scene component and field-use anchors" if scene_anchor_used else "") + ("; named-root prefab position anchor" if named_root_position_used else "") + ("; declared Unity tag evidence anchor" if declared_unity_tag_used else "") + ("; uniquely matched named-type method" if named_type_method_used else "") + ("; experimental same-container method promotion" if related_rank_used else "") + ("; explicit named GameObject lookup" if named_object_used else "") + ("; parsed-identifier behavior anchors" if behavior_anchor_used else "")),
                "related_rank": related_rank}

    finally:

        db.close()





def format_results(result):

    lines = [f"{len(result['results'])} results; {len(result['context'])} context records."]

    freshness = {}
    for item in result.get('source_freshness', []):
        freshness.setdefault(item['path'], set()).add(item['status'])

    for row in result["results"] + result["context"]:

        source = row["sources"][0]

        where = source["path"] + (f":{source['start_line']}-{source['end_line']}" if source["start_line"] else "")

        label = row.get("title") or f"{row.get('source')} -> {row.get('target') or row.get('target_asset')}"

        statuses = ', '.join(sorted(freshness.get(source['path'], {'unchecked'})))
        lines.append(f"\n{label}\n{where} [{row['evidence_type']}; source: {statuses}]")
        for dependency in row.get('sources', []):
            if dependency.get('role') != 'reference_dependency':
                continue
            states = freshness.get(dependency['path'], {'unchecked'})
            if states != {'current'}:
                lines.append(f"Reference dependency: {dependency['path']} [{', '.join(sorted(states))}]")

        lines.append(row.get("text") or f"{row['kind']} [{row['resolution']}]")

    if not result["results"]:

        lines.append("No matching evidence.")

    return "\n".join(lines)
