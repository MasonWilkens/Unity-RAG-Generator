"""Content-addressed embeddings and deterministic local cosine retrieval."""
from .workspace import resolve_workspace, require_writable, cache_workspace
import json
import math
import hashlib
import tempfile
import time
from contextlib import closing
import sqlite3
from pathlib import Path

from .asset_map import fingerprint
from .embeddings import FastEmbedProvider, DEFAULT_MODEL, DEFAULT_BATCH_SIZE, validate_batch_size

SEMANTIC_KINDS = ('asset', 'asset_object', 'code_member', 'code_type',
                  'documentation', 'prefab_override', 'shader_source', 'ui_source', 'configuration_source')


def normalized(vector, dimension=None):
    values = [float(v) for v in vector]
    if not values or (dimension is not None and len(values) != dimension):
        raise ValueError("Embedding dimension mismatch or empty vector.")
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Embedding contains non-finite values.")
    norm = math.sqrt(sum(v*v for v in values))
    if norm == 0:
        raise ValueError("Embedding has zero length.")
    return [v/norm for v in values]


def passage_windows(item):
    # Multiple bounded windows prevent the model silently dropping long methods.
    # Original evidence and citations remain intact; these windows are ranking inputs only.
    from .retrieval import terms
    heading = f"{item['kind']} {' '.join(terms(item['title']))}\n"
    body = item['text']
    for start in range(0, max(1, len(body)), 800):
        yield heading + body[start:start+1000], start, min(len(body), start + 1000)


def passages(item):
    for text, _, _ in passage_windows(item):
        yield text


class PassageScores(dict):
    """Compatible score mapping with winning passage identities and diagnostics."""
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.winners = {}
        self.profile = {}

    def passage(self, item):
        # Existing immutable indexes need no migration. Recover offsets only for
        # selected chunks, and verify the exact embedded input by its hash.
        if (self.config.get('passage_version'), self.config.get('window_chars'),
                self.config.get('stride_chars')) != (1, 1000, 800):
            return None
        digest = self.winners.get(item['id'])
        for text, start, end in passage_windows(item):
            if fingerprint(text) == digest:
                return {'hash': digest, 'start_char': start, 'end_char': end}
        return None


def _validated_vector(raw, dimension=None):
    values = json.loads(raw)
    if not isinstance(values,list) or any(type(v) not in (int,float) for v in values):
        raise ValueError('Cached embedding must be a numeric list.')
    normalized(values, dimension)  # Validate without renormalizing cached values.
    if not math.isclose(sum(float(v)*float(v) for v in values), 1.0, rel_tol=1e-5, abs_tol=1e-5):
        raise ValueError('Cached embedding is not normalized.')
    return len(values)


def build_semantic(workspace, provider=None, model=DEFAULT_MODEL, progress=None,
                   include_kinds=None, observe=None, batch_size=DEFAULT_BATCH_SIZE):
    validate_batch_size(batch_size)
    if include_kinds is not None:
        if (not isinstance(include_kinds, list) or not include_kinds or
                any(not isinstance(kind, str) or kind not in SEMANTIC_KINDS for kind in include_kinds)):
            raise ValueError('Semantic kinds must be a nonempty list of recognized evidence kinds, or None for all.')
        include_kinds = sorted(set(include_kinds))
    workspace = require_writable(workspace)
    from .workspace import writer_lock
    # Different lock from refresh: refresh already owns the workspace writer lock.
    with writer_lock(cache_workspace(workspace), filename='.semantic.lock'):
        return _build_semantic(workspace, provider, model, progress, include_kinds, observe, batch_size)


