"""Counts, uncertainty and diagnostic reports for frozen quality benchmarks."""
from collections import Counter
import math


def rate(passed, total):
    """Wilson interval for descriptive question-level proportions, not a project guarantee."""
    interval = None
    if total:
        p, z = passed / total, 1.959963984540054
        denominator = 1 + z * z / total
        center = (p + z * z / (2 * total)) / denominator
        radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
        interval = [max(0, center - radius), min(1, center + radius)]
    return {'passed': passed, 'total': total, 'rate': passed / total if total else None,
            'wilson_95': interval}


def summarize(rows):
    answerable = [r for r in rows if r['expected_outcome'] == 'evidence']
    empty = [r for r in rows if r['expected_outcome'] == 'insufficient_evidence']
    negatives = [r for r in rows if r['negative_labels']]
    def met(row, stage):
        return row.get('status') == 'completed' and row[stage]['expected_outcome_met']
    return {'questions': len(rows), 'errors': sum(r['status'] != 'completed' for r in rows),
            'all_essential_retrieved': rate(sum(met(r, 'retrieved') for r in answerable), len(answerable)),
            'all_essential_packed': rate(sum(met(r, 'packed') for r in answerable), len(answerable)),
            'packed_pass': rate(sum(r.get('packed', {}).get('passed', False)
                                   for r in rows if r['status'] == 'completed'), len(rows)),
            'no_evidence_outcome': rate(sum(met(r, 'packed') and r.get('answer_status') == 'insufficient_evidence'
                                             for r in empty), len(empty)),
            'negative_free': rate(sum(r['status'] == 'completed' and r['packed']['negative_passages_found'] == 0
                                     for r in negatives), len(negatives)),
            'failure_counts': dict(sorted(Counter(f for r in rows for f in r['failures']).items()))}


def diagnose(row, indexed_groups):
    if row['status'] != 'completed':
        return ['evaluation_error']
    failures = []
    # A group may have alternative passages; only loss of every alternative is a miss.
    for i, packed in enumerate(row['packed']['group_hits']):
        if packed:
            continue
        if not indexed_groups[i]:
            failures.append('index_representation')
        elif not row['retrieved']['group_hits'][i]:
            failures.append('retrieval_selection')
        else:
            failures.append('context_packing')
    if row['packed']['negative_passages_found']:
        failures.append('negative_evidence')
    if row['expected_outcome'] == 'insufficient_evidence' and (
            not row['packed']['expected_outcome_met'] or row.get('answer_status') != 'insufficient_evidence'):
        failures.append('no_evidence_outcome')
    return sorted(set(failures))


def markdown_report(report):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
    def fraction(metric):
        return f"{metric['passed']}/{metric['total']}" if metric['total'] else 'not measured'
    lines = [f"# Quality benchmark: {cell(report['name'])}", '',
             f"Input status: **{report['status']}**. Scope: **{report['scope']}**.",
             f"Frozen input SHA-256: `{report['lock_sha256']}`.",
             f"Implementation SHA-256: `{report['implementation_sha256']}`.", '',
             'Counts include failed cases. Essential coverage requires every labeled group in the final evidence budget.',
             'JSON contains descriptive 95% Wilson intervals; questions from the same project are correlated.', '',
             '| Split / project / category | Questions | Essential retrieved | Essential packed | No-evidence outcome | Errors |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    groups = [(f'split: {name}', value) for name, value in report['splits'].items()]
    groups += [(f"project: {p['name']}", p['summary']) for p in report['projects']]
    groups += [(f'category: {name}', value) for name, value in report['categories'].items()]
    for name, summary in groups:
        lines.append(f"| {cell(name)} | {summary['questions']} | {fraction(summary['all_essential_retrieved'])} | "
                     f"{fraction(summary['all_essential_packed'])} | {fraction(summary['no_evidence_outcome'])} | {summary['errors']} |")
    lines += ['', '## Failures', '', '| Project | Case | Category | Diagnosis | Missing packed groups |',
              '| --- | --- | --- | --- | --- |']
    failures = 0
    for project in report['projects']:
        for row in project['cases']:
            if not row['failures']:
                continue
            failures += 1
            missing = [str(i + 1) for i, found in enumerate(row.get('packed', {}).get('group_hits', [])) if not found]
            lines.append(f"| {cell(project['name'])} | {cell(row['id'])} | {cell(row['category'])} | "
                         f"{cell(', '.join(row['failures']))} | {', '.join(missing) or '—'} |")
    if not failures:
        lines.append('| — | — | — | No labeled failures | — |')
    lines += ['', 'An index-representation miss means the required literal passage was absent from stored evidence; '
              'it does not by itself distinguish ingestion, mapping, transformed asset facts, or chunk boundaries.', '',
              '## Acceptance', '',
              f"Held-out retrieval gate passed: **{report['retrieval_gate_passed']}**.",
              f"Independent corpus prerequisites met: **{report['corpus_requirements_met']}**.",
              'Generated-claim support and explanation abstention: **not evaluated**. '
              'This runner uses extractive answers and makes no generation calls. The 95% supported-claims target remains open.',
              'Repository independence and reviewer declarations require external review; a manifest cannot prove them.']
    if report.get('validation_errors'):
        lines += ['', '## Invalidated inputs', ''] + [f'- {cell(error)}' for error in report['validation_errors']]
    return '\n'.join(lines) + '\n'
