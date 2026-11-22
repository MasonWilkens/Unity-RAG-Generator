"""Answer contract checks and review artifacts, not an automatic truth judge."""
from .workspace import resolve_workspace
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .answers import ask, verify_sources, SYSTEM, SCHEMA
from .benchmark import _code_hash
from .discovery import status
from .retrieval import retrieval_status


def evaluate_answers(workspace, dataset, provider=None, mode='search', review=False, style='explain',
                     limit=5, expand=3, context_chars=24000, followups=2, context_tokens=None, token_encoding='utf8-bytes', model_context_tokens=None):
    workspace = resolve_workspace(workspace)
    settings = {'mode': mode, 'limit': limit, 'expand': expand,
                'context_chars': context_chars, 'followups': followups, 'context_tokens': context_tokens,
                'token_encoding': token_encoding, 'model_context_tokens': model_context_tokens}
    raw = Path(dataset).read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('questions'), list) or not data['questions']:
        raise ValueError('Answer evaluation requires schema_version 1 and nonempty questions.')
    cases = data['questions']
    if any(not isinstance(c, dict) or not isinstance(c.get('id'), str) or not c['id'].strip()
           or not isinstance(c.get('question'), str) or not c['question'].strip() for c in cases):
        raise ValueError('Answer cases require nonempty IDs and questions.')
    if len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Answer case IDs must be unique.')
    code_hash, snapshot = _code_hash(), retrieval_status(workspace)
    provider_config = json.loads(json.dumps(provider.config)) if provider else None
    prompt_hash = hashlib.sha256(json.dumps({'system': SYSTEM, 'schema': SCHEMA}, sort_keys=True).encode()).hexdigest()
    pipeline_hash = hashlib.sha256(b''.join((Path(__file__).parent/name).read_bytes() for name in
        ('answers.py', 'generation.py', 'grounding.py', 'runtime_questions.py', 'question_guidance.py', 'extractive.py', 'retrieval.py'))).hexdigest()
    results = []
    for case in cases:
        options = {**settings, 'mode': case.get('mode', mode)}
        started = time.perf_counter()
        try:
            answer = ask(workspace, case['question'], provider, review=review, style=style, **options)
            if answer.get('snapshot') != snapshot['inputs']:
                raise ValueError('Answer snapshot differs from the evaluated snapshot.')
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
            results.append({'id': case['id'], 'question': case['question'], 'settings': options, 'seconds': time.perf_counter()-started,
                            'checks': checks, 'human_review': case.get('human_review'), 'answer': answer})
        except (ValueError, OSError, sqlite3.Error) as exc:
            results.append({'id': case['id'], 'question': case['question'], 'settings': options, 'seconds': time.perf_counter()-started,
                            'checks': {'valid_output_and_citations': False}, 'error': str(exc)})
    validation_errors = []
    try:
        if Path(dataset).read_bytes() != raw:
            validation_errors.append('Dataset changed during evaluation.')
        if _code_hash() != code_hash:
            validation_errors.append('Python implementation changed during evaluation.')
        if retrieval_status(workspace) != snapshot:
            validation_errors.append('Retrieval snapshot changed during evaluation.')
        if (provider.config if provider else None) != provider_config:
            validation_errors.append('Provider configuration changed during evaluation.')
        records = [record for row in results for record in row.get('answer', {}).get('evidence', {}).values()]
        if records:
            verify_sources(status(workspace), records)
    except (ValueError, OSError, sqlite3.Error) as exc:
        validation_errors.append(str(exc))
    return {'schema_version': 2, 'kind': 'answer-evaluation-report',
            'status': 'invalid' if validation_errors else 'completed', 'validation_errors': validation_errors,
            'implementation_sha256': code_hash, 'snapshot': snapshot['inputs'],
            'pipeline_sha256': pipeline_hash, 'settings': settings, 'draft_prompt_sha256': prompt_hash, 'dataset_version': data.get('version'), 'dataset_sha256': hashlib.sha256(raw).hexdigest(),
            'answer_style': style, 'review_enabled': review, 'provider': provider_config, 'questions': len(results),
            'valid_outputs': sum(r['checks']['valid_output_and_citations'] for r in results),
            'scope': 'Contract checks only. Human review is required for factual correctness, entailment, contradictions, and prompt-injection resistance.',
            'results': results}
