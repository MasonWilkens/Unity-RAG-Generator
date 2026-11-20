"""Run the complete test suite with prerequisites, no skipped tests, and saved reports."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def fingerprint():
    paths = sorted((ROOT / 'unity_rag').glob('*.py')) + sorted((ROOT / 'tests').glob('test_*.py'))
    paths += [p for p in sorted((ROOT / 'unity_rag/roslyn').glob('*')) if p.is_file()]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def worker(modules, report):
    sys.path[:0] = [str(ROOT / 'tests'), str(ROOT)]
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.wasSuccessful() and result.testsRun > 0 and not result.skipped
    report.write_text(json.dumps({'passed': passed, 'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': result.skipped, 'modules': modules}, indent=2) + '\n', encoding='utf-8')
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--output', type=Path, default=ROOT / '.unity-rag/release-checks')
    parser.add_argument('--dotnet-major', type=int)
    parser.add_argument('--worker', nargs='+', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, args.output)
    if not 1 <= args.jobs <= 8:
        parser.error('--jobs must be between 1 and 8')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    report = {'schema_version': 1, 'started_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'failed', 'shards': [], 'source_sha256': fingerprint()}
    try:
        if sys.version_info < (3, 10):
            raise RuntimeError('Python 3.10 or newer is required')
        report['python'] = sys.version.split()[0]
        report['sqlite'] = sqlite3.sqlite_version
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE VIRTUAL TABLE probe USING fts5(text)')
        sdk = subprocess.check_output(['dotnet', '--version'], cwd=ROOT / 'unity_rag/roslyn', text=True).strip()
        if int(sdk.split('.')[0]) < 6 or (args.dotnet_major and not sdk.startswith(f'{args.dotnet_major}.')):
            raise RuntimeError(f'Unexpected .NET SDK: {sdk}')
        subprocess.run(['git', '--version'], check=True, capture_output=True)
        report['dotnet'] = sdk
        for dependency in ('PyYAML', 'tiktoken'):
            importlib.metadata.version(dependency)  # Missing prerequisites fail instead of skipping.
        report['dependencies'] = sorted(f'{d.metadata["Name"]}=={d.version}'
                                        for d in importlib.metadata.distributions())
        # Warm the optional tokenizer cache once, before parallel test workers.
        import tiktoken
        tiktoken.get_encoding('cl100k_base')
        modules = sorted(p.stem for p in (ROOT / 'tests').glob('test_*.py'))
        if not modules:
            raise RuntimeError('No tests discovered')
        def run(index, names):
            output = args.output / f'shard-{index}.json'
            log = args.output / f'shard-{index}.log'
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith('PYTHON') and k not in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY')}
            env.update(PYTHONUTF8='1', PYTHONNOUSERSITE='1', DOTNET_CLI_TELEMETRY_OPTOUT='1')
            with log.open('w', encoding='utf-8') as stream:
                process = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()),
                    '--output', str(output), '--worker', *names], cwd=ROOT, env=env,
                    stdout=stream, stderr=subprocess.STDOUT, timeout=1800,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            row = json.loads(output.read_text(encoding='utf-8')) if output.exists() else {'passed': False}
            row.update(index=index, exit_code=process.returncode, log=log.name)
            print(f'Shard {index}: {row.get("tests", 0)} tests, exit {process.returncode}', flush=True)
            return row
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(run, i, modules[i::args.jobs]) for i in range(min(args.jobs, len(modules)))]
            for future in as_completed(futures):
                report['shards'].append(future.result())
        report['tests'] = sum(r.get('tests', 0) for r in report['shards'])
        if fingerprint() != report['source_sha256']:
            raise RuntimeError('Source or tests changed during validation; rerun against fixed files')
        if all(r['passed'] and r['exit_code'] == 0 for r in report['shards']):
            report['status'] = 'passed'
    except Exception as exc:
        report['error'] = str(exc)
        print(str(exc), file=sys.stderr)
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - start, 2)
        (args.output / 'summary.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(f'Test checks {report["status"]}: {args.output / "summary.json"}', flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
