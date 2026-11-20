"""Explicit answer-quality review, bound to the exact evaluated output bytes."""
import hashlib
import json
from pathlib import Path

CRITERIA = ('correctness', 'support', 'completeness', 'abstention')


def _read_report(path):
    raw = Path(path).read_bytes()
    report = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(report, dict):
        raise ValueError('Evaluation report must be an object.')
    rows = report.get('results')
    if not isinstance(rows, list) or not rows:
        raise ValueError('A nonempty answer evaluation report is required.')
    if any(not isinstance(r, dict) for r in rows):
        raise ValueError('Evaluation cases must be objects.')
    ids = [r.get('id') for r in rows]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise ValueError('Evaluation case IDs must be unique nonempty strings.')
    if any(not isinstance(r.get('checks'), dict) or
           type(r['checks'].get('valid_output_and_citations')) is not bool for r in rows):
        raise ValueError('Evaluation report is missing citation contract checks.')
    return report, hashlib.sha256(raw).hexdigest()


def review_template(report_path):
    report, digest = _read_report(report_path)
    return {'schema_version': 1, 'report_sha256': digest,
            'reviewer': '', 'reviewer_kind': 'human_or_agent',
            'scope': 'Inspect every final claim AND gap against cited sources. Completeness means each requested part is answered or appropriately identified as unavailable. Abstention includes runtime flags and refusal to invent missing facts. Citation validity alone is insufficient.',
            'reviews': [{'id': r['id'], **{c: None for c in CRITERIA}, 'rationale': ''}
                        for r in report['results']]}


def review_answers(report_path, judgments_path):
    report, digest = _read_report(report_path)
    judgments = json.loads(Path(judgments_path).read_text(encoding='utf-8-sig'))
    if not isinstance(judgments, dict):
        raise ValueError('Review must be an object.')
    if judgments.get('schema_version') != 1 or judgments.get('report_sha256') != digest:
        raise ValueError('Review does not match this exact evaluation report. Regenerate the template.')
    if (not isinstance(judgments.get('reviewer'), str) or not judgments['reviewer'].strip()
            or judgments.get('reviewer_kind') not in ('human', 'agent')):
        raise ValueError('Identify the reviewer and whether it is a human or agent.')
    reviews = judgments.get('reviews')
    if not isinstance(reviews, list) or len(reviews) != len(report['results']):
        raise ValueError('Review must cover every evaluation case.')
    by_id = {}
    for row in reviews:
        if not isinstance(row, dict) or set(row) != {'id', 'rationale', *CRITERIA}:
            raise ValueError('Invalid review fields.')
        if not isinstance(row['id'], str) or row['id'] in by_id:
            raise ValueError('Duplicate or invalid review case ID.')
        if any(type(row[c]) is not bool for c in CRITERIA):
            raise ValueError('Every quality criterion must be explicitly true or false.')
        if not isinstance(row['rationale'], str) or not row['rationale'].strip():
            raise ValueError('Each case needs a source-based review rationale.')
        by_id[row['id']] = row
    if set(by_id) != {r['id'] for r in report['results']}:
        raise ValueError('Review case IDs differ from the evaluation report.')
    results = []
    for case in report['results']:
        row = by_id[case['id']]
        failed = [c for c in CRITERIA if not row[c]]
        if not case['checks']['valid_output_and_citations']:
            failed.append('citation_contract')
        # Explicit expected flags/status are independent checks, not overrideable by review.
        for key in ('expected_runtime_flag', 'expected_status'):
            if case['checks'].get(key) is False:
                failed.append(key)
        results.append({**row, 'passed': not failed, 'failed_criteria': failed})
    return {'schema_version': 1, 'report_sha256': digest, 'reviewer': judgments['reviewer'],
            'reviewer_kind': judgments['reviewer_kind'], 'cases': len(results),
            'passed': sum(r['passed'] for r in results),
            'accepted': all(r['passed'] for r in results), 'results': results,
            'limitation': 'Acceptance covers these reviewed outputs only, not future answers or all repositories. Human/agent judgments may be mistaken.'}
