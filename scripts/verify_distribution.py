"""Audit and install both release artifacts outside the repository checkout."""
import argparse
from datetime import datetime, timezone
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROSLYN = {'unity_rag/roslyn/' + name for name in
          ('Program.cs', 'UnityRag.Analyzer.csproj', 'NuGet.Config')}
DOCS = {'LICENSE', 'README.md', 'pyproject.toml', 'MANIFEST.in', 'CHANGELOG.md',
        'RELEASE.md', 'PRODUCTION_READINESS_PLAN.md', 'DEVELOPMENT_PLAN.md',
        'REFINEMENT_PLAN.md', 'BENCHMARK.md', 'SEMANTIC_INDEXING.md', 'OPENAI.md',
        'RAG_CREATION_PLAN.md', 'SHEET_INGESTION_DESIGN.md', 'ANSWER_ACCEPTANCE.md'}
SCRIPTS = {'scripts/verify_distribution.py', 'scripts/install_smoke.py',
           'requirements/release.txt', 'requirements/test.txt'}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def audit(artifact, root=ROOT):
    """Reject missing/stale code and unexpected files before pip sees the artifact."""
    if artifact.suffix == '.whl':
        with zipfile.ZipFile(artifact) as archive:
            names = [n for n in archive.namelist() if not n.endswith('/')]
            require(len(names) == len(set(names)), 'Duplicate wheel entries')
            files = {n: archive.read(n) for n in names}
        metadata_paths = [n for n in files if n.endswith('.dist-info/METADATA')]
        require(len(metadata_paths) == 1, 'Expected one wheel metadata record')
        metadata_path = metadata_paths[0]
        metadata_root = metadata_path.rsplit('/', 1)[0]
        metadata_files = {f'{metadata_root}/{name}' for name in
                          ('METADATA', 'WHEEL', 'RECORD', 'entry_points.txt', 'top_level.txt', 'licenses/LICENSE')}
        allowed_extra = metadata_files
    else:
        require(artifact.name.endswith('.tar.gz'), 'Expected a wheel or .tar.gz source distribution')
        with tarfile.open(artifact, 'r:gz') as archive:
            entries = [m for m in archive.getmembers() if not m.isdir()]
            require(all(m.isfile() for m in entries), 'Source archive contains links or special files')
            require(all(not PurePosixPath(m.name).is_absolute() and '..' not in PurePosixPath(m.name).parts
                        and '\\' not in m.name for m in entries), 'Invalid source archive path')
            roots = {m.name.split('/')[0] for m in entries}
            require(len(roots) == 1, 'Source archive must have one root')
            names = [m.name.partition('/')[2] for m in entries]
            require(len(names) == len(set(names)), 'Duplicate source archive entries')
            files = {name: archive.extractfile(m).read() for name, m in zip(names, entries)}
        metadata_path = 'PKG-INFO'
        fixtures = {p.relative_to(root).as_posix() for p in
                    (root / 'tests/fixtures/retrieval_project').rglob('*') if p.is_file()}
        egg = {'unity_rag.egg-info/' + name for name in
               ('PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'entry_points.txt', 'requires.txt', 'top_level.txt')}
        allowed_extra = DOCS | SCRIPTS | fixtures | egg | {'PKG-INFO', 'setup.cfg'}
        for required in DOCS | SCRIPTS | fixtures:
            require(required in files, f'Source archive is missing {required}')
            require(files[required] == (root / required).read_bytes(), f'Stale source archive file: {required}')
    expected = {p.relative_to(root).as_posix() for p in (root / 'unity_rag').rglob('*.py')} | ROSLYN
    for name in files:
        path = PurePosixPath(name)
        require(not path.is_absolute() and '..' not in path.parts and '\\' not in name,
                'Invalid archive path')
        require(name in expected | allowed_extra, f'Unexpected release content: {name}')
    for name in expected:
        require(name in files, f'Artifact is missing {name}')
        require(files[name] == (root / name).read_bytes(), f'Artifact has stale source: {name}')
    require(metadata_path in files, 'Distribution metadata is missing')
    metadata = BytesParser().parsebytes(files[metadata_path])
    require(metadata['Name'] == 'unity-rag' and metadata['Version'], 'Incorrect package identity')
    require(metadata['License-Expression'] == 'MIT', 'Missing MIT license metadata')
    require(metadata['Requires-Python'] == '>=3.10', 'Incorrect Python requirement')
    return {'file': artifact.name, 'version': metadata['Version'], 'files': len(files),
            'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}


def clean_environment():
    env = dict(os.environ)
    for name in list(env):
        if name.startswith('PYTHON') or name in ('VIRTUAL_ENV', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
            env.pop(name)
    env.update(PYTHONNOUSERSITE='1', PYTHONUTF8='1', DOTNET_CLI_TELEMETRY_OPTOUT='1',
               DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
    return env


def verify(artifact, identity, extras):
    with tempfile.TemporaryDirectory(prefix='unity-rag-install-') as temporary:
        base = Path(temporary).resolve()
        require(not base.is_relative_to(ROOT), 'Clean-install temporary directory must be outside checkout')
        env = clean_environment()
        environment = base / 'environment'
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        candidate = base / artifact.name
        shutil.copyfile(artifact, candidate)
        shutil.copyfile(ROOT / 'scripts/install_smoke.py', base / 'probe.py')
        shutil.copytree(ROOT / 'tests/fixtures/retrieval_project', base / 'Unity project')
        def run(arguments, timeout=600):
            result = subprocess.run([str(python), '-I', *map(str, arguments)], cwd=base, env=env,
                                    capture_output=True, text=True, encoding='utf-8', timeout=timeout)
            require(result.returncode == 0, f'Clean-install command failed:\n{result.stdout}\n{result.stderr}')
            return result.stdout
        target = str(candidate) + (f'[{extras}]' if extras else '')
        run(['-m', 'pip', 'install', '--no-cache-dir', target])
        run(['-m', 'pip', 'check'])
        return json.loads(run(['probe.py', '--project', base / 'Unity project',
                               '--workspace', base / 'Knowledge workspace', '--version', identity['version'],
                               '--extras', extras]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist-dir', type=Path, default=ROOT / 'dist')
    parser.add_argument('--report', type=Path, default=ROOT / '.unity-rag/release-install.json')
    parser.add_argument('--extras', default='', help='Optional semantic,tokens dependency import checks; no model download')
    args = parser.parse_args()
    require(set(filter(None, args.extras.split(','))) <= {'semantic', 'tokens'}, 'Unknown package extra')
    started = time.monotonic()
    report = {'schema_version': 1, 'started_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'failed', 'artifacts': []}
    try:
        wheels = list(args.dist_dir.glob('*.whl'))
        sources = list(args.dist_dir.glob('*.tar.gz'))
        require(len(wheels) == len(sources) == 1, 'Use a fresh directory with exactly one wheel and one source archive')
        identities = [(a.resolve(), audit(a.resolve())) for a in wheels + sources]
        require(len({i['version'] for _, i in identities}) == 1, 'Artifact versions differ')
        for artifact, identity in identities:
            print(f'Checking isolated install: {artifact.name}', flush=True)
            row = {**identity, 'status': 'running'}
            report['artifacts'].append(row)
            row['install'] = verify(artifact, identity, args.extras)
            row['status'] = 'passed'
        report['status'] = 'passed'
    except Exception as exc:
        report['error'] = str(exc)
        print(str(exc), file=sys.stderr)
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(f'Install verification {report["status"]}; report: {args.report}', flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
