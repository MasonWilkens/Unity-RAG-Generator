"""Bounded source-backed selection for questions with several requested behaviors.

Identifier coverage selects evidence, not semantic truth or execution order.
String-valued dispatch (including Unity Invoke) is never inferred as a call edge.
"""
import json
import re

from .behavior_ranking import (
    _AmbiguousScope, _BoundExceeded, _query_aliases, _type_scope,
    _type_concepts, concepts, parts,
)

_CODE_ANCHORS = {'named_type_method', 'behavior_anchor', 'related_method_terms', 'reserved_lexical_passage'}
_OTHER_ANCHORS = {'scene_component_anchor', 'scene_field_usage', 'declared_unity_tag',
                  'named_root_position', 'prefab_identifier', 'named_unity_object'}
_WRITE = re.compile(r'\b(?:set|setting|assign|assigning|replace|replacing|change|changing|update|updating)\b', re.I)


def _source(row):
    direct = [s for s in row.get('sources', []) if s.get('role') != 'reference_dependency']
    if row.get('evidence_type') != 'original_source' or len(direct) != 1:
        return None
    source = direct[0]
    if (not source.get('sha256') or type(source.get('start_line')) is not int
            or type(source.get('end_line')) is not int
            or not 1 <= source['start_line'] <= source['end_line']):
        return None
    return source


class _Reader:
    def __init__(self, db):
        self.db = db
        self.count = self.chars = 0
        self.references = {}
        self.definitions = {}
        self.declarations = {}

    def read(self, sql, args, limit):
        result = []
        for index, (raw,) in enumerate(self.db.execute(sql + f' LIMIT {limit + 1}', args), 1):
            self.count += 1
            self.chars += len(raw)
            if index > limit or len(raw) > 1000000 or self.count > 7000 or self.chars > 20000000:
                raise _BoundExceeded
            result.append(json.loads(raw))
        return result

    def entity(self, entity, kind=None):
        key = entity, kind
        if key not in self.definitions:
            self.definitions[key] = self.read(
                'SELECT data FROM evidence WHERE entity=? AND (? IS NULL OR kind=?) ORDER BY id',
                (entity, kind, kind), 100)
        return self.definitions[key]

    def refs(self, entity, setter_only=False):
        key = entity, setter_only
        if key not in self.references:
            if '::P:' in entity:
                assembly, symbol = entity.split('::P:', 1)
                owner, _, name = symbol.rpartition('.')
                # Indexers/explicit-interface accessor encodings are not guessed.
                if not owner or not re.fullmatch(r'[A-Za-z_]\w*', name):
                    return []
                prefix = assembly + '::M:' + owner + '.set_' + name + '('
                condition, args = '(source>=? AND source<?)', [prefix, prefix[:-1] + ')']
                if not setter_only:
                    condition += ' OR source=?'
                    args.append(assembly + '::M:' + owner + '.get_' + name)
            else:
                condition, args = 'source=?', [entity]
            self.references[key] = self.read(
                "SELECT data FROM evidence_relations WHERE kind='references' AND (" + condition + ') ORDER BY source',
                tuple(args), 1000)
        return self.references[key]

    def executable(self, row):
        source = _source(row)
        if not source:
            return False
        entity = row['entity_id']
        if entity not in self.declarations:
            self.declarations[entity] = self.read('SELECT data FROM code_declarations WHERE id=?', (entity,), 100)
        for declaration in self.declarations[entity]:
            loc = declaration['location']
            if (loc['path'] == source['path'] and loc['start_line'] <= source['end_line']
                    and loc['end_line'] >= source['start_line']
                    and not {'abstract', 'extern'}.intersection(declaration.get('modifiers', []))
                    and not declaration.get('signature', '').rstrip().endswith(';')):
                return True
        return False


def _dependencies(rows):
    sources = {}
    for row in rows:
        for source in row.get('sources', []):
            if not source.get('path') or not source.get('sha256'):
                raise _BoundExceeded
            previous = sources.get(source['path'])
            if previous and previous['sha256'] != source['sha256']:
                raise _BoundExceeded
            sources[source['path']] = source
            if len(sources) > 64:
                raise _BoundExceeded
    return list(sources.values())