def _build_semantic(workspace, provider, model, progress, include_kinds, observe, batch_size):
    from .retrieval import _open, retrieval_status
    def event(phase, **counts):
        if observe is not None:
            observe(phase, counts)
    event('start', batch_size=batch_size)
    provider = provider or FastEmbedProvider(model, cache_workspace(workspace)/'models', batch_size=batch_size)
    if isinstance(provider, FastEmbedProvider):
        provider.batch_size = batch_size
    config = {**provider.config, 'passage_version': 1, 'window_chars': 1000, 'stride_chars': 800}
    config_id = fingerprint(config)
    cache_path = cache_workspace(workspace)/'embedding-checkpoints.sqlite3'
    with tempfile.NamedTemporaryFile(prefix='semantic-stage-', suffix='.sqlite3', dir=workspace, delete=False) as file:
        stage_path = Path(file.name)
    try:
        with closing(sqlite3.connect(stage_path)) as stage, \
                closing(sqlite3.connect(Path(workspace)/'semantic.sqlite3')) as store, \
                closing(sqlite3.connect(cache_path)) as checkpoint:
            for connection in (stage, store, checkpoint):
                connection.execute('PRAGMA cache_size=-2048')
                connection.execute('PRAGMA temp_store=FILE')
            stage.executescript('CREATE TABLE passages (hash TEXT PRIMARY KEY, text TEXT, vector TEXT);'
                                'CREATE TABLE chunks (chunk TEXT, hash TEXT, PRIMARY KEY(chunk,hash));'
                                'CREATE INDEX missing_passages ON passages(hash) WHERE vector IS NULL;')
            store.executescript('CREATE TABLE IF NOT EXISTS cache (config TEXT, hash TEXT, vector TEXT, PRIMARY KEY(config,hash));'
                                'CREATE TABLE IF NOT EXISTS active (id INTEGER PRIMARY KEY, data TEXT);'
                                'CREATE TABLE IF NOT EXISTS vectors (chunk TEXT, hash TEXT, PRIMARY KEY(chunk,hash));')
            checkpoint.execute('PRAGMA synchronous=FULL')
            checkpoint.execute('CREATE TABLE IF NOT EXISTS embeddings '
                               '(config TEXT, hash TEXT, vector TEXT, checksum TEXT, dimension INTEGER, PRIMARY KEY(config,hash))')
            checkpoint.commit()
            db, report = _open(workspace)
            chunks = passage_count = 0
            try:
                total_chunks = db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]
                sql, parameters = 'SELECT data FROM evidence', []
                if include_kinds is not None:
                    sql += ' WHERE kind IN (' + ','.join('?' for _ in include_kinds) + ')'
                    parameters = include_kinds
                for (raw,) in db.execute(sql + ' ORDER BY id', parameters):
                    item = json.loads(raw)
                    chunks += 1
                    for text in passages(item):
                        digest = fingerprint(text)
                        stage.execute('INSERT OR IGNORE INTO passages VALUES (?,?,NULL)', (digest,text))
                        stage.execute('INSERT OR IGNORE INTO chunks VALUES (?,?)', (item['id'],digest))
                        passage_count += 1
                stage.commit()
            finally:
                db.close()
            event('evidence_loaded', chunks=chunks, total_chunks=total_chunks)
            unique = stage.execute('SELECT COUNT(*) FROM passages').fetchone()[0]
            event('passages_prepared', passages=passage_count, unique_passages=unique)
            dimension, hits, invalid = None, 0, 0
            for (digest,) in stage.execute('SELECT hash FROM passages ORDER BY hash'):
                row = checkpoint.execute('SELECT vector,checksum,dimension FROM embeddings WHERE config=? AND hash=?',
                                         (config_id,digest)).fetchone()
                raw = None
                if row:
                    try:
                        if not isinstance(row[0],str) or hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
                            raise ValueError('Checkpoint checksum mismatch.')
                        size = _validated_vector(row[0],dimension)
                        if size != row[2]:
                            raise ValueError('Checkpoint dimension mismatch.')
                        raw = row[0]
                    except (ValueError, TypeError, OverflowError):
                        invalid += 1
                        checkpoint.execute('DELETE FROM embeddings WHERE config=? AND hash=?',(config_id,digest))
                if raw is None:
                    legacy = store.execute('SELECT vector FROM cache WHERE config=? AND hash=?',(config_id,digest)).fetchone()
                    if legacy:
                        try:
                            size = _validated_vector(legacy[0],dimension)
                            raw = legacy[0]
                        except (ValueError, TypeError, OverflowError):
                            invalid += 1
                if raw is not None:
                    dimension = size
                    stage.execute('UPDATE passages SET vector=? WHERE hash=?',(raw,digest))
                    checkpoint.execute('INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?)',
                                       (config_id,digest,raw,hashlib.sha256(raw.encode()).hexdigest(),size))
                    hits += 1
                if (hits + invalid) % 256 == 0:
                    checkpoint.commit()
            checkpoint.commit()
            stage.commit()
            missing = unique - hits
            event('cache_loaded', cache_hits=hits, missing=missing, invalid_cache_entries=invalid)
            embedded, batch_number = 0, 0
            while True:
                batch = stage.execute('SELECT hash,text FROM passages WHERE vector IS NULL ORDER BY hash LIMIT ?',
                                      (batch_size,)).fetchall()
                if not batch:
                    break
                batch_number += 1
                event('batch_start', batch=batch_number, embedded=embedded, total=missing)
                vectors = list(provider.passages([row[1] for row in batch]))
                if len(vectors) != len(batch):
                    raise ValueError('Embedding provider returned the wrong number of vectors.')
                validated = []
                for (digest,_), vector in zip(batch,vectors):
                    vector = normalized(vector,dimension)
                    dimension = len(vector)
                    raw = json.dumps(vector)
                    validated.append((config_id,digest,raw,hashlib.sha256(raw.encode()).hexdigest(),dimension))
                # A whole validated batch is durable before reporting completion.
                with checkpoint:
                    checkpoint.executemany('INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?)',validated)
                with stage:
                    stage.executemany('UPDATE passages SET vector=? WHERE hash=?',[(r[2],r[1]) for r in validated])
                embedded += len(batch)
                event('batch_complete', batch=batch_number, embedded=embedded, total=missing)
                if progress:
                    progress(embedded,missing)
            event('verify', embedded=embedded)
            if retrieval_status(workspace) != report:
                raise ValueError('Retrieval changed during embedding. Retry build-semantic.')
            result = {'schema_version': 1, 'retrieval':report, 'config':config, 'config_id':config_id,
                      'dimension':dimension, 'chunks':chunks, 'passages':passage_count,
                      'embedded':embedded, 'cache_hits':hits, 'batch_size':batch_size,
                      'checkpoint_version':1, 'invalid_cache_entries':invalid,
                      'include_kinds':include_kinds, 'excluded_chunks':total_chunks-chunks}
            event('publish', embedded=embedded)
            store.execute('ATTACH DATABASE ? AS staging',(str(stage_path),))
            with store:
                store.execute('INSERT OR REPLACE INTO cache SELECT ?,hash,vector FROM staging.passages',(config_id,))
                store.execute('DELETE FROM vectors')
                store.execute('INSERT INTO vectors SELECT chunk,hash FROM staging.chunks')
                store.execute('INSERT OR REPLACE INTO active VALUES (1,?)',(json.dumps(result),))
            event('complete', embedded=embedded)
            return result
    finally:
        stage_path.unlink(missing_ok=True)


