"""Installed-package acceptance probe. Run only from verify_distribution.py."""
from contextlib import closing
import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--extras', default='')
    args = parser.parse_args()
    import unity_rag

    package = Path(unity_rag.__file__).resolve().parent
    require(package.is_relative_to(Path(sys.prefix).resolve()), 'Imported outside the clean environment')
    require(sys.prefix != sys.base_prefix, 'A fresh virtual environment is required')
    require(unity_rag.__version__ == args.version == importlib.metadata.version('unity-rag'),
            'Installed version does not match distribution metadata')
    for name in ('Program.cs', 'UnityRag.Analyzer.csproj', 'NuGet.Config'):
        require((package / 'roslyn' / name).is_file(), f'Missing bundled analyzer file: {name}')
    for extra in filter(None, args.extras.split(',')):
        __import__({'semantic': 'fastembed', 'tokens': 'tiktoken'}[extra])
    if not args.extras:
        require(importlib.util.find_spec('fastembed') is None, 'Base install unexpectedly needs FastEmbed')
        require(importlib.util.find_spec('tiktoken') is None, 'Base install unexpectedly needs tiktoken')
    with closing(sqlite3.connect(':memory:')) as db, db:
        db.execute('CREATE VIRTUAL TABLE probe USING fts5(text)')

    executable = Path(sys.executable).parent / ('unity-rag.exe' if sys.platform == 'win32' else 'unity-rag')
    def command(*parts, structured=True):
        run = subprocess.run([str(executable), *map(str, parts)], check=True, capture_output=True,
                             text=True, encoding='utf-8', timeout=300)
        return json.loads(run.stdout) if structured else run.stdout.strip()

    require(command('--version', structured=False) == f'unity-rag {args.version}', 'Broken console version')
    module = subprocess.run([sys.executable, '-I', '-m', 'unity_rag', '--version'],
                            check=True, capture_output=True, text=True, timeout=30)
    require(module.stdout.strip() == f'unity-rag {args.version}', 'Broken module entry point')
    project = args.project.resolve()
    before, package_before = hashes(project), hashes(package)
    work = ('--workspace', args.workspace)
    preflight = command('creation-preflight', '--project', project, *work)
    require(preflight['ready_for_attempt'], 'Clean-install preflight failed')
    built = command('refresh', '--project', project, *work, '--no-semantic')
    require(built['state'] == 'complete', 'Initial refresh did not publish a snapshot')
    exact = command('retrieve', 'Game.Enemy.CanAttack', *work, '--mode', 'exact', '--json')
    require(any(r['entity_id'] == 'Assembly-CSharp::M:Game.Enemy.CanAttack(System.Single)'
                for r in exact['results']), 'Qualified code lookup failed')
    lexical = command('retrieve', 'attack cooldown', *work, '--mode', 'search', '--json')
    require(any('CanAttack' in r['entity_id'] for r in lexical['results']), 'Keyword retrieval failed')
    guid = command('retrieve', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', *work, '--mode', 'exact', '--json')
    require(any(r['entity_id'] == 'asset:Assets/Goblin.prefab' for r in guid['results']), 'GUID lookup failed')
    links = command('retrieve', 'Assets/Enemy.cs', *work, '--mode', 'used-by', '--json')
    require(any(r['kind'] == 'uses_script' for r in links['results']), 'Asset-to-code link is missing')
    answer = command('ask', 'CanAttack', *work, '--mode', 'exact', '--style', 'extractive', '--json')
    require(answer['model_calls'] == 0 and answer['claims'] == [] and answer['excerpts'],
            'Extractive answer must return source excerpts without generation')
    for excerpt in answer['excerpts']:
        source = project / excerpt['path']
        raw = source.read_bytes()
        lines = raw.decode('utf-8-sig').splitlines()
        require(excerpt['text'] == '\n'.join(lines[excerpt['start_line']-1:excerpt['end_line']]),
                'Cited excerpt differs from literal source')
        require(excerpt['sha256'] == hashlib.sha256(raw).hexdigest(), 'Citation hash mismatch')
    repeat = command('refresh', *work)
    require(repeat['state'] == 'unchanged' and repeat['generation'] == built['generation'],
            'Unchanged refresh rebuilt the snapshot')
    require(hashes(project) == before, 'Indexing modified the Unity fixture')
    require(hashes(package) == package_before, 'Analyzer build modified installed package files')

    # Intentional source edit: refresh must publish new evidence and keep the old generation.
    marker = project / 'Assets' / 'ReleaseInstallProbe.cs'
    marker.write_text('public class ReleaseInstallProbe { public const int Value = 73; }\n', encoding='utf-8')
    status = command('workspace-status', *work)
    require(not status['fresh'], 'Source change was not detected')
    updated = command('refresh', *work)
    require(updated['state'] == 'complete' and updated['generation'] != built['generation'],
            'Changed-source refresh did not publish a new generation')
    found = command('retrieve', 'ReleaseInstallProbe', *work, '--mode', 'exact', '--json')
    require(found['results'], 'Refreshed code is missing from retrieval')
    require((args.workspace / 'generations' / built['generation'] / 'complete.json').is_file(),
            'Previous completed generation was lost')
    status = command('workspace-status', *work)
    require(status['healthy'] and status['fresh'], 'Refreshed workspace is unhealthy or stale')
    print(json.dumps({
        'status': 'passed', 'version': args.version, 'python': sys.version.split()[0],
        'sqlite': sqlite3.sqlite_version, 'dotnet': subprocess.check_output(
            ['dotnet', '--version'], text=True, cwd=package / 'roslyn').strip(),
        'checks': ['installed_import', 'console_and_module_entry_points', 'analyzer_bundle',
                   'fts5', 'preflight', 'code_lookup', 'keyword_lookup', 'guid_lookup',
                   'asset_script_link', 'literal_citations', 'no_model_calls',
                   'source_unchanged', 'installed_package_unchanged', 'unchanged_refresh',
                   'changed_source_refresh', 'previous_generation_preserved'],
        'extras_imported': args.extras.split(',') if args.extras else [],
        'dependencies': sorted(f'{d.metadata["Name"]}=={d.version}'
                               for d in importlib.metadata.distributions()),
    }, indent=2))


if __name__ == '__main__':
    main()
