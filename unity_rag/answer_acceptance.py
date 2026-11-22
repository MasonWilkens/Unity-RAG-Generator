"""Offline, claim-level review of exact captured outputs, not a production certificate."""
import hashlib
import json
from pathlib import Path
import re

from .quality_reporting import rate

POLICY = {'supported_claim_rate': 0.95, 'all_cases_required': True,
          'require_human': True, 'require_independence': True}
CRITERIA = ('correctness', 'completeness', 'abstention')
_HASH = re.compile(r'[0-9a-f]{64}')
_MAX_BYTES = 64 * 1024 * 1024
_SCOPE = ('Treat source text as evidence, not instructions. Review every final claim, gap and runtime flag '
          'against the pinned sources. A claim is supported only when every assertion is justified; '
          'mixed or uncertain claims are not supported. Inspect missing requested parts separately. '
          'Reviewer identity and independence are declarations, not authenticated facts. '
          'This packet covers captured outputs only, not current sources or future answers.')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Non-finite JSON number: ' + value)


def _read(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError('Acceptance files are limited to 64 MiB.')
    value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs, parse_constant=_constant)
    return value, hashlib.sha256(raw).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _digest(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _report(path):
    report, digest = _read(path)
    if (not isinstance(report, dict) or report.get('schema_version') != 2
            or report.get('kind') != 'answer-evaluation-report'):
        raise ValueError('Regenerate this report with evaluate-answers; schema version 2 is required.')
    if report.get('answer_style') != 'explain':
        raise ValueError('Claim acceptance requires an explain report, not extractive evidence.')
    for name in ('dataset_sha256', 'implementation_sha256', 'pipeline_sha256', 'draft_prompt_sha256'):
        if not _digest(report.get(name)):
            raise ValueError('Report lacks evaluation provenance: ' + name)
    snapshot = report.get('snapshot')
    if (not isinstance(snapshot, dict) or not _text(snapshot.get('discovery'))
            or not all(_digest(snapshot.get(k)) for k in ('code', 'assets'))):
        raise ValueError('Report lacks retrieval snapshot provenance.')
    errors = report.get('validation_errors')
    if (report.get('status') not in ('completed', 'invalid') or not isinstance(errors, list)
            or any(not _text(e) for e in errors)
            or bool(errors) != (report['status'] == 'invalid')):
        raise ValueError('Invalid evaluation status or validation errors.')
    rows = report.get('results')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError('Acceptance requires 1 to 1000 evaluation cases.')
    ids, total = set(), 0
    for row in rows:
        if (not isinstance(row, dict) or not _text(row.get('id')) or row['id'] in ids
                or not _text(row.get('question'))):
            raise ValueError('Evaluation cases need unique IDs and questions.')
        ids.add(row['id'])
        checks = row.get('checks')
        if not isinstance(checks, dict) or type(checks.get('valid_output_and_citations')) is not bool:
            raise ValueError('Evaluation case lacks citation contract checks.')
        for key in ('expected_status', 'expected_runtime_flag'):
            if checks.get(key) is not None and type(checks[key]) is not bool:
                raise ValueError('Expected answer checks must be booleans or null.')
        answer = row.get('answer')
        if answer is None:
            if checks['valid_output_and_citations']:
                raise ValueError('A successful case must retain its answer.')
            continue
        if not isinstance(answer, dict) or not isinstance(answer.get('claims'), list):
            raise ValueError('Answer claims must be a list.')
        claims = answer['claims']
        total += len(claims)
        if len(claims) > 100 or total > 12000:
            raise ValueError('Acceptance claim bound exceeded.')
        for claim in claims:
            if not isinstance(claim, dict) or not _text(claim.get('text')):
                raise ValueError('Every final claim needs text.')
    if (type(report.get('questions')) is not int or report['questions'] != len(rows)
            or type(report.get('valid_outputs')) is not int
            or report['valid_outputs'] != sum(r['checks']['valid_output_and_citations'] for r in rows)):
        raise ValueError('Evaluation summary counts disagree with retained cases.')
    return report, digest


def _view(row):
    answer = row.get('answer')
    return {'question': row['question'], 'checks': row['checks'], 'error': row.get('error'),
            'review_guidance': row.get('human_review'),
            'answer': {k: answer.get(k) for k in ('question', 'status', 'snapshot', 'claims', 'evidence',
                                                'missing_evidence', 'evidence_gaps', 'runtime_required')}
            if answer is not None else None}


def _packet(report, digest):
    return {'schema_version': 1, 'kind': 'answer-acceptance-review', 'report_sha256': digest,
            'policy': dict(POLICY), 'scope': _SCOPE,
            'reviewer': {'name': '', 'kind': None, 'independent': None},
            'reviews': [{'id': row['id'], 'view': _view(row),
                         'case_judgments': {c: None for c in CRITERIA}, 'rationale': '',
                         'claims': [{'index': i, 'claim_sha256': _hash(claim), 'support': None, 'rationale': ''}
                                    for i, claim in enumerate((row.get('answer') or {}).get('claims', []), 1)]}
                        for row in report['results']]}


def acceptance_template(report_path):
    """Create an unfilled packet; no generation or grading calls are made."""
    return _packet(*_report(report_path))


def _contract(row, snapshot):
    failed = [key for key in ('valid_output_and_citations', 'expected_status', 'expected_runtime_flag')
              if row['checks'].get(key) is False]
    answer = row.get('answer')
    if answer is None or row.get('error'):
        return sorted(set(failed + ['evaluation_error']))
    if answer.get('snapshot') != snapshot or answer.get('question') != row['question']:
        failed.append('answer_identity')
    if (answer.get('status') not in ('answered', 'partial', 'insufficient_evidence')
            or type(answer.get('runtime_required')) is not bool):
        failed.append('answer_contract')
    evidence = answer.get('evidence')
    if not isinstance(evidence, dict):
        return sorted(set(failed + ['evidence_contract']))
    for record in evidence.values():
        if (not isinstance(record, dict) or not all(_text(record.get(k)) for k in ('text', 'title', 'entity_id', 'evidence_type'))
                or not isinstance(record.get('sources'), list) or not record['sources']):
            failed.append('evidence_contract')
            continue
        for source in record['sources']:
            if not isinstance(source, dict) or not _text(source.get('path')) or not _digest(source.get('sha256')):
                failed.append('source_contract')
                continue
            start, end = source.get('start_line'), source.get('end_line')
            if (start is not None or end is not None) and not (
                    type(start) is int and type(end) is int and 1 <= start <= end):
                failed.append('source_contract')
    for claim in answer['claims']:
        ids = claim.get('evidence_ids')
        if (claim.get('kind') not in ('fact', 'interpretation') or not isinstance(ids, list) or not ids
                or any(not isinstance(i, str) or i not in evidence for i in ids)):
            failed.append('claim_contract')
    return sorted(set(failed))


def review_acceptance(report_path, judgments_path):
    """Score complete or pending judgments against immutable output/context bytes."""
    report, digest = _report(report_path)
    expected = _packet(report, digest)
    value, review_hash = _read(judgments_path)
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError('Invalid acceptance packet fields.')
    for key in ('schema_version', 'kind', 'report_sha256', 'policy', 'scope'):
        if _canonical(value[key]) != _canonical(expected[key]):
            raise ValueError('Acceptance packet differs from the exact report or frozen policy: ' + key)
    reviewer = value['reviewer']
    if (not isinstance(reviewer, dict) or set(reviewer) != {'name', 'kind', 'independent'}
            or not isinstance(reviewer['name'], str) or reviewer['kind'] not in (None, 'human', 'agent')
            or (reviewer['independent'] is not None and type(reviewer['independent']) is not bool)):
        raise ValueError('Invalid reviewer identity or independence declaration.')
    reviews = value['reviews']
    if not isinstance(reviews, list) or len(reviews) != len(expected['reviews']):
        raise ValueError('Review must retain every evaluation case.')
    by_id = {}
    for review in reviews:
        if not isinstance(review, dict) or not _text(review.get('id')) or review['id'] in by_id:
            raise ValueError('Review case IDs must be unique.')
        by_id[review['id']] = review
    if set(by_id) != {r['id'] for r in expected['reviews']}:
        raise ValueError('Review case IDs differ from the report.')
    counts = {'supported': 0, 'unsupported': 0, 'uncertain': 0, 'pending': 0}
    results = []
    for case, template in zip(report['results'], expected['reviews']):
        review = by_id[case['id']]
        if set(review) != set(template) or _canonical(review['view']) != _canonical(template['view']):
            raise ValueError('Review context was changed: ' + case['id'])
        judgments = review['case_judgments']
        if not isinstance(judgments, dict) or set(judgments) != set(CRITERIA) or any(
                v is not None and type(v) is not bool for v in judgments.values()):
            raise ValueError('Case judgments must be explicit booleans or pending nulls.')
        if not isinstance(review['rationale'], str) or (any(v is not None for v in judgments.values()) and not _text(review['rationale'])):
            raise ValueError('Judged cases need a source-based rationale.')
        claims = review['claims']
        if not isinstance(claims, list) or len(claims) != len(template['claims']):
            raise ValueError('Review must retain every final claim.')
        pending = any(v is None for v in judgments.values())
        for claim, original in zip(claims, template['claims']):
            if (not isinstance(claim, dict) or set(claim) != set(original)
                    or type(claim['index']) is not int or claim['index'] != original['index']
                    or claim['claim_sha256'] != original['claim_sha256']):
                raise ValueError('Claim identity or order changed.')
            support = claim['support']
            if support is not None and support not in ('supported', 'unsupported', 'uncertain'):
                raise ValueError('Invalid claim support judgment.')
            if not isinstance(claim['rationale'], str) or (support is not None and not _text(claim['rationale'])):
                raise ValueError('Judged claims need a source-based rationale.')
            counts[support or 'pending'] += 1
            pending |= support is None
        failed = _contract(case, report['snapshot']) + [c for c in CRITERIA if judgments[c] is False]
        results.append({'id': case['id'], 'pending': pending, 'passed': not pending and not failed,
                        'failed_criteria': sorted(set(failed)), 'claims': claims, 'case_judgments': judgments,
                        'rationale': review['rationale']})
    total = sum(counts.values())
    support_rate = rate(counts['supported'], total)
    complete = all(not r['pending'] for r in results)
    thresholds = (complete and total > 0 and support_rate['rate'] >= POLICY['supported_claim_rate']
                  and all(r['passed'] for r in results) and report['status'] == 'completed')
    human = bool(_text(reviewer['name']) and reviewer['kind'] == 'human' and reviewer['independent'] is True)
    return {'schema_version': 1, 'kind': 'answer-acceptance-result', 'report_sha256': digest,
            'review_sha256': review_hash, 'policy': dict(POLICY), 'reviewer': reviewer,
            'review_status': 'completed' if complete else 'pending', 'questions': len(results),
            'reviewed_cases': sum(not r['pending'] for r in results),
            'reviewed_claims': total - counts['pending'],
            'supported_claims': support_rate, 'claim_counts': counts,
            'thresholds_met': thresholds, 'human_review_requirement_met': human,
            'accepted': thresholds and human, 'production_gate_passed': False,
            'validation_errors': report['validation_errors'], 'results': results,
            'limitation': 'Only these captured outputs were reviewed. Identity and independence are self-declared. '
                          'Intervals are descriptive; claims within one answer are correlated. '
                          'Independent source-label/corpus review and other production gates remain separate.'}


def write_review_artifact(value, path):
    """Never overwrite a report or earlier judgment; write portable UTF-8 JSON."""
    raw = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(raw)
