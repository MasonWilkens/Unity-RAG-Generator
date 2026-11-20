"""Answer contract checks and review artifacts, not an automatic truth judge."""
from .workspace import resolve_workspace
import hashlib
import json
from pathlib import Path
import time

from .answers import ask, SYSTEM, SCHEMA


def evaluate_answers(workspace, dataset, provider=None, mode='search', review=False, style='explain',
                     limit=5, expand=3, context_chars=24000, followups=2, context_tokens=None, token_encoding='utf8-bytes', model_context_tokens=None):
    workspace = resolve_workspace(workspace)
    settings = {'mode': mode, 'limit': limit, 'expand': expand,
                'context_chars': context_chars, 'followups': followups, 'context_tokens': context_tokens,
                'token_encoding': token_encoding, 'model_context_tokens': model_context_tokens}
    raw = Path(dataset).read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    if data.get('schema_version') != 1 or not data.get('questions'):
        raise ValueError('Answer evaluation requires schema_version 1 and nonempty questions.')
    results = []
    for case in data['questions']:
        options = {**settings, 'mode': case.get('mode', mode)}
        started = time.perf_counter()
        try:
            answer = ask(workspace, case['question'], provider, review=review, style=style, **options)
            cited = {answer['evidence'][i]['entity_id'] for c in answer['claims'] for i in c['evidence_ids']}
            cited.update(answer['evidence'][e['evidence_id']]['entity_id'] for e in answer.get('excerpts', []))
            expected = set(case.get('expected_entities', []))
            retrieved = {r['entity_id'] for r in answer['evidence'].values()}
            checks = {'valid_output_and_citations': True,
                'retrieved_expected_entity_coverage': len(expected & retrieved)/len(expected) if expected else None,
                'evidence_records': len(answer['evidence']),
                'evidence_chars': answer['context_budget']['used_chars'],
                'evidence_tokens': answer['context_budget'].get('used_tokens'),
                'truncated_evidence_records': answer['context_budget'].get('truncated_records', 0),
                'omitted_evidence_records': answer['context_budget'].get('omitted_records', 0),
                'retained_claims': len(answer['claims']),
                'source_excerpts': len(answer.get('excerpts', [])),
                'removed_claims': answer['grounding_review']['removed_claims'],
                'model_calls': answer['model_calls'],
                'expected_entity_coverage': len(expected & cited)/len(expected) if expected else None,
                'expected_status': answer['status'] == case['expected_status'] if 'expected_status' in case else None,
                'expected_runtime_flag': answer['runtime_required'] == case['runtime_required'] if 'runtime_required' in case else None}
            results.append({'id': case['id'], 'settings': options, 'seconds': time.perf_counter()-started,
                            'checks': checks, 'human_review': case.get('human_review'), 'answer': answer})
        except (ValueError, OSError) as exc:
            results.append({'id': case['id'], 'settings': options, 'seconds': time.perf_counter()-started,
                            'checks': {'valid_output_and_citations': False}, 'error': str(exc)})
    prompt_hash = hashlib.sha256(json.dumps({'system': SYSTEM, 'schema': SCHEMA}, sort_keys=True).encode()).hexdigest()
    pipeline_hash = hashlib.sha256(b''.join((Path(__file__).parent/name).read_bytes() for name in
        ('answers.py', 'generation.py', 'grounding.py', 'runtime_questions.py', 'question_guidance.py', 'extractive.py', 'retrieval.py'))).hexdigest()
    return {'pipeline_sha256': pipeline_hash, 'settings': settings, 'draft_prompt_sha256': prompt_hash, 'dataset_version': data.get('version'), 'dataset_sha256': hashlib.sha256(raw).hexdigest(),
            'answer_style': style, 'review_enabled': review, 'provider': provider.config if provider else None, 'questions': len(results),
            'valid_outputs': sum(r['checks']['valid_output_and_citations'] for r in results),
            'scope': 'Contract checks only. Human review is required for factual correctness, entailment, contradictions, and prompt-injection resistance.',
            'results': results}
