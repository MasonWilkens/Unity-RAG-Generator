"""Run frozen retrieval datasets against separate project workspaces."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .discovery import status
from .evaluation import evaluate
from .workspace import resolve_workspace
from .retrieval import retrieval_status


def benchmark(manifest, *, mode='search', limit=10, expand=0):
    path = Path(manifest).resolve()
    raw = path.read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    projects = data.get('projects')
    if data.get('schema_version') != 1 or not isinstance(projects, list) or not projects:
        raise ValueError('Benchmark manifest requires schema_version 1 and nonempty projects.')
    settings = {'mode': mode, 'limit': limit, 'expand': expand}
    if data.get('frozen'):
        _validate_freeze(data, path, settings)
    seen_names, seen_projects, reports = set(), set(), []
    for item in projects:
        name, split = item.get('name'), item.get('split')
        if not isinstance(name, str) or not name.strip() or name in seen_names or split not in ('development', 'held-out'):
            raise ValueError('Each project needs a unique name and development or held-out split.')
        seen_names.add(name)
        workspace = resolve_workspace(path.parent / item['workspace'])
        dataset = (path.parent / item['dataset']).resolve()
        project = str(Path(status(workspace)['project']).resolve()).casefold()
        if project in seen_projects:
            raise ValueError('Each project may appear only once; project splits must be disjoint.')
        seen_projects.add(project)
        dataset_data = json.loads(dataset.read_text(encoding='utf-8-sig'))
        if dataset_data.get('split') != split:
            raise ValueError('Dataset split must match its manifest project split.')
        cases = dataset_data.get('questions', [])
        ids = [case.get('id') for case in cases]
        if not ids or any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise ValueError('Questions require unique nonempty IDs within each dataset.')
        report = evaluate(workspace, dataset, limit=limit, mode=mode, expand=expand)
        categories = {}
        for row in report['results']:
            categories.setdefault(row['category'], []).append(row['recall_at_k'])
        reports.append({'name': name, 'split': split, 'project': project, 'workspace': str(workspace),
                        'categories': {k: {'questions': len(v), 'recall_at_k': sum(v)/len(v)}
                                       for k, v in sorted(categories.items())}, 'report': report})
    summary = {}
    for split in ('development', 'held-out'):
        subset = [r for r in reports if r['split'] == split]
        if subset:
            summary[split] = {'projects': len(subset),
                              'macro_project_recall_at_k': sum(r['report']['recall_at_k'] for r in subset)/len(subset),
                              'questions': sum(r['report']['questions'] for r in subset)}
    if data.get('frozen'):
        _validate_freeze(data, path, settings)
    return {'schema_version': 1, 'frozen_inputs_verified': bool(data.get('frozen')),
            'retrieval_code_sha256': _code_hash(), 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
            'settings': {'mode': mode, 'limit': limit, 'expand': expand}, 'splits': summary,
            'projects': reports, 'scope': 'Retrieval labels only; not answer correctness or an independence guarantee.'}


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _code_hash():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _validate_freeze(data, path, settings):
    frozen = data['frozen']
    if frozen.get('version') != 1 or frozen.get('settings') != settings:
        raise ValueError('Frozen benchmark settings do not match; create a new version for different settings.')
    baseline = path.parent / frozen['baseline']
    if file_sha256(baseline) != frozen['baseline_sha256']:
        raise ValueError('Frozen benchmark baseline changed.')
    for item in data['projects']:
        workspace = (path.parent / item['workspace']).resolve()
        if resolve_workspace(workspace) != workspace or not (workspace / 'complete.json').is_file():
            raise ValueError('Frozen benchmark must pin a completed generation, not an active workspace.')
        for filename, expected in item['workspace_files'].items():
            if filename not in ('inventory.sqlite3', 'semantic.sqlite3', 'complete.json'):
                raise ValueError('Invalid frozen workspace filename.')
            if file_sha256(workspace / filename) != expected:
                raise ValueError('Frozen benchmark workspace changed: ' + item['name'])
        if not {'inventory.sqlite3', 'complete.json'} <= item['workspace_files'].keys():
            raise ValueError('Frozen benchmark is missing required workspace hashes.')
        if file_sha256(path.parent / item['dataset']) != item['dataset_sha256']:
            raise ValueError('Frozen benchmark dataset changed: ' + item['name'])
        if retrieval_status(workspace)['inputs'] != item['snapshot']:
            raise ValueError('Frozen benchmark retrieval snapshot changed.')


def freeze_benchmark(manifest, output, baseline):
    """Pin an existing baseline, without relabeling cases or running retrieval."""
    path, destination = Path(manifest).resolve(), Path(output).resolve()
    baseline = Path(baseline).resolve()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    prior = json.loads(baseline.read_text(encoding='utf-8-sig'))
    if data.get('schema_version') != 1 or not data.get('projects'):
        raise ValueError('Invalid benchmark manifest.')
    if prior.get('manifest_sha256') != file_sha256(path):
        raise ValueError('Baseline does not belong to this exact manifest.')
    def relative(target):
        try:
            return Path(os.path.relpath(target, destination.parent)).as_posix()
        except ValueError:  # Separate Windows drives.
            return str(target)
    rows = []
    for item in data['projects']:
        workspace = resolve_workspace(path.parent / item['workspace'])
        dataset = (path.parent / item['dataset']).resolve()
        reports = [r for r in prior['projects'] if r['name'] == item['name']]
        if len(reports) != 1:
            raise ValueError('Baseline project names do not match.')
        report = reports[0]['report']
        if (file_sha256(dataset) != report['dataset_sha256'] or
                retrieval_status(workspace)['inputs'] != report['snapshot']):
            raise ValueError('Baseline inputs changed; select its original generation and dataset.')
        rows.append({**item, 'workspace': relative(workspace), 'dataset': relative(dataset),
                     'dataset_sha256': file_sha256(dataset), 'snapshot': report['snapshot'],
                     'workspace_files': {name: file_sha256(workspace / name)
                         for name in ('inventory.sqlite3', 'complete.json', 'semantic.sqlite3')
                         if (workspace / name).exists()}})
    result = {'schema_version': 1, 'projects': rows, 'frozen': {
        'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
        'settings': prior['settings'], 'baseline': relative(baseline),
        'baseline_sha256': file_sha256(baseline),
        'scope': 'Snapshot regression benchmark. Freezing does not create independent labels or prove current-source freshness.'}}
    _validate_freeze(result, destination, prior['settings'])
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    return result
