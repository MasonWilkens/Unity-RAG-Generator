"""Literal evidence for two resolved calls; never an execution or data-flow trace."""
import re

from .behavior_ranking import _AmbiguousScope, _BoundExceeded, _type_scope, _type_concepts, concepts
from .cross_file_ranking import _inside
from .multipart_ranking import _Reader, _source, _coverage, _dependencies, _CODE_ANCHORS, _OTHER_ANCHORS

_PROTECTED = _CODE_ANCHORS | {'multipart_code', 'cross_file_call'}
_DYNAMIC = {'virtual', 'override', 'abstract', 'extern'}


class _Graph:
    """Bound work independently of the result count and source chunk size."""
    def __init__(self, db):
        self.reader = _Reader(db)
        self.nodes, self.methods, self.calls, self.types = set(), {}, {}, {}
        self.inspected = self.paths = 0
        self.dependencies = []

    def visit(self, entity):
        self.nodes.add(entity)
        if len(self.nodes) > 64:
            raise _BoundExceeded

    def members(self, entity):
        self.visit(entity)
        if entity not in self.methods:
            rows = self.reader.entity(entity, 'code_member')
            executable = [r for r in rows if self.reader.executable(r)]
            if any(_DYNAMIC.intersection(d.get('modifiers', [])) for d in self.reader.declarations.get(entity, [])):
                executable = []
            self.methods[entity] = []
            for row in executable:
                parent = row.get('parent') or ''
                if parent not in self.types:
                    declarations = self.reader.entity(parent, 'code_type') if '::T:' in parent else []
                    definitions = self.reader.read('SELECT data FROM code_declarations WHERE id=?', (parent,), 100) if declarations else []
                    # Default interface methods have bodies without an explicit
                    # virtual modifier, but still do not establish runtime dispatch.
                    self.types[parent] = declarations if definitions and all(
                        d.get('kind') != 'InterfaceDeclaration' for d in definitions) else []
                declarations = self.types[parent]
                if declarations:
                    self.methods[entity].append(row)
                    self.dependencies.extend(declarations)
            self.dependencies.extend(rows)
        return self.methods[entity]

    def edges(self, row):
        entity, source = row['entity_id'], _source(row)
        self.visit(entity)
        if entity not in self.calls:
            self.calls[entity] = self.reader.read(
                "SELECT data FROM evidence_relations WHERE source=? AND kind='calls' ORDER BY target,data", (entity,), 200)
        for edge in self.calls[entity]:
            self.inspected += 1
            if self.inspected > 1000:
                raise _BoundExceeded
            if (source and edge.get('source') == entity and edge.get('resolution') == 'resolved'
                    and '::M:' in (edge.get('target') or '') and _inside(edge.get('location', {}), source)):
                yield edge


def _paths(graph, owner, wanted):
    prefix = owner.replace('::T:', '::M:', 1) + '.'
    roots = graph.reader.read("SELECT data FROM evidence WHERE entity>=? AND entity<? AND kind='code_member' ORDER BY entity,id",
                              (prefix, prefix[:-1] + '/'), 200)
    graph.dependencies.extend(roots)
    for row in roots:
        if row.get('parent') != owner:
            continue
        caller = _coverage(graph.reader, row, wanted)
        if not caller:
            continue
        for first in graph.edges(row):
            if first['target'] == row['entity_id']:
                continue
            for middle in graph.members(first['target']):
                bridge = _coverage(graph.reader, middle, wanted) or {
                    'row': middle, 'covered': set(), 'spans': [], 'dependencies': []}
                covered = caller['covered'] | bridge['covered']
                for second in graph.edges(middle):
                    if second['target'] in (row['entity_id'], middle['entity_id']):
                        continue
                    graph.paths += 1
                    if graph.paths > 128:
                        raise _BoundExceeded
                    leaves = []
                    for leaf in graph.members(second['target']):
                        coverage = _coverage(graph.reader, leaf, wanted)
                        if coverage and coverage['covered'] - covered:
                            leaves.append(coverage)
                    if leaves:
                        values = [caller, bridge, *leaves]
                        graph.dependencies.extend(d for c in values for d in c['dependencies'])
                        yield {'caller': caller, 'bridge': bridge, 'leaves': leaves, 'edges': [first, second],
                               'chain': (row['entity_id'], middle['entity_id'], second['target']),
                               'covered': covered | set().union(*(c['covered'] for c in leaves))}


