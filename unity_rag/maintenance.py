"""Read-only retention preview and explicitly invoked, checkpointed refresh tickets."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from .discovery import is_link, scan, status
from .workspace import _atomic, _identity, _pipeline_hash, _read, changes, refresh, refresh_plan, resolve_workspace, writer_lock


def generation_inventory(workspace, retain=2):
    if type(retain) is not int or retain < 1:
        raise ValueError('Retain must be a positive integer.')
    root = Path(workspace).resolve()
    active = resolve_workspace(root)
    generations = root / 'generations'
    if not generations.is_dir() or is_link(generations):
        raise ValueError('Generation directory missing or linked.')
    pinned, uncertain = set(), False
    for plans in (root / 'useful-work', root / 'backlogs'):
        if not plans.exists():
            continue
        if is_link(plans):
            uncertain = True
        else:
            for run in plans.iterdir():
                if plans.name == 'backlogs' and run.name in ('authority.json', 'spend.sqlite3', '.refresh.lock') and run.is_file() and not is_link(run):
                    continue
                if is_link(run) or not run.is_dir():
                    uncertain = True
                    continue
                plan = run / 'plan.json'
                if not plan.exists() and any((run / name).exists() for name in ('state.json', 'spend.sqlite3')):
                    uncertain = True
                if plan.exists():
                    try:
                        if is_link(plan):
                            raise ValueError
                        pinned.add(_read(plan)['generation'])
                    except (OSError, ValueError, KeyError, TypeError):
                        uncertain = True
    rows = []
    for item in sorted(generations.iterdir()):
        if is_link(item) or not item.is_dir():
            uncertain = True
            continue
        manifest = item / 'complete.json'
        complete, time_key = False, ''
        try:
            if manifest.is_file() and not is_link(manifest):
                data = _read(manifest)
                complete = data.get('generation') == item.name
                time_key = data.get('sealed_at', data.get('completed_at', ''))
        except (OSError, ValueError, TypeError):
            uncertain = True
        total, unsafe = 0, False
        def raise_walk_error(error):
            raise error
        for base, dirs, files in os.walk(item, followlinks=False, onerror=raise_walk_error):
            for name in dirs + files:
                target = Path(base) / name
                if is_link(target):
                    unsafe = True
                elif target.is_file():
                    total += target.stat().st_size
        rows.append({'generation': item.name, 'bytes': total, 'complete': complete,
                     'active': item == active, 'pinned': item.name in pinned, 'unsafe_links': unsafe,
                     'completed_at': time_key})
    journal = _read(root / 'refresh-state.json') if (root / 'refresh-state.json').is_file() else {}
    keep = {r['generation'] for r in sorted((r for r in rows if r['complete']),
                                             key=lambda r: (r['completed_at'], r['generation']), reverse=True)[:retain]}
    if journal.get('state') == 'running':
        keep.add(journal.get('generation'))
    for row in rows:
        row['eligible_for_review'] = bool(not uncertain and row['complete'] and not row['active'] and not row['pinned']
                                          and not row['unsafe_links'] and row['generation'] not in keep)
    return {'generations': rows, 'pin_discovery_complete': not uncertain, 'retain_newest': retain,
            'deletion_available': False, 'note': 'Preview only; no files are deleted or automatically selected for deletion.'}


def _observe(workspace, threshold):
    root = Path(workspace).resolve()
    active = resolve_workspace(root)
    before = status(active)
    with tempfile.TemporaryDirectory(prefix='rag-ticket-') as directory:
        temp = Path(directory)
        config = temp / 'config.json'
        config.write_text(json.dumps(before['config']), encoding='utf-8')
        current = scan(before['project'], temp / 'scan', config)
    identity = hashlib.sha256(json.dumps(_identity(current), sort_keys=True).encode()).hexdigest()
    plan = refresh_plan(before, current, threshold=threshold)
    return {'generation': active.name, 'identity': identity, 'pipeline': _pipeline_hash(), 'plan': plan}


def observe_ticket(workspace, *, threshold=50, now=None, clock=None):
    """Record one observation; the caller must invoke again after a quiet period."""
    if now is not None and clock is not None:
        raise ValueError('Specify either an observation time or a clock, not both.')
    root = Path(workspace).resolve() / 'idle-refresh'
    if root.exists() and is_link(root):
        raise ValueError('Ticket directory must not be a link.')
    with writer_lock(root):
        path = root / 'ticket.json'
        if path.exists() and is_link(path):
            raise ValueError('Ticket must not be a link.')
        previous = _read(path) if path.exists() else None
        if previous and previous.get('state') in ('running', 'blocked'):
            raise ValueError('Ticket outcome needs manual review; no automatic retry.')
        observed = _observe(workspace, threshold)
        # Count quiet time from completion of the full content scan, not its
        # start. Slow scans must not consume the required quiet interval.
        now = now if now is not None else (clock() if clock is not None else datetime.now(timezone.utc))
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('Ticket observation must use a timezone-aware timestamp.')
        same = previous and all(previous.get(k) == observed[k] for k in ('generation', 'identity', 'pipeline'))
        first = previous['first_observed'] if same else now.isoformat()
        ticket = {**observed, 'state': 'pending' if observed['plan']['refresh_pending'] else 'below_threshold',
                  'first_observed': first, 'last_observed': now.isoformat(), 'automatic_refresh': False}
        _atomic(path, ticket)
        return ticket


class IdleGateClosed(ValueError):
    """The optional checked idle condition is not currently satisfied."""


def run_ticket(workspace, *, confirm_idle=False, quiet_seconds=120, now=None, runner=refresh, idle_check=None):
    """Run after two full observations and an explicit or independently checked idle assertion."""
    if not confirm_idle or type(quiet_seconds) is not int or quiet_seconds < 1:
        raise ValueError('Explicit idle confirmation and positive quiet period required.')
    root = Path(workspace).resolve() / 'idle-refresh'
    if not root.is_dir() or is_link(root):
        raise ValueError('No safe pending ticket.')
    now = now or datetime.now(timezone.utc)
    with writer_lock(root):
        path = root / 'ticket.json'
        if not path.is_file() or is_link(path):
            raise ValueError('No safe pending ticket.')
        ticket = _read(path)
        if ticket.get('state') != 'pending':
            raise ValueError('No runnable pending ticket; uncertain outcomes require review.')
        first = datetime.fromisoformat(ticket['first_observed'])
        if first.tzinfo is None or (now - first).total_seconds() < quiet_seconds:
            raise ValueError('Quiet interval has not elapsed.')
        observed = _observe(workspace, ticket['plan']['threshold'])
        if any(ticket[k] != observed[k] for k in ('generation', 'identity', 'pipeline')) or not observed['plan']['refresh_pending']:
            raise ValueError('Source or generation changed; observe again before running.')
        # Recheck the optional external idle condition *after* the second scan,
        # while holding the ticket lock and before marking the ticket running.
        if idle_check is not None and idle_check() is not True:
            raise IdleGateClosed('Idle condition is no longer satisfied.')
        ticket['state'] = 'running'
        _atomic(path, ticket)
        try:
            result = runner(workspace)
            ticket.update(state='complete', result=result)
            _atomic(path, ticket)
            return ticket
        except Exception:
            ticket['state'] = 'blocked'
            _atomic(path, ticket)
            raise
