"""Explicit ticket-based watcher; never installs or starts a background service."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import time

from .discovery import is_link
from .maintenance import IdleGateClosed, observe_ticket, run_ticket
from .workspace import refresh, writer_lock


def unity_editors_closed():
    """Windows process-name check only; no project ownership or input-idle claim."""
    if os.name != 'nt':
        raise ValueError('The unity-closed idle policy is available only on Windows.')
    script = ("$ErrorActionPreference='Stop'; "
              "$found=@(Get-Process | Where-Object { $_.ProcessName -ieq 'Unity' }); "
              "if ($found.Count -eq 0) { 'closed' } else { 'open' }")
    try:
        result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, text=True, timeout=15, check=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('Cannot verify Unity process names; idle watch stopped.') from exc
    output = result.stdout.strip()
    if output not in ('closed', 'open'):
        raise ValueError('Cannot verify Unity process names; idle watch stopped.')
    return output == 'closed'


def watch_tickets(workspace, *, threshold=50, quiet_seconds=120, interval=300, iterations=0,
                  idle_check=None, runner=None, clock=None, sleeper=None):
    """Poll locked tickets and run only when the explicit idle gate is satisfied.

    The Unity-name gate checks only immediately before refresh. It cannot detect
    changes reverted between observations or another editor starting afterward.
    Refresh still verifies the final source snapshot before promotion.
    """
    if type(threshold) is not int or threshold < 1:
        raise ValueError('Threshold must be a positive integer.')
    if type(quiet_seconds) is not int or quiet_seconds < 1:
        raise ValueError('Quiet period must be a positive integer.')
    if type(interval) is not int or interval < 60 or type(iterations) is not int or iterations < 0:
        raise ValueError('Ticket watch interval must be at least 60 seconds and iterations cannot be negative.')
    policy = 'unity-closed' if idle_check is None else 'custom'
    idle_check = idle_check or unity_editors_closed
    runner = runner or refresh
    clock = clock or (lambda: datetime.now(timezone.utc))
    sleeper = sleeper or time.sleep
    root = Path(workspace).resolve() / 'idle-watch'
    if root.exists() and is_link(root):
        raise ValueError('Watcher lock directory must not be a link.')
    with writer_lock(root):
        count = 0
        while True:
            ticket = observe_ticket(workspace, threshold=threshold, clock=clock)
            now = datetime.fromisoformat(ticket['last_observed'])
            event = {'state': ticket['state'], 'generation': ticket['generation'],
                     'indexed_changed_files': ticket['plan']['indexed_changed_files'],
                     'threshold': threshold, 'idle_policy': policy, 'action': 'observe'}
            if ticket['state'] == 'pending':
                first = datetime.fromisoformat(ticket['first_observed'])
                if first.tzinfo is None or now.tzinfo is None:
                    raise ValueError('Ticket observation must use timezone-aware timestamps.')
                if (now - first).total_seconds() >= quiet_seconds:
                    if idle_check() is True:
                        try:
                            completed = run_ticket(workspace, confirm_idle=True, quiet_seconds=quiet_seconds,
                                                   now=now, runner=runner, idle_check=idle_check)
                        except IdleGateClosed:
                            event['action'] = 'not_idle'
                        else:
                            event['action'] = 'refreshed'
                            event['state'] = completed['state']
                            event['result'] = completed['result']
                    else:
                        event['action'] = 'not_idle'
                else:
                    event['action'] = 'wait_quiet'
            yield event
            count += 1
            if iterations and count >= iterations:
                return
            sleeper(interval)
