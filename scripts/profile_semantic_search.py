"""Compare exact vector scans on fixed indexes without changing ranking inputs."""
from contextlib import closing
import argparse
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unity_rag.benchmark import file_sha256, _code_hash
from unity_rag.embeddings import FastEmbedProvider
from unity_rag.semantic import normalized, rank, semantic_status
from unity_rag.semantic_profile import process_memory


def original_scan(workspace, query, provider):
    report = semantic_status(workspace)
    if any(report['config'].get(k) != v for k, v in provider.config.items()):
        raise ValueError('Embedding configuration changed.')
    if not report['passages']:
        return {}
    vector = normalized(provider.query(query), report['dimension'])
    scores = {}
    with closing(sqlite3.connect((workspace/'semantic.sqlite3').as_uri()+'?mode=ro', uri=True)) as db, db:
        for chunk, raw in db.execute('SELECT v.chunk,c.vector FROM vectors v JOIN cache c ON c.hash=v.hash WHERE c.config=?',
                                     (report['config_id'],)):
            passage = normalized(json.loads(raw), len(vector))
            score = sum(a*b for a, b in zip(vector, passage))
            scores[chunk] = max(scores.get(chunk, -1), score)
    return scores


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles', nargs='+', required=True)
    parser.add_argument('--model-cache', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        raise ValueError('Use 1-20 repeats.')
    output = Path(args.output)
    if output.exists():
        raise ValueError('Choose a new report path.')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    embedding = FastEmbedProvider(cache_dir=args.model_cache)
    query = 'How does heat affect weapon cooldown and cooling?'
    vector = embedding.query(query)
    class CachedQuery:
        config = embedding.config
        def query(self, text):
            return vector
    provider = CachedQuery()
    reports = []
    for filename in args.profiles:
        profile = json.loads(Path(filename).read_text(encoding='utf-8'))
        workspace = (Path(profile['artifacts'])/'workspace').resolve()
        identity = file_sha256(workspace/'semantic.sqlite3')
        old = original_scan(workspace, query, provider)
        new = rank(workspace, query, provider)
        if old.keys() != new.keys() or any(abs(old[k]-new[k]) > 1e-12 for k in old):
            raise ValueError('Exact cosine results changed.')
        timings = {'original': [], 'current': []}
        for i in range(args.repeats):
            jobs = [('original', original_scan), ('current', rank)]
            for name, function in jobs if i % 2 == 0 else reversed(jobs):
                start = time.perf_counter()
                function(workspace, query, provider)
                timings[name].append(time.perf_counter()-start)
        peaks = {}
        for name, function in [('original', original_scan), ('current', rank)]:
            tracemalloc.start()
            function(workspace, query, provider)
            _, peaks[name] = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        if file_sha256(workspace/'semantic.sqlite3') != identity:
            raise ValueError('Profile modified its index.')
        reports.append({'workspace': str(workspace), 'semantic_sha256': identity,
                        'index_bytes': (workspace/'semantic.sqlite3').stat().st_size,
                        'equal_scores': True, 'entities_or_chunks_scored': len(new),
                        'scan': new.profile, 'seconds': timings,
                        'median_seconds': {k: statistics.median(v) for k, v in timings.items()},
                        'python_peak_bytes': peaks, 'process_memory': process_memory(os.getpid())})
        print(workspace.name, reports[-1]['median_seconds'], flush=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump({'implementation_sha256': _code_hash(), 'reports': reports,
                   'scope': 'Warm exact-scan comparison with one cached query vector. Excludes model inference; full regression tests may run concurrently. Python peak excludes native SQLite/model allocations; process peak is lifetime, not a per-scan delta.'}, stream, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
