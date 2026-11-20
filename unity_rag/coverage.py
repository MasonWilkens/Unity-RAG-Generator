"""Read-only coverage summary for one pinned managed workspace generation."""
from collections import Counter
from contextlib import closing
import json
from pathlib import Path, PurePosixPath
import sqlite3

from .discovery import sensitive_path_name, status
from .source_checks import inspect_source
from .workspace import resolve_workspace


def _safe_path(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('Use canonical project-relative forward-slash paths.')
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or str(path) != value or ':' in value or
            any(part in ('.', '..') or sensitive_path_name(Path(part)) for part in path.parts)):
        raise ValueError('Requested path is outside the permitted project scope.')
    return value


_MAX_CODE_REPORT_BYTES = 32 * 1024 * 1024


def _conditional_coverage(db, scanned_at, requested):
    """Read bounded code-map metadata through SQLite without materializing source text in Python."""
    empty = {'status': 'not_mapped', 'supplied_defines': None, 'inactive_text_regions': None,
             'files_with_inactive_text': None}
    row = db.execute("SELECT length(CAST(value AS BLOB)) FROM metadata WHERE key='code_report'").fetchone()
    if not row:
        return empty, {}, set()
    if row[0] is None:
        return {**empty, 'status': 'unavailable'}, {}, set()
    if row[0] > _MAX_CODE_REPORT_BYTES:
        return {**empty, 'status': 'too_large'}, {}, set()
    if not db.execute("SELECT json_valid(value) FROM metadata WHERE key='code_report'").fetchone()[0]:
        return {**empty, 'status': 'unavailable'}, {}, set()
    row = db.execute("SELECT json_extract(value,'$.discovery_scanned_at'), json_type(value,'$.defines'), "
                     "json_extract(value,'$.defines'), json_type(value,'$.conditions'), json_type(value,'$.files') "
                     "FROM metadata WHERE key='code_report'").fetchone()
    stamp, defines_type, defines_raw, conditions_type, files_type = row
    if not isinstance(stamp, str):
        return {**empty, 'status': 'unavailable'}, {}, set()
    if stamp != scanned_at:
        return {**empty, 'status': 'stale'}, {}, set()
    if defines_type != 'array' or conditions_type != 'array' or files_type != 'array':
        return {**empty, 'status': 'unavailable'}, {}, set()
    defines = json.loads(defines_raw)
    if not all(isinstance(value, str) for value in defines):
        return {**empty, 'status': 'unavailable'}, {}, set()
    # Reject malformed arrays instead of counting their omissions as zero.
    for key, alias in (('conditions', 'c'), ('files', 'f')):
        fields = ("json_type(" + alias + ".value,'$.path') IS NOT 'text'")
        if key == 'conditions':
            fields += " OR json_type(" + alias + ".value,'$.kind') IS NOT 'text'"
        invalid = ("SELECT 1 FROM metadata m, json_each(m.value,'$." + key + "') " + alias
                   + " WHERE m.key='code_report' AND CASE WHEN " + alias + ".type='object' THEN ("
                   + fields + ") ELSE 1 END LIMIT 1")
        if db.execute(invalid).fetchone():
            return {**empty, 'status': 'unavailable'}, {}, set()
    orphan = ("SELECT 1 FROM metadata m, json_each(m.value,'$.conditions') c "
              "WHERE m.key='code_report' AND json_extract(c.value,'$.path') NOT IN "
              "(SELECT json_extract(f.value,'$.path') FROM metadata x, json_each(x.value,'$.files') f "
              "WHERE x.key='code_report') LIMIT 1")
    if db.execute(orphan).fetchone():
        return {**empty, 'status': 'unavailable'}, {}, set()
    inactive, total, files = {}, 0, 0
    query = ("SELECT json_extract(c.value,'$.path'), count(*) FROM metadata m, json_each(m.value,'$.conditions') c "
             "WHERE m.key='code_report' AND json_extract(c.value,'$.kind')='DisabledTextTrivia' GROUP BY 1")
    wanted = set(requested)
    for path, count in db.execute(query):
        total += count
        if isinstance(path, str):
            files += 1
            if path in wanted:
                inactive[path] = count
    mapped = set()
    if requested:
        placeholders = ','.join('?' for _ in requested)
        query = ("SELECT json_extract(f.value,'$.path') FROM metadata m, json_each(m.value,'$.files') f "
                 "WHERE m.key='code_report' AND json_extract(f.value,'$.path') IN (" + placeholders + ")")
        mapped = {row[0] for row in db.execute(query, requested)}
    return {'status': 'current', 'supplied_defines': defines, 'inactive_text_regions': total,
            'files_with_inactive_text': files}, inactive, mapped


