"""Optional small, exact excerpts from current indexed serialized sources."""
from pathlib import Path
import re
from collections import Counter

from .asset_map import fingerprint
from .code_map import _read_snapshot_file
from .source_checks import inspect_source

UNITY_BOILERPLATE = {'m_ObjectHideFlags', 'm_CorrespondingSourceObject', 'm_PrefabInstance',
                     'm_PrefabAsset', 'm_GameObject', 'm_Enabled', 'm_EditorHideFlags', 'm_Script'}


def source_excerpts(project, entries, items, query, limit=24):
    # These are query-matched source lines, not verified answers or citation repairs.
    from .retrieval import terms
    wanted = set(terms(query))
    tiers, seen = [[], [], []], set()
    if not wanted:
        return result
    for item in items:
        if item.get('kind') not in ('asset', 'asset_object'):
            continue
        for citation in item.get('sources', []):
            path = citation.get('path', '')
            if path in seen or Path(path).suffix.lower() not in ('.asset', '.prefab', '.unity'):
                continue
            seen.add(path)
            entry = entries.get(path, {})
            size = entry.get('size')
            if type(size) is not int or size < 0 or size > 2_000_000 or inspect_source(project, entries, citation)['status'] != 'current':
                continue
            try:
                lines = _read_snapshot_file(Path(project).resolve(), entry).splitlines()
            except (OSError, UnicodeError, ValueError):
                continue
            if not lines:
                continue
            start = max(1, citation.get('start_line') or 1)
            end = min(len(lines), citation.get('end_line') or len(lines))
            fields = None
            if item.get('kind') == 'asset_object':
                fields = set()
                for field in item.get('text', '').splitlines():
                    match = re.match(r'^([\w.]+):\s*(.*)$', field)
                    if match:
                        fields.add((match.group(1).rsplit('.', 1)[-1], match.group(2)))
                if not fields:
                    continue
                occurrences = Counter()
                for line in lines[start - 1:end]:
                    raw = line.strip()
                    if raw.startswith('- '):
                        raw = raw[2:]
                    field = re.match(r'^([\w]+):\s*(.*)$', raw)
                    if field:
                        occurrences[(field.group(1), field.group(2))] += 1
            matches = []
            for index in range(start - 1, end):
                field_name = re.match(r'^\s*(?:-\s*)?([\w]+):', lines[index])
                if field_name and field_name.group(1) in UNITY_BOILERPLATE and field_name.group(1).casefold() not in query.casefold():
                    continue
                if fields is not None:
                    raw = lines[index].strip()
                    if raw.startswith('- '):
                        raw = raw[2:]
                    field = re.match(r'^([\w]+):\s*(.*)$', raw)
                    key = (field.group(1), field.group(2)) if field else None
                    if key not in fields or occurrences[key] != 1:
                        continue
                matched = wanted.intersection(terms(lines[index]))
                if matched:
                    matches.append((index, matched))
            if not matches:
                continue
            uncovered, spans = set(wanted), []
            for ordinal in range(3 if Path(path).suffix.lower() == '.asset' else 1):
                candidates = [(len(tokens & uncovered), index) for index, tokens in matches
                              if not any(first <= index < last for first, last in spans)]
                if not candidates:
                    break
                score, index = max(candidates, key=lambda pair: (pair[0], -pair[1]))
                if not score:
                    break
                first, last = max(start - 1, index - 3), min(end, index + 4)
                spans.append((first, last))
                text = '\n'.join(lines[first:last])
                uncovered -= set(terms(text))
                narrowed = {'path': path, 'sha256': citation['sha256'], 'start_line': first + 1, 'end_line': last}
                excerpt = {'id': 'source-excerpt:' + fingerprint([narrowed, text])[:32],
                           'entity_id': item.get('entity_id'), 'kind': 'source_excerpt',
                           'evidence_type': 'original_source', 'title': path,
                           'text': text, 'sources': [narrowed],
                           'expansion': {'from': item.get('id'), 'reason': 'query_matched_current_source',
                                         'relationship': None}}
                tiers[ordinal].append(excerpt)
    return [excerpt for tier in tiers for excerpt in tier][:limit]
