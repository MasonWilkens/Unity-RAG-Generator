"""Rebuild evidence/embeddings on isolated copies of frozen real-project maps."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unity_rag.benchmark import _code_hash, file_sha256, _validate_freeze
from unity_rag.embeddings import FastEmbedProvider
from unity_rag.evaluation import evaluate
from unity_rag.retrieval import build_retrieval
from unity_rag.semantic import build_semantic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', required=True)
    parser.add_argument('--profiles', nargs='+', required=True)
    parser.add_argument('--model-cache', required=True)
    parser.add_argument('--artifacts', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    manifest_path = Path(args.frozen).resolve()
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    _validate_freeze(manifest, manifest_path, manifest['frozen']['settings'])
    if len(args.profiles) != len(manifest['projects']):
        raise ValueError('Supply one profile per frozen project.')
    output, artifacts = Path(args.output), Path(args.artifacts)
    if output.exists() or artifacts.exists():
        raise ValueError('Choose fresh output and artifact paths.')
    artifacts.mkdir(parents=True)
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    provider = FastEmbedProvider(cache_dir=args.model_cache)
    reports = []
    for index, (item, profile_path) in enumerate(zip(manifest['projects'], args.profiles)):
        profile = json.loads(Path(profile_path).read_text())
        original = Path(profile['artifacts'])/'workspace'
        if Path(profile['source']).resolve() != (manifest_path.parent/item['workspace']).resolve():
            raise ValueError('Profile source does not match the frozen generation.')
        work = artifacts/str(index)
        work.mkdir()
        hashes = {}
        for filename in ('inventory.sqlite3', 'semantic.sqlite3'):
            hashes[filename] = file_sha256(original/filename)
            shutil.copyfile(original/filename, work/filename)
        started = time.perf_counter()
        tracemalloc.start()
        retrieval = build_retrieval(work)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        build_seconds = time.perf_counter()-started
        semantic = build_semantic(work, provider)
        modes = {mode: evaluate(work, manifest_path.parent/item['dataset'], mode=mode,
                               limit=manifest['frozen']['settings']['limit'], expand=0, provider=provider)
                 for mode in ('search', 'semantic', 'hybrid')}
        if hashes != {name: file_sha256(original/name) for name in hashes}:
            raise ValueError('Source profile changed during the experiment.')
        reports.append({'name': item['name'], 'workspace': str(work.resolve()),
                        'source_files': hashes, 'retrieval': retrieval, 'semantic': semantic,
                        'retrieval_build_seconds_with_tracing': build_seconds, 'python_peak_bytes': peak,
                        'modes': modes})
        print(item['name'], {mode: r['recall_at_k'] for mode, r in modes.items()}, flush=True)
    _validate_freeze(manifest, manifest_path, manifest['frozen']['settings'])
    with output.open('x', encoding='utf-8') as stream:
        json.dump({'implementation_sha256': _code_hash(), 'projects': reports,
                   'scope': 'Rebuilt evidence and semantic indexes over frozen code/asset maps. Same consulted labels; not fresh Unity reanalysis or independent acceptance. Python peak excludes native allocations.'}, stream, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
