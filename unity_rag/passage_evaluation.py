"""Evaluate decisive source passages before and after real answer-context packing."""
import hashlib
import json
from pathlib import Path
import time

from .answers import ask
from .discovery import status
from .retrieval import retrieve, retrieval_status
from .source_checks import inspect_source
from .source_evaluation import _passage_checks
from .workspace import resolve_workspace


def passage_labels(case):
    """Validate the shape shared by the passage and multi-project evaluators."""
    if not isinstance(case, dict):
        raise ValueError('Each passage case must be an object.')
    question = case.get('question')
    groups = case.get('passage_groups', [])
    negatives = case.get('negative_passages', [])
    outcome = case.get('expected_outcome', 'evidence')
    if (not isinstance(question, str) or not question.strip() or outcome not in ('evidence', 'insufficient_evidence')
            or not isinstance(groups, list) or not isinstance(negatives, list)
            or any(not isinstance(g, list) or not g for g in groups)
            or (outcome == 'evidence' and not groups) or (outcome == 'insufficient_evidence' and groups)):
        raise ValueError('Each case needs a question and passage groups, or an explicit insufficient-evidence outcome.')
    labels = [p for group in groups for p in group] + negatives
    if any(not isinstance(p, dict) for p in labels):
        raise ValueError('Passage labels must be objects.')
    if any(not isinstance(p.get('path'), str) or not p['path'] for p in labels):
        raise ValueError('Every passage needs a nonempty source path.')
    label_case = {**case, 'expected_sources': sorted({p['path'] for p in labels}),
                  'required_passages': labels}
    return label_case, groups, negatives, outcome


def validate_passage_case(case, inventory):
    label_case, groups, negatives, outcome = passage_labels(case)
    checks = _passage_checks(label_case, inventory, {'results': [], 'context': [], 'source_freshness': []})
    if any(c['review_status'] != 'current' for c in checks):
        raise ValueError('Passage labels are stale or invalid: ' + case['id'])
    return checks


def evaluate_passage_case(workspace, case, inventory, *, mode='search', limit=5, expand=0,
                          context_chars=24000, context_tokens=None, token_encoding='utf8-bytes', provider=None):
    """Run one real retrieval/packing case; callers own dataset and snapshot guards."""
    label_case, groups, negatives, outcome = passage_labels(case)
    labels = label_case['required_passages']
    question = case['question']
    entries = {r['path']: r for r in inventory['entries']}
    started = time.perf_counter()
    result = retrieve(workspace, question, mode=mode, limit=limit, expand=expand,
                      context_chars=1000000, provider=provider)
    retrieval_seconds = time.perf_counter() - started
    started = time.perf_counter()
    answer = ask(workspace, question, mode=mode, limit=limit, expand=expand, style='extractive',
                 followups=0, embedding_provider=provider, context_chars=context_chars,
                 context_tokens=context_tokens, token_encoding=token_encoding)
    packed = list(answer['evidence'].values())
    answer_result = {'results': packed, 'context': [], 'source_freshness': [
        inspect_source(inventory['project'], entries, source)
        for record in packed for source in record['sources']]}
    stages = {}
    for name, evidence in (('retrieved', result), ('packed', answer_result)):
        checks = _passage_checks(label_case, inventory, evidence) if labels else []
        if any(c['review_status'] != 'current' for c in checks):
            raise ValueError('Passage labels are stale or invalid: ' + case['id'])
        offset, hits = 0, []
        for group in groups:
            hits.append(any(c['matched_current'] for c in checks[offset:offset + len(group)]))
            offset += len(group)
        negative_hits = sum(c['matched_current'] for c in checks[offset:])
        records = evidence['results'] + evidence['context']
        expected_met = all(hits) if outcome == 'evidence' else not records
        stages[name] = {'groups_found': sum(hits), 'groups': len(groups), 'group_hits': hits,
                        'passage_recall': sum(hits) / len(hits) if hits else None,
                        'negative_passages_found': negative_hits,
                        'expected_outcome_met': expected_met,
                        'passed': expected_met and negative_hits == 0,
                        'checks': checks}
    return {'id': case['id'], 'category': case.get('category', 'unspecified'),
            'expected_outcome': outcome, 'answer_status': answer.get('status'),
            'retrieval_seconds': retrieval_seconds, 'answer_seconds': time.perf_counter() - started,
            'candidate_count': result['candidate_count'], 'semantic_profile': result.get('semantic_profile'),
            'retrieved': stages['retrieved'], 'packed': stages['packed'],
            'context_budget': answer['context_budget']}


def evaluate_passages(workspace, dataset, *, mode='search', limit=5, expand=0,
                      context_chars=24000, context_tokens=None, token_encoding='utf8-bytes', provider=None):
    """Version 2 labels permit alternatives, negative passages and empty outcomes.

    This exercises the actual extractive answer path without a model judge.
    Passing establishes passage delivery, not that an explanation is correct.
    """
    workspace = resolve_workspace(workspace)
    path = Path(dataset)
    raw = path.read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    cases = data.get('cases')
    if data.get('schema_version') != 2 or not isinstance(cases, list) or not cases:
        raise ValueError('Passage evaluation requires schema_version 2 and nonempty cases.')
    ids = [c.get('id') for c in cases]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise ValueError('Passage case IDs must be unique nonempty strings.')
    inventory, snapshot = status(workspace), retrieval_status(workspace)
    rows = [evaluate_passage_case(workspace, case, inventory, mode=mode, limit=limit, expand=expand,
                                  context_chars=context_chars, context_tokens=context_tokens,
                                  token_encoding=token_encoding, provider=provider) for case in cases]
    if path.read_bytes() != raw or retrieval_status(workspace) != snapshot:
        raise ValueError('Evaluation dataset or snapshot changed during the run.')
    return {'schema_version': 1, 'dataset_sha256': hashlib.sha256(raw).hexdigest(), 'split': data.get('split'),
            'snapshot': snapshot['inputs'], 'generation': str(workspace),
            'settings': {'mode': mode, 'limit': limit, 'expand': expand, 'context_chars': context_chars,
                         'retrieval_context_chars': 1000000,
                         'context_tokens': context_tokens, 'token_encoding': token_encoding},
            'cases': rows, 'summary': {stage: {
                'passed': sum(r[stage]['passed'] for r in rows), 'cases': len(rows),
                'groups_found': sum(r[stage]['groups_found'] for r in rows),
                'groups': sum(r[stage]['groups'] for r in rows),
                'negative_passages_found': sum(r[stage]['negative_passages_found'] for r in rows)}
                for stage in ('retrieved', 'packed')},
            'scope': 'Hash-checked passage delivery and labeled negatives; not generated-answer factuality or independent review.'}
