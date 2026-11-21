"""Bounded identifier-based anchors for implementation questions.

These query-time heuristics use parsed references, not comments, string values,
containing-type repetition or model confidence. They do not prove behavior.
"""
import itertools
import json
import re

_FILLER = {'a', 'an', 'the', 'is', 'are', 'of', 'to', 'in', 'for', 'how', 'what',
           'where', 'which', 'does', 'do', 'and', 'or', 'when', 'from', 'its',
           'their', 'this', 'that', 'with', 'into', 'can', 'not', 'has', 'have'}
_CALCULATIONS = {'get', 'calculate', 'compute', 'evaluate'}
_PROTECTED = {'named_type_method', 'scene_component_anchor', 'scene_field_usage',
              'declared_unity_tag', 'named_root_position', 'prefab_identifier', 'named_unity_object',
              'reserved_lexical_passage', 'related_method_terms'}


def parts(text):
    split = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', text)
    split = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', split).replace('_', ' ')
    return [word.casefold() for word in re.findall(r'[A-Za-z][A-Za-z0-9]*', split)]


def stem(word):
    if len(word) >= 6 and word.endswith('ing'):
        word = word[:-3]
    elif len(word) >= 5 and word.endswith('ed'):
        word = word[:-2]
    elif len(word) >= 5 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
        word = word[:-1]
    return word[:-1] if len(word) > 3 and word.endswith('e') else word


def concepts(text):
    return {stem(word) for word in parts(text) if len(word) >= 3 and word not in _FILLER}


def _variants(word):
    result = [word]
    if len(word) >= 6 and word.endswith('ing'):
        result += [word[:-3], word[:-3] + 'e']
    elif len(word) >= 5 and word.endswith('ed'):
        result += [word[:-2], word[:-1]]
    elif len(word) >= 5 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
        result += [word[:-1]]
    return list(dict.fromkeys(result))


def _query_aliases(query):
    matches = list(re.finditer(r'[A-Za-z_][A-Za-z0-9_]*', query))
    words = [match.group() for match in matches]
    if len(words) > 32:
        raise _BoundExceeded
    aliases = set()
    for start in range(len(words)):
        for width in range(1, min(4, len(words) - start) + 1):
            phrase = words[start:start + width]
            if any(not query[matches[i].end():matches[i + 1].start()].isspace()
                   for i in range(start, start + width - 1)):
                continue
            if any(word.casefold() in _FILLER for word in phrase):
                continue
            for combination in itertools.product(*(_variants(word.casefold()) for word in phrase)):
                aliases.add(''.join(combination))
            if len(aliases) > 256:
                raise _BoundExceeded
    return aliases, words


def _type_scope(db, query, exclude_aliases=()):
    aliases, words = _query_aliases(query)
    aliases.difference_update(exclude_aliases)
    if not aliases:
        return None, []
    rows = db.execute(
        "SELECT DISTINCT e.entity,e.data FROM evidence_aliases a JOIN evidence e ON e.entity=a.entity "
        "WHERE e.kind='code_type' AND a.alias IN (" + ','.join('?' for _ in aliases) + ') LIMIT 101',
        tuple(sorted(aliases))).fetchall()
    if len(rows) > 100:
        raise _BoundExceeded
    owners = {}
    explicit_names = {word for word in words if word[0].isupper() and word.casefold() not in _FILLER}
    for entity, raw in rows:
        if len(raw) > 1000000:
            raise _BoundExceeded
        record = json.loads(raw)
        name = record['title'].rsplit('.', 1)[-1].split('<', 1)[0]
        # Accept a complete multipart name, or an explicitly capitalized type.
        # A generic word such as "weapon" alone must not select one base class.
        if len(parts(name)) >= 2 or name in explicit_names:
            owners.setdefault(entity, []).append(record)
    if len(owners) > 1:
        raise _AmbiguousScope
    return next(iter(owners.items())) if owners else (None, [])


class _BoundExceeded(Exception):
    pass


class _AmbiguousScope(Exception):
    pass


def _name(row):
    return row.get('title', '').split('(', 1)[0].rsplit('.', 1)[-1].split('<', 1)[0]


def _type_concepts(title):
    name = title.rsplit('.', 1)[-1].split('<', 1)[0]
    return concepts(title) | {stem(name.casefold())}


def promote_behavior_methods(db, ordered, query, kind=None):
    """Promote scoped behavior or an unambiguous calculation candidate.

    Only the first 100 existing candidates are examined. Existing exact/structured
    anchors are authoritative. Ambiguous types, unsupported requests and exceeded
    bounds preserve the original ordering. No extra evidence is fetched as a hit.
    """
    if (kind not in (None, 'code_member') or not re.match(r'^\s*how\s+does\b', query, re.I)
            or re.search(r'\b(scene|prefab|serialized|inspector|asset|configuration|configure)\b', query, re.I)
            or re.search(r'\b[A-Za-z_]\w*\.[A-Za-z_]\w*', query)
            or any(row.get('match') == 'exact_alias' or _PROTECTED.intersection(row.get('ranking_diagnostics', {}))
                   for row in ordered)):
        return ordered, False
    try:
        return _promote(db, ordered, query)
    except (_BoundExceeded, _AmbiguousScope):
        return ordered, False