def promote_dependency_chain(db, ordered, query, kind=None):
    """Select caller, bridge and at most two complementary endpoint chunks.

    Traversal stops after two resolved calls. A unique best identity path and a
    novel endpoint concept are required. Bounds/ambiguity preserve ordering.
    Existing anchors and final context/result budgets remain authoritative.
    """
    if (kind not in (None, 'code_member') or len(query) > 4096
            or not re.match(r'^\s*(?:how|what|when|explain|describe)\b', query, re.I)
            or not re.search(r'\band\b', query, re.I)
            or re.search(r'\b(scene|prefab|asset|serialized|inspector|configuration|signature|declarations?)\b', query, re.I)
            or any(r.get('match') == 'exact_alias' or (_OTHER_ANCHORS | {'type_comparison'}).intersection(
                r.get('ranking_diagnostics', {})) for r in ordered)):
        return ordered, False
    try:
        owner, declarations = _type_scope(db, query)
        if not owner:
            return ordered, False
        wanted = concepts(query) - _type_concepts(declarations[0]['title'])
        graph = _Graph(db)
        graph.dependencies.extend(declarations)
        anchors = [r for r in ordered if _PROTECTED.intersection(r.get('ranking_diagnostics', {}))]
        ranks = {r['id']: i for i, r in enumerate(ordered)}
        def priority(path):
            rows = [path['caller']['row'], path['bridge']['row']]
            return (-len(path['covered']), *(ranks.get(r['id'], len(ranks)) for r in rows),
                    *(r['id'] for r in rows))
        paths = {}
        for path in _paths(graph, owner, wanted):
            if any(r.get('entity_id') not in path['chain'] for r in anchors):
                continue
            previous = paths.get(path['chain'])
            if previous is None or priority(path) < priority(previous):
                paths[path['chain']] = path
        ordered_paths = sorted(paths.values(), key=priority)
        if not ordered_paths or (len(ordered_paths) > 1 and
                                 len(ordered_paths[0]['covered']) == len(ordered_paths[1]['covered'])):
            return ordered, False
        path = ordered_paths[0]
        chosen = [path['caller'], path['bridge']]
        covered = set().union(*(c['covered'] for c in chosen))
        for _ in range(2):
            available = [c for c in path['leaves'] if c['covered'] - covered]
            if not available:
                break
            value = min(available, key=lambda c: (-len(c['covered'] - covered),
                        ranks.get(c['row']['id'], len(ranks)), c['row']['id']))
            chosen.append(value)
            covered.update(value['covered'])
        dependencies = _dependencies(graph.dependencies)
        edges = [{'source': e['source'], 'target': e['target'], 'call_site': e['location']} for e in path['edges']]
        existing, selected = {r['id']: r for r in ordered}, {}
        for hop, c in enumerate(chosen):
            row = dict(existing.get(c['row']['id'], c['row']))
            present = {s['path'] for s in row['sources']}
            row['sources'] = row['sources'] + [{**s, 'role': 'reference_dependency'} for s in dependencies if s['path'] not in present]
            # Keep the literal outgoing call before optional identifier spans.
            spans = ([{**path['edges'][hop]['location'], 'terms': []}] if hop < 2 else []) + list(c['spans'])
            row.update(match='dependency_chain', score=None)
            row['ranking_diagnostics'] = {**row.get('ranking_diagnostics', {}), 'selected_passage': 'dependency_chain',
                'dependency_chain': {'version': 1, 'depth': 2, 'chain': list(path['chain']),
                                     'resolution': 'resolved_static_path', 'edges': edges,
                                     'role': ('caller' if hop == 0 else 'bridge' if hop == 1 else 'endpoint'),
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
