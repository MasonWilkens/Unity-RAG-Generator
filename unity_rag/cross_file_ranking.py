"""Bounded caller/callee evidence selection from resolved static call edges."""
import re

from .behavior_ranking import _AmbiguousScope, _BoundExceeded, _type_scope, _type_concepts, concepts
from .multipart_ranking import _Reader, _source, _coverage, _dependencies, _CODE_ANCHORS, _OTHER_ANCHORS


def _inside(location, source):
    return (location.get('path') == source['path']
            and type(location.get('start_line')) is int and type(location.get('end_line')) is int
            and source['start_line'] <= location['start_line'] <= location['end_line'] <= source['end_line'])


def promote_cross_file_call(db, ordered, query, kind=None):
    """Offer a literal caller and up to two complementary target chunks.

    Selection describes a static call target, never runtime dispatch or order.
    A unique best caller/target identity pair is required before changing rank.
    """
    protected = _CODE_ANCHORS | {'multipart_code'}
    if (kind not in (None, 'code_member')
            or not re.match(r'^\s*(?:how|what|when|explain|describe)\b', query, re.I)
            or not re.search(r'\band\b', query, re.I)
            or re.search(r'\b(scene|prefab|asset|serialized|inspector|configuration|signature|declarations?)\b', query, re.I)
            or any(r.get('match') == 'exact_alias' or _OTHER_ANCHORS.intersection(r.get('ranking_diagnostics', {})) for r in ordered)):
        return ordered, False
    try:
        reader = _Reader(db)
        owner, declarations = _type_scope(db, query)
        if not owner:
            return ordered, False
        anchors = [r for r in ordered if protected.intersection(r.get('ranking_diagnostics', {}))]
        if any(r.get('parent') != owner for r in anchors):
            return ordered, False
        wanted = concepts(query) - _type_concepts(declarations[0]['title'])
        prefix = owner.replace('::T:', '::M:', 1) + '.'
        callers = reader.read("SELECT data FROM evidence WHERE entity>=? AND entity<? AND kind='code_member' ORDER BY entity,id",
                              (prefix, prefix[:-1] + '/'), 200)
        calls, pairs, dependencies = {}, {}, list(declarations)
        inspected_calls = 0
        for row in callers:
            if row.get('parent') != owner:
                continue
            caller = _coverage(reader, row, wanted)
            if not caller:
                continue
            entity, source = row['entity_id'], _source(row)
            if entity not in calls:
                calls[entity] = reader.read("SELECT data FROM evidence_relations WHERE source=? AND kind='calls' ORDER BY target",
                                            (entity,), 200)
            for edge in calls[entity]:
                inspected_calls += 1
                if inspected_calls > 1000:
                    raise _BoundExceeded
                target = edge.get('target') or ''
                if (edge.get('resolution') != 'resolved' or '::M:' not in target
                        or not _inside(edge.get('location', {}), source)):
                    continue
                targets = reader.entity(target, 'code_member')
                dependencies.extend(targets)
                for target_row in targets:
                    target_source = _source(target_row)
                    if (target_row.get('parent') == owner or not target_source
                            or target_source['path'] == source['path']):
                        continue
                    coverage = _coverage(reader, target_row, wanted)
                    if not coverage or not coverage['covered'] - caller['covered']:
                        continue
                    # The mapped target of a virtual/interface call is not a
                    # proven runtime implementation. Do not select it as one.
                    if any({'virtual', 'override', 'abstract', 'extern'}.intersection(d.get('modifiers', []))
                           for d in reader.declarations.get(target, [])):
                        continue
                    target_declarations = reader.entity(target_row.get('parent'), 'code_type')
                    if not target_declarations:
                        continue
                    dependencies.extend(target_declarations + caller['dependencies'] + coverage['dependencies'])
                    key = entity, target
                    pair = pairs.setdefault(key, {'callers': {}, 'targets': {}, 'edges': {}, 'covered': set()})
                    pair['callers'][row['id']] = caller
                    pair['targets'][target_row['id']] = coverage
                    pair['edges'][row['id']] = edge
                    pair['covered'].update(caller['covered'] | coverage['covered'])
        if not pairs:
            return ordered, False
        ranked = sorted(pairs, key=lambda key: (-len(pairs[key]['covered']), key))
        if len(ranked) > 1 and len(pairs[ranked[0]]['covered']) == len(pairs[ranked[1]]['covered']):
            return ordered, False
        pair = pairs[ranked[0]]
        ranks = {row['id']: index for index, row in enumerate(ordered)}
        priority = lambda c: (-len(c['covered']), ranks.get(c['row']['id'], len(ordered)), c['row']['id'])
        caller = min(pair['callers'].values(), key=priority)
        chosen, covered = [caller], set(caller['covered'])
        for _ in range(2):
            candidates = [c for c in pair['targets'].values() if c['covered'] - covered]
            if not candidates:
                break
            target = min(candidates, key=lambda c: (-len(c['covered'] - covered), *priority(c)))
            chosen.append(target)
            covered.update(target['covered'])
        if len(chosen) == 1:
            return ordered, False
        dependencies = _dependencies(dependencies + [c['row'] for c in chosen])
        edge = pair['edges'][caller['row']['id']]
        existing = {r['id']: r for r in ordered}
        selected = {}
        for c in chosen:
            row = dict(existing.get(c['row']['id'], c['row']))
            source_paths = {s['path'] for s in row['sources']}
            row['sources'] = row['sources'] + [{**s, 'role': 'reference_dependency'} for s in dependencies
                                              if s['path'] not in source_paths]
            spans = list(c['spans'])
            if c is caller:
                spans.append({**edge['location'], 'terms': []})
            row.update(match='cross_file_call', score=None)
            row['ranking_diagnostics'] = {**row.get('ranking_diagnostics', {}), 'selected_passage': 'cross_file_call',
                'cross_file_call': {'version': 1, 'source': edge['source'], 'target': edge['target'],
                                    'resolution': 'resolved_static_target', 'call_site': edge['location'],
                                    'matched_terms': sorted(c['covered']), 'reference_spans': spans[:32],
                                    'reference_spans_omitted': max(0, len(spans) - 32)}}
            selected[row['id']] = row
        result = [selected.get(r['id'], r) for r in anchors]
        seen = {r['id'] for r in result}
        result += [r for key, r in selected.items() if key not in seen]
        seen.update(selected)
        return result + [r for r in ordered if r['id'] not in seen], True
    except (_BoundExceeded, _AmbiguousScope):
        return ordered, False
