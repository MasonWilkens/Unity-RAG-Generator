"""Explicit hosted answer probe on synthetic packing controls and real development questions."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compare_passage_packing import prefix_control
from unity_rag.answers import ask
from unity_rag.benchmark import _code_hash, file_sha256
from unity_rag.embeddings import FastEmbedProvider
from unity_rag.openai_generation import OpenAIProvider
from unity_rag.workspace import resolve_workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-key-file', required=True)
    parser.add_argument('--ledger', required=True)
    parser.add_argument('--budget-usd', required=True)
    parser.add_argument('--fixture-workspace', required=True)
    parser.add_argument('--real-workspace', help='Optional private corpus; requires approval to send its retrieved excerpts')
    parser.add_argument('--real-dataset', type=Path, help='Explicit question labels for --real-workspace')
    parser.add_argument('--model-cache', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if bool(args.real_workspace) != bool(args.real_dataset):
        parser.error('--real-workspace and --real-dataset must be provided together')
    output = Path(args.output)
    if output.exists():
        raise ValueError('Choose a new report path.')
    os.environ['HF_HUB_OFFLINE'] = '1'
    embedding = FastEmbedProvider(cache_dir=args.model_cache)
    provider = OpenAIProvider(api_key_file=args.api_key_file, ledger_path=args.ledger,
                              budget_usd=args.budget_usd, allow_remote=True,
                              service_tier='default', max_calls=6, max_output_tokens=2048)
    fixture_data = json.loads((Path(__file__).resolve().parents[1] /
                              'tests/evaluation/passage-development-v1.json').read_text())
    cases = []
    for variant in ('prefix_control', 'query_windows'):
        for case in fixture_data['cases'][:2]:
            cases.append((case['id'], case['question'], args.fixture_workspace, variant, 1400, 5))
    if args.real_workspace:
        real_data = json.loads(args.real_dataset.read_text(encoding='utf-8-sig'))
        for case in real_data['questions']:
            if case['id'] in ('overheat-cooldown', 'cooling'):
                cases.append((case['id'], case['query'], args.real_workspace, 'query_windows', 24000, 10))
    results = []
    for identity, question, workspace, variant, budget, limit in cases:
        generation = resolve_workspace(workspace)
        index_hash = file_sha256(generation/'inventory.sqlite3')
        try:
            with patch('unity_rag.context_packing.fit_record', prefix_control) if variant == 'prefix_control' else nullcontext():
                answer = ask(generation, question, provider, mode='hybrid', limit=limit, expand=0,
                             context_chars=budget, followups=0, embedding_provider=embedding, show_sources=True)
            results.append({'id': identity, 'variant': variant, 'generation': str(generation),
                            'inventory_sha256': index_hash, 'answer': answer})
            print(identity, variant, answer['status'], len(answer['claims']), flush=True)
        except (OSError, ValueError) as error:
            results.append({'id': identity, 'variant': variant, 'error': str(error)})
            print(identity, variant, 'failed; stopping without retry', flush=True)
            break
    with output.open('x', encoding='utf-8') as stream:
        json.dump({'implementation_sha256': _code_hash(), 'results': results, 'spend': provider.ledger.summary(),
                   'scope': 'Four synthetic packing controls, optionally two explicitly approved real-project questions. Review each claim against source; contract validity is not factual correctness.'}, stream, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
