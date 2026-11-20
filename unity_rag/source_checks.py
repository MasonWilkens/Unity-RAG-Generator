"""Verify cited files against the inventory; quote repairs remain proposals."""
from pathlib import Path
from .code_map import _read_snapshot_file
from .discovery import sensitive_path_name, hidden_directory, is_link


def inspect_source(project, entries, source):
    path = source.get('path', '')
    if not isinstance(path, str) or not path:
        return {'path': '', 'status': 'excluded'}
    rel = Path(path)
    if rel.is_absolute() or rel.drive or '..' in rel.parts:
        return {'path': path, 'status': 'excluded'}
    current = Path(project).resolve()
    for part in rel.parts:
        if sensitive_path_name(Path(part)):
            return {'path': path, 'status': 'excluded'}
        current = current / part
        try:
            if is_link(current) or hidden_directory(current):
                return {'path': path, 'status': 'excluded'}
        except FileNotFoundError:
            return {'path': path, 'status': 'missing'}
        except OSError:
            return {'path': path, 'status': 'unreadable'}
    entry = entries.get(path)
    if entry is None:
        return {'path': path, 'status': 'unindexed'}
    if entry.get('status') != 'supported':
        return {'path': path, 'status': 'excluded' if entry.get('status') == 'excluded' else 'unsupported'}
    if not source.get('sha256') or source['sha256'] != entry.get('sha256'):
        return {'path': path, 'status': 'snapshot_mismatch'}
    try:
        _read_snapshot_file(Path(project).resolve(), entry)
    except FileNotFoundError:
        return {'path': path, 'status': 'missing'}
    except (OSError, UnicodeError):
        return {'path': path, 'status': 'unreadable'}
    except ValueError:
        return {'path': path, 'status': 'stale'}
    return {'path': path, 'status': 'current', 'sha256': source['sha256']}


def normalize_quote(project, entries, citation, quote):
    """Propose exact lines only for one occurrence in a current verified source."""
    result = inspect_source(project, entries, citation)
    if result['status'] != 'current':
        return {'status': result['status']}
    if not isinstance(quote, str) or not quote.strip():
        return {'status': 'invalid_quote'}
    entry = entries[citation['path']]
    text = '\n'.join(_read_snapshot_file(Path(project).resolve(), entry).splitlines())
    positions = []
    start = 0
    while True:
        pos = text.find(quote, start)
        if pos < 0:
            break
        positions.append(pos)
        if len(positions) > 1:
            return {'status': 'ambiguous'}
        start = pos + 1
    if not positions:
        return {'status': 'not_found'}
    first = text.count('\n', 0, positions[0]) + 1
    last = first + quote.count('\n')
    return {'status': 'proposed', 'citation': {'path': citation['path'], 'sha256': citation['sha256'],
            'start_line': first, 'end_line': last, 'quote': quote}, 'automatic_acceptance': False}
