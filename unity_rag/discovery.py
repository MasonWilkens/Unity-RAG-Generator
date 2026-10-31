"""Read-only Unity project discovery and persistent inventory."""
from collections import Counter
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

DEFAULT_CONFIG = {
    "include": ["Assets", "Packages", "ProjectSettings"],
    "exclude": ["Library", "Temp", "Obj", "Logs", "Build", "Builds",
                ".git", ".vs", ".idea", "node_modules", ".unity-rag", ".venv",
                ".env", ".env.*"],
    "max_file_bytes": 2_000_000,
    "external_packages": "report_only",
}
KINDS = {
    ".cs": "script", ".unity": "scene", ".prefab": "prefab",
    ".asset": "serialized_asset", ".mat": "material", ".meta": "metadata",
    ".shader": "shader", ".compute": "shader", ".cginc": "shader",
    ".hlsl": "shader", ".shadergraph": "shader", ".shadersubgraph": "shader",
    ".md": "documentation", ".txt": "documentation", ".rst": "documentation",
    ".asmdef": "assembly_definition", ".asmref": "assembly_reference",
    ".json": "configuration", ".uxml": "ui", ".uss": "ui",
    ".anim": "animation", ".controller": "animation",
    ".overridecontroller": "animation",
}


def load_config(path=None):
    config = dict(DEFAULT_CONFIG)
    if path:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Configuration must be a JSON object.")
        unknown = set(value) - set(config)
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        config.update(value)
    for key in ("include", "exclude"):
        if not isinstance(config[key], list) or not all(isinstance(x, str) and x for x in config[key]):
            raise ValueError(f"{key} must be a list of nonempty strings.")
    if not config["include"]:
        raise ValueError("include must contain at least one project-relative directory.")
    for item in config["include"]:
        p = Path(item)
        if p.is_absolute() or p.drive or ".." in p.parts:
            raise ValueError("Included directories must stay within the project.")
    if type(config["max_file_bytes"]) is not int or config["max_file_bytes"] < 1:
        raise ValueError("max_file_bytes must be a positive integer.")
    if config["external_packages"] != "report_only":
        raise ValueError("Stage 1 supports external_packages='report_only' only.")
    return config


def git_state(root):
    def run(*args):
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    try:
        top = run("rev-parse", "--show-toplevel")
        if not top:
            return {"available": False, "reason": "not_a_git_repository"}
        changes = run("status", "--porcelain", "--untracked-files=normal")
        return {"available": True, "root": top, "commit": run("rev-parse", "--verify", "HEAD"),
                "branch": run("symbolic-ref", "--quiet", "--short", "HEAD"),
                "dirty": bool(changes) if changes is not None else None,
                "status": changes}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": type(exc).__name__}


