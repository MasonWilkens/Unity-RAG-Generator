"""Freeze source-reviewed multi-project inputs before measuring evidence delivery."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import tempfile
from urllib.parse import urlsplit

from .benchmark import _code_hash, file_sha256
from .discovery import scan, status
from .passage_evaluation import evaluate_passage_case, validate_passage_case
from .quality_reporting import diagnose, summarize
from .retrieval import _open, retrieval_status
from .source_checks import inspect_source
from .workspace import resolve_workspace

DEFAULT_SETTINGS = {'mode': 'search', 'limit': 5, 'expand': 0, 'context_chars': 24000,
                    'context_tokens': None, 'token_encoding': 'utf8-bytes'}
DEFAULT_TARGETS = {'essential_coverage': 0.9, 'supported_claims': 0.95,
                   'minimum_projects': 3, 'minimum_questions': 60, 'minimum_held_out_questions': 20}
WORKSPACE_FILES = ('inventory.sqlite3', 'complete.json', 'semantic.sqlite3')


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def source_identity(inventory):
    # Exclude checkout paths, timestamps and unrelated Git worktree changes.
    return _hash({k: inventory.get(k) for k in
                  ('entries', 'config', 'unity_version', 'packages', 'package_lock', 'assemblies')})


def _environment():
    dependencies = {}
    for name in ('PyYAML', 'tiktoken', 'fastembed'):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    from . import __version__
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'sqlite': sqlite3.sqlite_version, 'unity_rag': __version__, 'dependencies': dependencies}


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a nonempty string.')
    return value


def _settings(data):
    if not isinstance(data.get('settings', {}), dict) or not isinstance(data.get('targets', {}), dict):
        raise ValueError('Settings and targets must be objects.')
    settings = {**DEFAULT_SETTINGS, **data.get('settings', {})}
    if set(settings) != set(DEFAULT_SETTINGS) or settings['mode'] not in ('search', 'exact', 'hybrid', 'semantic'):
        raise ValueError('Invalid quality benchmark settings.')
    for key, low, high in (('limit', 1, 100), ('expand', 0, 20), ('context_chars', 1000, 100000)):
        if type(settings[key]) is not int or not low <= settings[key] <= high:
            raise ValueError(f'Invalid benchmark {key}.')
    tokens = settings['context_tokens']
    if tokens is not None and (type(tokens) is not int or not 2 <= tokens <= 1000000):
        raise ValueError('Invalid benchmark context_tokens.')
    _text(settings['token_encoding'], 'token_encoding')
    targets = {**DEFAULT_TARGETS, **data.get('targets', {})}
    if set(targets) != set(DEFAULT_TARGETS):
        raise ValueError('Unknown quality benchmark target.')
    for key in ('essential_coverage', 'supported_claims'):
        if type(targets[key]) not in (int, float) or not 0 < targets[key] <= 1:
            raise ValueError('Quality rate targets must be in (0, 1].')
    for key in ('minimum_projects', 'minimum_questions', 'minimum_held_out_questions'):
        if type(targets[key]) is not int or targets[key] < DEFAULT_TARGETS[key]:
            raise ValueError(f'{key} cannot weaken the production-plan corpus minimum.')
    return settings, targets


def _repository(value):
    _text(value, 'repository')
    if value.startswith('fixture:'):
        return value.casefold().rstrip('/')
    url = urlsplit(value)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Use a credential-free HTTPS repository URL or fixture: identity.')
    return (url.hostname + url.path.rstrip('/').removesuffix('.git')).casefold()


def _plan(data, kind):
    if not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('kind') != kind:
        raise ValueError(f'Expected schema_version 1 and kind {kind}.')
    _text(data.get('name'), 'Benchmark name')
    if data.get('scope') not in ('synthetic-control', 'independent-candidate'):
        raise ValueError('Scope must be synthetic-control or independent-candidate.')
    settings, targets = _settings(data)
    projects = data.get('projects')
    if not isinstance(projects, list) or not projects or any(not isinstance(p, dict) for p in projects):
        raise ValueError('A benchmark needs nonempty project objects.')
    names, repositories = set(), set()
    for project in projects:
        name = _text(project.get('name'), 'Project name')
        repository = _repository(project.get('repository'))
        if name in names or repository in repositories:
            raise ValueError('Project names and repository identities must be unique across splits.')
        names.add(name); repositories.add(repository)
        if project.get('split') not in ('development', 'held-out'):
            raise ValueError('Each repository needs a development or held-out split.')
        if type(project.get('previously_used_for_tuning')) is not bool:
            raise ValueError('Declare previously_used_for_tuning for every repository.')
        if project['split'] == 'held-out' and project['previously_used_for_tuning']:
            raise ValueError('A previously tuned repository cannot be a repository holdout.')
        if data['scope'] == 'independent-candidate' and repository.startswith('fixture:'):
            raise ValueError('Synthetic fixtures cannot be independent repositories.')
        _text(project.get('workspace'), 'Workspace path')
        _text(project.get('dataset'), 'Dataset path')
    if {p['split'] for p in projects} != {'development', 'held-out'}:
        raise ValueError('Keep at least one development repository and one held-out repository.')
    return settings, targets


def _dataset(path, project, inventory):
    data = _json(path)
    cases = data.get('cases') if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get('schema_version') != 2 or data.get('split') != project['split']
            or not isinstance(cases, list) or not cases):
        raise ValueError('Expected passage schema_version 2, cases, and a matching project split.')
    review = data.get('label_review', {})
    if not isinstance(review, dict) or review.get('kind') not in ('human', 'agent'):
        raise ValueError('Labels need an explicit human or agent label_review.')
    _text(review.get('reviewer'), 'Label reviewer')
    _text(review.get('rationale'), 'Source-review rationale')
    ids = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError('Each question must be an object.')
        name = _text(case.get('id'), 'Case ID')
        if name in ids:
            raise ValueError('Duplicate case ID within a project.')
        ids.add(name)
        _text(case.get('category'), 'Question category')
        if len(_text(case.get('question'), 'Question')) > 2000:
            raise ValueError('Questions must be at most 2000 characters.')
        validate_passage_case(case, inventory)
    return data


def _git_revision(project, inventory):
    if project['repository'].startswith('fixture:'):
        return None
    revision = project.get('revision')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Real repositories require an exact 40-character Git revision.')
    def git(*args):
        result = subprocess.run(['git', '-C', inventory['project'], *args], capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        if result.returncode:
            raise ValueError('Cannot verify the benchmark repository Git identity.')
        return result.stdout.strip()
    if git('rev-parse', 'HEAD') != revision or git('status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('Benchmark repository must be a clean checkout of its declared revision.')
    remote = git('remote', 'get-url', 'origin')
    if _repository(remote) != _repository(project['repository']):
        raise ValueError('Benchmark repository origin differs from its declared identity.')
    return revision


def _generation(path):
    generation = resolve_workspace(path)
    if not (generation / 'complete.json').is_file():
        raise ValueError('Quality benchmarks require a completed managed workspace generation.')
    return generation


def _workspace_hashes(path):
    return {name: file_sha256(path / name) for name in WORKSPACE_FILES if (path / name).is_file()}


def _fresh(inventory):
    with tempfile.TemporaryDirectory(prefix='unity-rag-benchmark-') as temporary:
        root = Path(temporary)
        config = root / 'config.json'
        config.write_text(json.dumps(inventory['config']), encoding='utf-8')
        current = scan(inventory['project'], root / 'inventory', config)
    if source_identity(current) != source_identity(inventory):
        raise ValueError('Project sources changed from the indexed snapshot; rebuild before freezing or evaluating.')


def _relative(path, base):
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:  # Separate Windows drives.
        return str(path)


def freeze_quality_benchmark(manifest, output):
    """Freeze questions, thresholds, source bytes and index bindings before retrieval runs."""
    path, destination = Path(manifest).resolve(), Path(output).resolve()
    original = path.read_bytes()
    data = json.loads(original.decode('utf-8-sig'))
    settings, targets = _plan(data, 'quality-benchmark')
    rows, roots, sources = [], set(), set()
    for project in data['projects']:
        generation = _generation(path.parent / project['workspace'])
        before = _workspace_hashes(generation)
        inventory = status(generation)
        root = os.path.normcase(str(Path(inventory['project']).resolve()))
        identity = source_identity(inventory)
        if root in roots or identity in sources:
            raise ValueError('Copied or repeated source projects cannot span repository splits.')
        roots.add(root); sources.add(identity)
        _fresh(inventory)
        _git_revision(project, inventory)
        dataset = (path.parent / project['dataset']).resolve()
        dataset_hash = file_sha256(dataset)
        labels = _dataset(dataset, project, inventory)
        snapshot = retrieval_status(generation)
        if settings['mode'] in ('semantic', 'hybrid'):
            from .semantic import semantic_status
            semantic_status(generation)
        if _workspace_hashes(generation) != before or file_sha256(dataset) != dataset_hash:
            raise ValueError('Inputs changed while freezing the benchmark.')
        rows.append({**project, 'workspace': _relative(generation, destination.parent),
                     'dataset': _relative(dataset, destination.parent), 'dataset_sha256': dataset_hash,
                     'source_sha256': identity, 'workspace_files': before, 'snapshot': snapshot,
                     'label_review': labels['label_review'], 'questions': len(labels['cases'])})
    result = {'schema_version': 1, 'kind': 'frozen-quality-benchmark', 'name': data['name'],
              'scope': data['scope'], 'created_at': datetime.now(timezone.utc).isoformat(),
              'manifest_sha256': hashlib.sha256(original).hexdigest(), 'settings': settings,
              'targets': targets, 'projects': rows}
    if path.read_bytes() != original:
        raise ValueError('Manifest changed while freezing.')
    # Recheck earlier projects too; a later project's preparation must not invalidate the lock.
    _validate_lock(result, destination)
    with destination.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    return result


def _validate_lock(data, path, bindings=None):
    _plan(data, 'frozen-quality-benchmark')
    loaded = []
    for project in data['projects']:
        workspace = (bindings or {}).get(project['name'], path.parent / project['workspace'])
        generation = _generation(workspace)
        hashes = _workspace_hashes(generation)
        if not bindings and (generation != (path.parent / project['workspace']).resolve()
                             or hashes != project.get('workspace_files')):
            raise ValueError('Frozen workspace changed: ' + project['name'])
        dataset_path = (path.parent / project['dataset']).resolve()
        if file_sha256(dataset_path) != project.get('dataset_sha256'):
            raise ValueError('Frozen dataset changed: ' + project['name'])
        inventory = status(generation)
        if source_identity(inventory) != project.get('source_sha256'):
            raise ValueError('Candidate sources differ from the frozen corpus: ' + project['name'])
        _fresh(inventory)
        _git_revision(project, inventory)
        dataset = _dataset(dataset_path, project, inventory)
        snapshot = retrieval_status(generation)
        if not bindings and snapshot != project['snapshot']:
            raise ValueError('Frozen retrieval snapshot changed.')
        if dataset['label_review'] != project.get('label_review') or len(dataset['cases']) != project.get('questions'):
            raise ValueError('Frozen label metadata changed.')
        loaded.append((project, generation, inventory, dataset, hashes, snapshot))
    return loaded


def _indexed_groups(workspace, dataset, inventory):
    """Stream the index once; retain only Boolean passage matches, not the corpus."""
    labels = {}
    for case in dataset['cases']:
        for group in case.get('passage_groups', []):
            for passage in group:
                labels[_hash(passage)] = passage
    found = {key: False for key in labels}
    by_path = {}
    for key, passage in labels.items():
        by_path.setdefault(passage['path'], []).append(key)
    entries = {r['path']: r for r in inventory['entries']}
    checked = {}
    db, _ = _open(workspace)
    with closing(db):
        for raw, in db.execute('SELECT data FROM evidence'):
            record = json.loads(raw)
            for source in record.get('sources', []):
                if source.get('role') == 'reference_dependency':
                    continue
                for key in by_path.get(source.get('path'), []):
                    label = labels[key]
                    if found[key] or label['quote'] not in record.get('text', ''):
                        continue
                    if (type(source.get('start_line')) is not int or type(source.get('end_line')) is not int
                            or source['start_line'] > label['start_line'] or source['end_line'] < label['end_line']):
                        continue
                    dependencies = [s for s in record['sources'] if s is source or s.get('role') == 'reference_dependency']
                    for s in dependencies:
                        identity = (s.get('path'), s.get('sha256'))
                        if identity not in checked:
                            checked[identity] = inspect_source(inventory['project'], entries, s)['status'] == 'current'
                    found[key] = all(checked[(s.get('path'), s.get('sha256'))] for s in dependencies)
    return {case['id']: [any(found[_hash(p)] for p in group) for group in case.get('passage_groups', [])]
            for case in dataset['cases']}


def evaluate_quality_benchmark(frozen, *, workspaces=None, providers=None):
    path = Path(frozen).resolve()
    raw = path.read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    settings, targets = _plan(data, 'frozen-quality-benchmark')
    bindings = None
    if workspaces is not None:
        binding_path = Path(workspaces).resolve()
        values = _json(binding_path)
        if not isinstance(values, dict) or set(values) != {p['name'] for p in data['projects']}:
            raise ValueError('Candidate workspace bindings must cover exactly the frozen project names.')
        bindings = {name: (binding_path.parent / _text(value, 'Workspace binding')).resolve()
                    for name, value in values.items()}
    loaded = _validate_lock(data, path, bindings)
    code_hash = _code_hash()
    projects = []
    for project, generation, inventory, dataset, hashes, snapshot in loaded:
        rows = []
        provider = (providers or {}).get(project['name'])
        provider_error = None
        if settings['mode'] in ('semantic', 'hybrid'):
            try:
                from .semantic import semantic_status
                from .embeddings import FastEmbedProvider
                from .workspace import cache_workspace
                config = semantic_status(generation)['config']
                provider = provider or FastEmbedProvider(config['model'], cache_workspace(generation) / 'models')
                if provider.config != config:
                    raise ValueError('Embedding provider configuration differs from the index.')
            except (ValueError, OSError, ImportError) as exc:
                provider_error = str(exc)
        try:
            indexed = _indexed_groups(generation, dataset, inventory)
        except (ValueError, OSError, sqlite3.Error) as exc:
            provider_error = 'Index diagnostics failed: ' + str(exc)
            indexed = {c['id']: [False] * len(c.get('passage_groups', [])) for c in dataset['cases']}
        for case in dataset['cases']:
            row = {'id': case['id'], 'category': case['category'], 'question': case['question'],
                   'expected_outcome': case.get('expected_outcome', 'evidence'),
                   'negative_labels': len(case.get('negative_passages', [])),
                   'indexed_group_hits': indexed[case['id']]}
            try:
                if provider_error:
                    raise ValueError(provider_error)
                row.update(evaluate_passage_case(generation, case, inventory, provider=provider, **settings))
                row['status'] = 'completed'
            except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
                row.update(status='error', error=str(exc))
            row['failures'] = diagnose(row, indexed[case['id']])
            rows.append(row)
        projects.append({'name': project['name'], 'split': project['split'], 'repository': project['repository'],
                         'revision': project.get('revision'), 'label_review': dataset['label_review'],
                         'source_sha256': project['source_sha256'], 'dataset_sha256': project['dataset_sha256'],
                         'embedding_provider': provider.config if provider is not None else None,
                         'workspace_files': hashes, 'snapshot': snapshot, 'cases': rows,
                         'summary': summarize(rows), 'categories': {name: summarize([r for r in rows if r['category'] == name])
                             for name in sorted({r['category'] for r in rows})}})
    validation_errors = []
    try:
        after = _validate_lock(data, path, bindings)
        if any(a[4:] != b[4:] for a, b in zip(loaded, after)):
            raise ValueError('Candidate workspace changed during evaluation.')
        if path.read_bytes() != raw or _code_hash() != code_hash:
            raise ValueError('Frozen manifest or implementation changed during evaluation.')
    except (ValueError, OSError, sqlite3.Error) as exc:
        validation_errors.append(str(exc))
    rows = [r for project in projects for r in project['cases']]
    splits = {split: summarize([r for p in projects if p['split'] == split for r in p['cases']])
              for split in ('development', 'held-out')}
    for split, summary in splits.items():
        rates = [p['summary']['all_essential_packed']['rate'] for p in projects if p['split'] == split]
        measured = [r for r in rates if r is not None]
        summary['macro_project_essential_coverage'] = sum(measured) / len(measured) if measured else None
        summary['projects'] = len(rates)
    held = splits['held-out']
    eligible = (data['scope'] == 'independent-candidate' and len(projects) >= targets['minimum_projects']
                and len(rows) >= targets['minimum_questions'] and held['questions'] >= targets['minimum_held_out_questions']
                and all(p['label_review']['kind'] == 'human' for p in projects))
    gate = (not validation_errors and not any(r['status'] == 'error' for r in rows)
            and held['all_essential_packed']['rate'] is not None
            and held['all_essential_packed']['rate'] >= targets['essential_coverage']
            and all(not r['failures'] or set(r['failures']) <= {'retrieval_selection', 'context_packing', 'index_representation'}
                    for p in projects if p['split'] == 'held-out' for r in p['cases']))
    return {'schema_version': 1, 'kind': 'quality-benchmark-report', 'name': data['name'], 'scope': data['scope'],
            'status': 'invalid' if validation_errors else 'completed', 'validation_errors': validation_errors,
            'created_at': datetime.now(timezone.utc).isoformat(), 'lock_sha256': hashlib.sha256(raw).hexdigest(),
            'implementation_sha256': code_hash, 'settings': settings, 'targets': targets,
            'environment': _environment(),
            'projects': projects, 'summary': summarize(rows), 'splits': splits,
            'categories': {name: summarize([r for r in rows if r['category'] == name])
                           for name in sorted({r['category'] for r in rows})},
            'retrieval_gate_passed': gate, 'corpus_requirements_met': eligible,
            'answer_quality': {'status': 'not_evaluated', 'supported_claims': None, 'reviewed_claims': 0,
                               'target': targets['supported_claims']},
            'production_gate_passed': False,
            'scope_note': 'Extractive evidence delivery only. No generated claims or explanation abstention evaluated; '
                          'repository independence and review identity are declarations, not automatically proven.'}


def write_quality_report(report, output, markdown=None):
    """Keep each attempt, including failures, in new files rather than replacing a baseline."""
    from .quality_reporting import markdown_report
    output = Path(output).resolve()
    markdown = Path(markdown).resolve() if markdown else output.with_suffix('.md')
    if output == markdown or output.exists() or markdown.exists():
        raise ValueError('Choose distinct, new JSON and Markdown report files; previous runs are preserved.')
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, indent=2) + '\n')
    with markdown.open('x', encoding='utf-8') as stream:
        stream.write(markdown_report(report))
    return output, markdown