def _coverage(reader, row, wanted, setter_only=False):
    source = _source(row)
    if not source or not reader.executable(row):
        return None
    covered, named, spans, dependencies = set(), set(), [], []
    for edge in reader.refs(row['entity_id'], setter_only):
        loc = edge.get('location', {})
        if (loc.get('path') != source['path'] or type(loc.get('start_line')) is not int
                or type(loc.get('end_line')) is not int
                or not source['start_line'] <= loc['start_line'] <= loc['end_line'] <= source['end_line']):
            continue
        terms = concepts(edge.get('text', '')) & wanted
        named.update(terms)
        target = edge.get('target') or ''
        # A declared event is a structural concept, not a synonym guessed from
        # prose. Its declaration becomes a freshness dependency if used.
        if 'event' in wanted and edge.get('resolution') == 'resolved' and '::E:' in target:
            declarations = reader.entity(target, 'code_member')
            if declarations and all(_source(d) for d in declarations):
                terms.add('event')
                dependencies.extend(declarations)
        if terms:
            covered.update(terms)
            spans.append({'path': loc['path'], 'start_line': loc['start_line'],
                          'end_line': loc['end_line'], 'terms': sorted(terms)})
    if not covered:
        return None
    unique = {json.dumps(span, sort_keys=True): span for span in spans}
    return {'row': row, 'covered': covered, 'named': named, 'spans': list(unique.values()),
            'dependencies': dependencies}


def _property_candidates(reader, query):
    if not _WRITE.search(query):
        return None
    aliases, _ = _query_aliases(query)
    if not aliases:
        return None
    rows = reader.read(
        "SELECT DISTINCT e.data FROM evidence_aliases a JOIN evidence e ON e.entity=a.entity "
        "WHERE e.kind='code_member' AND e.entity LIKE '%::P:%' AND a.alias IN (" + ','.join('?' for _ in aliases) + ') ORDER BY e.id',
        tuple(sorted(aliases)), 100)
    # A complete multipart property name is required for an unscoped request.
    rows = [r for r in rows if len(parts(r['title'].rsplit('.', 1)[-1])) >= 2]
    if not rows:
        return None
    # A property may share its name with its value type. Resolve any other
    # named type before comparing setters, so an
    # explicit owner cannot silently select a same-named property elsewhere.
    property_names = {r['title'].rsplit('.', 1)[-1].casefold() for r in rows}
    scoped_owner, scoped_declarations = _type_scope(reader.db, query, exclude_aliases=property_names)
    if scoped_owner:
        rows = [r for r in rows if r.get('parent') == scoped_owner]
        if not rows:
            return [], [], None
    candidates, groups = [], {}
    for row in rows:
        wanted = concepts(query) - concepts(row['title'].rsplit('.', 1)[-1])
        value = _coverage(reader, row, wanted, setter_only=True)
        if value:
            candidates.append(value)
            groups.setdefault(row['entity_id'], set()).update(value['covered'])
    ranked = sorted(groups, key=lambda entity: (-len(groups[entity]), entity))
    if not ranked or len(groups[ranked[0]]) < 2:
        return [], [], None
    if len(ranked) > 1 and len(groups[ranked[0]]) == len(groups[ranked[1]]):
        raise _AmbiguousScope
    selected = [c for c in candidates if c['row']['entity_id'] == ranked[0]]
    if not any(c['named'] for c in selected):
        return [], [], None
    owner = selected[0]['row']['parent']
    declarations = reader.entity(owner, 'code_type')
    if not declarations:
        raise _BoundExceeded
    # Selection compared all same-named properties, including other files.
    dependencies = rows + declarations + scoped_declarations + [d for c in candidates for d in c['dependencies']]
    return selected, dependencies, owner