def _calculation_question(query):
    """Read a narrow active input/change/output question, without guessing roles."""
    words = re.findall(r'[A-Za-z_][A-Za-z0-9_]*', query)
    verbs = {'affect', 'change', 'alter', 'modify', 'influence', 'determine',
             'adjust', 'scale', 'increase', 'decrease', 'reduce'}
    positions = [i for i, word in enumerate(words) if word.casefold() in verbs]
    if len(positions) != 1:
        return None
    index = positions[0]
    output_words = words[index + 1:]
    qualifiers = {'with', 'when', 'as', 'by', 'under', 'during', 'because', 'if', 'after',
                  'before', 'while', 'of', 'for', 'in', 'on', 'at', 'to', 'from', 'without'}
    if not output_words or qualifiers.intersection(word.casefold() for word in output_words):
        return None
    inputs = concepts(' '.join(words[:index]))
    outputs = [stem(word) for word in parts(' '.join(output_words)) if len(word) >= 3 and word not in _FILLER]
    return (inputs, outputs) if inputs and outputs else None


def _promote(db, ordered, query):
    owner, declarations = _type_scope(db, query)
    owner_terms = _type_concepts(declarations[0]['title']) if owner else set()
    wanted = concepts(query) - owner_terms
    if len(wanted) < 2:
        return ordered, False
    calculation = _calculation_question(query) if not owner else None
    if not owner and calculation is None:
        return ordered, False
    methods = [(rank, row) for rank, row in enumerate(ordered[:100], 1)
               if row.get('kind') == 'code_member' and '::M:' in row.get('entity_id', '')
               and (not owner or row.get('parent') == owner)]
    if not methods:
        return ordered, False
    query_words = {word.casefold() for word in re.findall(r'\w+', query)}
    explicit = {row['entity_id'] for _, row in methods
                if owner and _name(row).casefold() in query_words
                and concepts(_name(row)) - owner_terms}
    if len(explicit) > 1:
        # An overloaded method or a question naming multiple methods needs the
        # ordinary ranking, not an arbitrary choice between identities.
        return ordered, False
    selected = []
    cache = {}
    total_count = total_chars = 0
    for rank, row in methods:
        if explicit and row['entity_id'] not in explicit:
            continue
        name_parts = parts(_name(row))
        output_sequence = [stem(word) for word in name_parts[1:]]
        output = set(output_sequence)
        if not owner and (len(name_parts) < 2 or name_parts[0] not in _CALCULATIONS
                          or calculation[1][-len(output_sequence):] != output_sequence):
            continue
        source = next((src for src in row.get('sources', []) if src.get('role') != 'reference_dependency'), {})
        if (not source.get('sha256') or type(source.get('start_line')) is not int
                or type(source.get('end_line')) is not int):
            continue
        entity = row['entity_id']
        if entity not in cache:
            edges = []
            for index, (raw,) in enumerate(db.execute(
                    "SELECT data FROM evidence_relations WHERE source=? AND kind='references' LIMIT 1001", (entity,)), 1):
                total_count += 1
                total_chars += len(raw)
                if index > 1000 or len(raw) > 1000000 or total_count > 5000 or total_chars > 20000000:
                    raise _BoundExceeded
                edges.append(json.loads(raw))
            cache[entity] = edges
        occurrences = []
        body = set()
        matched_occurrences = 0
        for edge in cache[entity]:
            loc = edge.get('location') or {}
            if (loc.get('path') != source.get('path') or type(loc.get('start_line')) is not int
                    or type(loc.get('end_line')) is not int
                    or not source['start_line'] <= loc['start_line'] <= loc['end_line'] <= source['end_line']):
                continue
            covered = concepts(edge.get('text', '')) & wanted
            if covered:
                matched_occurrences += 1
                if covered - body:
                    occurrences.append({'terms': sorted(covered), 'path': loc['path'],
                                        'start_line': loc['start_line'], 'end_line': loc['end_line']})
                body.update(covered)
        covered = (body | concepts(_name(row))) & wanted
        if explicit:
            rule = 'scoped_member_name'
        elif owner and len(covered) >= 2 and body:
            rule = 'scoped_identifier_coverage'
        elif not owner and len(name_parts) >= 2 and name_parts[0] in _CALCULATIONS:
            output = {stem(word) for word in name_parts[1:]}
            # The output name alone is insufficient: require a separate input
            # concept in this chunk, excluding the containing type's words.
            parent_words = _type_concepts(row.get('parent', '').split('::T:')[-1])
            if not ((body & calculation[0]) - output - parent_words):
                continue
            rule = 'calculation_name_and_input'
        else:
            continue
        selected.append((len(covered), rank, row, rule, sorted(covered), occurrences, matched_occurrences))
    if not selected:
        return ordered, False
    if not owner and len({slot[2]['entity_id'] for slot in selected}) != 1:
        return ordered, False
    selected.sort(key=lambda slot: (-slot[0], slot[1]))
    seen, promoted = set(), []
    dependencies = []
    for declaration in declarations:
        for source in declaration.get('sources', []):
            if not source.get('sha256'):
                return ordered, False
            if source.get('path') not in {s['path'] for s in dependencies}:
                dependencies.append(source)
    for _, rank, original, rule, covered, occurrences, matched_occurrences in selected:
        if original['entity_id'] in seen:
            continue
        seen.add(original['entity_id'])
        row = dict(original)
        sources = list(row.get('sources', []))
        existing_paths = {s.get('path') for s in sources}
        row['sources'] = sources + [{**s, 'role': 'reference_dependency'} for s in dependencies
                                   if s.get('path') not in existing_paths]
        row['ranking_diagnostics'] = {**row.get('ranking_diagnostics', {}),
            'selected_passage': 'behavior_identifier', 'behavior_anchor': {
                'version': 1, 'rule': rule, 'scope': owner, 'original_rank': rank,
                'matched_terms': covered, 'reference_spans': occurrences[:32],
                'reference_spans_omitted': max(0, matched_occurrences - min(32, len(occurrences)))}}
        promoted.append(row)
    promoted_ids = {row['id'] for row in promoted}
    return promoted + [row for row in ordered if row['id'] not in promoted_ids], True
