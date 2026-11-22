"""Bounded, symmetric evidence selection for explicit two-type comparisons.

Parsed identifier coverage is a ranking signal, not a claim that implementations
are equivalent, different, connected by calls, or complete within a tiny budget.
"""
import re

from .behavior_ranking import _AmbiguousScope, _BoundExceeded, _name, concepts
from .multipart_ranking import _Reader, _coverage, _dependencies, _CODE_ANCHORS, _OTHER_ANCHORS

_IDENTIFIER = r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*'
_NAMED = rf'(?:`{_IDENTIFIER}`|{_IDENTIFIER})'
_PAIR = re.compile(
    rf'^\s*(?P<intent>compare(?:\s+how)?|how\s+do|what\s+(?:is|are)\s+the\s+differences?\s+between)'
    rf'\s+(?P<left>{_NAMED})\s+(?:and|with|vs\.?|versus)\s+(?P<right>{_NAMED})(?P<tail>.*)$', re.I)
_PROSE = concepts('compare comparison difference differ differently between implementation implementations behavior')
_PROTECTED = _CODE_ANCHORS | {'multipart_code', 'cross_file_call'}


def _request(query):
    if len(query) > 4096 or len(re.findall(r'\w+', query)) > 32:
        return None
    match = _PAIR.fullmatch(query)
    if not match:
        return None
    tail = match['tail'].strip()
    # A third name adjacent to the pair is not a behavior clause.
    if re.match(r'^(?:and|with|vs\.?|versus)\b|^,\s*[A-Za-z_`]', tail, re.I):
        return None
    if match['intent'].lower().startswith('how') and not re.search(r'\bdiffer(?:ent(?:ly)?)?\b', tail, re.I):
        return None
    wanted = concepts(tail) - _PROSE
    if not wanted:
        return None
    return [match['left'].strip('`'), match['right'].strip('`')], tail, wanted


def _types(reader, name):
    rows = reader.read(
        "SELECT DISTINCT e.data FROM evidence_aliases a JOIN evidence e ON e.entity=a.entity "
        "WHERE e.kind='code_type' AND a.alias=? ORDER BY e.id", (name.casefold(),), 100)
    return {r['entity_id'] for r in rows}, rows


def _candidates(reader, owner, wanted):
    rows = []
    for marker in ('::M:', '::P:'):
        prefix = owner.replace('::T:', marker, 1) + '.'
        rows += reader.read("SELECT data FROM evidence WHERE entity>=? AND entity<? AND kind='code_member' ORDER BY entity,id",
                            (prefix, prefix[:-1] + '/'), 200)
    if len(rows) > 200:
        raise _BoundExceeded
    return [value for row in rows if row.get('parent') == owner
            and (value := _coverage(reader, row, wanted))]


def _choose(candidates, ranks):
    chosen, covered = [], set()
    for _ in range(2):
        available = [c for c in candidates if c['covered'] - covered]
        if not available:
            break
        best = max(len(c['covered'] - covered) for c in available)
        tied = [c for c in available if len(c['covered'] - covered) == best]
        # Do not silently choose an overload or equally supported competing
        # implementation. Separate chunks of one identity remain eligible.
        for index, a in enumerate(tied):
            for b in tied[index + 1:]:
                if a['row']['entity_id'] != b['row']['entity_id'] and (
                        _name(a['row']) == _name(b['row']) or a['covered'] - covered == b['covered'] - covered):
                    raise _AmbiguousScope
        value = min(tied, key=lambda c: (ranks.get(c['row']['id'], len(ranks)), c['row']['id']))
        chosen.append(value)
        covered.update(value['covered'])
    return chosen


def promote_type_comparison(db, ordered, query, kind=None):
    """Reserve one relevant chunk per explicitly named type, then at most one more.

    Namespace and assembly collisions require an unambiguous indexed alias.
    Bounds, unsupported requests or either missing side leave ranking unchanged.
    Existing protected anchors retain priority. No calls are traversed.
    """
    request = _request(query)
    if (request is None or kind not in (None, 'code_member')
            or re.search(r'\b(scene|prefab|asset|serialized|inspector|configuration|signature|declarations?)\b', query, re.I)
            or any(r.get('match') == 'exact_alias' or _OTHER_ANCHORS.intersection(r.get('ranking_diagnostics', {})) for r in ordered)):
        return ordered, False
    try:
        names, tail, wanted = request
        reader, owners, dependencies = _Reader(db), [], []
        for name in names:
            identities, declarations = _types(reader, name)
            if len(identities) != 1:
                return ordered, False
            owner = next(iter(identities))
            if '::T:' not in owner or owner in owners:
                return ordered, False
            owners.append(owner)
            dependencies.extend(declarations)
        # Additional type aliases anywhere in the clause are outside this rule.
        # C# allows lowercase type names too; capitalization is not resolution.
        for name in re.findall(_IDENTIFIER, tail):
            identities, _ = _types(reader, name)
            if identities - set(owners):
                return ordered, False
        anchors = [r for r in ordered if _PROTECTED.intersection(r.get('ranking_diagnostics', {}))]
        if any(r.get('parent') not in owners for r in anchors):
            return ordered, False
        ranks = {row['id']: index for index, row in enumerate(ordered)}
        sides = []
        for owner in owners:
            candidates = _candidates(reader, owner, wanted)
            chosen = _choose(candidates, ranks)
            if not chosen:
                return ordered, False
            sides.append(chosen)
            dependencies.extend(c['row'] for c in candidates)
            dependencies.extend(d for c in candidates for d in c['dependencies'])
        dependencies = _dependencies(dependencies)
        existing = {r['id']: r for r in ordered}
        selected = {}
        # Both sides precede optional complementary chunks, regardless of the
        # amount of lexical/dense support for either type.
        for position in range(2):
            for side, chosen in enumerate(sides):
                if position >= len(chosen):
                    continue
                c = chosen[position]
                row = dict(existing.get(c['row']['id'], c['row']))
                paths = {s['path'] for s in row['sources']}
                row['sources'] = row['sources'] + [{**s, 'role': 'reference_dependency'} for s in dependencies if s['path'] not in paths]
                row.update(match='type_comparison', score=None)
                row['ranking_diagnostics'] = {**row.get('ranking_diagnostics', {}), 'selected_passage': 'type_comparison',
                    'type_comparison': {'version': 1, 'scopes': owners, 'side': side,
                                        'matched_terms': sorted(c['covered']), 'reference_spans': c['spans'][:32],
                                        'reference_spans_omitted': max(0, len(c['spans']) - 32)}}
                selected[row['id']] = row
        result = [selected.get(r['id'], r) for r in anchors]
        seen = {r['id'] for r in result}
        result += [r for key, r in selected.items() if key not in seen]
        seen.update(selected)
        return result + [r for r in ordered if r['id'] not in seen], True
    except (_BoundExceeded, _AmbiguousScope):
        return ordered, False
