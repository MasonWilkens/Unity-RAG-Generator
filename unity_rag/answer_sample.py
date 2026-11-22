"""Freeze and run a local answer sample; review remains an independent step."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.request import Request, build_opener, ProxyHandler

from .answer_acceptance import POLICY, _read, acceptance_template, review_acceptance, write_review_artifact
from .answer_evaluation import evaluate_answers, provider_settings
from .benchmark import _code_hash, file_sha256
from .discovery import status
from .generation import NoRedirect, OllamaProvider
from .quality_benchmark import _fresh, _generation, _git_revision, _workspace_hashes, source_identity
from .retrieval import retrieval_status

SETTINGS = {'mode', 'limit', 'expand', 'context_chars', 'followups', 'context_tokens',
            'token_encoding', 'model_context_tokens'}
PROVIDER = {'model', 'endpoint', 'timeout', 'thinking', 'max_output_tokens', 'seed'}


def _implementation():
    root = Path(__file__).parent / 'roslyn'
    return {'python': _code_hash(), 'analyzer': {name: file_sha256(root / name) for name in
            ('Program.cs', 'UnityRag.Analyzer.csproj', 'NuGet.Config')}}


def _model_digest(provider):
    # Only loopback endpoints are accepted by _inputs; ignore proxy variables.
    request = Request(provider.endpoint + '/api/tags', headers={'Accept': 'application/json'})
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError('Model identity response exceeds the size limit.')
    try:
        matches = [m['digest'] for m in json.loads(raw)['models'] if m['name'] == provider.config['model']]
        if len(matches) != 1 or not re.fullmatch('[0-9a-f]{64}', matches[0]):
            raise ValueError
        return matches[0]
    except (KeyError, TypeError, ValueError):
        raise ValueError('Cannot verify the exact installed model digest.') from None


def _inputs(manifest, workspace):
    path = Path(manifest).resolve()
    data, digest = _read(path)
    if (not isinstance(data, dict) or data.get('schema_version') != 1
            or data.get('kind') != 'answer-sample' or data.get('scope') != 'fresh-candidate'
            or data.get('previously_used_for_tuning') is not False
            or data.get('policy') != POLICY):
        raise ValueError('Expected a fresh-candidate answer sample with the fixed acceptance policy.')
    settings, provider_options = data.get('settings'), data.get('provider')
    if (not isinstance(settings, dict) or set(settings) != SETTINGS
            or settings['mode'] not in ('search', 'exact') or type(settings['followups']) is not int or settings['followups'] != 0
            or not isinstance(provider_options, dict) or set(provider_options) != PROVIDER):
        raise ValueError('Sample needs explicit lexical settings, no followups and a complete local provider configuration.')
    for key, low, high in (('limit', 1, 100), ('expand', 0, 20), ('context_chars', 1000, 100000)):
        if type(settings[key]) is not int or not low <= settings[key] <= high:
            raise ValueError('Invalid sample setting: ' + key)
    if (settings['context_tokens'] is not None or settings['model_context_tokens'] is not None
            or settings['token_encoding'] != 'utf8-bytes'):
        raise ValueError('This sample runner uses explicit character budgets.')
    provider = OllamaProvider(**provider_options, allow_remote=False)
    review = data.get('source_review')
    if (not isinstance(review, dict) or review.get('kind') != 'agent'
            or not all(isinstance(review.get(k), str) and review[k].strip() for k in ('reviewer', 'rationale'))
            or not isinstance(data.get('dataset'), str) or not data['dataset']):
        raise ValueError('This candidate needs explicit agent-authored source guidance and a dataset path.')
    dataset = (path.parent / data['dataset']).resolve()
    questions, dataset_hash = _read(dataset)
    if dataset_hash != data.get('dataset_sha256'):
        raise ValueError('Frozen question dataset changed.')
    cases = questions.get('questions') if isinstance(questions, dict) else None
    if (not isinstance(questions, dict) or questions.get('schema_version') != 1 or not isinstance(cases, list) or not 1 <= len(cases) <= 1000
            or type(data.get('questions')) is not int or len(cases) != data['questions']):
        raise ValueError('Sample question counts or schema are invalid.')
    ids = set()
    generation = _generation(workspace)
    workspace_before = _workspace_hashes(generation)
    inventory = status(generation)
    _git_revision(data, inventory)
    _fresh(inventory)
    root = Path(inventory['project']).resolve()
    for case in cases:
        if (not isinstance(case, dict) or not all(isinstance(case.get(k), str) and case[k].strip()
                for k in ('id', 'question', 'category', 'human_review')) or case['id'] in ids
                or not isinstance(case.get('references'), list)):
            raise ValueError('Sample questions need unique IDs, categories, review guidance and source references.')
        ids.add(case['id'])
        if case.get('mode', settings['mode']) not in ('search', 'exact'):
            raise ValueError('Sample question has an unsupported retrieval mode.')
        for reference in case['references']:
            if (not isinstance(reference, dict) or not isinstance(reference.get('path'), str)
                    or not isinstance(reference.get('sha256'), str)
                    or not isinstance(reference.get('quote'), str)):
                raise ValueError('Invalid source reference.')
            source = (root / reference['path']).resolve()
            if not source.is_relative_to(root) or file_sha256(source) != reference['sha256']:
                raise ValueError('Source reference changed or leaves the project.')
            lines = source.read_text(encoding='utf-8-sig').splitlines()
            start, end = reference.get('start_line'), reference.get('end_line')
            if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines)
                    or not reference.get('quote') or '\n'.join(lines[start-1:end]) != reference['quote']):
                raise ValueError('Source reference does not match its exact line range.')
    if _model_digest(provider) != data.get('model_digest'):
        raise ValueError('Installed model differs from the frozen digest.')
    _fresh(inventory)
    _git_revision(data, inventory)
    binding = {'manifest_sha256': digest, 'dataset_sha256': dataset_hash,
               'implementation': _implementation(), 'source_sha256': source_identity(inventory),
               'workspace_files': _workspace_hashes(generation), 'snapshot': retrieval_status(generation),
               'provider_settings': provider_settings(provider), 'model_digest': data['model_digest']}
    if (file_sha256(path) != digest or file_sha256(dataset) != dataset_hash
            or binding['workspace_files'] != workspace_before):
        raise ValueError('Sample inputs changed during validation.')
    return data, dataset, generation, provider, binding


def prepare_sample(manifest, workspace, output):
    """Write a new run lock before any model generation."""
    if Path(output).exists():
        raise FileExistsError('Refusing to overwrite an existing sample lock.')
    data, dataset, generation, provider, binding = _inputs(manifest, workspace)
    lock = {'schema_version': 1, 'kind': 'frozen-answer-sample',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'manifest': str(Path(manifest).resolve()), 'workspace': str(generation), 'binding': binding}
    write_review_artifact(lock, output)
    return lock


def _locked(path):
    lock, digest = _read(path)
    if not isinstance(lock, dict) or lock.get('schema_version') != 1 or lock.get('kind') != 'frozen-answer-sample':
        raise ValueError('Expected a frozen answer sample lock.')
    data, dataset, generation, provider, binding = _inputs(lock['manifest'], lock['workspace'])
    if binding != lock.get('binding'):
        raise ValueError('Frozen sample inputs or implementation changed; no answers generated.')
    if file_sha256(path) != digest:
        raise ValueError('Sample lock changed during validation.')
    return lock, digest, data, dataset, generation, provider


def run_sample(lock_path, output):
    """Retain all outputs and an unfilled review; never silently retry a run."""
    destination = Path(output)
    if destination.exists():
        raise FileExistsError('Use a new sample output directory; earlier runs are never overwritten.')
    lock, lock_hash, data, dataset, generation, provider = _locked(lock_path)
    destination.mkdir(parents=True, exist_ok=False)
    # Save the run identity even if the process is interrupted before a report.
    write_review_artifact({'lock_sha256': lock_hash, 'manifest_sha256': lock['binding']['manifest_sha256'],
                           'state': 'started', 'automatic_retries': 0}, destination / 'started.json')
    report = evaluate_answers(generation, dataset, provider, review=False, style='explain', **data['settings'])
    try:
        _locked(lock_path)
        if file_sha256(lock_path) != lock_hash:
            raise ValueError('Sample lock changed during generation.')
    except (ValueError, OSError, sqlite3.Error) as exc:
        report['status'] = 'invalid'
        report['validation_errors'].append(str(exc))
    for row in report['results']:
        row.get('answer', {}).pop('project', None)
    report.update(sample={'lock_sha256': lock_hash, 'manifest_sha256': lock['binding']['manifest_sha256'],
                          'repository': data['repository'], 'revision': data['revision'],
                          'model_digest': data['model_digest'], 'source_review': data['source_review']},
                  redactions=['results[*].answer.project'])
    report_path, review_path = destination / 'report.json', destination / 'review.json'
    write_review_artifact(report, report_path)
    write_review_artifact(acceptance_template(report_path), review_path)
    pending = review_acceptance(report_path, review_path)
    write_review_artifact(pending, destination / 'pending.json')
    return {'status': report['status'], 'questions': report['questions'], 'valid_outputs': report['valid_outputs'],
            'review_status': pending['review_status'], 'accepted': pending['accepted'],
            'production_gate_passed': False}