def coverage(workspace, paths=(), *, include_conditional=False):
    """Summarize a saved snapshot; verify only explicitly requested source paths."""
    if type(include_conditional) is not bool:
        raise ValueError('Conditional coverage must be true or false.')
    if len(paths) > 50:
        raise ValueError('At most 50 source paths can be checked at once.')
    requested = list(dict.fromkeys(_safe_path(path) for path in paths))
    generation = resolve_workspace(workspace)
    inventory = status(generation)
    entries = {row['path']: row for row in inventory['entries']}
    groups = Counter((row['kind'], row['status']) for row in inventory['entries'])
    spreadsheet = Counter((Path(row['path']).suffix.lower(), row['status'])
                          for row in inventory['entries']
                          if Path(row['path']).suffix.lower() in ('.csv', '.tsv', '.xlsx', '.xls', '.ods'))
    database = generation / 'inventory.sqlite3'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if include_conditional:
            db.execute('BEGIN')  # Keep all code-map reads on one SQLite snapshot.
        present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence'").fetchone() is not None
        evidence = dict(db.execute('SELECT kind, count(*) FROM evidence GROUP BY kind')) if present else {}
        by_path = {}
        if present and requested:
            placeholders = ','.join('?' for _ in requested)
            query = ("SELECT json_extract(source.value, '$.path'), evidence.kind, count(*) "
                     "FROM evidence, json_each(evidence.data, '$.sources') AS source "
                     "WHERE json_extract(source.value, '$.path') IN (" + placeholders + ") "
                     "GROUP BY 1, 2")
            for path, kind, count in db.execute(query, requested):
                by_path.setdefault(path, {})[kind] = count
        if include_conditional:
            conditional, inactive_by_path, mapped_scripts = _conditional_coverage(db, inventory['scanned_at'], requested)
    details = []
    for path in requested:
        entry = entries.get(path)
        row = {'path': path, 'inventory_status': entry['status'] if entry else 'not_in_inventory',
               'evidence_records': by_path.get(path, {}) if present else None}
        if entry:
            row['kind'] = entry['kind']
            row['reason'] = entry.get('reason')
            if include_conditional and entry['kind'] == 'script':
                row['inactive_preprocessor_regions'] = (inactive_by_path.get(path, 0)
                    if conditional['status'] == 'current' and path in mapped_scripts else None)
            row['source_status'] = inspect_source(inventory['project'], entries,
                                                  {'path': path, 'sha256': entry.get('sha256')})['status']
        else:
            row['source_status'] = 'not_checked'
        details.append(row)
    result = {
        'generation': generation.name,
        'project': inventory['project'],
        'inventory': [{'kind': kind, 'status': state, 'files': count}
                      for (kind, state), count in sorted(groups.items())],
        'spreadsheet_files': [{'extension': extension, 'status': state, 'files': count}
                              for (extension, state), count in sorted(spreadsheet.items())],
        'evidence_table_present': present,
        'evidence_records_by_kind': evidence if present else None,
        'requested_paths': details,
        'limitations': [
            'Counts describe the saved generation, not a full live-project freshness scan.',
            'Excluded directories are pruned; paths not in inventory are not necessarily nonexistent.',
            'Supported inventory files do not necessarily produce retrieval evidence.',
            'Local discovery does not fetch live Google Sheets or other remote source data.',
            'A generated local asset is a snapshot and does not establish that a live sheet matches.',
            'CSV, TSV and spreadsheet binary formats are not supported as sheet-row ingestion.',
        ],
    }
    if include_conditional:
        result['conditional_compilation'] = conditional
        result['limitations'].append('Inactive text segments reflect supplied defines, not runtime behavior or a count of branches.')
    return result
