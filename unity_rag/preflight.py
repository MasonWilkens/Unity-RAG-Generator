"""Read-only readiness checks for creating a local Unity RAG workspace."""
from contextlib import closing
import importlib.util
from pathlib import Path
import shutil
import sqlite3
import sys

from .discovery import KINDS


def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def creation_preflight(project, workspace, *, semantic=False):
    """Inspect prerequisites without creating files, installing packages or fetching models."""
    if not isinstance(project, (str, Path)) or not str(project).strip():
        raise ValueError('A project path is required.')
    if not isinstance(workspace, (str, Path)) or not str(workspace).strip():
        raise ValueError('A workspace path is required.')
    if type(semantic) is not bool:
        raise ValueError('Semantic selection must be true or false.')
    project = Path(project).expanduser().resolve()
    workspace = Path(workspace).expanduser().resolve()
    checks = []
    def check(name, ok, detail):
        checks.append({'name': name, 'status': 'pass' if ok else 'fail', 'detail': detail})

    project_ok = project.is_dir() and (project / 'Assets').is_dir() and (project / 'ProjectSettings').is_dir()
    check('unity_project', project_ok, 'Requires an existing Unity root with Assets and ProjectSettings.')
    outside = not workspace.is_relative_to(project)
    check('workspace_outside_project', outside, 'The workspace must be outside the source project.')
    workspace_type_ok = not workspace.exists() or workspace.is_dir()
    check('workspace_path', workspace_type_ok, 'An existing workspace path must be a directory.')
    binding_ok, binding_detail = True, 'No existing workspace inventory was found.'
    if workspace_type_ok and ((workspace / 'active.json').exists() or (workspace / 'inventory.sqlite3').exists()):
        try:
            from .discovery import status
            existing = status(workspace)
            binding_ok = Path(existing['project']).resolve() == project
            binding_detail = ('Existing inventory belongs to this project.' if binding_ok else
                              'Existing inventory belongs to a different project.')
        except (OSError, ValueError, KeyError, sqlite3.Error):
            binding_ok, binding_detail = False, 'Existing workspace cannot be verified; run diagnostics.'
    check('workspace_binding', binding_ok, binding_detail)
    check('python', sys.version_info >= (3, 10), 'Python 3.10 or newer is required.')
    check('yaml_dependency', _module_available('yaml'), 'PyYAML must be importable.')
    check('dotnet_on_path', shutil.which('dotnet') is not None,
          'A .NET SDK is required for code mapping; this only checks PATH, not SDK compatibility.')
    try:
        with closing(sqlite3.connect(':memory:')) as db, db:
            db.execute('CREATE VIRTUAL TABLE fts_probe USING fts5(content)')
        fts_ok = True
    except sqlite3.Error:
        fts_ok = False
    check('sqlite_fts5', fts_ok, 'SQLite FTS5 is required for local retrieval.')
    if semantic:
        check('fastembed_dependency', _module_available('fastembed'),
              'FastEmbed must be importable for the default semantic provider.')
    existing = workspace
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    try:
        free_bytes = shutil.disk_usage(existing).free
        check('disk_space_visible', True, 'Free space was read; required index size is not estimated.')
    except OSError:
        free_bytes = None
        check('disk_space_visible', False, 'Free disk space could not be determined.')
    return {
        'project': str(project), 'workspace': str(workspace),
        'ready_for_attempt': all(row['status'] == 'pass' for row in checks),
        'semantic_requested': semantic, 'checks': checks, 'free_bytes': free_bytes,
        'supported_extensions': sorted(KINDS),
        'limitations': [
            'No files are created; workspace write permission and future disk availability are not proven.',
            'This does not scan project contents or certify secret exclusion for a particular configuration.',
            'A PATH entry does not prove .NET SDK version or reference compatibility.',
            'Cached embedding model availability is not checked; no model or package is downloaded.',
            'Live Google Sheets are not fetched and spreadsheet rows are not automatically ingested.',
            'CSV, TSV, XLSX, XLS and ODS are not supported as sheet-row ingestion.',
        ],
    }
