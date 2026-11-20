"""Reusable, explicitly budgeted source-backed draft batches; no auto-acceptance."""
from contextlib import closing
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from .discovery import is_link, status
from .openai_generation import OpenAIProvider, SpendLedger, _nanodollars
from .workspace import _atomic, resolve_workspace, writer_lock
from . import work_queue as q


def _deadline(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (AttributeError, ValueError, TypeError):
        raise ValueError('A timezone-qualified credit and key deadline is required.') from None


def _root(workspace, name):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
        raise ValueError('Backlog name must be a safe identifier.')
    root = Path(workspace).resolve() / 'backlogs' / name
    for part in (root.parent, root):
        if part.exists() and is_link(part):
            raise ValueError('Backlog directories must not be links.')
    return root


def prepare(workspace, name, manifest_path, *, budget_usd, credit_stop, key_stop):
    """Freeze independently reviewed anchors; do not load keys or call a model."""
    _nanodollars(budget_usd)
    stop = min(_deadline(credit_stop), _deadline(key_stop))
    if datetime.now(timezone.utc) >= stop:
        raise ValueError('Backlog deadline has passed.')
    active = resolve_workspace(workspace)
    inventory = status(active)
    root = _root(workspace, name)
    if root.is_relative_to(Path(inventory['project']).resolve()):
        raise ValueError('Backlog must be outside the source project.')
    manifest = q.read_json(manifest_path)
    tasks = q.snapshot_tasks(manifest, inventory['project'], {e['path']:e for e in inventory['entries']}, active.name)
    plan = {'engine_version':q.ENGINE_VERSION, 'task_version':manifest['task_version'], 'manifest_hash':q.digest(manifest),
            'project':inventory['project'], 'workspace':str(Path(workspace).resolve()), 'generation':active.name,
            'budget_usd':str(budget_usd), 'credit_stop':credit_stop, 'key_stop':key_stop,
            'service_tier':'default', 'items':tasks}
    with writer_lock(root.parent):
        authority, ledger = root.parent/'authority.json', root.parent/'spend.sqlite3'
        if authority.exists() or ledger.exists():
            _verify_authority(root.parent, str(budget_usd))
        else:
            if any(p.is_dir() for p in root.parent.iterdir()):
                raise ValueError('Missing shared ledger for existing backlogs; review required.')
            _atomic(authority, {'budget_nanodollars':_nanodollars(budget_usd)})
            SpendLedger(ledger, str(budget_usd))
        root.mkdir(exist_ok=True)
        plan_file = root / 'plan.json'
        if plan_file.exists():
            old, _ = _load(root)
            if old != plan:
                raise ValueError('Frozen backlog differs; do not replace its budget or ledger.')
        else:
            if (root / 'state.json').exists():
                raise ValueError('Incomplete existing backlog requires review.')
            _atomic(plan_file, plan)
            _atomic(root / 'state.json', {'plan_hash':q.digest(plan), 'phases':{}, 'items':{}})
    return {'directory':str(root), 'items':len(tasks), 'budget_usd':str(budget_usd), 'api_calls':0}


def _verify_authority(parent, budget):
    authority, ledger = parent/'authority.json', parent/'spend.sqlite3'
    if any(not p.is_file() or is_link(p) for p in (authority, ledger)):
        raise ValueError('Original shared backlog authority and ledger are required.')
    if q.read_json(authority).get('budget_nanodollars') != _nanodollars(budget):
        raise ValueError('The shared backlog budget is fixed; no alternate cap.')
    try:
        with closing(sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro', uri=True)) as db, db:
            db.execute('BEGIN')
            cap, reserved, blocked = db.execute('SELECT cap,reserved,blocked FROM budget WHERE id=1').fetchone()
            total = db.execute('SELECT COALESCE(SUM(reserved),0) FROM attempts').fetchone()[0]
        if cap != _nanodollars(budget) or reserved != total or not 0 <= reserved <= cap or blocked not in (0,1):
            raise ValueError
    except (sqlite3.Error, TypeError, ValueError):
        raise ValueError('Shared backlog ledger is corrupt; no budget recreation.') from None


def _load(root):
    for name in ('plan.json','state.json'):
        path = root / name
        if not path.is_file() or is_link(path):
            raise ValueError('Original backlog plan, state and ledger are required.')
    plan, state = q.read_json(root/'plan.json'), q.read_json(root/'state.json')
    _verify_authority(root.parent, plan['budget_usd'])
    if plan.get('engine_version') != q.ENGINE_VERSION or state.get('plan_hash') != q.digest(plan):
        raise ValueError('Frozen backlog changed; review required.')
    return plan, state


def run(workspace, name, *, api_key_file, allow_remote=False, provider_factory=OpenAIProvider):
    if not allow_remote:
        raise ValueError('Explicit remote-transfer authorization is required.')
    root = _root(workspace, name)
    with writer_lock(root):
        plan, state = _load(root)
        q.check_fresh(plan)
        if state.get('finished'):
            return state
        deadline = min(_deadline(plan['credit_stop']), _deadline(plan['key_stop']))
        if datetime.now(timezone.utc) >= deadline:
            raise ValueError('Backlog deadline has passed; no request sent.')
        provider = provider_factory(api_key_file=api_key_file, ledger_path=root.parent/'spend.sqlite3',
                                    budget_usd=plan['budget_usd'], allow_remote=allow_remote,
                                    service_tier='default', max_output_tokens=q.MAX_OUTPUT, max_calls=2, timeout=60)
        draft = q.execute_phase(root, state, 'generation', provider, q.messages_for(plan), q.GEN_SCHEMA, deadline=deadline)
        expected = {t['id']:t for t in plan['items']}
        ids = [x['id'] for x in draft['items']]
        if len(ids) != len(set(ids)) or set(ids) != set(expected):
            raise ValueError('Generated IDs differ from frozen tasks.')
        candidates = {x['id']:q.materialize(expected[x['id']], x) for x in draft['items']}
        for name, artifact in candidates.items():
            state['items'][name] = {'local_errors':q.validate_artifact(expected[name], artifact), 'status':'needs_human_review'}
        _atomic(root/'state.json', state)
        q.check_fresh(plan)
        reviews = q.execute_phase(root, state, 'review', provider, q.messages_for(plan, list(candidates.values())), q.REVIEW_SCHEMA, deadline=deadline)
        review_ids = [x['id'] for x in reviews['items']]
        if len(review_ids) != len(set(review_ids)) or set(review_ids) != set(expected):
            raise ValueError('Review IDs differ from frozen tasks.')
        q.check_fresh(plan)
        for review in reviews['items']:
            state['items'][review['id']]['api_review'] = review
            if set(review['checked_fact_ids']) != {f['id'] for f in expected[review['id']]['required_facts']}:
                state['items'][review['id']]['local_errors'].append('review_fact_coverage_incomplete')
        _atomic(root/'drafts.json', {'items':list(candidates.values())})
        state['finished'] = True
        _atomic(root/'state.json', state)
        return state


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True)
    parser.add_argument('--name',required=True)
    actions=parser.add_subparsers(dest='action',required=True)
    create=actions.add_parser('prepare')
    create.add_argument('--manifest',required=True)
    create.add_argument('--budget-usd',required=True)
    create.add_argument('--credit-stop',required=True)
    create.add_argument('--key-stop',required=True)
    live=actions.add_parser('run')
    live.add_argument('--api-key-file',required=True)
    live.add_argument('--allow-remote',action='store_true')
    args=parser.parse_args()
    try:
        value = (prepare(args.workspace,args.name,args.manifest,budget_usd=args.budget_usd,credit_stop=args.credit_stop,key_stop=args.key_stop)
                 if args.action=='prepare' else run(args.workspace,args.name,api_key_file=args.api_key_file,allow_remote=args.allow_remote))
        print(json.dumps(value,indent=2))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'status':'blocked','reason':q.KEY_PATTERN.sub('[redacted]',str(exc))[:500]}))
        raise SystemExit(1) from None


if __name__=='__main__':
    main()