def is_link(path):
    # Junctions and other Windows reparse points must not escape the source root.
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def scan(project, workspace, config_path=None):
    root = Path(project).resolve(strict=True)
    if not root.is_dir() or not (root / "Assets").is_dir() or not (root / "ProjectSettings").is_dir():
        raise ValueError("Choose a Unity project containing Assets and ProjectSettings directories.")
    destination = Path(workspace).resolve()
    if destination == root or destination.is_relative_to(root):
        raise ValueError("The discovery workspace must be outside the target Unity project.")
    config = load_config(config_path)
    database = destination / "inventory.sqlite3"
    if database.exists():
        with sqlite3.connect(database) as db:
            previous = db.execute("SELECT value FROM metadata WHERE key='report'").fetchone()
            if previous and json.loads(previous[0])["project"] != str(root):
                raise ValueError("This workspace belongs to a different project.")
    records, diagnostics, documents = [], [], {}
    visited = set()

    def record(path, status, reason=None, **extra):
        relative = path.relative_to(root).as_posix()
        row = {"path": relative, "kind": KINDS.get(path.suffix.lower(), "other"),
               "status": status, "reason": reason, "size": None, "sha256": None}
        row.update(extra)
        records.append(row)
        return row

    def excluded(path):
        relative = path.relative_to(root).as_posix().lower()
        return any(fnmatch.fnmatchcase(relative, pattern.lower()) or
                   fnmatch.fnmatchcase(path.name.lower(), pattern.lower())
                   for pattern in config["exclude"])

    def visit(path):
        key = str(path)
        if key in visited:
            return
        visited.add(key)
        try:
            if excluded(path):
                record(path, "excluded", "configured_exclusion")
                return
            if is_link(path) or not path.resolve().is_relative_to(root):
                record(path, "excluded", "link_or_external_path")
                return
            info = path.stat()
            if stat.S_ISDIR(info.st_mode):
                for child in sorted(path.iterdir(), key=lambda p: p.name):
                    visit(child)
                return
            if not stat.S_ISREG(info.st_mode):
                record(path, "unsupported", "not_a_regular_file")
                return
            if info.st_size > config["max_file_bytes"]:
                record(path, "skipped", "size_limit", size=info.st_size)
                return
            with path.open("rb") as stream:
                data = stream.read(config["max_file_bytes"] + 1)
            if len(data) > config["max_file_bytes"]:
                record(path, "skipped", "size_limit", size=len(data))
                return
            row = record(path, "supported", size=len(data), sha256=hashlib.sha256(data).hexdigest())
            try:
                if b"\0" in data:
                    raise UnicodeError("NUL byte")
                content = data.decode("utf-8-sig")
            except UnicodeError:
                row.update(status="binary", reason="binary_or_non_utf8")
                return
            if row["kind"] == "other":
                row.update(status="unsupported", reason="unsupported_extension")
                return
            relative = row["path"]
            if relative in ("ProjectSettings/ProjectVersion.txt", "Packages/manifest.json", "Packages/packages-lock.json") or path.suffix.lower() in (".asmdef", ".asmref"):
                documents[relative] = content
        except FileNotFoundError:
            record(path, "missing", "missing_or_removed_during_scan")
        except OSError as exc:
            record(path, "unreadable", type(exc).__name__)

    for folder in config["include"]:
        path = root / folder
        # Check ancestors too: an explicitly included subdirectory may pass through a link.
        ancestors = [p for p in path.parents if p != root and p.is_relative_to(root)]
        if str(path) in visited:
            continue
        if any(excluded(p) for p in ancestors):
            visited.add(str(path))
            record(path, "excluded", "excluded_ancestor")
        elif any(is_link(p) for p in ancestors if p.exists()):
            visited.add(str(path))
            record(path, "excluded", "link_or_external_path")
        else:
            visit(path)

    version_text = documents.get("ProjectSettings/ProjectVersion.txt", "")
    match = re.search(r"^m_EditorVersion:\s*(.+)$", version_text, re.MULTILINE)
    version = match.group(1).strip() if match else None
    if not version:
        diagnostics.append({"path": "ProjectSettings/ProjectVersion.txt", "reason": "unity_version_unavailable"})
    parsed = {}
    for path, content in documents.items():
        if path.endswith((".json", ".asmdef", ".asmref")):
            try:
                parsed[path] = json.loads(content)
            except ValueError as exc:
                diagnostics.append({"path": path, "reason": "invalid_json", "detail": str(exc)})
    manifest = parsed.get("Packages/manifest.json", {})
    dependencies = manifest.get("dependencies", {}) if isinstance(manifest, dict) else {}
    if not isinstance(dependencies, dict):
        diagnostics.append({"path": "Packages/manifest.json", "reason": "invalid_dependencies"})
        dependencies = {}
    packages = [{"name": name, "declaration": value,
                 "handling": "declared_only_not_resolved"}
                for name, value in sorted(dependencies.items())]
    # Directory exclusions represent pruned subtrees; their unseen children are not counted.
    report = {
        "schema_version": 1, "project": str(root), "workspace": str(destination),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "unity_version": version, "git": git_state(root), "config": config,
        "packages": packages,
        "package_lock": parsed.get("Packages/packages-lock.json"),
        "assemblies": [{"path": p, "declaration": value} for p, value in sorted(parsed.items())
                       if p.endswith((".asmdef", ".asmref"))],
        "counts": dict(sorted(Counter(r["status"] for r in records).items())),
        "kinds": dict(sorted(Counter(r["kind"] for r in records if r["size"] is not None).items())),
        "diagnostics": diagnostics, "entries": sorted(records, key=lambda r: r["path"]),
        "coverage_note": "Counts cover visited entries. Excluded directories represent whole unvisited subtrees. Supported means UTF-8 text with a recognized extension, not parsed Unity semantics. Asset subtypes and package contents are not resolved."
    }
    destination.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, kind TEXT, status TEXT, reason TEXT, size INTEGER, sha256 TEXT)")
        db.execute("DELETE FROM files")
        db.executemany("INSERT INTO files VALUES (:path,:kind,:status,:reason,:size,:sha256)", report["entries"])
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('report', ?)", (json.dumps(report),))
    return report


def status(workspace):
    database = Path(workspace).resolve() / "inventory.sqlite3"
    if not database.is_file():
        raise ValueError("No saved inventory found. Run scan first.")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        result = db.execute("SELECT value FROM metadata WHERE key='report'").fetchone()
    if not result:
        raise ValueError("Inventory has no completed scan.")
    return json.loads(result[0])


def format_report(report):
    lines = [f"Project: {report['project']}", f"Unity: {report['unity_version'] or 'unknown'}",
             f"Workspace: {report['workspace']}", f"Scanned: {report['scanned_at']}",
             "Coverage: " + ", ".join(f"{k}={v}" for k, v in report["counts"].items()),
             f"Package declarations: {len(report['packages'])}; assembly declarations: {len(report['assemblies'])}"]
    for row in report["entries"]:
        if row["status"] != "supported":
            lines.append(f"  {row['status']}: {row['path']} ({row['reason']})")
    for issue in report["diagnostics"]:
        lines.append(f"  diagnostic: {issue['path']} ({issue['reason']})")
    lines.append(report["coverage_note"])
    return "\n".join(lines)
