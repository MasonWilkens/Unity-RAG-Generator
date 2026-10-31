"""Map saved Unity assets and connect their script references to C# evidence."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import yaml

from .discovery import status
from .code_map import _read_snapshot_file, code_report
from .unity_yaml import parse_unity, reference, walk_values

SERIALIZED = {".unity", ".prefab", ".asset", ".mat", ".anim", ".controller", ".overridecontroller"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _saved_code(workspace, inventory):
    database = Path(workspace).resolve() / "inventory.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='code_report'").fetchone()
    if row is None:
        return None
    return code_report(workspace)


def map_assets(workspace):
    inventory = status(workspace)
    root = Path(inventory["project"]).resolve()
    destination = Path(workspace).resolve()
    if destination.is_relative_to(root):
        raise ValueError("Asset workspace must be outside the source project.")
    code = _saved_code(workspace, inventory)
    diagnostics, assets, objects, edges, overrides = [], [], [], [], []
    entries = {r["path"]: r for r in inventory["entries"]}
    texts = {}
    for path, entry in entries.items():
        if entry["status"] == "supported" and (entry["kind"] == "metadata" or Path(path).suffix.lower() in SERIALIZED):
            texts[path] = _read_snapshot_file(root, entry)
    # C# links must refer to the same inventoried source, not newer files.
    if code:
        for f in code["files"]:
            _read_snapshot_file(root, entries[f["path"]])
    guid_paths = defaultdict(list)
    metadata = {}
    for path, text in texts.items():
        if not path.endswith(".meta"):
            continue
        matches = re.findall(r"^guid:\s*([0-9a-fA-F]{32})\s*$", text, re.MULTILINE)
        target = path[:-5]
        if len(matches) != 1:
            diagnostics.append({"path": path, "reason": "missing_or_invalid_guid"})
            continue
        guid = matches[0].lower()
        guid_paths[guid].append(target)
        metadata[target] = {"guid": guid, "metadata_path": path,
                            "folder": bool(re.search(r"^folderAsset:\s*yes\s*$", text, re.MULTILINE))}
    for guid, paths in guid_paths.items():
        if len(paths) > 1:
            diagnostics.append({"reason": "duplicate_guid", "guid": guid, "paths": paths})
    for path in sorted(set(p for p in entries if not p.endswith(".meta")) | set(metadata)):
        entry = entries.get(path)
        meta = metadata.get(path, {})
        if entry is None and meta.get("folder"):
            coverage = "folder_metadata_only"
        else:
            coverage = entry["status"] if entry else "missing_or_not_in_inventory"
        asset = {"id": "asset:" + path, "path": path, "guid": meta.get("guid"),
                 "metadata_path": meta.get("metadata_path"), "kind": entry["kind"] if entry else "unknown",
                 "inventory_status": coverage, "parse_status": "metadata_only", "sha256": entry["sha256"] if entry else None}
        assets.append(asset)
        if Path(path).suffix.lower() not in SERIALIZED:
            continue
        if path not in texts:
            asset["parse_status"] = "unavailable_" + coverage
            continue
        try:
            parsed = parse_unity(texts[path], path)
        except (ValueError, yaml.YAMLError, RecursionError) as exc:
            asset["parse_status"] = "unparsed"
            diagnostics.append({"path": path, "reason": "serialization_parse_failed", "detail": str(exc)})
            continue
        asset["parse_status"] = "parsed"
        for obj in parsed:
            obj.update(id=f"asset:{path}#{obj['file_id']}", asset=asset["id"], path=path,
                       guid=asset["guid"], name=obj["data"].get("m_Name"))
            obj["values"] = [{"property": p, "value": v, "origin": "serialized",
                              "location": obj["locations"].get(p, obj["location"])}
                             for p, v, is_ref in walk_values(obj["data"]) if not is_ref]
            objects.append(obj)
    by_asset = {a["path"]: a for a in assets}
    by_object = {(o["path"], o["file_id"]): o for o in objects}

    def resolve(value, source_path):
        result = {"target": None, "target_asset": None, "resolution": "invalid_reference"}
        file_id = str(value.get("fileID", ""))
        if not re.fullmatch(r"-?\d+", file_id):
            return result
        if file_id == "0":
            result["resolution"] = "null"
            return result
        guid = value.get("guid", "").lower()
        path = source_path
        if guid and guid != "0" * 32:
            paths = guid_paths.get(guid, [])
            if not paths:
                result["resolution"] = "external_or_missing_guid"
                return result
            if len(paths) != 1:
                result["resolution"] = "ambiguous_guid"
                return result
            path = paths[0]
        asset = by_asset.get(path)
        if asset is None or asset["inventory_status"] == "missing_or_not_in_inventory":
            result["resolution"] = "missing_or_unscanned_asset"
            return result
        result["target_asset"] = asset["id"]
        obj = by_object.get((path, file_id))
        if obj:
            result.update(target=obj["id"], resolution="resolved_object")
        elif guid and asset["inventory_status"] not in ("excluded", "missing", "unreadable"):
            if asset["parse_status"] == "parsed" and not (
                    file_id == "100100000" and Path(path).suffix.lower() == ".prefab"):
                result["resolution"] = "missing_file_id"
            else:
                result["resolution"] = "resolved_asset_only"
        else:
            result["resolution"] = "missing_or_unavailable_file_id"
        return result

    declarations = defaultdict(list)
    if code:
        for declaration in code["declarations"]:
            if declaration["kind"] in ("ClassDeclaration", "RecordDeclaration"):
                declarations[declaration["location"]["path"]].append(declaration)
    for obj in objects:
        for prop, value, is_ref in walk_values(obj["data"]):
            if not is_ref:
                continue
            kind = {"m_Script": "uses_script", "m_GameObject": "attached_to",
                    "m_Father": "transform_parent", "m_SourcePrefab": "prefab_source",
                    "m_CorrespondingSourceObject": "corresponding_source",
                    "m_PrefabInstance": "prefab_instance"}.get(prop, "references_asset_or_object")
            if prop.startswith("m_Component."):
                kind = "has_component"
            edge = {"source": obj["id"], "kind": kind, "property": prop, "reference": value,
                    "location": obj["locations"].get(prop, obj["location"]), **resolve(value, obj["path"])}
            edges.append(edge)
            if prop != "m_Script" or not edge["target_asset"] or value.get("fileID") != "11500000":
                continue
            script_path = edge["target_asset"][len("asset:"):]
            candidates = declarations.get(script_path, []) if script_path.endswith(".cs") else []
            identifier = obj["data"].get("m_EditorClassIdentifier", "")
            explicit = [d for d in candidates if identifier == d["assembly"] + "::" + d["qualified_name"]]
            matching = explicit or [d for d in candidates if d["name"] == Path(script_path).stem]
            unique = {d["id"]: d for d in matching}
            obj["script"] = {"path": script_path, "candidate_types": list(unique),
                             "resolution": "serialized_type_identifier" if len(unique) == 1 and explicit else
                                           "filename_candidate" if len(unique) == 1 else
                                           "ambiguous_or_unmapped"}
            if len(unique) == 1:
                d = next(iter(unique.values()))
                edges.append({"source": obj["id"], "kind": "script_type", "target": d["id"],
                              "target_asset": edge["target_asset"],
                              "resolution": "explicit_identifier" if explicit else "inferred_filename",
                              "property": prop, "location": edge["location"]})
                obj["script"]["unity_type"] = d["unity_type"]
        obj["role"] = "serialized_object"
        if obj["type"] == "GameObject":
            obj["role"] = "game_object"
        elif reference(obj["data"].get("m_GameObject")) and obj["data"]["m_GameObject"]["fileID"] != "0":
            obj["role"] = "component"
        elif obj.get("script", {}).get("unity_type") == "UnityEngine.ScriptableObject":
            obj["role"] = "scriptable_object_candidate"
        if obj["type"] != "PrefabInstance":
            continue
        modification = obj["data"].get("m_Modification", {})
        if not isinstance(modification, dict):
            diagnostics.append({"path": obj["path"], "reason": "invalid_prefab_modification"})
            continue
        changes = modification.get("m_Modifications", [])
        if not isinstance(changes, list):
            diagnostics.append({"path": obj["path"], "reason": "invalid_prefab_override_list"})
            continue
        source = obj["data"].get("m_SourcePrefab", {})
        source_result = resolve(source, obj["path"]) if reference(source) else {}
        source_path = (source_result.get("target_asset") or "")[len("asset:"):]
        structural = any(modification.get(k) for k in (
            "m_RemovedComponents", "m_RemovedGameObjects", "m_AddedComponents", "m_AddedGameObjects"))
        nested = any(o["path"] == source_path and (o["stripped"] or o["type"] == "PrefabInstance") for o in objects)
        if structural or nested:
            diagnostics.append({"path": obj["path"], "object": obj["id"],
                                "reason": "structural_or_nested_prefab_resolution_not_supported"})
        for index, change in enumerate(changes):
            entry = {"instance": obj["id"], "index": index, "record": change,
                     "origin": "prefab_override", "resolution": "unsupported_override",
                     "location": obj["locations"].get(f"m_Modification.m_Modifications.{index}", obj["location"])}
            overrides.append(entry)
            if not isinstance(change, dict) or not reference(change.get("target")):
                continue
            target = resolve(change["target"], obj["path"])
            target_obj = next((o for o in objects if o["id"] == target["target"]), None)
            entry["target"] = target["target"]
            prop = change.get("propertyPath")
            # Conservative subset: direct scalar fields on a plain source prefab.
            if (structural or nested or not target_obj or target_obj["path"] != source_path
                    or target_obj["stripped"] or not isinstance(prop, str)
                    or not re.fullmatch(r"[A-Za-z_]\w*", prop)
                    or prop not in target_obj["data"] or not isinstance(target_obj["data"][prop], str)
                    or not reference(change.get("objectReference"))
                    or change["objectReference"].get("fileID") != "0"
                    or not isinstance(change.get("value"), str)):
                continue
            entry.update(resolution="resolved_scalar_override", property=prop,
                         base_value=target_obj["data"][prop], effective_value=change["value"],
                         base_location=target_obj["locations"].get(prop, target_obj["location"]))
    def override_key(entry):
        record = entry["record"] if isinstance(entry["record"], dict) else {}
        return (entry["instance"], fingerprint(record.get("target")), record.get("propertyPath")
                if isinstance(record.get("propertyPath"), str) else None)
    override_keys = Counter(override_key(e) for e in overrides)
    for entry in overrides:
        if override_keys[override_key(entry)] > 1:
            entry["resolution"] = "ambiguous_duplicate_override"
            entry.pop("effective_value", None)
    report = {
        "schema_version": 1, "project": str(root), "discovery_scanned_at": inventory["scanned_at"],
        "code_fingerprint": fingerprint(code) if code else None,
        "assets": assets, "objects": objects, "edges": edges, "overrides": overrides,
        "diagnostics": diagnostics, "counts": {
            "assets": len(assets), "objects": len(objects), "edges": len(edges),
            "overrides": len(overrides), "diagnostics": len(diagnostics),
            "reference_resolution": dict(Counter(e["resolution"] for e in edges))},
        "coverage_note": "Static serialized evidence only. Scalar values remain strings. Filename-to-class links are inferred. Only direct scalar overrides on plain source prefabs are resolved; nested/variant chains and structural overrides remain explicit records."
    }
    if status(workspace)["scanned_at"] != inventory["scanned_at"]:
        raise ValueError("Discovery changed during asset mapping. Retry.")
    if fingerprint(_saved_code(workspace, inventory)) != fingerprint(code):
        raise ValueError("Code map changed during asset mapping. Retry.")
    with sqlite3.connect(destination / "inventory.sqlite3") as db:
        db.execute("CREATE TABLE IF NOT EXISTS unity_assets (id TEXT PRIMARY KEY, path TEXT, guid TEXT, data TEXT)")
        db.execute("CREATE INDEX IF NOT EXISTS unity_guids ON unity_assets(guid)")
        db.execute("CREATE TABLE IF NOT EXISTS unity_objects (id TEXT PRIMARY KEY, asset TEXT, data TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS unity_edges (source TEXT, target TEXT, target_asset TEXT, resolution TEXT, data TEXT)")
        db.execute("CREATE INDEX IF NOT EXISTS unity_targets ON unity_edges(target_asset)")
        for table in ("unity_assets", "unity_objects", "unity_edges"):
            db.execute(f"DELETE FROM {table}")
        db.executemany("INSERT INTO unity_assets VALUES (?,?,?,?)",
                       [(a["id"], a["path"], a["guid"], json.dumps(a)) for a in assets])
        db.executemany("INSERT INTO unity_objects VALUES (?,?,?)",
                       [(o["id"], o["asset"], json.dumps(o)) for o in objects])
        db.executemany("INSERT INTO unity_edges VALUES (?,?,?,?,?)",
                       [(e["source"], e.get("target"), e.get("target_asset"), e["resolution"], json.dumps(e)) for e in edges])
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('asset_report', ?)", (json.dumps(report),))
    return report


def asset_report(workspace):
    inventory = status(workspace)
    database = Path(workspace).resolve() / "inventory.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='asset_report'").fetchone()
    if not row:
        raise ValueError("No asset map found. Run map-assets first.")
    report = json.loads(row[0])
    if report["discovery_scanned_at"] != inventory["scanned_at"]:
        raise ValueError("Asset map predates discovery. Run map-assets again.")
    current = _saved_code(workspace, inventory)
    if report["code_fingerprint"] != (fingerprint(current) if current else None):
        raise ValueError("Code map changed. Run map-assets again.")
    return report


def find_assets(workspace, query):
    report = asset_report(workspace)
    return [a for a in report["assets"] if query.casefold() in a["path"].casefold()
            or query.lower() == a["guid"] or query == a["id"]]


def asset_users(workspace, query):
    report = asset_report(workspace)
    matched = {a["id"] for a in report["assets"] if query in (a["id"], a["path"], a["guid"])}
    return [e for e in report["edges"] if e.get("target_asset") in matched or e.get("target") == query]


def asset_summary(report):
    c = report["counts"]
    return (f"Mapped {c['assets']} assets and {c['objects']} serialized objects; {c['edges']} references.\n"
            f"Prefab overrides: {c['overrides']}; diagnostics: {c['diagnostics']}.\n"
            f"{report['coverage_note']}")


def inspect_asset(workspace, query):
    report = asset_report(workspace)
    selected = [a for a in report["assets"] if query in (a["path"], a["id"], a["guid"])]
    if not selected:
        raise ValueError("No asset with that exact path, GUID, or ID.")
    asset_ids = {a["id"] for a in selected}
    objects = [o for o in report["objects"] if o["asset"] in asset_ids]
    object_ids = {o["id"] for o in objects}
    return {"assets": selected, "objects": objects,
            "outgoing": [e for e in report["edges"] if e["source"] in object_ids],
            "incoming": [e for e in report["edges"] if e.get("target_asset") in asset_ids],
            "overrides": [e for e in report["overrides"] if e["instance"] in object_ids]}
