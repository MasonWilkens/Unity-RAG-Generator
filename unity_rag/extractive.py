"""Bounded source excerpts in retrieval order; no generation model is used."""
import json
from pathlib import Path

from .code_map import _read_snapshot_file

GAPS = {
    'no_supporting_evidence': 'No source excerpts are available from this retrieval.',
    'retrieval_incomplete': 'This is a bounded source view; relevant evidence may be omitted.',
}


def source_blocks(inventory, evidence, budget):
    entries = {r['path']: r for r in inventory['entries']}
    files, blocks, seen = {}, {}, set()
    omitted = 0
    for evidence_id, record in evidence.items():
        for source in record['sources']:
            if source.get('role') == 'reference_dependency':
                continue
            start, end = source.get('start_line'), source.get('end_line')
            # Whole-file metadata citations are not useful source-range candidates.
            if type(start) is not int or type(end) is not int:
                omitted += 1
                continue
            path = source['path']
            if path not in files:
                files[path] = _read_snapshot_file(Path(inventory['project']), entries[path]).splitlines()
            if not 1 <= start <= end <= len(files[path]):
                raise ValueError('Extractive source range is invalid.')
            # Bounded, adjacent windows preserve access to long methods without paraphrasing.
            for first in range(start, end+1, 20):
                last = min(first+19, end)
                identity = (path, first, last)
                if identity in seen:
                    continue
                seen.add(identity)
                key = f'S{len(blocks)+1}'
                block = {'evidence_id': evidence_id, 'path': path, 'sha256': source['sha256'],
                         'start_line': first, 'end_line': last,
                         'text': '\n'.join(files[path][first-1:last])}
                candidate = {**blocks, key: block}
                if len(json.dumps(candidate, ensure_ascii=False)) > budget:
                    omitted += 1
                    continue
                blocks = candidate
    return blocks, omitted


def extract_excerpts(inventory, evidence, budget):
    blocks, omitted = source_blocks(inventory, evidence, budget)
    excerpts = []
    for block in list(blocks.values())[:4]:
        excerpts.append({k: block[k] for k in ('evidence_id', 'path', 'sha256', 'start_line', 'end_line', 'text')})
    gaps = []
    if not excerpts:
        gaps.append('no_supporting_evidence')
    if omitted or len(blocks) > len(excerpts):
        gaps.append('retrieval_incomplete')
    return excerpts, gaps, {'model_calls': 0, 'blocks': len(blocks), 'omitted_blocks': omitted,
        'unselected_blocks': len(blocks)-len(excerpts), 'selection': 'retrieval_order',
        'input_chars': len(json.dumps(blocks, ensure_ascii=False)),
        'limitation': 'Source inspection only. These excerpts are not an answer or a relevance/entailment judgment.'}
