"""Attach literal YAML to serialized-value queries through mapped object IDs."""
import json
import re


def literal_asset_companions(db, ordered, query):
    """Keep parsed facts while offering one bounded source passage per object.

    No source is read at query time. Older indexes simply have no companions.
    Only serialization intent is recognized; ordinary code ranking stays intact.
    """
    if not re.search(r'\b(?:serialized|serialization|saved|stored|yaml)\b', query, re.I):
        return ordered
    from .retrieval import terms
    result, seen, inspected = [], set(), set()
    for item in ordered:
        if item['id'] in seen:
            continue
        # Existing verified anchors keep their first slot and all provenance.
        # Their literal companion follows rather than replacing the anchor.
        if item.get('ranking_diagnostics'):
            result.append(item)
            seen.add(item['id'])
        if item['kind'] == 'asset_object' and item['entity_id'] not in inspected and len(inspected) < 20:
            inspected.add(item['entity_id'])
            rows = db.execute("SELECT data FROM evidence WHERE entity=? AND kind='asset_source' ORDER BY id LIMIT 101",
                              (item['entity_id'],)).fetchall()
            if len(rows) <= 100:
                wanted = set(terms(query)) - set(terms(item['title']))
                candidates = [json.loads(raw) for raw, in rows]
                candidates.sort(key=lambda row: (-len(wanted & set(terms(row['text']))),
                                                 row['sources'][0]['start_line'], row['id']))
                if candidates:
                    source = candidates[0]
                    if source['id'] not in seen:
                        result.append({**source, 'match': 'literal_asset_companion', 'score': None,
                                       'sources': source['sources'] + [s for s in item['sources']
                                                   if s.get('role') == 'reference_dependency'],
                                       'ranking_diagnostics': {**item.get('ranking_diagnostics', {}),
                                                               'literal_asset_companion': item['id']}})
                        seen.add(source['id'])
        if item['id'] not in seen:
            result.append(item)
            seen.add(item['id'])
    return result