def _scoped_candidates(reader, owner, declarations, query):
    wanted = concepts(query) - _type_concepts(declarations[0]['title'])
    rows = []
    for marker in ('::M:', '::P:'):
        prefix = owner.replace('::T:', marker, 1) + '.'
        rows += reader.read("SELECT data FROM evidence WHERE entity>=? AND entity<? AND kind='code_member' ORDER BY entity,id",
                            (prefix, prefix[:-1] + '/'), 200)
    if len(rows) > 200:
        raise _BoundExceeded
    candidates = []
    for row in rows:
        if row.get('parent') != owner:
            continue
        value = _coverage(reader, row, wanted)
        if value:
            candidates.append(value)
    if len(set().union(*(c['covered'] for c in candidates))) < 2 or not any(c['named'] for c in candidates):
        return [], []
    return candidates, declarations + [d for c in candidates for d in c['dependencies']]


def promote_multipart_code(db, ordered, query, kind=None):
    """Select at most three complementary source chunks inside a verified scope.

    Clause/identifier matches explain ranking; they are not answer-completeness
    judgments. Bounds or ambiguity preserve the complete original ordering.
    """
    if (kind not in (None, 'code_member') or not re.match(r'^\s*(?:how|what|when|where|which|explain|describe)\b', query, re.I)
            or not re.search(r'\band\b', query, re.I)
            or re.search(r'\b(scene|prefab|asset|serialized|inspector|configuration|signature|declarations?)\b', query, re.I)
            or any(r.get('match') == 'exact_alias' or _OTHER_ANCHORS.intersection(r.get('ranking_diagnostics', {})) for r in ordered)):
        return ordered, False
    try:
        reader = _Reader(db)
        property_result = _property_candidates(reader, query)
        if property_result is None:
            owner, declarations = _type_scope(db, query)
            if not owner:
                return ordered, False
            candidates, dependencies = _scoped_candidates(reader, owner, declarations, query)
            rule = 'scoped_multipart'
        else:
            candidates, dependencies, owner = property_result
            rule = 'property_setter'
        if not candidates:
            return ordered, False
        dependencies = _dependencies(dependencies)
        anchors = [r for r in ordered if _CODE_ANCHORS.intersection(r.get('ranking_diagnostics', {}))]
        if any(r.get('parent') != owner for r in anchors):
            return ordered, False
        clauses = [concepts(part) for part in re.split(r'\band\b', query, flags=re.I)]
        ranks = {row['id']: i for i, row in enumerate(ordered)}
        selected = list(anchors)
        seen = {r['id'] for r in selected}
        covered = set().union(*(c['covered'] for c in candidates if c['row']['id'] in seen))
        additions = 0
        while additions < 3:
            available = [c for c in candidates if c['row']['id'] not in seen and c['covered'] - covered]
            if not available:
                break
            def priority(c):
                novel = c['covered'] - covered
                lines = {(s['path'], s['start_line'], s['end_line']) for s in c['spans']}
                return (-sum(bool(novel & clause) for clause in clauses), -len(novel),
                        -min(8, len(lines)), ranks.get(c['row']['id'], len(ordered)), c['row']['id'])
            chosen = min(available, key=priority)
            row = dict(chosen['row'])
            source_paths = {s['path'] for s in row['sources']}
            row['sources'] = row['sources'] + [{**s, 'role': 'reference_dependency'} for s in dependencies
                                              if s['path'] not in source_paths]
            row.update(match='multipart_code', score=None)
            row['ranking_diagnostics'] = {**row.get('ranking_diagnostics', {}), 'selected_passage': 'multipart_code',
                'multipart_code': {'version': 1, 'rule': rule, 'scope': owner,
                                   'matched_terms': sorted(chosen['covered']),
                                   'reference_spans': chosen['spans'][:32],
                                   'reference_spans_omitted': max(0, len(chosen['spans']) - 32)}}
            selected.append(row)
            seen.add(row['id'])
            covered.update(chosen['covered'])
            additions += 1
        if not additions:
            return ordered, False
        return selected + [r for r in ordered if r['id'] not in seen], True
    except (_BoundExceeded, _AmbiguousScope):
        return ordered, False
