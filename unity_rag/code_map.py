"""Run the bundled Roslyn helper against the discovery snapshot, never project builds."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess

from .discovery import is_link, status


def _run(command, **kwargs):
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=180, **kwargs)
    except FileNotFoundError as exc:
        raise ValueError("C# mapping requires a .NET SDK (6 or newer) on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("C# analyzer timed out; the previous map was preserved.") from exc
    if result.returncode:
        raise ValueError(f"C# analyzer failed:\n{result.stderr or result.stdout}")
    return result.stdout


def _helper(workspace):
    source = Path(__file__).parent / "roslyn"
    output = Path(workspace).resolve() / "analyzer"
    sdk = _run(["dotnet", "--version"], cwd=source).strip()
    if int(sdk.split(".")[0]) < 6:
        raise ValueError("C# mapping requires a .NET SDK version 6 or newer.")
    fingerprint = hashlib.sha256(sdk.encode() + b"".join(
        (source / name).read_bytes() for name in ("Program.cs", "UnityRag.Analyzer.csproj", "NuGet.Config"))).hexdigest()
    stamp = output / "source.sha256"
    dll = output / "UnityRag.Analyzer.dll"
    if not dll.is_file() or not stamp.is_file() or stamp.read_text() != fingerprint:
        intermediate = str(output / "obj") + os.sep
        _run(["dotnet", "build", str(source / "UnityRag.Analyzer.csproj"),
              "--configuration", "Release", "--nologo", "--configfile", str(source / "NuGet.Config"),
              "-p:BaseIntermediateOutputPath=" + intermediate,
              "-p:MSBuildProjectExtensionsPath=" + intermediate,
              "-o", str(output)], cwd=source,
             env={**os.environ, "DOTNET_CLI_TELEMETRY_OPTOUT": "1"})
        stamp.write_text(fingerprint)
    return dll


def _read_snapshot_file(root, entry):
    relative = Path(entry["path"])
    path = root / relative
    if relative.is_absolute() or ".." in relative.parts or not path.resolve().is_relative_to(root):
        raise ValueError(f"Invalid source path: {relative}")
    for item in (path, *path.parents):
        if item == root:
            break
        if is_link(item):
            raise ValueError(f"Source became a link: {relative}. Run scan again.")
    with path.open("rb") as stream:
        raw = stream.read(entry["size"] + 1)
    if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise ValueError(f"Source changed since discovery: {relative}. Run scan again.")
    return raw.decode("utf-8-sig")


def map_code(workspace, defines=(), references=(), language_version="10"):
    inventory = status(workspace)
    root = Path(inventory["project"])
    destination = Path(workspace).resolve()
    if destination.is_relative_to(root.resolve()):
        raise ValueError("Code workspace must be outside the source project.")
    for define in defines:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", define):
            raise ValueError(f"Invalid preprocessor symbol: {define}")
    references = [str(Path(p).resolve(strict=True)) for p in references]
    entries = [r for r in inventory["entries"] if r["status"] == "supported"]
    # Verify declarations and metadata too: assembly ownership is snapshot-dependent.
    texts = {r["path"]: _read_snapshot_file(root, r) for r in entries
             if r["kind"] in ("script", "assembly_definition", "assembly_reference", "metadata")}
    boundaries, diagnostics, guid_names = {}, [], {}
    declarations = inventory["assemblies"]
    for row in declarations:
        value, path = row["declaration"], row["path"]
        if path.endswith(".asmdef") and isinstance(value, dict) and isinstance(value.get("name"), str):
            metadata = texts.get(path + ".meta", "")
            match = re.search(r"^guid:\s*(\S+)", metadata, re.MULTILINE)
            if match:
                guid_names[match.group(1)] = value["name"]
    # Include malformed definitions as boundaries; never leak files into a parent assembly.
    for path in texts:
        if not path.endswith((".asmdef", ".asmref")):
            continue
        row = next((r for r in declarations if r["path"] == path), None)
        value = row["declaration"] if row and isinstance(row["declaration"], dict) else {}
        name = value.get("name") if path.endswith(".asmdef") else value.get("reference")
        if isinstance(name, str) and name.startswith("GUID:"):
            name = guid_names.get(name[5:])
        if not isinstance(name, str) or not name:
            name = "Unresolved-" + hashlib.sha256(path.encode()).hexdigest()[:12]
            diagnostics.append({"path": path, "reason": "unresolved_assembly_boundary"})
        folder = str(Path(path).parent).replace("\\", "/")
        if folder in boundaries:
            name = "Ambiguous-" + hashlib.sha256(folder.encode()).hexdigest()[:12]
            diagnostics.append({"path": path, "reason": "multiple_assembly_boundaries"})
        boundaries[folder] = name

    files = []
    for entry in entries:
        if entry["kind"] != "script":
            continue
        path = entry["path"]
        owners = [(folder, name) for folder, name in boundaries.items()
                  if Path(path).is_relative_to(Path(folder))]
        if owners:
            assembly = max(owners, key=lambda item: len(Path(item[0]).parts))[1]
            ownership = "declared_boundary"
        else:
            # A useful grouping, explicitly not a recreation of Unity's build pipeline.
            assembly = "Assembly-CSharp-Editor" if "Editor" in Path(path).parts else "Assembly-CSharp"
            ownership = "default_approximation"
        files.append({"path": path, "text": texts[path], "assembly": assembly,
                      "sha256": entry["sha256"], "ownership": ownership})
    helper = _helper(destination)
    request = {"files": files, "defines": list(defines), "references": references,
               "language_version": language_version}
    result = json.loads(_run(["dotnet", str(helper)], input=json.dumps(request)))
    result.update(project=str(root), discovery_scanned_at=inventory["scanned_at"],
                  defines=list(defines), references=references, language_version=language_version,
                  files=[{k: v for k, v in f.items() if k != "text"} for f in files],
                  assembly_declarations=declarations, mapping_diagnostics=diagnostics,
                  semantic_scope="Bindings are relative to supplied defines, per-assembly source and host .NET references; Unity compilation is not reproduced. Source assemblies are analyzed separately. Cross-assembly bindings require explicit DLL references.",
                  coverage_note="Inactive preprocessor branches are preserved as text, not declarations. Unity serialized fields are candidates; supported field-type rules are not validated.")
    # Recheck the inventory marker before replacing the saved map.
    if status(workspace)["scanned_at"] != inventory["scanned_at"]:
        raise ValueError("Discovery changed during code mapping. Retry the command.")
    with sqlite3.connect(destination / "inventory.sqlite3") as db:
        db.execute("CREATE TABLE IF NOT EXISTS code_declarations (id TEXT, path TEXT, start INTEGER, name TEXT, qualified_name TEXT, data TEXT, PRIMARY KEY(id,path,start))")
        db.execute("CREATE INDEX IF NOT EXISTS code_names ON code_declarations(name)")
        db.execute("CREATE TABLE IF NOT EXISTS code_edges (target TEXT, source TEXT, resolution TEXT, data TEXT)")
        db.execute("CREATE INDEX IF NOT EXISTS code_targets ON code_edges(target)")
        db.execute("DELETE FROM code_declarations")
        db.execute("DELETE FROM code_edges")
        db.executemany("INSERT INTO code_declarations VALUES (?,?,?,?,?,?)",
                       [(r["id"], r["location"]["path"], r["location"]["start"], r["name"],
                         r["qualified_name"], json.dumps(r)) for r in result["declarations"]])
        db.executemany("INSERT INTO code_edges VALUES (?,?,?,?)",
                       [(r["target"], r["source"], r["resolution"], json.dumps(r)) for r in result["edges"]])
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('code_report', ?)", (json.dumps(result),))
    return result


def code_report(workspace):
    inventory = status(workspace)
    database = Path(workspace).resolve() / "inventory.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='code_report'").fetchone()
    if not row:
        raise ValueError("No code map found. Run map-code first.")
    report = json.loads(row[0])
    if report["discovery_scanned_at"] != inventory["scanned_at"]:
        raise ValueError("The code map predates the current scan. Run map-code again.")
    return report


def find_symbols(workspace, query):
    report = code_report(workspace)
    return [r for r in report["declarations"]
            if query.casefold() in (r["qualified_name"] or r["name"]).casefold() or query == r["id"]]


def find_references(workspace, symbol_id):
    report = code_report(workspace)
    return [r for r in report["edges"] if r["target"] == symbol_id]


def summary(report):
    resolved = sum(r["resolution"] == "resolved" for r in report["edges"])
    return (f"Mapped {len(report['files'])} C# files; {len(report['declarations'])} declarations.\n"
            f"References: {resolved} resolved in supplied context; {len(report['edges']) - resolved} unresolved/ambiguous.\n"
            f"Compiler diagnostics: {len(report['diagnostics'])}; assembly diagnostics: {len(report['mapping_diagnostics'])}.\n"
            f"{report['semantic_scope']}")
