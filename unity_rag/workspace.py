"""Immutable index generations and crash-safe publication for maintained workspaces."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
import uuid


def _metadata_json(key, value):
    if key == 'asset_report':
        from .asset_map import decode_asset_report
        value = decode_asset_report(value)
    return json.loads(value)


def _encoded_assets(value, db):
    # Incremental refresh preserves the previous generation's representation.
    row = db.execute("SELECT typeof(value) FROM metadata WHERE key='asset_report'").fetchone()
    if row and row[0] == 'blob':
        from .asset_map import encode_asset_report
        return encode_asset_report(value)
    return json.dumps(value)


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _atomic(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_workspace(workspace):
    root = Path(workspace).resolve()
    pointer = root / 'active.json'
    if not pointer.exists():
        return root
    pointer_data = _read(pointer)
    name = pointer_data.get('generation', '')
    if not isinstance(name, str) or not re.fullmatch(r'[a-f0-9]{32}', name):
        raise ValueError('Invalid active workspace generation.')
    destination = (root / 'generations' / name).resolve()
    if not destination.is_relative_to(root) or not (destination / 'complete.json').is_file():
        raise ValueError('Active workspace generation is missing or incomplete. Run diagnostics.')
    manifest_path = destination / 'complete.json'
    manifest_bytes = manifest_path.read_bytes()
    expected = pointer_data.get('manifest_sha256')
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected)
                                 or hashlib.sha256(manifest_bytes).hexdigest() != expected):
        raise ValueError('Active generation manifest checksum does not match its pointer.')
    if json.loads(manifest_bytes).get('generation') != name:
        raise ValueError('Active generation manifest does not match its pointer.')
    return destination


def _published_pointer(destination, name):
    return {'generation': name, 'manifest_sha256': hashlib.sha256((destination / 'complete.json').read_bytes()).hexdigest()}


def require_writable(workspace):
    root = Path(workspace).resolve()
    if (root / 'active.json').exists() or (root / 'complete.json').exists():
        raise ValueError('Managed snapshots are immutable. Use refresh or rebuild.')
    return root


def cache_workspace(workspace):
    root = Path(workspace).resolve()
    if root.parent.name == 'generations' and re.fullmatch(r'[a-f0-9]{32}', root.name):
        return root.parent.parent
    return root


@contextmanager
def writer_lock(root, filename=".refresh.lock"):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / filename).open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('Another refresh is already running for this workspace.') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def changes(before, after):
    old = {row['path']: row for row in before.get('entries', [])}
    new = {row['path']: row for row in after['entries']}
    added, deleted = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    renamed = []
    for source in deleted[:]:
        digest = old[source].get('sha256')
        targets = [p for p in added if digest and new[p].get('sha256') == digest]
        sources = [p for p in deleted if digest and old[p].get('sha256') == digest]
        if len(targets) == len(sources) == 1:
            target = targets[0]
            renamed.append({'from': source, 'to': target})
            added.remove(target)
            deleted.remove(source)
    return {'added': added, 'deleted': deleted, 'renamed': renamed,
            'modified': sorted(p for p in old.keys() & new.keys() if old[p] != new[p]),
            'git_changed': before.get('git') != after.get('git'),
            'config_changed': before.get('config') != after.get('config')}


def _identity(report):
    return {key: report.get(key) for key in ('entries', 'git', 'config', 'unity_version', 'packages', 'package_lock', 'assemblies')}


def refresh_plan(before, after, *, pipeline_compatible=True, references_changed=False, threshold=50):
    """Classify indexed changes; recommendation never starts a refresh."""
    if type(threshold) is not int or threshold < 1:
        raise ValueError('Refresh threshold must be a positive integer.')
    delta = changes(before, after)
    old = {r['path']: r for r in before.get('entries', [])}
    new = {r['path']: r for r in after.get('entries', [])}
    supported = lambda row: row is not None and row.get('status') == 'supported'
    indexed = [p for p in delta['added'] + delta['deleted'] + delta['modified']
               if supported(old.get(p)) or supported(new.get(p))]
    renames = [r for r in delta['renamed'] if supported(old.get(r['from'])) or supported(new.get(r['to']))]
    count = len(indexed) + len(renames)
    removed = delta['deleted'] + [r['from'] for r in delta['renamed']]
    added = delta['added'] + [r['to'] for r in delta['renamed']]
    doc_only = (bool(delta['modified'] or removed or added)
                and not delta['git_changed'] and not delta['config_changed']
                and all(supported(old.get(p)) and supported(new.get(p))
                        and old[p].get('kind') == new[p].get('kind') == 'documentation'
                        for p in delta['modified'])
                and all(supported(old.get(p)) and old[p].get('kind') == 'documentation'
                        and p + '.meta' not in old and p + '.meta' not in new for p in removed)
                and all(supported(new.get(p)) and new[p].get('kind') == 'documentation'
                        and p + '.meta' not in old and p + '.meta' not in new for p in added)
                and all(before.get(k) == after.get(k) for k in ('unity_version', 'packages', 'package_lock', 'assemblies')))
    script_only = (bool(delta['modified'] or removed or added)
                   and not delta['git_changed'] and not delta['config_changed']
                   and all(supported(old.get(p)) and supported(new.get(p))
                           and old[p].get('kind') == new[p].get('kind') == 'script'
                           for p in delta['modified'])
                   and all(supported(old.get(p)) and old[p].get('kind') == 'script'
                           and p + '.meta' not in old and p + '.meta' not in new for p in removed)
                   and all(supported(new.get(p)) and new[p].get('kind') == 'script'
                           and p + '.meta' not in old and p + '.meta' not in new for p in added)
                   and all(before.get(k) == after.get(k) for k in ('unity_version', 'packages', 'package_lock', 'assemblies')))
    asset_only = (bool(delta['modified']) and not removed and not added
                  and not delta['git_changed'] and not delta['config_changed']
                  and all(supported(old.get(p)) and supported(new.get(p))
                          and old[p].get('kind') == new[p].get('kind') == 'serialized_asset'
                          and Path(p).suffix.lower() == '.asset' for p in delta['modified'])
                  and all(before.get(k) == after.get(k) for k in ('unity_version', 'packages', 'package_lock', 'assemblies')))
    deleted_assets = [p for p in delta['deleted'] if old.get(p, {}).get('kind') == 'serialized_asset']
    asset_delete = (len(deleted_assets) == 1 and not delta['modified'] and not added and not delta['renamed']
                    and set(delta['deleted']) == {deleted_assets[0], deleted_assets[0]+'.meta'}
                    and all(supported(old.get(p)) for p in delta['deleted'])
                    and old.get(deleted_assets[0]+'.meta', {}).get('kind') == 'metadata'
                    and not delta['git_changed'] and not delta['config_changed']
                    and all(before.get(k) == after.get(k) for k in ('unity_version','packages','package_lock','assemblies')))
    added_assets = [p for p in delta['added'] if new.get(p, {}).get('kind') == 'serialized_asset']
    asset_add = (len(added_assets) == 1 and not delta['modified'] and not removed and not delta['renamed']
                 and set(delta['added']) == {added_assets[0], added_assets[0]+'.meta'}
                 and all(supported(new.get(p)) for p in delta['added'])
                 and new.get(added_assets[0]+'.meta', {}).get('kind') == 'metadata'
                 and not delta['git_changed'] and not delta['config_changed']
                 and all(before.get(k) == after.get(k) for k in ('unity_version','packages','package_lock','assemblies')))
    if not pipeline_compatible or references_changed:
        mode, reason = 'full', 'Index pipeline or compilation references changed.'
    elif _identity(before) == _identity(after):
        mode, reason = 'unchanged', 'Indexed inputs and configuration are unchanged.'
    elif doc_only:
        mode, reason = 'documentation', 'Only supported documentation changed; reuse maps when asset dependencies permit.'
    elif script_only:
        mode, reason = 'script', 'Remap all code; reuse assets only if serialized script dependencies are unchanged.'
    elif asset_only:
        mode, reason = 'asset_scalar', 'Reuse maps only for verified scalar or resolved-reference edits; otherwise rebuild fully.'
    elif asset_delete:
        mode, reason = 'asset_delete', 'Reuse maps only if the deleted GUID has no remaining dependents; otherwise rebuild fully.'
    elif asset_add:
        mode, reason = 'asset_add', 'Reuse maps only for isolated assets with supported resolved links; otherwise rebuild fully.'
    else:
        mode, reason = 'full', 'Changes require conservative full code and asset mapping.'
    return {'mode': mode, 'reason': reason, 'indexed_changed_files': count,
            'threshold': threshold, 'refresh_pending': count >= threshold,
            'automatic_refresh': False, 'changes': delta}


def _backup(source, destination):
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)


def _create_metadata_seal(workspace):
    """Validate canonical map stamps and bind them to exact serialized bytes."""
    from .retrieval import _database_stamp
    database = (Path(workspace) / 'inventory.sqlite3').resolve()
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        stamp, code_raw, asset_raw = _database_stamp(db, include_hashes=True)
        row = db.execute("SELECT value FROM metadata WHERE key='retrieval_report'").fetchone()
        if row is None or json.loads(row[0]).get('inputs') != stamp:
            raise ValueError('Cannot seal a stale or missing retrieval index.')
    return {'version': 1, 'code_raw_sha256': code_raw, 'asset_raw_sha256': asset_raw, 'stamp': stamp}


def seal_workspace(workspace):
    """Clone and validate a managed snapshot without rescanning or re-embedding."""
    from .discovery import is_link
    from .semantic import semantic_status
    root = Path(workspace).resolve()
    with writer_lock(root):
        previous = resolve_workspace(root)
        if previous == root or not (previous / 'complete.json').is_file():
            raise ValueError('A complete managed generation is required.')
        original = _read(previous / 'complete.json')
        if not _pipeline_compatible(original.get('settings', {}).get('pipeline')):
            raise ValueError('Index pipeline is incompatible; use a managed refresh instead.')
        if original.get('settings', {}).get('semantic') and not (previous / 'semantic.sqlite3').is_file():
            raise ValueError('The semantic index is missing; no clone created.')
        if is_link(previous) or any(is_link(p) for p in previous.iterdir()):
            raise ValueError('Generation files must not be links.')
        allowed = {'complete.json', 'inventory.sqlite3', 'semantic.sqlite3', 'scan-config.json'}
        if any(p.name not in allowed or not p.is_file() for p in previous.iterdir()):
            raise ValueError('Unrecognized generation contents; no clone created.')
        pipeline = _pipeline_hash()
        prior_seal = _create_metadata_seal(previous)
        active_pointer = _read(root / 'active.json')
        if (original.get('metadata_seal') == prior_seal
                and active_pointer == _published_pointer(previous, previous.name)):
            return {'state': 'unchanged', 'generation': previous.name}
        semantic_before = semantic_status(previous) if (previous / 'semantic.sqlite3').is_file() else None
        name = uuid.uuid4().hex
        destination = root / 'generations' / name
        if is_link(root / 'generations') or not destination.resolve().is_relative_to(root):
            raise ValueError('Generation storage must stay inside the workspace without links.')
        destination.mkdir(parents=True)
        journal = {'generation': name, 'state': 'running', 'stage': 'seal-copy',
                   'started_at': datetime.now(timezone.utc).isoformat()}
        _atomic(root / 'refresh-state.json', journal)
        try:
            _backup(previous / 'inventory.sqlite3', destination / 'inventory.sqlite3')
            if semantic_before is not None:
                _backup(previous / 'semantic.sqlite3', destination / 'semantic.sqlite3')
            if (previous / 'scan-config.json').is_file():
                shutil.copyfile(previous / 'scan-config.json', destination / 'scan-config.json')
            journal['stage'] = 'seal-verify'
            _atomic(root / 'refresh-state.json', journal)
            seal = _create_metadata_seal(destination)
            if seal != prior_seal or (semantic_before is not None and semantic_status(destination) != semantic_before):
                raise ValueError('Copied snapshot differs from the verified original.')
            if resolve_workspace(root) != previous or _pipeline_hash() != pipeline:
                raise ValueError('Active generation or pipeline changed during sealing.')
            manifest = {**original, 'generation': name, 'metadata_seal': seal,
                        'sealed_from': previous.name, 'sealed_at': datetime.now(timezone.utc).isoformat()}
            _atomic(destination / 'complete.json', manifest)
            _atomic(root / 'active.json', _published_pointer(destination, name))
            journal.update(state='complete', stage='publish')
            _atomic(root / 'refresh-state.json', journal)
            return {'state': 'complete', 'generation': name, 'sealed_from': previous.name,
                    'reembedded': 0, 'source_rescanned': False}
        except BaseException as exc:
            journal.update(state='failed', error=str(exc))
            _atomic(root / 'refresh-state.json', journal)
            raise


def _reuse_maps_for_documents(previous, destination, inventory, delta):
    """Copy maps and restamp eligible nonserialized inventory changes."""
    from .asset_map import fingerprint
    from .retrieval import retrieval_status
    retrieval_status(previous)  # Check prior map fingerprints before any reuse.
    new_rows = {row['path']: row for row in inventory['entries']}
    database = destination / 'inventory.sqlite3'
    copied = destination / 'reused-inventory.sqlite3'
    _backup(previous / 'inventory.sqlite3', copied)
    os.replace(copied, database)
    with sqlite3.connect(database) as db:
        db.execute('BEGIN IMMEDIATE')
        records = {k: _metadata_json(k, v) for k, v in db.execute(
            "SELECT key,value FROM metadata WHERE key IN ('code_report','asset_report')")}
        if set(records) != {'code_report', 'asset_report'}:
            raise ValueError('Prior code or asset report missing; full refresh required.')
        code, assets = records['code_report'], records['asset_report']
        if assets.get('code_fingerprint') != fingerprint(code):
            raise ValueError('Prior maps disagree; full refresh required.')
        code['discovery_scanned_at'] = inventory['scanned_at']
        assets['discovery_scanned_at'] = inventory['scanned_at']
        assets['code_fingerprint'] = fingerprint(code)
        for path in delta['modified']:
            matches = [a for a in assets['assets'] if a['path'] == path]
            if len(matches) != 1 or matches[0]['inventory_status'] != 'supported':
                raise ValueError('Document asset map is incompatible; full refresh required.')
            asset = matches[0]
            asset['sha256'] = new_rows[path]['sha256']
            changed = db.execute('UPDATE unity_assets SET data=? WHERE id=?', (json.dumps(asset), asset['id']))
            if changed.rowcount != 1:
                raise ValueError('Document asset record missing; full refresh required.')
        removed = delta['deleted'] + [r['from'] for r in delta['renamed']]
        added = delta['added'] + [r['to'] for r in delta['renamed']]
        for path in removed:
            found = [a for a in assets['assets'] if a['path'] == path]
            if (len(found) != 1 or found[0].get('guid') is not None or found[0].get('metadata_path') is not None
                    or any(e.get('target_asset') == found[0]['id'] or e.get('target') == found[0]['id']
                           for e in assets['edges'])
                    or any(o.get('asset') == found[0]['id'] for o in assets['objects'])):
                raise ValueError('Document has asset dependencies; full refresh required.')
            assets['assets'].remove(found[0])
            if db.execute('DELETE FROM unity_assets WHERE id=?', (found[0]['id'],)).rowcount != 1:
                raise ValueError('Document asset record missing; full refresh required.')
        for path in added:
            if any(a['path'] == path for a in assets['assets']):
                raise ValueError('Document asset already exists; full refresh required.')
            entry = new_rows[path]
            asset = {'id':'asset:' + path, 'path':path, 'guid':None, 'metadata_path':None,
                     'kind':entry['kind'], 'inventory_status':'supported', 'parse_status':'metadata_only', 'sha256':entry['sha256']}
            assets['assets'].append(asset)
            db.execute('INSERT INTO unity_assets VALUES (?,?,?,?)', (asset['id'], path, None, json.dumps(asset)))
        assets['assets'].sort(key=lambda a: a['path'])
        assets['counts']['assets'] = len(assets['assets'])
        db.execute('DELETE FROM files')
        db.executemany('INSERT INTO files VALUES (:path,:kind,:status,:reason,:size,:sha256)', inventory['entries'])
        db.executemany('INSERT OR REPLACE INTO metadata VALUES (?, ?)', [
            ('report', json.dumps(inventory)), ('code_report', json.dumps(code)), ('asset_report', _encoded_assets(assets, db))])


def _remap_code_reuse_assets(previous, destination, inventory, delta, options, analyzer_workspace):
    """Recompile every source; retain asset links only when their code dependencies match."""
    from .asset_map import fingerprint
    from .code_map import map_code
    _reuse_maps_for_documents(previous, destination, inventory, delta)
    with sqlite3.connect(destination / 'inventory.sqlite3') as db:
        rows = {k: _metadata_json(k, v) for k, v in db.execute(
            "SELECT key,value FROM metadata WHERE key IN ('code_report','asset_report')")}
    old_code, assets = rows['code_report'], rows['asset_report']
    paths = set()
    for edge in assets['edges']:
        target = edge.get('target_asset')
        if edge.get('kind') == 'uses_script' and isinstance(target, str) and target.startswith('asset:'):
            paths.add(target[len('asset:'):])
    def class_inputs(report):
        found = {path: [] for path in paths}
        for declaration in report['declarations']:
            path = declaration['location']['path']
            if declaration['kind'] in ('ClassDeclaration', 'RecordDeclaration') and path in found:
                found[path].append(declaration)
        return found
    new_code = map_code(destination, **options, analyzer_workspace=analyzer_workspace, reuse_report=old_code)
    if class_inputs(old_code) != class_inputs(new_code):
        raise ValueError('Serialized script dependencies changed; full asset mapping required.')
    assets['code_fingerprint'] = fingerprint(new_code)
    with sqlite3.connect(destination / 'inventory.sqlite3') as db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('asset_report', ?)", (_encoded_assets(assets, db),))


def _check_cached_code_toolchain(previous, analyzer_workspace):
    from .code_map import _helper, _run
    code = _metadata(previous, 'code_report')
    recorded = code.get('environment_fingerprint')
    if not isinstance(recorded, str) or not re.fullmatch(r'[0-9a-f]{64}', recorded):
        raise ValueError('Code toolchain provenance missing; full refresh required.')
    helper = _helper(analyzer_workspace)
    request = {'files':[], 'defines':code.get('defines', []), 'references':code.get('references', []),
               'language_version':code.get('language_version', '10')}
    probe = json.loads(_run(['dotnet',str(helper)], input=json.dumps(request)))
    if (probe.get('schema_version') != 1 or probe.get('environment_fingerprint') != recorded
            or probe.get('roslyn_version') != code.get('roslyn_version')):
        raise ValueError('Code toolchain changed; full refresh required.')


def _reuse_maps_for_scalar_assets(previous, destination, inventory, delta, analyzer_workspace):
    """Update scalar values and bounded, unambiguous generic reference changes."""
    import yaml
    from .code_map import _read_snapshot_file
    from .unity_yaml import parse_unity, walk_values
    _check_cached_code_toolchain(previous, analyzer_workspace)
    _reuse_maps_for_documents(previous, destination, inventory, delta)
    entries = {e['path']:e for e in inventory['entries']}
    project = Path(inventory['project']).resolve()
    database = destination/'inventory.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT value FROM metadata WHERE key='asset_report'").fetchone()
        if row is None:
            raise ValueError('Prior asset report missing; full refresh required.')
        assets = _metadata_json('asset_report', row[0])
        pending = {}
        for path in delta['modified']:
            asset_id = 'asset:' + path
            matches = [a for a in assets['assets'] if a['id'] == asset_id]
            if len(matches) != 1:
                raise ValueError('Asset record missing; full refresh required.')
            asset = matches[0]; guid = asset.get('guid')
            if (asset.get('parse_status') != 'parsed' or not guid
                    or asset.get('metadata_path') != path + '.meta'
                    or entries.get(path + '.meta', {}).get('status') != 'supported'
                    or sum(a.get('guid') == guid for a in assets['assets']) != 1
                    or any(d.get('path') == path for d in assets['diagnostics'])):
                raise ValueError('Asset metadata or parse status requires full mapping.')
            indices = [i for i,o in enumerate(assets['objects']) if o['path'] == path]
            old = [assets['objects'][i] for i in indices]
            ids = {o['id'] for o in old}
            if (any(o.get('type') == 'PrefabInstance' or o.get('stripped') for o in old)
                    or any(x.get('target') in ids or x.get('base_location', {}).get('path') == path
                           for x in assets['overrides'])):
                raise ValueError('Prefab or override dependencies require full mapping.')
            try:
                parsed = parse_unity(_read_snapshot_file(project, entries[path]), path)
            except (ValueError, yaml.YAMLError, RecursionError) as exc:
                raise ValueError('Asset parse failed; full mapping required.') from exc
            if len(parsed) != len(old):
                raise ValueError('Object identities changed; full mapping required.')
            for index, prior, current in zip(indices, old, parsed):
                keys = ('file_id','class_id','type','stripped','location','locations')
                old_refs = [(p,v) for p,v,is_ref in walk_values(prior['data']) if is_ref]
                new_refs = [(p,v) for p,v,is_ref in walk_values(current['data']) if is_ref]
                if (any(prior.get(k) != current.get(k) for k in keys)
                        or [p for p,_ in old_refs] != [p for p,_ in new_refs]
                        or prior['data'].get('m_EditorClassIdentifier') != current['data'].get('m_EditorClassIdentifier')):
                    raise ValueError('Asset structure or references changed; full mapping required.')
                special = {'m_Script','m_GameObject','m_Father','m_SourcePrefab',
                           'm_CorrespondingSourceObject','m_PrefabInstance'}
                for (prop, old_ref), (_, new_ref) in zip(old_refs, new_refs):
                    if json.dumps(old_ref,ensure_ascii=False) == json.dumps(new_ref,ensure_ascii=False):
                        continue
                    key = (prior['id'], prop)
                    if prop in special or prop.startswith('m_Component.') or key in pending:
                        raise ValueError('Special or ambiguous reference requires full mapping.')
                    pending[key] = (old_ref, new_ref)
                    if len(pending) > 128:
                        raise ValueError('Too many changed references; full mapping required.')
                current.update(id=prior['id'], asset=prior['asset'], path=path, guid=guid,
                               name=current['data'].get('m_Name'))
                current['values'] = [{'property':p,'value':v,'origin':'serialized',
                                      'location':current['locations'].get(p,current['location'])}
                                     for p,v,is_ref in walk_values(current['data']) if not is_ref]
                if 'script' in prior:
                    current['script'] = prior['script']
                current['role'] = prior['role']
                assets['objects'][index] = current
                changed = db.execute('UPDATE unity_objects SET data=? WHERE id=?', (json.dumps(current),current['id']))
                if changed.rowcount != 1:
                    raise ValueError('Asset object record missing; full mapping required.')
        if pending:
            from collections import Counter, defaultdict
            by_guid = defaultdict(list)
            for asset in assets['assets']:
                if asset.get('guid'):
                    by_guid[asset['guid']].append(asset)
            object_ids = Counter(o['id'] for o in assets['objects'])
            candidates = defaultdict(list)
            for index,edge in enumerate(assets['edges']):
                key = (edge.get('source'),edge.get('property'))
                if key in pending:
                    candidates[key].append((index,edge))
            sources = sorted({source for source,_ in pending})
            placeholders = ','.join('?' for _ in sources)
            rows = defaultdict(list)
            for record in db.execute('SELECT rowid,source,target,target_asset,resolution,data FROM unity_edges WHERE source IN ('+placeholders+')', sources):
                rowid,source,target,target_asset,resolution,data = record
                decoded = json.loads(data)
                key = (source,decoded.get('property'))
                if key in pending:
                    rows[key].append((rowid,source,target,target_asset,resolution,data))
            for key,(old_ref,new_ref) in pending.items():
                if len(candidates[key]) != 1 or len(rows[key]) != 1:
                    raise ValueError('Reference edge is missing or ambiguous; full mapping required.')
                index,edge = candidates[key][0]
                rowid,source,target,target_asset,resolution,data = rows[key][0]
                if (edge.get('kind') != 'references_asset_or_object'
                        or json.dumps(edge.get('reference'),ensure_ascii=False) != json.dumps(old_ref,ensure_ascii=False)
                        or data != json.dumps(edge) or (source,target,target_asset,resolution) !=
                        (edge.get('source'),edge.get('target'),edge.get('target_asset'),edge.get('resolution'))):
                    raise ValueError('Reference edge integrity failed; full mapping required.')
                file_id = str(new_ref.get('fileID',''))
                if file_id == '0':
                    resolved = {'target':None,'target_asset':None,'resolution':'null'}
                else:
                    guid = str(new_ref.get('guid','')).lower()
                    targets = by_guid.get(guid, [])
                    if not re.fullmatch(r'-?\d+',file_id) or not re.fullmatch(r'[0-9a-f]{32}',guid) or guid == '0'*32 or len(targets) != 1:
                        raise ValueError('Reference target is not unique; full mapping required.')
                    asset = targets[0]; object_id = asset['id'] + '#' + file_id
                    if asset.get('inventory_status') != 'supported' or asset.get('parse_status') != 'parsed' or object_ids[object_id] != 1:
                        raise ValueError('Reference target is not a parsed object; full mapping required.')
                    resolved = {'target':object_id,'target_asset':asset['id'],'resolution':'resolved_object'}
                updated = dict(edge);updated['reference'] = new_ref;updated.update(resolved)
                assets['edges'][index] = updated
                db.execute('UPDATE unity_edges SET target=?,target_asset=?,resolution=?,data=? WHERE rowid=?',
                           (updated['target'],updated['target_asset'],updated['resolution'],json.dumps(updated),rowid))
            assets['counts']['reference_resolution'] = dict(Counter(e['resolution'] for e in assets['edges']))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('asset_report', ?)", (_encoded_assets(assets, db),))
    return bool(pending)


def _reuse_maps_for_unreferenced_asset_deletion(previous, destination, inventory, delta, analyzer_workspace):
    """Remove one asset and metadata only when no retained reference uses its GUID."""
    from collections import Counter
    from .asset_map import fingerprint
    from .retrieval import retrieval_status
    _check_cached_code_toolchain(previous, analyzer_workspace)
    retrieval_status(previous)
    path = next(p for p in delta['deleted'] if p.lower().endswith('.asset'))
    database = destination/'inventory.sqlite3'; copied = destination/'reused-inventory.sqlite3'
    _backup(previous/'inventory.sqlite3',copied);os.replace(copied,database)
    with sqlite3.connect(database) as db:
        db.execute('BEGIN IMMEDIATE')
        records = {k:_metadata_json(k,v) for k,v in db.execute(
            "SELECT key,value FROM metadata WHERE key IN ('code_report','asset_report')")}
        if set(records) != {'code_report','asset_report'}:
            raise ValueError('Prior maps missing; full refresh required.')
        code, assets = records['code_report'], records['asset_report']
        if assets.get('code_fingerprint') != fingerprint(code):
            raise ValueError('Prior maps disagree; full refresh required.')
        matches = [a for a in assets['assets'] if a.get('id') == 'asset:' + path]
        if len(matches) != 1:
            raise ValueError('Deleted asset record is missing; full refresh required.')
        asset = matches[0]; guid = asset.get('guid')
        objects = [o for o in assets['objects'] if o.get('asset') == asset['id']]
        ids = {o['id'] for o in objects}
        if (asset.get('inventory_status') != 'supported' or asset.get('parse_status') != 'parsed'
                or asset.get('metadata_path') != path+'.meta' or not guid
                or sum(a.get('guid') == guid for a in assets['assets']) != 1
                or not objects or len(objects) != len(ids) or len(ids) > 128
                or any(o.get('type') == 'PrefabInstance' or o.get('stripped') for o in objects)
                or any(d.get('path') in (path,path+'.meta') for d in assets['diagnostics'])
                or any(o.get('target') in ids or o.get('base_location',{}).get('path') == path for o in assets['overrides'])):
            raise ValueError('Deleted asset is not safely isolated; full mapping required.')
        for edge in assets['edges']:
            ref = edge.get('reference')
            if edge.get('source') not in ids and isinstance(ref,dict) and isinstance(ref.get('guid'),str) and ref['guid'].lower() == guid:
                raise ValueError('Deleted asset has a remaining GUID reference; full mapping required.')
        asset_row=db.execute('SELECT id,path,guid,data FROM unity_assets WHERE id=?',(asset['id'],)).fetchone()
        if asset_row != (asset['id'],path,guid,json.dumps(asset)):
            raise ValueError('Deleted asset table is inconsistent; full mapping required.')
        old_objects=Counter((o['id'],o['asset'],json.dumps(o)) for o in objects)
        stored_objects=Counter(db.execute('SELECT id,asset,data FROM unity_objects WHERE asset=?',(asset['id'],)).fetchall())
        if old_objects != stored_objects:
            raise ValueError('Deleted object table is inconsistent; full mapping required.')
        outgoing=[e for e in assets['edges'] if e.get('source') in ids]
        expected=Counter((e['source'],e.get('target'),e.get('target_asset'),e['resolution'],json.dumps(e)) for e in outgoing)
        placeholders=','.join('?' for _ in ids)
        values=tuple(sorted(ids))
        stored=Counter(db.execute('SELECT source,target,target_asset,resolution,data FROM unity_edges WHERE source IN ('+placeholders+')',values).fetchall())
        if expected != stored:
            raise ValueError('Deleted edge table is inconsistent; full mapping required.')
        db.execute('DELETE FROM unity_edges WHERE source IN ('+placeholders+')',values)
        db.execute('DELETE FROM unity_objects WHERE asset=?',(asset['id'],))
        db.execute('DELETE FROM unity_assets WHERE id=?',(asset['id'],))
        assets['assets']=[a for a in assets['assets'] if a['id'] != asset['id']]
        assets['objects']=[o for o in assets['objects'] if o['id'] not in ids]
        assets['edges']=[e for e in assets['edges'] if e.get('source') not in ids]
        assets['counts']['assets']=len(assets['assets']);assets['counts']['objects']=len(assets['objects'])
        assets['counts']['edges']=len(assets['edges'])
        assets['counts']['reference_resolution']=dict(Counter(e['resolution'] for e in assets['edges']))
        code['discovery_scanned_at']=inventory['scanned_at'];assets['discovery_scanned_at']=inventory['scanned_at']
        assets['code_fingerprint']=fingerprint(code)
        db.execute('DELETE FROM files')
        db.executemany('INSERT INTO files VALUES (:path,:kind,:status,:reason,:size,:sha256)',inventory['entries'])
        db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',[
            ('report',json.dumps(inventory)),('code_report',json.dumps(code)),('asset_report',_encoded_assets(assets, db))])


def _reuse_maps_for_isolated_asset_addition(previous, destination, inventory, delta, analyzer_workspace):
    """Add one unreferenced asset with bounded, unambiguous resolved links."""
    import yaml
    from collections import Counter, defaultdict
    from .asset_map import fingerprint
    from .code_map import _read_snapshot_file
    from .retrieval import retrieval_status
    from .unity_yaml import parse_unity, reference, walk_values
    _check_cached_code_toolchain(previous, analyzer_workspace)
    retrieval_status(previous)
    path = next(p for p in delta['added'] if p.lower().endswith('.asset'))
    entries = {e['path']:e for e in inventory['entries']};project = Path(inventory['project']).resolve()
    meta = _read_snapshot_file(project,entries[path+'.meta'])
    matches = re.findall(r'^guid:\s*([0-9a-fA-F]{32})\s*$',meta,re.MULTILINE)
    if len(matches) != 1 or re.search(r'^folderAsset:\s*yes\s*$',meta,re.MULTILINE):
        raise ValueError('Added metadata is ambiguous; full mapping required.')
    guid = matches[0].lower()
    if guid == '0'*32:
        raise ValueError('Added metadata uses the zero GUID; full mapping required.')
    try:
        parsed = parse_unity(_read_snapshot_file(project,entries[path]),path)
    except (ValueError,yaml.YAMLError,RecursionError) as exc:
        raise ValueError('Added asset cannot be parsed; full mapping required.') from exc
    if not parsed or len(parsed) > 128 or any(o['type']=='PrefabInstance' or o['stripped'] for o in parsed):
        raise ValueError('Added asset structure requires full mapping.')
    database=destination/'inventory.sqlite3';copied=destination/'reused-inventory.sqlite3'
    _backup(previous/'inventory.sqlite3',copied);os.replace(copied,database)
    with sqlite3.connect(database) as db:
        db.execute('BEGIN IMMEDIATE')
        records={k:_metadata_json(k,v) for k,v in db.execute("SELECT key,value FROM metadata WHERE key IN ('code_report','asset_report')")}
        if set(records) != {'code_report','asset_report'}:
            raise ValueError('Prior maps missing; full refresh required.')
        code,assets=records['code_report'],records['asset_report']
        if assets.get('code_fingerprint') != fingerprint(code):
            raise ValueError('Prior maps disagree; full refresh required.')
        if (any(a.get('path') == path or a.get('guid') == guid for a in assets['assets'])
                or any(isinstance(e.get('reference'),dict) and isinstance(e['reference'].get('guid'),str)
                       and e['reference']['guid'].lower() == guid for e in assets['edges'])
                or any(isinstance(o.get('record'),dict) and isinstance(o['record'].get('target'),dict)
                       and str(o['record']['target'].get('guid','')).lower() == guid for o in assets['overrides'])):
            raise ValueError('Added GUID has an existing dependency or collision; full mapping required.')
        old_objects=assets['objects'];old_edges=assets['edges']
        old_order={o['id']:i for i,o in enumerate(old_objects)}
        if (len(old_order) != len(old_objects) or [a['path'] for a in assets['assets']] != sorted(a['path'] for a in assets['assets'])
                or [o['path'] for o in old_objects] != sorted(o['path'] for o in old_objects)):
            raise ValueError('Prior asset order is incompatible; full mapping required.')
        try:
            positions=[old_order[e['source']] for e in old_edges]
        except KeyError as exc:
            raise ValueError('Prior edge source is missing; full mapping required.') from exc
        if positions != sorted(positions):
            raise ValueError('Prior edge order is incompatible; full mapping required.')
        guid_assets=defaultdict(list)
        for a in assets['assets']:
            if a.get('guid'): guid_assets[a['guid']].append(a)
        declarations=defaultdict(list)
        for declaration in code['declarations']:
            if declaration['kind'] in ('ClassDeclaration','RecordDeclaration'):
                declarations[declaration['location']['path']].append(declaration)
        added_objects=[];added_edges=[]
        added_ids={'asset:'+path+'#'+obj['file_id'] for obj in parsed}
        for obj in parsed:
            obj.update(id=f"asset:{path}#{obj['file_id']}",asset='asset:'+path,path=path,guid=guid,name=obj['data'].get('m_Name'))
            obj['values']=[{'property':p,'value':v,'origin':'serialized',
                            'location':obj['locations'].get(p,obj['location'])}
                           for p,v,is_ref in walk_values(obj['data']) if not is_ref]
            for prop,value,is_ref in walk_values(obj['data']):
                if not is_ref: continue
                kind={'m_Script':'uses_script','m_GameObject':'attached_to','m_Father':'transform_parent',
                      'm_SourcePrefab':'prefab_source','m_CorrespondingSourceObject':'corresponding_source',
                      'm_PrefabInstance':'prefab_instance'}.get(prop,'references_asset_or_object')
                if prop.startswith('m_Component.'): kind='has_component'
                location=obj['locations'].get(prop,obj['location'])
                file_id=str(value.get('fileID',''))
                if file_id == '0':
                    target={'target':None,'target_asset':None,'resolution':'null'}
                elif prop == 'm_Script':
                    script_guid=value.get('guid','')
                    candidates=guid_assets.get(script_guid.lower(),[]) if isinstance(script_guid,str) else []
                    if (file_id != '11500000' or len(candidates) != 1
                            or candidates[0].get('kind') != 'script' or candidates[0].get('inventory_status') != 'supported'
                            or candidates[0].get('parse_status') != 'metadata_only'):
                        raise ValueError('Added reference is not a unique script link; full mapping required.')
                    script=candidates[0];script_path=script['path']
                    if any(o['path'] == script_path and o['file_id'] == file_id for o in old_objects):
                        raise ValueError('Script target has a conflicting object; full mapping required.')
                    classes=declarations.get(script_path,[])
                    if any(not isinstance(d.get('qualified_name'),str) or not isinstance(d.get('assembly'),str) for d in classes):
                        raise ValueError('Script declarations are unresolved; full mapping required.')
                    identifier=obj['data'].get('m_EditorClassIdentifier','')
                    explicit=[d for d in classes if identifier == d['assembly']+'::'+d['qualified_name']]
                    matching=explicit or [d for d in classes if d['name']==Path(script_path).stem]
                    unique={d['id']:d for d in matching}
                    if len(unique) != 1:
                        raise ValueError('Script candidate is ambiguous; full mapping required.')
                    declaration=next(iter(unique.values()))
                    if declaration.get('status') != 'declared' or declaration.get('unity_type') == 'unresolved':
                        raise ValueError('Script type is unresolved; full mapping required.')
                    target={'target':None,'target_asset':script['id'],'resolution':'resolved_asset_only'}
                else:
                    special={'m_GameObject','m_Father','m_SourcePrefab','m_CorrespondingSourceObject','m_PrefabInstance','m_Component'}
                    target_guid=value.get('guid','')
                    if (prop in special or prop.startswith('m_Component.') or not re.fullmatch(r'-?\d+',file_id)
                            or not isinstance(target_guid,str)):
                        raise ValueError('Added special or malformed reference requires full mapping.')
                    target_guid=target_guid.lower()
                    if target_guid in ('','0'*32,guid):
                        target_asset='asset:'+path;target_id=target_asset+'#'+file_id
                        if target_id not in added_ids:
                            raise ValueError('Added local target is unresolved; full mapping required.')
                    else:
                        candidates=guid_assets.get(target_guid,[])
                        if not re.fullmatch(r'[0-9a-f]{32}',target_guid) or len(candidates) != 1:
                            raise ValueError('Added target GUID is unresolved or ambiguous; full mapping required.')
                        candidate=candidates[0];target_asset=candidate['id'];target_id=target_asset+'#'+file_id
                        if (candidate.get('inventory_status') != 'supported' or candidate.get('parse_status') != 'parsed'
                                or target_id not in old_order):
                            raise ValueError('Added target is not a parsed object; full mapping required.')
                    target={'target':target_id,'target_asset':target_asset,'resolution':'resolved_object'}
                edge={'source':obj['id'],'kind':kind,'property':prop,'reference':value,'location':location,**target}
                added_edges.append(edge)
                if prop == 'm_Script' and target['target_asset'] and file_id == '11500000':
                    obj['script']={'path':script_path,'candidate_types':list(unique),
                                   'resolution':'serialized_type_identifier' if explicit else 'filename_candidate',
                                   'unity_type':declaration['unity_type']}
                    added_edges.append({'source':obj['id'],'kind':'script_type','target':declaration['id'],
                                        'target_asset':script['id'],'resolution':'explicit_identifier' if explicit else 'inferred_filename',
                                        'property':prop,'location':location})
                if len(added_edges) > 512:
                    raise ValueError('Added asset has too many links; full mapping required.')
            obj['role']='serialized_object'
            if obj['type']=='GameObject': obj['role']='game_object'
            elif reference(obj['data'].get('m_GameObject')) and obj['data']['m_GameObject']['fileID'] != '0': obj['role']='component'
            elif obj.get('script',{}).get('unity_type')=='UnityEngine.ScriptableObject': obj['role']='scriptable_object_candidate'
            added_objects.append(obj)
        asset={'id':'asset:'+path,'path':path,'guid':guid,'metadata_path':path+'.meta',
               'kind':entries[path]['kind'],'inventory_status':'supported','parse_status':'parsed','sha256':entries[path]['sha256']}
        if db.execute('SELECT 1 FROM unity_assets WHERE id=?',(asset['id'],)).fetchone():
            raise ValueError('Added asset table already contains the path; full mapping required.')
        if db.execute('SELECT 1 FROM unity_objects WHERE asset=?',(asset['id'],)).fetchone():
            raise ValueError('Added object table already contains the path; full mapping required.')
        ids=[o['id'] for o in added_objects];placeholders=','.join('?' for _ in ids)
        if db.execute('SELECT 1 FROM unity_objects WHERE id IN ('+placeholders+') LIMIT 1',ids).fetchone():
            raise ValueError('Added object IDs already exist; full mapping required.')
        if db.execute('SELECT 1 FROM unity_edges WHERE source IN ('+placeholders+') LIMIT 1',ids).fetchone():
            raise ValueError('Added edge table already contains the path; full mapping required.')
        insert_at=next((i for i,e in enumerate(old_edges) if old_objects[old_order[e['source']]]['path'] > path),len(old_edges))
        assets['assets']=sorted(assets['assets']+[asset],key=lambda a:a['path'])
        assets['objects']=sorted(old_objects+added_objects,key=lambda o:o['path'])
        assets['edges']=old_edges[:insert_at]+added_edges+old_edges[insert_at:]
        assets['counts']['assets']=len(assets['assets']);assets['counts']['objects']=len(assets['objects'])
        assets['counts']['edges']=len(assets['edges'])
        assets['counts']['reference_resolution']=dict(Counter(e['resolution'] for e in assets['edges']))
        db.execute('INSERT INTO unity_assets VALUES (?,?,?,?)',(asset['id'],path,guid,json.dumps(asset)))
        db.executemany('INSERT INTO unity_objects VALUES (?,?,?)',[(o['id'],o['asset'],json.dumps(o)) for o in added_objects])
        db.executemany('INSERT INTO unity_edges VALUES (?,?,?,?,?)',[(e['source'],e.get('target'),e.get('target_asset'),e['resolution'],json.dumps(e)) for e in added_edges])
        code['discovery_scanned_at']=inventory['scanned_at'];assets['discovery_scanned_at']=inventory['scanned_at']
        assets['code_fingerprint']=fingerprint(code)
        db.execute('DELETE FROM files')
        db.executemany('INSERT INTO files VALUES (:path,:kind,:status,:reason,:size,:sha256)',inventory['entries'])
        db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',[
            ('report',json.dumps(inventory)),('code_report',json.dumps(code)),('asset_report',_encoded_assets(assets, db))])


def refresh(workspace, project=None, config_path=None, *, rebuild=False,
            semantic=None, provider=None, model=None, defines=None, references=None,
            language_version=None, semantic_kinds=None, semantic_progress=None, stage_guard=None,
            compress_asset_report=None, semantic_batch_size=None, chunk_budget=None, chunk_encoding=None):
    from .discovery import scan, status
    from .code_map import map_code
    from .asset_map import map_assets
    from .retrieval import build_retrieval
    from .semantic import build_semantic, SEMANTIC_KINDS
    from .embeddings import DEFAULT_MODEL, FastEmbedProvider, DEFAULT_BATCH_SIZE, validate_batch_size
    if compress_asset_report is not None and type(compress_asset_report) is not bool:
        raise ValueError('Asset-report compression must be true or false.')
    root = Path(workspace).resolve()
    if (root / 'complete.json').exists():
        raise ValueError('Refresh requires the workspace root, not a generation.')
    # The optional guard is checked before work and at stage boundaries.
    if stage_guard is not None:
        stage_guard('preflight')
    # Validate location before creating anything in the source project.
    if project and root.is_relative_to(Path(project).resolve()):
        raise ValueError('Workspace must be outside the source project.')
    with writer_lock(root):
        previous = resolve_workspace(root)
        before = status(previous) if (previous / 'inventory.sqlite3').exists() else {}
        project = project or before.get('project')
        if not project:
            raise ValueError('A project path is required for a new workspace.')
        if before and Path(project).resolve() != Path(before['project']).resolve():
            raise ValueError('Workspace belongs to a different project.')
        if root.is_relative_to(Path(project).resolve()):
            raise ValueError('Workspace must be outside the source project.')
        old_code = _metadata(previous, 'code_report') if before else {}
        options = {'defines': list(defines if defines is not None else old_code.get('defines', [])),
                   'references': list(references if references is not None else old_code.get('references', [])),
                   'language_version': language_version or old_code.get('language_version', '10')}
        old_semantic = None
        if (previous / 'semantic.sqlite3').exists():
            # Only reuse configuration/cache here: legacy derived maps may be stale.
            with sqlite3.connect((previous / 'semantic.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
                row = db.execute('SELECT data FROM active WHERE id=1').fetchone()
                old_semantic = json.loads(row[0]) if row else None
        selected_batch_size = semantic_batch_size if semantic_batch_size is not None else (old_semantic or {}).get('batch_size', DEFAULT_BATCH_SIZE)
        validate_batch_size(selected_batch_size)
        enabled = (old_semantic is not None or provider is not None) if semantic is None else semantic
        if semantic_kinds is None:
            selected_kinds = (old_semantic or {}).get('include_kinds')
        elif semantic_kinds == ['all']:
            selected_kinds = None
        elif (not isinstance(semantic_kinds, list) or not semantic_kinds or
              any(kind not in SEMANTIC_KINDS for kind in semantic_kinds)):
            raise ValueError('Choose recognized semantic kinds, or all by itself.')
        else:
            selected_kinds = sorted(set(semantic_kinds))
        selected_model = model or (provider.config.get('model') if provider else None) or (old_semantic or {}).get('config', {}).get('model', DEFAULT_MODEL)
        if enabled and provider is None and old_semantic and old_semantic['config'].get('provider') != 'fastembed':
            raise ValueError('Refresh needs the embedding provider used by this workspace.')
        if enabled and provider is None:
            provider = FastEmbedProvider(selected_model, root / 'models')
        options['references'] = [str(Path(p).resolve(strict=True)) for p in options['references']]
        reference_hashes = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in options['references']}
        name = uuid.uuid4().hex
        destination = root / 'generations' / name
        from .discovery import is_link
        if ((root / 'generations').exists() and is_link(root / 'generations')) or not destination.resolve().is_relative_to(root):
            raise ValueError('Generation storage must remain inside the workspace without links.')
        destination.mkdir(parents=True)
        journal = {'generation': name, 'state': 'running', 'stage': 'scan',
                   'started_at': datetime.now(timezone.utc).isoformat()}
        def stage(value):
            if stage_guard is not None:
                stage_guard(value)
            journal['stage'] = value
            _atomic(root / 'refresh-state.json', journal)
        try:
            stage('scan')
            config = destination / 'scan-config.json'
            if config_path:
                config.write_text(Path(config_path).read_text(encoding='utf-8-sig'), encoding='utf-8')
            elif before:
                config.write_text(json.dumps(before['config']), encoding='utf-8')
            inventory = scan(project, destination, config if config.exists() else None)
            delta = changes(before, inventory)
            old_manifest = _read(previous / 'complete.json') if (previous / 'complete.json').exists() else {}
            previous_settings = dict(old_manifest.get('settings', {}))
            from .chunking import ChunkPolicy, DEFAULT_CHUNK_BUDGET, DEFAULT_CHUNK_ENCODING
            old_chunks = previous_settings.get('chunking', {})
            selected_chunks = ChunkPolicy(
                chunk_budget if chunk_budget is not None else old_chunks.get('budget', DEFAULT_CHUNK_BUDGET),
                chunk_encoding if chunk_encoding is not None else old_chunks.get('encoding', DEFAULT_CHUNK_ENCODING)).config
            # Workspaces predating the opt-in setting used the legacy text format.
            if previous_settings:
                previous_settings.setdefault('compress_asset_report', False)
            selected_compression = (previous_settings.get('compress_asset_report', False)
                                    if compress_asset_report is None else compress_asset_report)
            settings = {'code': options, 'semantic': enabled, 'model': selected_model if enabled else None,
                        'provider': provider.config if enabled else None,
                        'semantic_kinds': selected_kinds if enabled else None,
                        'semantic_batch_size': selected_batch_size if enabled else None,
                        'compress_asset_report': selected_compression,
                        'chunking': selected_chunks,
                        'reference_hashes': reference_hashes, 'pipeline': _pipeline_hash()}
            settings_compatible = (bool(previous_settings) and _pipeline_compatible(previous_settings.get('pipeline'))
                                   and {k: v for k, v in previous_settings.items() if k != 'pipeline'}
                                   == {k: v for k, v in settings.items() if k != 'pipeline'})
            plan = refresh_plan(before, inventory, pipeline_compatible=settings_compatible and not rebuild)
            if not rebuild and before and _identity(before) == _identity(inventory) and settings_compatible:
                if stage_guard is not None:
                    stage_guard('unchanged')
                (destination / 'inventory.sqlite3').unlink()
                config.unlink(missing_ok=True)
                destination.rmdir()
                journal.update(state='unchanged', generation=previous.name)
                _atomic(root / 'refresh-state.json', journal)
                return {'state': 'unchanged', 'generation': previous.name, 'changes': delta}
            reused = False
            reuse_mode = plan['mode']
            if before and not rebuild and plan['mode'] == 'documentation':
                stage('reuse-document-maps')
                try:
                    _reuse_maps_for_documents(previous, destination, inventory, delta)
                    reused = True
                except ValueError:
                    # Recreate the scanned inventory; map rebuild replaces any copied map tables.
                    stage('scan-fallback')
                    inventory = scan(project, destination, config if config.exists() else None)
                    delta = changes(before, inventory)
            elif before and not rebuild and plan['mode'] == 'script':
                stage('remap-code-check-assets')
                try:
                    _remap_code_reuse_assets(previous, destination, inventory, delta, options, root)
                    reused = True
                except ValueError:
                    stage('scan-fallback')
                    inventory = scan(project, destination, config if config.exists() else None)
                    delta = changes(before, inventory)
            elif before and not rebuild and plan['mode'] == 'asset_scalar':
                stage('reuse-scalar-asset-maps')
                try:
                    if _reuse_maps_for_scalar_assets(previous, destination, inventory, delta, root):
                        reuse_mode = 'asset_reference'
                    reused = True
                except ValueError:
                    stage('scan-fallback')
                    inventory = scan(project, destination, config if config.exists() else None)
                    delta = changes(before, inventory)
            elif before and not rebuild and plan['mode'] == 'asset_delete':
                stage('reuse-isolated-asset-deletion')
                try:
                    _reuse_maps_for_unreferenced_asset_deletion(previous, destination, inventory, delta, root)
                    reused = True
                except ValueError:
                    stage('scan-fallback')
                    inventory = scan(project, destination, config if config.exists() else None)
                    delta = changes(before, inventory)
            elif before and not rebuild and plan['mode'] == 'asset_add':
                stage('reuse-isolated-asset-addition')
                try:
                    _reuse_maps_for_isolated_asset_addition(previous, destination, inventory, delta, root)
                    reused = True
                except ValueError:
                    stage('scan-fallback')
                    inventory = scan(project, destination, config if config.exists() else None)
                    delta = changes(before, inventory)
            if not reused:
                stage('code')
                map_code(destination, **options, analyzer_workspace=root)
                stage('assets')
                if selected_compression:
                    map_assets(destination, compress_report=True)
                else:
                    map_assets(destination)
            stage('retrieval')
            build_retrieval(destination, chunk_budget=selected_chunks['budget'], chunk_encoding=selected_chunks['encoding'])
            semantic_report = None
            if enabled:
                stage('semantic')
                if old_semantic:
                    _backup(previous / 'semantic.sqlite3', destination / 'semantic.sqlite3')
                semantic_report = build_semantic(destination, provider=provider, model=selected_model,
                                                 include_kinds=selected_kinds, progress=semantic_progress, batch_size=selected_batch_size)
            seal = _create_metadata_seal(destination)
            stage('verify')
            with tempfile.TemporaryDirectory(prefix='verify-', dir=root) as temporary:
                verified = scan(project, temporary, config if config.exists() else None)
            if (_identity(verified) != _identity(inventory) or settings['pipeline'] != _pipeline_hash() or
                    any(hashlib.sha256(Path(p).read_bytes()).hexdigest() != digest
                        for p, digest in reference_hashes.items())):
                raise ValueError('Project changed during refresh. Previous snapshot preserved; retry refresh.')
            manifest = {'schema_version': 1, 'generation': name, 'settings': settings, 'metadata_seal': seal,
                        'completed_at': datetime.now(timezone.utc).isoformat(), 'changes': delta}
            _atomic(destination / 'complete.json', manifest)
            stage('publish')
            _atomic(root / 'active.json', _published_pointer(destination, name))
            journal.update(state='complete')
            _atomic(root / 'refresh-state.json', journal)
            return {**manifest, 'state': 'complete', 'semantic': semantic_report,
                    'refresh_mode': reuse_mode if reused else 'full'}
        except BaseException as exc:
            journal.update(state='failed', error=str(exc))
            _atomic(root / 'refresh-state.json', journal)
            raise


def _metadata(workspace, key):
    with sqlite3.connect((workspace / 'inventory.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else {}


def workspace_status(workspace, *, threshold=50):
    from .discovery import scan, status
    from .retrieval import retrieval_status
    from .semantic import semantic_status
    if type(threshold) is not int or threshold < 1:
        raise ValueError('Refresh threshold must be a positive integer.')
    root = Path(workspace).resolve()
    result = {'workspace': str(root), 'healthy': False}
    journal = root / 'refresh-state.json'
    result['last_refresh'] = _read(journal) if journal.exists() else None
    try:
        with writer_lock(root):
            result['writer_active'] = False
    except ValueError:
        result['writer_active'] = True
    result['interrupted'] = bool(result['last_refresh'] and result['last_refresh']['state'] == 'running' and not result['writer_active'])
    try:
        active = resolve_workspace(root)
        inventory = status(active)
        result['generation'] = active.name if active != root else None
        result['retrieval'] = retrieval_status(active)
        if (active / 'semantic.sqlite3').exists():
            result['semantic'] = semantic_status(active)
        with tempfile.TemporaryDirectory(prefix='unity-rag-status-') as temporary:
            base = Path(temporary)
            config = base / 'config.json'
            config.write_text(json.dumps(inventory['config']), encoding='utf-8')
            current = scan(inventory['project'], base / 'scan', config)
        settings = _read(active / 'complete.json')['settings'] if (active / 'complete.json').exists() else {}
        result['pipeline_changed'] = bool(settings and not _pipeline_compatible(settings.get('pipeline')))
        result['references_changed'] = any(hashlib.sha256(Path(p).read_bytes()).hexdigest() != digest
                                          for p, digest in settings.get('reference_hashes', {}).items())
        result.update(healthy=True, fresh=(_identity(inventory) == _identity(current)
                      and not result['pipeline_changed'] and not result['references_changed']),
                      changes=changes(inventory, current))
        result['refresh_plan'] = refresh_plan(inventory, current, pipeline_compatible=not result['pipeline_changed'],
                                              references_changed=result['references_changed'], threshold=threshold)
    except (OSError, ValueError, sqlite3.Error) as exc:
        result['error'] = str(exc)
    result['unpublished_generations'] = sorted(p.name for p in (root / 'generations').glob('*')
        if p.is_dir() and not (p / 'complete.json').exists() and not (p / 'unused.json').exists())
    return result


def _pipeline_hash():
    base = Path(__file__).parent
    names = ('discovery.py', 'code_map.py', 'asset_map.py', 'unity_yaml.py', 'retrieval.py', 'semantic.py', 'embeddings.py',
             'chunking.py', 'context_packing.py')
    paths = [base / name for name in names] + sorted((base / 'roslyn').glob('*'))
    if not all((base / name).is_file() for name in names):
        raise ValueError('Required index pipeline component missing.')
    digest = hashlib.sha256(b'index-abi-v2')
    for path in paths:
        if not path.is_file():
            continue
        data = path.read_bytes()
        if path.name == 'retrieval.py':
            marker = b'def _database_stamp('
            if data.count(marker) != 1:
                raise ValueError('Cannot identify the retrieval index-builder boundary.')
            data = data.split(marker, 1)[0]
        digest.update(path.name.encode() + data)
    return digest.hexdigest()


def _pipeline_compatible(saved):
    current = _pipeline_hash()
    # Accept only verified legacy snapshots with byte-identical index-building code.
    previous = {'40f2e992072d6e28a7e7612ad50d289799e065116a217ff726bef44e1f2186fa',
                '18905bffbfcc445fe01f27332a5bfb4841b6fa1a6567ebc99399d08acf6d2987',
                'bbcd82fe4c99a6105e13eedb602a3b2e4fafd1e83c6035e9478c8ccf5c3b3143'}
    legacy = 'c581401b33685466419eeb7cf9c66f5b02be4fd15d8d46ed8544b744e5a355d4'
    provenance_only = 'ec837bc062003b5b91b0e86add8fca2236b727278db79e2f2ed30dd207268d22'
    simple_asmdef = '9b74e770ddc2ee0729ecbf13e8bb9e81c12319487e8601883062879f51549ecf'
    # The new mapper adds provenance and optional reuse; older maps remain readable,
    # but cannot be reused by group until a normal code remap records provenance.
    text_report_pipeline = '82369f7ab5f7b77e4b58360dba7798ce5ae53bf716c980c90adda4a87897012a'
    optional_compression_pipeline = '4c312291034db34e036b2945c2a630af241b6f35edbe0556f4333ea826cf0cdb'
    return (saved == current or (saved == text_report_pipeline and current == optional_compression_pipeline)
            or (saved in previous and current == legacy)
            or (saved in previous | {legacy} and current == provenance_only)
            or (saved in previous | {legacy, provenance_only} and current == simple_asmdef))


def watch(workspace, *, interval=5, iterations=0, **options):
    """Poll using the same locked refresh path. Zero iterations means until interrupted."""
    if interval < 1 or iterations < 0:
        raise ValueError('Watch interval must be at least one second; iterations cannot be negative.')
    count = 0
    while True:
        yield refresh(workspace, **options)
        count += 1
        if iterations and count >= iterations:
            return
        time.sleep(interval)
