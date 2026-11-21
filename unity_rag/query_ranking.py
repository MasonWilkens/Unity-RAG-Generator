"""Bounded query-time entity anchors over existing Unity object maps."""
import json
import re


def explicit_symbol_lookup(query):
    """Recognize a complete lookup request, not identifiers inside prose.

    A missing exact name must not fall back to camel-case fragments. Unquoted
    lowercase words remain ordinary topical searches; quotes make intent exact.
    """
    request = re.fullmatch(r'\s*(?:find|locate|show)(?:\s+me)?\s+(.+?)\s*[.!?]?\s*', query, re.I)
    if not request:
        return None
    name = request.group(1)
    quoted = len(name) > 2 and name[0] in ('`', '"', "'") and name[-1] == name[0]
    if quoted:
        name = name[1:-1]
    if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\([A-Za-z0-9_., ?\[\]<>]*\))?', name):
        return None
    if not quoted and not (any(c.isupper() for c in name) or any(c in name for c in '_.(')):
        return None
    return name


# Deliberately a lookup grammar, not a bag-of-words type filter. A question
# about a GameObject's components must remain free to retrieve those components.
_LOOKUP = re.compile(
    r'^\s*(?:find|locate|show)(?:\s+me)?\s+(?:the\s+)?'
    r'(?:"(?P<quoted>[^"\r\n]{1,128})"|(?P<name>[A-Za-z_][A-Za-z0-9_]{0,127}))'
    r'\s+(?P<root>root\s+)?GameObject\s*[.!?]?\s*$', re.I)


def promote_named_gameobjects(db, ordered, query, kind=None):
    """Resolve explicit names/types; root requests also require a local root link.

    Retain every matching identity rather than asserting a unique object. Bounds,
    stripped records and unsupported root links cause abstention. Ranking does
    not establish runtime hierarchy or the truth of a generated explanation.
    """
    request = _LOOKUP.fullmatch(query)
    if not request or kind not in (None, 'asset_object'):
        return ordered, False
    name = request.group('quoted') or request.group('name')
    roots_only = bool(request.group('root'))
    objects = db.execute(
        "SELECT DISTINCT u.id,u.data FROM evidence_aliases a "
        "JOIN unity_objects u ON u.id=a.entity WHERE a.alias=? ORDER BY u.id LIMIT 101",
        (name.casefold(),)).fetchall()
    if len(objects) > 100:
        return ordered, False
    anchors = []
    inspected = 0
    for entity, raw in objects:
        if len(raw) > 1000000:
            return ordered, False
        obj = json.loads(raw)
        if (obj.get('type') != 'GameObject' or obj.get('stripped')
                or str(obj.get('data', {}).get('m_Name', '')).casefold() != name.casefold()):
            continue
        transform = None
        if roots_only:
            if not _standalone(obj):
                continue
            components = obj.get('data', {}).get('m_Component')
            if not isinstance(components, list) or not 1 <= len(components) <= 128:
                continue
            transforms, complete = [], True
            for item in components:
                ref = item.get('component') if isinstance(item, dict) else None
                if not _local_reference(ref) or ref['fileID'] == '0':
                    complete = False
                    break
                inspected += 1
                if inspected > 256:
                    return ordered, False
                record = db.execute('SELECT data FROM unity_objects WHERE id=?',
                                    (obj['asset'] + '#' + ref['fileID'],)).fetchone()
                if not record or len(record[0]) > 1000000:
                    complete = False
                    break
                component = json.loads(record[0])
                if component.get('type') in ('Transform', 'RectTransform'):
                    transforms.append(component)
            if not complete or len(transforms) != 1:
                continue
            transform = transforms[0]
            data = transform.get('data', {})
            go, father = data.get('m_GameObject'), data.get('m_Father')
            if (transform.get('stripped') or not _standalone(transform) or not _local_reference(go)
                    or go['fileID'] != obj['file_id'] or not _local_reference(father)
                    or father['fileID'] != '0'):
                continue
        chunks = db.execute("SELECT data FROM evidence WHERE entity=? AND kind='asset_object' ORDER BY id LIMIT 101",
                            (entity,)).fetchall()
        if len(chunks) > 100 or any(len(chunk[0]) > 1000000 for chunk in chunks):
            return ordered, False
        # Use the chunk that actually states the object's type/name, rather than
        # inheriting the best embedding passage from a different portion of it.
        chunks = [json.loads(chunk[0]) for chunk in chunks]
        chunk = next((r for r in chunks if r.get('text', '').startswith('Object: GameObject;')
                      and re.search(r'^m_Name: ' + re.escape(str(obj['data']['m_Name'])) + r'$',
                                    r['text'], re.M)), None)
        if not chunk:
            continue
        existing = next((r for r in ordered if r['id'] == chunk['id']), None)
        row = {**chunk, 'match': 'named_unity_object', 'score': None}
        diagnostics = dict((existing or {}).get('ranking_diagnostics', {}))
        if existing:
            row.update(match=existing['match'], score=existing['score'])
        diagnostics['selected_passage'] = 'named_object'
        diagnostics['named_unity_object'] = {'name': name, 'type': 'GameObject',
                                            'serialized_root_verified': roots_only}
        if transform:
            location = transform.get('location', {})
            # Only a same-asset link is supported; use the indexed file hash
            # already carried by the GameObject citation for freshness checks.
            source = next((s for s in row['sources'] if s.get('path') == location.get('path')), None)
            if not source or not source.get('sha256'):
                continue
            row['sources'] = row['sources'] + [{**source,
                'start_line': location['start_line'], 'end_line': location['end_line'],
                'role': 'reference_dependency'}]
            diagnostics['named_unity_object']['root_transform'] = transform['id']
        row['ranking_diagnostics'] = diagnostics
        anchors.append(row)
    if not anchors:
        return ordered, False
    # Exact aliases are always authoritative, including explicit signatures.
    exact = [r for r in ordered if r['match'] == 'exact_alias']
    exact_ids = {r['entity_id'] for r in exact}
    anchors = [r for r in anchors if r['entity_id'] not in exact_ids]
    taken = exact_ids | {r['entity_id'] for r in anchors}
    return exact + anchors + [r for r in ordered if r['entity_id'] not in taken], bool(anchors)


def _local_reference(ref):
    return (isinstance(ref, dict) and isinstance(ref.get('fileID'), str)
            and re.fullmatch(r'-?\d+', ref['fileID']) is not None
            and ref.get('guid') in (None, '', '0' * 32))


def _standalone(obj):
    # Prefab-instance inheritance/reparenting requires a different evidence chain.
    data = obj.get('data', {})
    return all(key not in data or (_local_reference(data[key]) and data[key]['fileID'] == '0')
               for key in ('m_PrefabInstance', 'm_CorrespondingSourceObject'))
