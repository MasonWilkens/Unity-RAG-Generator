"""Entity-diverse rank fusion; evidence records retain their source citations."""
import math


def unique_entities(rows):
    seen = set()
    result = []
    for row in rows:
        if row['entity_id'] not in seen:
            result.append(row)
            seen.add(row['entity_id'])
    return result


def reserve_source_matches(keyword, fused):
    """Reserve up to two lexical entities, including one code/asset passage."""
    candidates = unique_entities(keyword)
    if not candidates:
        return fused
    chosen = [candidates[0]]
    source_candidates = [r for r in candidates if r['entity_id'] != chosen[0]['entity_id']
                         and r.get('kind') in ('code_member', 'asset_object')]
    def substantial(row):
        if row.get('kind') == 'asset_object':
            return True
        return any(type(s.get('start_line')) is int and type(s.get('end_line')) is int
                   and s['end_line'] - s['start_line'] >= 3 for s in row.get('sources', []))
    source = next((r for r in source_candidates if substantial(r)), None)
    if source is None:
        source = next(iter(source_candidates), None)
    if source:
        chosen.append(source)
    indexed = {r['entity_id']: r for r in fused}
    promoted = []
    for row in chosen:
        existing = indexed.get(row['entity_id'])
        if existing is None:
            continue
        value = dict(row)
        value['score'] = existing.get('score')
        value['ranking_diagnostics'] = {**existing.get('ranking_diagnostics', {}),
                                        'cosine': None,
                                        'reserved_lexical_passage': True,
                                        'selected_passage': 'keyword',
                                        'keyword_passage_id': row['id']}
        promoted.append(value)
    exact = [r for r in fused if r['match'] == 'exact_alias']
    exact_ids = {r['entity_id'] for r in exact}
    promoted = [r for r in promoted if r['entity_id'] not in exact_ids]
    taken = exact_ids | {r['entity_id'] for r in promoted}
    return exact + promoted + [r for r in fused if r['entity_id'] not in taken]


def fuse(keyword, dense, scores, mode, limit, semantic_weight=0.8):
    if not math.isfinite(semantic_weight) or not 0 <= semantic_weight <= 1:
        raise ValueError('Semantic weight must be finite and between 0 and 1.')
    keyword = unique_entities(keyword)[:limit]
    dense = unique_entities(dense)[:limit]
    keyword_by_entity = {r['entity_id']: r for r in keyword}
    kr = {r['entity_id']: i+1 for i,r in enumerate(keyword)}
    dr = {r['entity_id']: i+1 for i,r in enumerate(dense)}
    dense_by_entity = {r['entity_id']: r for r in dense}
    # A disabled channel contributes no candidates or representative passages.
    # Exact aliases remain authoritative even at semantic_weight=1.
    merged = {r['entity_id']: dict(r) for r in dense
              if mode == 'semantic' or semantic_weight > 0}
    passage_channel = {entity: 'semantic' for entity in merged}
    if mode == 'hybrid':
        for row in keyword:
            entity = row['entity_id']
            if semantic_weight == 1 and row['match'] != 'exact_alias':
                continue
            lexical_vote = (1 - semantic_weight) / (60 + kr[entity])
            dense_vote = semantic_weight / (60 + dr[entity]) if entity in dr else 0
            if (entity not in merged or row['match'] == 'exact_alias'
                    or lexical_vote >= dense_vote):
                merged[entity] = dict(row)
                passage_channel[entity] = 'exact' if row['match'] == 'exact_alias' else 'keyword'
    for entity, row in merged.items():
        k, d = kr.get(entity), dr.get(entity)
        fusion = ((1-semantic_weight)/(60+k) if k else 0) + (semantic_weight/(60+d) if d else 0)
        lexical = keyword_by_entity.get(entity, {})
        row['ranking_diagnostics'] = {
            **lexical.get('ranking_diagnostics', {}), **row.get('ranking_diagnostics', {}),
            'keyword_rank': k, 'semantic_rank': d,
            'cosine': scores.get(row['id']), 'rrf': fusion if mode == 'hybrid' else None,
            'semantic_weight': semantic_weight,
            'keyword_contribution': ((1-semantic_weight)/(60+k) if k else 0) if mode == 'hybrid' else None,
            'semantic_contribution': (semantic_weight/(60+d) if d else 0) if mode == 'hybrid' else None,
            'selected_passage': passage_channel[entity],
            'keyword_passage_id': keyword_by_entity.get(entity, {}).get('id'),
            'semantic_passage_id': dense_by_entity.get(entity, {}).get('id'),
            'ranking_version': 3, 'unit': 'entity'}
        row['score'] = scores[row['id']] if mode == 'semantic' else fusion
    return sorted(merged.values(), key=lambda r: (
        r['match'] != 'exact_alias' if mode == 'hybrid' else False,
        -r['score'], r['id']))
