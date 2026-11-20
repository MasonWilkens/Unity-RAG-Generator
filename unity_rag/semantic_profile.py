"""Bounded semantic-build experiments on copies; no downloads or hosted inference."""
from contextlib import closing
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import tracemalloc
import uuid


def process_memory(pid):
    """Process RSS and lifetime peak RSS; includes native allocations on Windows."""
    if os.name == 'nt':
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        handle = kernel.OpenProcess(0x410, False, pid)
        if not handle:
            return {}
        try:
            value = Counters()
            value.cb = ctypes.sizeof(value)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(value), value.cb):
                return {}
            return {'rss_bytes':value.WorkingSetSize, 'peak_rss_bytes':value.PeakWorkingSetSize}
        finally:
            kernel.CloseHandle(handle)
    status = Path('/proc') / str(pid) / 'status'
    if status.exists():
        values = {}
        for line in status.read_text().splitlines():
            key, _, value = line.partition(':')
            if key in ('VmRSS', 'VmHWM'):
                values['rss_bytes' if key == 'VmRSS' else 'peak_rss_bytes'] = int(value.split()[0]) * 1024
        return values
    return {}


def semantic_state(workspace):
    path = Path(workspace) / 'semantic.sqlite3'
    if not path.exists():
        return {'exists':False, 'active_sha256':None, 'cache_entries':0, 'vectors':0}
    # The isolated copy can recover its own SQLite rollback journal after a kill.
    with closing(sqlite3.connect(path)) as db, db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        active = db.execute('SELECT data FROM active WHERE id=1').fetchone() if 'active' in tables else None
        return {'exists':True, 'integrity':db.execute('PRAGMA integrity_check').fetchone()[0],
                'active_sha256':hashlib.sha256(active[0].encode()).hexdigest() if active else None,
                'cache_entries':db.execute('SELECT COUNT(*) FROM cache').fetchone()[0] if 'cache' in tables else 0,
                'vectors':db.execute('SELECT COUNT(*) FROM vectors').fetchone()[0] if 'vectors' in tables else 0}


class ControlledProvider:
    """Cheap 384-dimensional vectors measure orchestration, never model quality."""
    config = {'provider':'profile-control', 'model':'constant-384-v1'}

    def passages(self, texts):
        return [[1.0] * 384 for _ in texts]


def worker(args):
    from .embeddings import FastEmbedProvider
    from .semantic import build_semantic
    if args.trace_python:
        tracemalloc.start()
    started = time.perf_counter()
    with Path(args.events).open('w', encoding='utf-8') as stream:
        def record(phase, counts):
            data = {'phase':phase, 'seconds':time.perf_counter()-started,
                    **process_memory(os.getpid()), **counts}
            if args.trace_python:
                data['python_current_bytes'], data['python_peak_bytes'] = tracemalloc.get_traced_memory()
            stream.write(json.dumps(data)+'\n')
            stream.flush()
            if phase == 'batch_complete' and args.cancel_after_batches and counts['batch'] >= args.cancel_after_batches:
                raise KeyboardInterrupt('Controlled cancellation after a completed embedding batch.')
        class OfflineProvider(FastEmbedProvider):
            def _load(self):
                if self._model is None:
                    from fastembed import TextEmbedding
                    self._model = TextEmbedding(model_name=self.config['model'], cache_dir=self.cache_dir,
                                                threads=4, local_files_only=True)
                return self._model
        provider = ControlledProvider() if args.provider == 'control' else OfflineProvider(cache_dir=args.model_cache)
        before = semantic_state(args.workspace)
        try:
            result = build_semantic(args.workspace, provider=provider, observe=record, batch_size=args.batch_size)
            outcome = {'state':'complete', 'build':result}
        except KeyboardInterrupt as exc:
            outcome = {'state':'cancelled', 'error':str(exc)}
        except Exception as exc:
            outcome = {'state':'failed', 'error':str(exc)}
        record('worker_finished', {})
        outcome.update(before=before, after=semantic_state(args.workspace))
        Path(args.worker_result).write_text(json.dumps(outcome, indent=2), encoding='utf-8')


