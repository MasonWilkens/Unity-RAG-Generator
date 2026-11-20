"""Path-coverage evaluation with current-source checks; no model judges."""
import hashlib
import json
import time
from pathlib import Path
from .retrieval import retrieve
from .workspace import resolve_workspace


def _passage_checks(case, inventory, result):
    """Compare reviewed exact passages with returned text and independently checked source bytes."""
    from .source_checks import inspect_source
    from .code_map import _read_snapshot_file
    import re
    reviewed = case.get('reviewed_source_sha256', {})
    expected = set(case.get('expected_sources', []))
    entries = {row['path']: row for row in inventory['entries']}
    items = result['results'] + result['context']
    freshness = {}
    for row in result['source_freshness']:
        freshness.setdefault(row['path'], set()).add((row['status'], row.get('sha256')))
    checks = []
    for passage in case.get('required_passages', []):
        if not isinstance(passage, dict):
            raise ValueError('Each required passage must be an object.')
        path, quote = passage.get('path'), passage.get('quote')
        first, last = passage.get('start_line'), passage.get('end_line')
        digest = reviewed.get(path) if isinstance(path, str) else None
        if (path not in expected or not isinstance(quote, str) or not quote.strip() or
                type(first) is not int or type(last) is not int or first < 1 or last < first or
                not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
            raise ValueError('Each required passage needs an expected path, quote, line range and reviewed SHA-256.')
        review_status = inspect_source(inventory['project'], entries, {'path': path, 'sha256': digest})['status']
        if review_status == 'current':
            try:
                text = '\n'.join(_read_snapshot_file(Path(inventory['project']).resolve(), entries[path]).splitlines())
                lines = text.split('\n')
                if (text.count(quote) != 1 or first > len(lines) or last > len(lines) or
                        quote not in '\n'.join(lines[first - 1:last])):
                    review_status = 'invalid_review_quote'
            except FileNotFoundError:
                review_status = 'missing'
            except (OSError, UnicodeError):
                review_status = 'unreadable'
            except ValueError:
                review_status = 'stale'
        matches = [item for item in items if quote in item.get('text', '') and any(
            source.get('role') != 'reference_dependency' and source.get('path') == path and type(source.get('start_line')) is int and
            type(source.get('end_line')) is int and source['start_line'] <= first and source['end_line'] >= last
            for source in item.get('sources', []))]
        returned = bool(matches)
        dependencies_current = any(all(freshness.get(s['path']) == {('current', s.get('sha256'))}
                                           and inspect_source(inventory['project'], entries, s)['status'] == 'current'
                                       for s in item.get('sources', []) if s.get('role') == 'reference_dependency')
                                   for item in matches)
        indexed_current = freshness.get(path) == {('current', digest)}
        checks.append({'path': path, 'start_line': first, 'end_line': last,
                       'review_status': review_status, 'returned': returned,
                       'retrieved_source_current': indexed_current, 'reference_dependencies_current': dependencies_current if returned else None,
                       'matched_current': returned and review_status == 'current' and indexed_current and dependencies_current})
    return checks


def evaluate_sources(workspace, dataset, *, mode='search', limit=5, expand=2, provider=None, path_refs=0, incoming_expand=0, context_chars=24000, source_aware=False, reserve_related_chars=0, related_rank=False):
    workspace = resolve_workspace(workspace)
    raw = Path(dataset).read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    if data.get('schema_version') != 1 or not data.get('cases'):
        raise ValueError('Source evaluation requires schema_version 1 and cases.')
    ids = [c['id'] for c in data['cases']]
    if len(ids) != len(set(ids)):
        raise ValueError('Evaluation IDs must be unique.')
    rows = []
    inventory = None
    if any(case.get('required_passages') for case in data['cases']):
        from .discovery import status
        inventory = status(workspace)
    for case in data['cases']:
        expected = set(case.get('expected_sources', []))
        if not expected or not case.get('question'):
            raise ValueError('Every case needs a question and independently reviewed source paths.')
        started = time.perf_counter()
        result = retrieve(workspace, case['question'], mode=mode, limit=limit, expand=expand, provider=provider,
                          path_refs=path_refs, incoming_expand=incoming_expand, context_chars=context_chars, source_aware=source_aware,
                          reserve_related_chars=reserve_related_chars, related_rank=related_rank)
        paths = {s['path'] for item in result['results'] + result['context'] for s in item.get('sources', [])
                 if s.get('role') != 'reference_dependency'}
        freshness = {}
        for item in result['source_freshness']:
            freshness.setdefault(item['path'], set()).add(item['status'])
        hits = expected & paths
        dependency_sources = [s for item in result['results'] + result['context'] for s in item.get('sources', [])
                              if s.get('role') == 'reference_dependency']
        if dependency_sources and inventory is None:
            from .discovery import status
            inventory = status(workspace)
        if dependency_sources:
            from .source_checks import inspect_source
            dependency_entries = {r['path']: r for r in inventory['entries']}
            noncurrent_dependencies = sorted({s['path'] for s in dependency_sources
                                              if freshness.get(s['path']) != {'current'} or
                                              inspect_source(inventory['project'], dependency_entries, s)['status'] != 'current'})
        else:
            noncurrent_dependencies = []
        reviewed = case.get('reviewed_source_sha256', {})
        current_hashes = {item['path']: item.get('sha256') for item in result['source_freshness'] if item['status'] == 'current'}
        passages = _passage_checks(case, inventory, result) if case.get('required_passages') else []
        rows.append({'id': case['id'], 'seconds': time.perf_counter() - started,
                     'expected_sources': sorted(expected), 'hits': sorted(hits), 'missing': sorted(expected - paths),
                     'any_expected_hit': bool(hits), 'all_expected_hit': expected <= paths,
                     'noncurrent_expected_hits': sorted(p for p in hits if freshness.get(p) != {'current'}),
                     'noncurrent_reference_dependencies': noncurrent_dependencies,
                     'reviewed_hash_mismatch_hits': sorted(p for p in hits if p in reviewed and current_hashes.get(p) != reviewed[p]),
                     'unversioned_expected_sources': sorted(expected - reviewed.keys()),
                     'required_passages': passages,
                     'all_required_passages_current': all(p['matched_current'] for p in passages) if passages else None,
                     'facts_not_automatically_graded': case.get('required_facts', [])})
    return {'dataset_sha256': hashlib.sha256(raw).hexdigest(), 'split': data.get('split'),
            'generation': workspace.name, 'mode': mode, 'path_refs': path_refs, 'incoming_expand': incoming_expand,
            'context_chars': context_chars, 'source_aware': source_aware, 'reserve_related_chars': reserve_related_chars,
            'related_rank': related_rank, 'cases': rows,
            'any_expected_hits': sum(x['any_expected_hit'] for x in rows),
            'all_expected_hits': sum(x['all_expected_hit'] for x in rows),
            'cases_with_required_passages': sum(bool(x['required_passages']) for x in rows),
            'all_required_passages_current': sum(x['all_required_passages_current'] is True for x in rows),
            'limitation': 'Source-path coverage is not factual correctness or runtime validation.'}
