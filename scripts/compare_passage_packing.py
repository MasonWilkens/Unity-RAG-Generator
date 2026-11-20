"""Paired prefix-control/current packing on identical immutable source evidence."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unity_rag.benchmark import file_sha256, _code_hash
from unity_rag.context_packing import count_evidence
from unity_rag.embeddings import FastEmbedProvider
from unity_rag.passage_evaluation import evaluate_passages
from unity_rag.workspace import refresh, resolve_workspace


def prefix_control(evidence, key, record, max_tokens, count, **ignored):
    """The former prefix-only answer packer, isolated from all other changes."""
    def fits(candidate):
        return count_evidence({**evidence, key: candidate}, count) <= max_tokens
    if fits(record):
        return record, False
    sources = record.get('sources', [])
    if record.get('evidence_type') != 'original_source' or len(sources) != 1:
        return None, False
    source = sources[0]
    lines = record['text'].splitlines()
    first, last = source.get('start_line'), source.get('end_line')
    if type(first) is not int or type(last) is not int or last - first + 1 != len(lines):
        return None, False
    best = None
    for size in range(1, len(lines)):
        candidate = {**record, 'text': '\n'.join(lines[:size]),
                     'sources': [{**source, 'end_line': first + size - 1}],
                     'packing': {'truncated': True, 'original_end_line': last}}
        if fits(candidate):
            best = candidate
    return best, best is not None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--model-cache', required=True)
    parser.add_argument('--dataset', default='tests/evaluation/passage-development-v1.json')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise ValueError('Choose a new report path; prior reports are immutable.')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    provider = FastEmbedProvider(cache_dir=args.model_cache)
    refresh(args.workspace, Path(__file__).resolve().parents[1]/'tests/fixtures/passage_project',
            semantic=True, provider=provider, chunk_budget=8192)
    generation = resolve_workspace(args.workspace)
    identities = {name: file_sha256(generation/name) for name in ('inventory.sqlite3', 'semantic.sqlite3', 'complete.json')}
    reports = {}
    for variant in ('prefix_control', 'query_windows'):
        reports[variant] = {}
        with patch('unity_rag.context_packing.fit_record', prefix_control) if variant == 'prefix_control' else nullcontext():
            for mode in ('search', 'semantic', 'hybrid'):
                result = evaluate_passages(generation, args.dataset, mode=mode, limit=5, expand=0,
                                           context_chars=1400, provider=provider)
                reports[variant][mode] = result
                print(variant, mode, result['summary'], flush=True)
    if identities != {name: file_sha256(generation/name) for name in identities}:
        raise ValueError('Evaluation modified its frozen generation.')
    with output.open('x', encoding='utf-8') as stream:
        json.dump({'implementation_sha256': _code_hash(), 'generation_files': identities,
                   'reports': reports,
                   'scope': 'Same rebuilt fixture index and rankings. Prefix-only packing control versus query windows, not a historical whole-pipeline baseline.'}, stream, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