def profile_semantic(workspace, output, *, provider='control', cancel_after_batches=0,
                     seconds=60, memory_mib=768, trace_python=False, batch_size=4, resume_from=None):
    from .benchmark import file_sha256, _code_hash
    from .workspace import resolve_workspace, cache_workspace
    from .discovery import status
    from .embeddings import validate_batch_size
    validate_batch_size(batch_size)
    if (provider not in ('control','local') or cancel_after_batches < 0 or
            not 1 <= seconds <= 600 or not 64 <= memory_mib <= 4096):
        raise ValueError('Use control/local, nonnegative cancellation batches, 1–600 seconds and 64–4096 MiB.')
    source = resolve_workspace(workspace)
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Profile output already exists; choose a new report path.')
    # Temporary artifacts are retained, under a uniquely named experiment directory.
    base = output.parent / ('semantic-profile-' + uuid.uuid4().hex)
    isolated = base / 'workspace'
    project = Path(status(source)['project']).resolve()
    if (isolated.is_relative_to(source) or source.is_relative_to(isolated)
            or isolated.is_relative_to(project) or output.is_relative_to(project)):
        raise ValueError('Profile output must be outside the source generation and Unity project.')
    isolated.mkdir(parents=True)
    expected = {name:file_sha256(source/name) for name in ('inventory.sqlite3','semantic.sqlite3','complete.json')
                if (source/name).exists()}
    for name in ('inventory.sqlite3','semantic.sqlite3'):
        if name not in expected:
            continue
        with closing(sqlite3.connect((source/name).as_uri()+'?mode=ro',uri=True)) as src, src:
            with closing(sqlite3.connect(isolated/name)) as dst, dst:
                src.backup(dst)
    if resume_from:
        prior = Path(resume_from).resolve()/'embedding-checkpoints.sqlite3'
        with closing(sqlite3.connect(prior.as_uri()+'?mode=ro',uri=True)) as src, src:
            with closing(sqlite3.connect(isolated/'embedding-checkpoints.sqlite3')) as dst, dst:
                src.backup(dst)
    before = semantic_state(isolated)
    events, result_path = base/'events.jsonl', base/'worker.json'
    command = [sys.executable, '-m', 'unity_rag.semantic_profile', '--worker',
               '--workspace',str(isolated),'--provider',provider,'--events',str(events),
               '--worker-result',str(result_path),'--model-cache',str(cache_workspace(source)/'models'),
               '--cancel-after-batches',str(cancel_after_batches),'--batch-size',str(batch_size)]
    if trace_python:
        command.append('--trace-python')
    started, peak, reason = time.perf_counter(), 0, None
    with (base/'worker.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, stdout=log, stderr=log,
            env={**os.environ,'HF_HUB_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1'},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            while process.poll() is None:
                memory = process_memory(process.pid)
                if not memory:
                    reason = 'memory_monitor_unavailable'
                peak = max(peak, memory.get('peak_rss_bytes', 0))
                if peak > memory_mib * 1024**2:
                    reason = 'memory_limit'
                if time.perf_counter()-started > seconds:
                    reason = 'time_limit'
                if reason:
                    process.terminate()
                    break
                time.sleep(.1)
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    samples, incomplete_events = [], 0
    if events.exists():
        for line in events.read_text(encoding='utf-8').splitlines():
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                incomplete_events += 1  # Hard termination may interrupt the last event write.
    peak = max([peak] + [row.get('peak_rss_bytes',0) for row in samples])
    outcome = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else {'state':'terminated' if reason else 'worker_error'}
    unchanged = all(file_sha256(source/name)==value for name,value in expected.items())
    report = {'schema_version':1, 'source':str(source), 'source_files':expected,
              'source_unchanged':unchanged, 'implementation_sha256':_code_hash(),
              'provider':provider, 'trace_python':trace_python,
              'limits':{'seconds':seconds,'memory_mib':memory_mib,'cancel_after_batches':cancel_after_batches,'batch_size':batch_size},
              'termination_reason':reason,'exit_code':process.returncode,
              'elapsed_seconds':time.perf_counter()-started,'peak_rss_bytes':peak,
              'phases':samples,'incomplete_events':incomplete_events,'before':before,'after':semantic_state(isolated), 'outcome':outcome,
              'checkpoint_entries':_checkpoint_count(isolated), 'resumed_from':str(resume_from) if resume_from else None,
              'artifacts':str(base),
              'scope':'Isolated instrumented build; control vectors are not model-quality results. RSS includes native memory; Python tracing adds overhead. Limits are polled, not hard OS quotas.'}
    with output.open('x',encoding='utf-8') as stream:
        json.dump(report,stream,indent=2)
        stream.write('\n')
    return report


def _checkpoint_count(workspace):
    path = Path(workspace)/'embedding-checkpoints.sqlite3'
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path)) as db, db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'").fetchone()
        return db.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0] if exists else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True)
    parser.add_argument('--output')
    parser.add_argument('--provider',choices=('control','local'),default='control')
    parser.add_argument('--cancel-after-batches',type=int,default=0)
    parser.add_argument('--seconds',type=int,default=60)
    parser.add_argument('--memory-mib',type=int,default=768)
    parser.add_argument('--trace-python',action='store_true')
    parser.add_argument('--batch-size',type=int,default=4)
    parser.add_argument('--resume-from',help='Copy checkpoints from an earlier isolated profile workspace')
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--events',help=argparse.SUPPRESS)
    parser.add_argument('--worker-result',help=argparse.SUPPRESS)
    parser.add_argument('--model-cache',help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
    else:
        if not args.output:
            parser.error('--output is required')
        result = profile_semantic(args.workspace,args.output,provider=args.provider,
            cancel_after_batches=args.cancel_after_batches,seconds=args.seconds,
            memory_mib=args.memory_mib,trace_python=args.trace_python,batch_size=args.batch_size,resume_from=args.resume_from)
        print(json.dumps({k:result[k] for k in ('provider','elapsed_seconds','peak_rss_bytes','termination_reason','source_unchanged')}))


if __name__ == '__main__':
    main()