def semantic_status(workspace):
    workspace = resolve_workspace(workspace)
    from .retrieval import retrieval_status
    path = (Path(workspace)/'semantic.sqlite3').resolve()
    if not path.exists():
        raise ValueError('No semantic index. Run build-semantic first.')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db, db:
        row = db.execute('SELECT data FROM active WHERE id=1').fetchone()
    if not row:
        raise ValueError('No completed semantic index. Run build-semantic first.')
    report = json.loads(row[0])
    if report['retrieval'] != retrieval_status(workspace):
        raise ValueError('Semantic index is stale. Run build-semantic again.')
    return report


def rank(workspace, query, provider=None):
    started = time.perf_counter()
    workspace = resolve_workspace(workspace)
    report = semantic_status(workspace)
    provider = provider or FastEmbedProvider(report['config']['model'], cache_workspace(workspace)/'models')
    if any(report['config'].get(k) != v for k,v in provider.config.items()):
        raise ValueError('Embedding configuration changed. Rebuild the semantic index.')
    scores = PassageScores(report['config'])
    if report['passages'] == 0:
        return scores
    query_started = time.perf_counter()
    q = normalized(provider.query(query), report['dimension'])
    query_seconds = time.perf_counter() - query_started
    scan_started = time.perf_counter()
    scanned = decoded = 0
    path = (Path(workspace)/'semantic.sqlite3').resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db, db:
        # Keep the streaming exact scan: grouping by hash added a SQLite sort
        # without reuse on the measured corpora. Retain winning identities,
        # but do not retain decoded vectors or assume a speed improvement.
        for chunk, digest, raw in db.execute(
                'SELECT v.chunk,v.hash,c.vector FROM vectors v JOIN cache c ON c.hash=v.hash '
                'WHERE c.config=?', (report['config_id'],)):
            scanned += 1
            v = normalized(json.loads(raw), len(q))
            score = sum(a*b for a,b in zip(q,v))
            decoded += 1
            if (chunk not in scores or score > scores[chunk]
                    or (score == scores[chunk] and digest < scores.winners[chunk])):
                scores[chunk] = score
                scores.winners[chunk] = digest
    scores.profile = {'query_seconds': query_seconds, 'scan_seconds': time.perf_counter() - scan_started,
                      'total_seconds': time.perf_counter() - started, 'passage_links': scanned,
                      'vectors_decoded': decoded, 'dimension': len(q), 'search': 'exact_cosine_scan'}
    return scores
