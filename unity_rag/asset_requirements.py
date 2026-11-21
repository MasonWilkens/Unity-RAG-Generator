"""Snapshot-scoped gaps for explicit saved-asset value questions."""
import json
from pathlib import PurePosixPath
import re

_NOUN = r'(?:prefab|scene|asset|material|animation|controller)'
_EXT = r'(?:prefab|unity|asset|mat|anim|controller|overridecontroller)'
_INTENT = re.compile(r'\b(?:serialized|serialization|saved|stored|yaml)\b', re.I)


def serialized_asset_requirement(db, query):
    """Identify one named serialization target, including name collisions.

    Does not read project files or assert anything about current Editor state.
    Unsupported grammar or multiple different targets leave retrieval unchanged.
    """
    if (not _INTENT.search(query) or len(query.split()) > 64
            or re.search(r'\b(?:load|loads|loading|instantiate|instantiates|instantiating|parse|parses|parsing)\b', query, re.I)
            or not re.search(r'\b' + _NOUN + r'\b|\.' + _EXT + r'\b', query, re.I)):
        return None
    normalized = query.replace('\\', '/')
    from .retrieval import _tag_query_quote_regions, _tag_query_match_is_whole
    quote_regions = _tag_query_quote_regions(normalized)
    rows = db.execute("SELECT path,status,reason,sha256 FROM files WHERE kind IN "
                      "('prefab','scene','serialized_asset','material','animation') ORDER BY path LIMIT 10001").fetchall()
    if len(rows) > 10000:
        return None

    def mentions(name, bare=False):
        for match in re.finditer(r'(?<![\w./])' + re.escape(name) + r'(?![\w/]|\.\w)', normalized, re.I):
            if not _tag_query_match_is_whole(normalized, match.span(), quote_regions):
                continue
            preceding = normalized[:match.start()].rstrip()
            if (bare and preceding and preceding[-1] not in '\"\x27`'
                    and not re.search(r'\b(?:the|a|an|in|of|on|from|for|and|or|prefab|scene|asset|material|named|called)$', preceding, re.I)):
                continue
            return True
        return False

    matches = [r for r in rows if mentions(r[0])]
    if not matches:
        matches = [r for r in rows if mentions(PurePosixPath(r[0]).name, bare=True)]
    if not matches:
        for row in rows:
            stem = PurePosixPath(row[0]).stem
            if stem.casefold() not in normalized.casefold():
                continue
            name = r'[\"\x27`]?' + re.escape(stem) + r'[\"\x27`]?'
            # Requiring a grammatical lead prevents "Old Menu prefab" from
            # silently matching a different asset called just "Menu".
            before = r'(?:^|\b(?:the|a|an|in|of|on|from|for|and|or)\s+)' + name + r'\s+' + _NOUN + r'\b'
            after = (r'\b' + _NOUN + r'\s+(?:named\s+|called\s+)?' + name
                     + r'(?=$|[?.!,;:]|\s+(?:is|are|was|were|has|have|with|where|which|does|that|and|or|in|for)\b)')
            if re.search(before, normalized, re.I) or re.search(after, normalized, re.I):
                matches.append(row)
    if not matches:
        return None
    if len(matches) > 1:
        # Distinct explicitly requested assets need a multi-target policy.
        if len({PurePosixPath(r[0]).stem.casefold() for r in matches}) > 1:
            return None
        return {'status': 'ambiguous_target', 'blocked': True, 'scope': 'indexed_snapshot',
                'paths': [r[0] for r in matches[:20]], 'omitted_paths': max(0, len(matches) - 20),
                'description': 'The saved asset name matches multiple indexed files. Use its full project-relative path.'}
    path, status, reason, digest = matches[0]
    result = {'status': 'available', 'blocked': False, 'scope': 'indexed_snapshot',
              'path': path, 'inventory_status': status, 'inventory_reason': reason, 'sha256': digest}
    if status != 'supported':
        result.update(status='source_unavailable', blocked=True,
                      description=f'The indexed snapshot cannot supply saved values from {path} ({reason or status}). '
                                  'Provide readable Unity text serialization, materialize LFS content when applicable, '
                                  'and refresh the workspace. Related C# defaults do not establish saved asset values.')
        return result
    mapped = db.execute('SELECT data FROM unity_assets WHERE path=? LIMIT 2', (path,)).fetchall()
    if len(mapped) != 1 or len(mapped[0][0]) > 1000000:
        parse_status = 'map_unavailable'
    else:
        parse_status = json.loads(mapped[0][0]).get('parse_status', 'map_unavailable')
    result['parse_status'] = parse_status
    if parse_status != 'parsed':
        result.update(status='serialization_unavailable', blocked=True,
                      description=f'The indexed snapshot has no parsed Unity serialization for {path} ({parse_status}). '
                                  'Check the saved asset and refresh the workspace; related code is not evidence of its saved values.')
    return result
