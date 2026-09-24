"""Offline, explicitly inventoried recovery under the operational write barrier.

The maintenance lock coordinates participating Keel writers. It is neither a
snapshot of the Muse service nor protection against a filesystem owner or an
unmanaged writer. Restores never start controllers or replay outstanding work.
"""
from contextlib import closing
import base64
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from keel_loki.common import (atomic_json, canonical, clone, decode_json, digest,
                              require_dict, require_hash, require_id)
from keel_muse.backup import _new_directory, _write
from tools.bench_inventory import directory_fd, read_file, safe_name

SCHEMA = 'keel.operational.runtime-inventory.v1'
MAX_BYTES = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
PROFILES = {
    'control': ('control.sqlite3', 'control.key'),
    'native_session': ('native-session.sqlite', 'native-session.key'),
    'sources': ('sources.sqlite3', 'store.identity'),
    'temporal': ('temporal.sqlite3',),
    'coordinator': ('coordinator.sqlite3', 'coordinator.sqlite3.key'),
    'recovery': ('recovery.sqlite3', 'recovery.sqlite3.key'),
    'agent': ('agent.sqlite3', 'agent.sqlite3.key', 'reviews.sqlite3'),
    'operational_runtime': ('operational-runtime.sqlite3', 'operational-runtime.key'),
    'skills': (),
}
DATABASES = {kind: tuple(n for n in names if n.endswith(('.sqlite', '.sqlite3')))
             for kind, names in PROFILES.items()}
FLAGS = {'execution_authorized': False, 'controller_started': False,
         'external_service_state_restored': False, 'checkpoint_authority_authenticated': False}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _inside(path, root):
    return path == root or root in path.parents


def validate_inventory(value):
    """An operator's explicit inventory, not discovery or proof of completeness."""
    value = clone(value)
    require_dict(value, {'schema', 'workspace_id', 'source_sha256', 'writer_coordination',
                         'stores', 'required_store_ids', 'canonical_store_ids', 'external_state'})
    _require(value['schema'] == SCHEMA, 'inventory_schema_invalid')
    require_id(value['workspace_id']); require_hash(value['source_sha256'])
    _require(value['writer_coordination'] == 'control_barrier', 'participating_writer_barrier_required')
    stores = value['stores']
    _require(type(stores) is list and 1 <= len(stores) <= 64, 'bounded_nonempty_inventory_required')
    ids, homes = set(), []
    for item in stores:
        _require(type(item) is dict, 'store_specification_required')
        kind = item.get('kind')
        extra = ({'database', 'key'} if kind == 'canonical_sqlite' else
                 {'files'} if kind == 'canonical_files' else set())
        require_dict(item, {'store_id', 'kind', 'home'} | extra)
        require_id(item['store_id'])
        _require(item['store_id'] not in ids, 'duplicate_store_id')
        ids.add(item['store_id'])
        _require(kind in PROFILES or kind in {'canonical_sqlite', 'canonical_files'}, 'unsupported_store_profile')
        _require(type(item['home']) is str, 'absolute_store_home_required')
        home = Path(item['home'])
        _require(home.is_absolute() and str(home) == item['home'] and '..' not in home.parts,
                 'canonical_absolute_store_home_required')
        _require(not any(_inside(home, other) or _inside(other, home) for other in homes),
                 'overlapping_store_homes_forbidden')
        homes.append(home)
        if kind == 'canonical_sqlite':
            for name in (item['database'], item['key']):
                if name is not None:
                    _require(type(name) is str and len(safe_name(name).parts) == 1, 'single_named_database_or_key_required')
            _require(item['database'] is not None and item['database'] != item['key'], 'distinct_database_and_key_required')
        if kind == 'canonical_files':
            _require(type(item['files']) is dict and 1 <= len(item['files']) <= 4096,
                     'explicit_canonical_file_pins_required')
            for name, pin in item['files'].items():
                safe_name(name); require_hash(pin)
    for name in ('required_store_ids', 'canonical_store_ids', 'external_state'):
        members = value[name]
        _require(type(members) is list and len(members) <= 64 and len(members) == len(set(members)),
                 'unique_bounded_inventory_ids_required')
        for member in members:
            require_id(member)
    _require(set(value['required_store_ids']) == ids, 'every_configured_store_must_be_required')
    _require(bool(value['canonical_store_ids']) and set(value['canonical_store_ids']) <= ids,
             'required_canonical_store_missing')
    by_id = {s['store_id']: s for s in stores}
    _require(all(by_id[i]['kind'] in {'sources', 'agent', 'canonical_sqlite', 'canonical_files'}
                 for i in value['canonical_store_ids']), 'canonical_store_profile_required')
    _require(sum(s['kind'] == 'control' for s in stores) == 1, 'exactly_one_control_store_required')
    return value


def _layout(config):
    # Paths are operator placement, not content identity. Restore relocates the
    # exact named stores while preserving their logical inventory and contents.
    return {**config, 'stores': [{k: v for k, v in s.items() if k != 'home'}
                                for s in sorted(config['stores'], key=lambda s: s['store_id'])]}


def _private(info, directory=False):
    _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
             and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
             and info.st_uid == os.getuid() and (directory or info.st_nlink == 1),
             'private_owned_singly_linked_storage_required')


def _walk(home):
    """Descriptor-anchored exhaustive enumeration; reject links and special files."""
    parent = directory_fd(home)
    result = []
    try:
        def descend(fd, prefix=''):
            _private(os.fstat(fd), True)
            for name in sorted(os.listdir(fd)):
                relative = prefix + name
                safe_name(relative)
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        descend(child, relative + '/')
                    finally:
                        os.close(child)
                else:
                    _private(info)
                    _require(info.st_size <= MAX_BYTES, 'individual_store_file_too_large')
                    result.append(relative)
                    _require(len(result) <= 8192, 'store_file_limit')
        descend(parent)
        return result
    finally:
        os.close(parent)


def _names(spec):
    kind = spec['kind']
    if kind == 'canonical_sqlite':
        return [spec['database']] + ([spec['key']] if spec['key'] else []), [spec['database']]
    if kind == 'canonical_files':
        return sorted(spec['files']), []
    return list(PROFILES[kind]), list(DATABASES[kind])


def _inventory_names(spec):
    names, databases = _names(spec)
    actual = _walk(spec['home'])
    allowed = set(names)
    sidecars = {n + suffix for n in databases for suffix in ('-wal', '-shm', '-journal')}
    kind = spec['kind']
    if kind == 'control':
        allowed.add('control.lock')
    if kind == 'skills':
        events = sorted(n for n in actual if re.fullmatch(r'event-[0-9]{8}\.json', n))
        _require(events == [f'event-{i:08d}.json' for i in range(len(events))], 'skill_history_gap')
        names += events; allowed.update(events)
    if kind == 'sources':
        objects = [n for n in actual if re.fullmatch(r'attachments/objects/[0-9a-f]{64}', n)]
        names += objects; allowed.update(objects)
    _require(set(actual) <= allowed | sidecars, 'unlisted_store_files_block_complete_backup')
    _require(set(names) <= set(actual), 'required_store_file_missing')
    if kind == 'control':
        _require('control.lock' in actual, 'control_lock_missing')
    return sorted(names), databases


def _cell(value):
    return {'sqlite_blob_base64': base64.b64encode(value).decode()} if type(value) is bytes else value


def _logical_database(db):
    """Pin all schema and row contents independently of SQLite page layout."""
    _require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'sqlite_integrity_failed')
    schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name').fetchall()
    rows = {}
    total = 0
    for kind, name, _table, _sql in schema:
        if kind != 'table':
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        table_rows = []
        for row in db.execute('SELECT * FROM ' + quoted):
            encoded = canonical([_cell(v) for v in row])
            total += len(encoded)
            _require(total <= MAX_BYTES, 'database_logical_size_limit')
            table_rows.append(encoded)
        table_rows.sort()
        rows[name] = hashlib.sha256(b'\n'.join(table_rows)).hexdigest()
    metadata = {name: db.execute('PRAGMA ' + name).fetchone()[0] for name in
                ('user_version', 'application_id', 'schema_version', 'encoding', 'auto_vacuum')}
    return digest({'schema': [list(r) for r in schema], 'tables': rows, 'database_metadata': metadata})


def _sqlite_copy(home, name):
    parent = directory_fd(home)
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd); _private(before)
        _require(before.st_size <= MAX_BYTES, 'database_size_limit')
        anchored = Path('/proc/self/fd') / str(parent) / name
        started = time.monotonic()
        with closing(sqlite3.connect(anchored.as_uri() + '?mode=ro', uri=True, timeout=1)) as src:
            src.execute('PRAGMA trusted_schema=OFF')
            page_size = src.execute('PRAGMA page_size').fetchone()[0]
            def progress(status, remaining, total):
                _require(status not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED), 'database_busy')
                _require(time.monotonic() - started < 30 and total * page_size <= MAX_BYTES,
                         'bounded_database_copy_required')
            with closing(sqlite3.connect(':memory:')) as copied:
                src.backup(copied, pages=64, progress=progress, sleep=0)
                copied.execute('PRAGMA trusted_schema=OFF')
                logical = _logical_database(copied)
                body = copied.serialize()
        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
        _require((before.st_dev, before.st_ino, before.st_nlink) ==
                 (after.st_dev, after.st_ino, after.st_nlink), 'database_replaced_during_capture')
        _require(len(body) <= MAX_BYTES, 'database_size_limit')
        return body, logical
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _deserialize(db, body):
    # sqlite3_deserialize cannot open a WAL-mode image in :memory:. Online
    # backup has already materialized every committed WAL page. Change only
    # the transient validator image's two journal-version header bytes; the
    # preserved backup/restore file bytes remain untouched.
    _require(body.startswith(b'SQLite format 3\0') and len(body) >= 100, 'sqlite_header_invalid')
    db.deserialize(body[:18] + b'\x01\x01' + body[20:])
    db.execute('PRAGMA trusted_schema=OFF')


def _source_validate(spec, workspace_id, payload):
    identity = decode_json(payload['store.identity'])
    require_dict(identity, {'schema', 'workspace_id', 'store_id'})
    _require(identity['schema'] == 'keel.source_store_identity.v1' and
             identity['workspace_id'] == workspace_id, 'source_workspace_mismatch')
    with closing(sqlite3.connect(':memory:')) as db:
        _deserialize(db, payload['sources.sqlite3'])
        metadata = dict(db.execute('SELECT key,value FROM source_meta'))
        _require(metadata.get('workspace_id') == workspace_id and metadata.get('store_id') == identity['store_id'],
                 'source_database_identity_mismatch')
        previous = '0' * 64
        for body, predecessor, head in db.execute('SELECT body_json,previous_sha256,event_sha256 FROM source_events ORDER BY sequence'):
            _require(predecessor == previous and digest({'previous_sha256': previous, 'body': decode_json(body)}) == head,
                     'source_event_integrity_failed')
            previous = head
        # Every historical approved attachment remains available, including
        # revoked and superseded material. Unreferenced content-addressed
        # objects are retained as well rather than silently discarded.
        for (raw,) in db.execute("SELECT descriptor_json FROM source_records WHERE component='attachments'"):
            record = decode_json(raw).get('record', {})
            for item in record.get('files', []):
                name = 'attachments/' + item['path']
                _require(name in payload and hashlib.sha256(payload[name]).hexdigest() == item['sha256']
                         and len(payload[name]) == item['size_bytes'], 'source_attachment_missing_or_changed')
    for name, body in payload.items():
        if name.startswith('attachments/objects/'):
            _require(hashlib.sha256(body).hexdigest() == name.rsplit('/', 1)[1], 'attachment_content_address_mismatch')


def _validate_profile(spec, workspace_id, payload):
    """Authenticate native journals where a passive reader is available."""
    kind, home = spec['kind'], spec['home']
    for name, body in payload.items():
        if name.endswith('.key'):
            _require(len(body) == 32, 'host_key_length_invalid')
    if kind == 'sources':
        _source_validate(spec, workspace_id, payload)
    elif kind == 'native_session':
        from keel_muse.session import NativeSession
        NativeSession(home, workspace_id).status()
    elif kind == 'coordinator':
        from keel_muse.coordinator import Coordinator
        Coordinator.open_readonly(home, workspace_id).snapshot()
    elif kind == 'recovery':
        from keel_loki.recovery import RecoveryJournal
        RecoveryJournal.open_readonly(home, workspace_id).snapshot()
    elif kind == 'control':
        from .control import StopLedger
        StopLedger(home, workspace_id).snapshot()
    elif kind == 'operational_runtime':
        from .runtime import inspect_home
        inspect_home(home, workspace_id)
    elif kind == 'temporal':
        from keel_loki.temporal import TemporalMemory
        with closing(sqlite3.connect(':memory:')) as db:
            _deserialize(db, payload['temporal.sqlite3']); db.row_factory = sqlite3.Row
            TemporalMemory.__new__(TemporalMemory)._history(db)
    elif kind == 'agent':
        with closing(sqlite3.connect(':memory:')) as db:
            _deserialize(db, payload['agent.sqlite3'])
            meta = dict(db.execute('SELECT name,value FROM metadata'))
            _require(meta.get('workspace_id') == workspace_id and
                     meta.get('key_id') == hashlib.sha256(payload['agent.sqlite3.key']).hexdigest(),
                     'agent_workspace_or_key_mismatch')
    elif kind == 'skills':
        previous = '0' * 64
        for index, name in enumerate(sorted(payload)):
            event = decode_json(payload[name])
            require_dict(event, {'sequence', 'previous_sha256', 'kind', 'payload', 'event_sha256'})
            _require(event['sequence'] == index and event['previous_sha256'] == previous and
                     digest({k: v for k, v in event.items() if k != 'event_sha256'}) == event['event_sha256'],
                     'skill_event_integrity_failed')
            previous = event['event_sha256']
    elif kind == 'canonical_files':
        for name, pin in spec['files'].items():
            _require(hashlib.sha256(payload[name]).hexdigest() == pin, 'canonical_file_pin_mismatch')


def _capture(config):
    files, store_pins, total = {}, {}, 0
    for spec in sorted(config['stores'], key=lambda s: s['store_id']):
        names, databases = _inventory_names(spec)
        bodies, logical = {}, {}
        for name in names:
            if name in databases:
                body, pin = _sqlite_copy(spec['home'], name)
            else:
                body = read_file(spec['home'], name)
                pin = hashlib.sha256(body).hexdigest()
            total += len(body)
            _require(total <= MAX_TOTAL, 'configured_runtime_size_limit')
            bodies[name] = body; logical[name] = pin
            files[spec['store_id'] + '/' + name] = body
        _validate_profile(spec, config['workspace_id'], bodies)
        _require(_inventory_names(spec) == (names, databases), 'store_inventory_changed_during_capture')
        store_pins[spec['store_id']] = {'kind': spec['kind'], 'files': logical, 'store_sha256': digest(logical)}
    links = _cross_validate(config)
    checkpoint = {'schema': 'keel.operational.runtime-checkpoint.v1',
                  'workspace_id': config['workspace_id'], 'source_sha256': config['source_sha256'],
                  'inventory_sha256': digest(_layout(config)), 'stores': store_pins, 'cross_bindings': links,
                  'consistency_scope': 'participating_writers_only', 'external_state_captured': False}
    checkpoint['checkpoint_sha256'] = digest(checkpoint)
    return files, checkpoint


def _cross_validate(config):
    """Bind wrapper metadata to its native session and shared intent ledger.

    PREPARING without a ledger row is a legitimate interrupted commit gap and
    remains unresolved, not repaired or converted into permission by backup.
    """
    from .control import StopLedger
    from keel_muse.session import NativeSession
    specs = config['stores']; workspace = config['workspace_id']
    control = next(s for s in specs if s['kind'] == 'control')
    shared = StopLedger(control['home'], workspace).snapshot()['state']['attempts']
    native = {s['store_id']: NativeSession(s['home'], workspace).status()
              for s in specs if s['kind'] == 'native_session'}
    links = {}
    for spec in specs:
        if spec['kind'] != 'operational_runtime':
            continue
        from .runtime import inspect_home
        state = inspect_home(spec['home'], workspace)['state']
        bindings = [(key, report) for key, report in native.items()
                    if report['config_sha256'] == state['config']['native_config_sha256']]
        _require(len(bindings) == 1, 'runtime_requires_exactly_one_bound_native_session')
        native_id, report = bindings[0]
        _require(report['source_sha256'] == state['config']['source_sha256'], 'runtime_native_source_binding_mismatch')
        for aid, row in state['attempts'].items():
            if aid not in shared:
                _require(row['stage'] in {'PREPARING', 'BLOCKED'}, 'runtime_shared_attempt_missing')
            else:
                _require(shared[aid]['binding'] == row['binding'], 'runtime_shared_attempt_binding_mismatch')
                if row['token'] is not None:
                    _require(digest(row['token']) == shared[aid]['token_sha256'], 'runtime_shared_attempt_token_mismatch')
        if report['pending_state'] == 'ISSUED' and state['active'] is not None:
            row = state['attempts'][state['active']]
            _require(row['request_sha256'] == report['request_sha256'], 'runtime_pending_native_request_mismatch')
        links[spec['store_id']] = native_id
    return links


def _control(config, control):
    from .control import StopLedger
    _require(isinstance(control, StopLedger), 'trusted_stop_ledger_instance_required')
    spec = next(s for s in config['stores'] if s['kind'] == 'control')
    _require(Path(control.home).absolute() == Path(spec['home']) and
             control.workspace_id == config['workspace_id'], 'control_barrier_scope_mismatch')


def checkpoint(config, *, control):
    config = validate_inventory(config); _control(config, control)
    with control.maintenance():
        _files, first = _capture(config)
        _files, second = _capture(config)
        _require(first == second, 'unmanaged_writer_or_changed_checkpoint')
        return first


def _checkpoint(value):
    value = clone(value)
    require_dict(value, {'schema', 'workspace_id', 'source_sha256', 'inventory_sha256', 'stores', 'cross_bindings',
                         'consistency_scope', 'external_state_captured', 'checkpoint_sha256'})
    _require(value['schema'] == 'keel.operational.runtime-checkpoint.v1' and
             value['consistency_scope'] == 'participating_writers_only' and
             value['external_state_captured'] is False, 'checkpoint_boundary_invalid')
    require_id(value['workspace_id'])
    for key in ('source_sha256', 'inventory_sha256', 'checkpoint_sha256'):
        require_hash(value[key])
    _require(type(value['stores']) is dict and 1 <= len(value['stores']) <= 64, 'checkpoint_store_inventory_required')
    for store_id, store in value['stores'].items():
        require_id(store_id)
        require_dict(store, {'kind', 'files', 'store_sha256'})
        _require(type(store['files']) is dict, 'checkpoint_file_inventory_required')
        for name, pin in store['files'].items():
            safe_name(name); require_hash(pin)
        _require(store['store_sha256'] == digest(store['files']), 'checkpoint_store_hash_mismatch')
    _require(type(value['cross_bindings']) is dict, 'checkpoint_cross_bindings_required')
    for source, target in value['cross_bindings'].items():
        _require(source in value['stores'] and target in value['stores']
                 and value['stores'][source]['kind'] == 'operational_runtime'
                 and value['stores'][target]['kind'] == 'native_session', 'checkpoint_cross_binding_invalid')
    _require(value['checkpoint_sha256'] == digest({k: v for k, v in value.items() if k != 'checkpoint_sha256'}),
             'checkpoint_hash_mismatch')
    return value


def _make_parents(root, relative):
    parts = safe_name(relative).parts
    parent = directory_fd(root)
    try:
        for name in parts[:-1]:
            try:
                os.mkdir(Path('/proc/self/fd') / str(parent) / name, mode=0o700)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            _private(os.fstat(child), True)
            os.close(parent); parent = child
        return parent, parts[-1]
    except BaseException:
        os.close(parent)
        raise


def _publish(root, files):
    for relative, body in sorted(files.items()):
        parent, name = _make_parents(root, relative)
        try:
            _write(parent, name, body); os.fsync(parent)
        finally:
            os.close(parent)


def _relocated(layout, root):
    return {**layout, 'stores': [{**s, 'home': str(Path(root) / s['store_id'])} for s in layout['stores']]}


def _create_empty_profile_dirs(config):
    for spec in config['stores']:
        home = Path(spec['home'])
        if not home.exists():
            path, fd = _new_directory(home); os.close(fd)
        if spec['kind'] == 'sources':
            for path in (home / 'attachments', home / 'attachments' / 'objects'):
                if not path.exists():
                    _path, fd = _new_directory(path); os.close(fd)
        if spec['kind'] == 'control':
            parent = directory_fd(home)
            try:
                _write(parent, 'control.lock', b'')
            finally:
                os.close(parent)


def snapshot(config, backup_home, *, control, expected_checkpoint):
    config = validate_inventory(config); _control(config, control)
    expected = _checkpoint(expected_checkpoint)
    destination = Path(backup_home).absolute()
    _require(not any(_inside(destination, Path(s['home'])) or _inside(Path(s['home']), destination)
                     for s in config['stores']), 'backup_must_be_outside_runtime')
    with control.maintenance():
        files, first = _capture(config)
        _require(first == expected, 'backup_checkpoint_mismatch')
        _second_files, second = _capture(config)
        _require(second == first, 'unmanaged_writer_or_changed_checkpoint')
        path, fd = _new_directory(destination)
        try:
            _publish(path, files)
            copied_config = _relocated(_layout(config), path)
            _create_empty_profile_dirs(copied_config)
            _copied, observed = _capture(copied_config)
            _require(observed == first, 'copied_runtime_checkpoint_mismatch')
            # Recheck after publication too. An unmanaged writer cannot be
            # excluded by flock, but an observed change must invalidate this cut.
            _latest_files, latest = _capture(config)
            _require(latest == first, 'runtime_changed_before_backup_publication')
            manifest = {'schema': 'keel.operational.runtime-backup.v1', 'inventory': _layout(config),
                        'checkpoint': first, 'files': {n: hashlib.sha256(b).hexdigest() for n, b in files.items()},
                        'configured_state_complete': True, 'inventory_completeness_authenticated': False,
                        'consistency_scope': 'participating_writers_only', 'contains_private_keys': True,
                        'restore_activation': 'OFFLINE_REVALIDATION_REQUIRED', **FLAGS}
            atomic_json(path / 'backup-manifest.json', manifest)
            os.fsync(fd)
            return {'manifest': manifest, 'manifest_sha256': digest(manifest), 'backup_home': str(path), **FLAGS}
        finally:
            os.close(fd)


def restore(backup_home, destination, *, authoritative_checkpoint, expected_manifest_sha256):
    """Exact-current restore to a new offline directory; no resume operation.

    The independently retained checkpoint must actually be current. Supplying
    an old backup's own checkpoint cannot prove absence of newer state.
    """
    authority = _checkpoint(authoritative_checkpoint); require_hash(expected_manifest_sha256)
    backup = Path(backup_home).absolute()
    _require(not _inside(Path(destination).absolute(), backup), 'restore_destination_must_be_outside_backup')
    manifest = decode_json(read_file(backup, 'backup-manifest.json'))
    require_dict(manifest, {'schema', 'inventory', 'checkpoint', 'files', 'configured_state_complete',
                           'inventory_completeness_authenticated', 'consistency_scope', 'contains_private_keys',
                           'restore_activation'} | set(FLAGS))
    _require(manifest['schema'] == 'keel.operational.runtime-backup.v1' and digest(manifest) == expected_manifest_sha256,
             'backup_manifest_pin_mismatch')
    _require(manifest['checkpoint'] == authority, 'authoritative_current_checkpoint_mismatch')
    _require(manifest['configured_state_complete'] is True and manifest['inventory_completeness_authenticated'] is False
             and manifest['consistency_scope'] == 'participating_writers_only'
             and manifest['contains_private_keys'] is True and manifest['restore_activation'] == 'OFFLINE_REVALIDATION_REQUIRED'
             and all(manifest[k] is v for k, v in FLAGS.items()), 'backup_boundary_invalid')
    config = validate_inventory(_relocated(manifest['inventory'], backup))
    _require(digest(_layout(config)) == authority['inventory_sha256'], 'restore_inventory_binding_mismatch')
    expected_names = {store_id + '/' + name for store_id, s in authority['stores'].items() for name in s['files']}
    _require(type(manifest['files']) is dict and set(manifest['files']) == expected_names, 'backup_exact_file_inventory_required')
    files = {}
    for name, pin in manifest['files'].items():
        require_hash(pin); body = read_file(backup, name)
        _require(hashlib.sha256(body).hexdigest() == pin, 'backup_file_hash_mismatch')
        files[name] = body
    _observed_files, observed = _capture(config)
    _require(observed == authority, 'backup_runtime_checkpoint_mismatch')
    path, fd = _new_directory(destination)
    try:
        # Fence even an interrupted restore before publishing the first runtime
        # store. The marker is deliberately retained after verification; a
        # separate final receipt distinguishes a completed copy from a crash.
        atomic_json(path / 'OFFLINE_RESTORE.json', {
            'schema': 'keel.operational.restore-hold.v1',
            'activation': 'OFFLINE_REVALIDATION_REQUIRED',
            'manifest_sha256': expected_manifest_sha256,
            'expected_checkpoint_sha256': authority['checkpoint_sha256'],
            'completion_receipt': 'RESTORE_VERIFIED.json', **FLAGS})
        os.fsync(fd)
        _publish(path, files)
        restored_config = _relocated(manifest['inventory'], path)
        _create_empty_profile_dirs(restored_config)
        _restored_files, restored = _capture(restored_config)
        _require(restored == authority, 'restored_runtime_checkpoint_mismatch')
        receipt = {'schema': 'keel.operational.runtime-restore.v1', 'manifest_sha256': expected_manifest_sha256,
                   'checkpoint': authority, 'inventory': restored_config, 'configured_state_complete': True,
                   'activation': 'OFFLINE_REVALIDATION_REQUIRED', 'unresolved_intents_preserved': True,
                   'native_browser_session_restored': False, 'destination': str(path), **FLAGS}
        atomic_json(path / 'RESTORE_VERIFIED.json', receipt)
        os.fsync(fd)
        return receipt
    finally:
        os.close(fd)


def demo(home):
    """Actual configured multi-store fixture recovery; no remote operations."""
    from datetime import timedelta
    from .runtime import fixture_environment
    from keel_agent.state import LocalState
    from keel_sources.store import SourceStore
    from keel_sources.decisions import prepare_request, decide_request, revoke_request
    from keel_loki.recovery import RecoveryJournal
    from keel_loki.temporal import TemporalMemory
    from keel_loki.skills import demo as skill_demo
    from keel_muse.coordinator import Coordinator
    from keel_workflow.reviews import ReviewStore
    from tools.make_source_producer_demo import ATTACHMENT, SyntheticClock, fixture_scope, fixture_sources
    path, descriptor = _new_directory(home); os.close(descriptor)
    env = fixture_environment(path / 'host')
    workspace = 'fixture-workspace'
    native, browser, _options = env['make']('interrupted')
    # Observe the first read, then interrupt an actual preparation intent.
    for index in range(2):
        proposed = native.next_request()
        issued = native.next_request(dispatch=True, native_permission=browser.permission(proposed['native']['request']))
        if index == 0:
            native.observe(browser(issued['native']['request']), request_sha256=issued['native']['request_sha256'])
    _require(issued['native']['request']['operation'] != 'accessibility_snapshot', 'fixture_effect_required')
    env['current']['now'] += 31
    native.recover()
    env['ledger'].record_stop({'schema': 'keel.operational.stop.v1', 'event_id': 'fixture-rate-limit',
        'workspace_id': workspace, 'kind': 'RATE_429', 'attempt_id': None,
        'account_id': 'fixture-account', 'role_id': 'fixture-role', 'resource_id': 'fixture-form',
        'observation_sha256': digest('synthetic429')})
    source = SourceStore(path / 'sources', workspace, clock=SyntheticClock())
    scope = {**fixture_scope(), 'workspace_id': workspace}
    source.register_scope(scope)
    records = fixture_sources(scope)
    attachment_hash = hashlib.sha256(ATTACHMENT).hexdigest()
    _publish(source.home, {'attachments/objects/' + attachment_hash: ATTACHMENT})
    records['attachments']['record']['files'][0].update(
        path='objects/' + attachment_hash, sha256=attachment_hash, size_bytes=len(ATTACHMENT))
    for family, record in records.items():
        source.put_source(scope, family, record, expected_generation=0)
    request = prepare_request(source, scope, expires_at=(source.clock() + timedelta(minutes=5)).isoformat())
    decide_request(source, request['request_id'], decision='APPROVE', actor_id='synthetic-reviewer',
        authority_record_ref='synthetic-fixture-authority', reviewed_sha256=request['review_sha256'],
        expires_at=(source.clock() + timedelta(minutes=2)).isoformat())
    revoke_request(source, request['request_id'], actor_id='synthetic-reviewer', reason='synthetic-revocation')
    journal = RecoveryJournal(path / 'journal', workspace, now=100)
    journal.register('job', 'a' * 64, now=101)
    journal.approve('job', 'a' * 64, 'b' * 64, now=102)
    lease = journal.claim('job', 'worker', now=103)
    journal.start('job', 'worker', lease['fence'], now=104)
    journal.revoke('job', now=105); journal.record_429(now=106)
    memory = TemporalMemory(path / 'memory', clock=lambda: 100)
    memory.record_claim({'revision_id': 'revision-1', 'subject_id': 'synthetic-person', 'key': 'name',
        'value': 'Synthetic Applicant', 'source': {'source_id': 'fixture-record', 'sha256': digest('name'),
            'classification': 'personal', 'allowed_scopes': ['fixture-role'], 'permitted_uses': ['application_fact']},
        'scope': 'fixture-role', 'permitted_uses': ['application_fact'], 'valid_from': 10,
        'valid_until': None, 'supersedes': []}, 'fixture-claim')
    agent_home, fd = _new_directory(path / 'agent'); os.close(fd)
    agent = LocalState(agent_home / 'agent.sqlite3', workspace)
    ReviewStore(agent_home / 'reviews.sqlite3')
    coordinator = Coordinator(path / 'coordinator', workspace, {'fixture': lambda _: None},
                              lambda _: None, clock=lambda: 100)
    coordinator.ingest({'schema': 'keel.muse.event.v1', 'event_id': 'fixture-rate', 'workspace_id': workspace,
                        'scope_id': 'fixture-role', 'kind': 'RATE_429', 'payload': {}})
    skill_demo(path / 'skills')
    canonical_home, fd = _new_directory(path / 'canonical'); os.close(fd)
    raw = canonical({'schema': 'synthetic.canonical-observation.v1', 'attempt': 'UNKNOWN', 'approval_revoked': True})
    _publish(canonical_home, {'attempt.json': raw})
    specs = [('control', 'control', env['ledger'].home),
             ('native', 'native_session', env['home'] / 'interrupted-native'),
             ('runtime', 'operational_runtime', env['home'] / 'interrupted-runtime'),
             ('sources', 'sources', source.home), ('journal', 'recovery', path / 'journal'),
             ('memory', 'temporal', path / 'memory'), ('agent', 'agent', agent_home),
             ('coordinator', 'coordinator', path / 'coordinator'), ('skills', 'skills', path / 'skills')]
    stores = [{'store_id': sid, 'kind': kind, 'home': str(where)} for sid, kind, where in specs]
    stores.append({'store_id': 'canonical', 'kind': 'canonical_files', 'home': str(canonical_home),
                   'files': {'attempt.json': hashlib.sha256(raw).hexdigest()}})
    config = {'schema': SCHEMA, 'workspace_id': workspace, 'source_sha256': env['current']['source'],
              'writer_coordination': 'control_barrier', 'stores': stores,
              'required_store_ids': [s['store_id'] for s in stores], 'canonical_store_ids': ['sources', 'agent', 'canonical'],
              'external_state': ['muse-browser', 'employer-service', 'host-qualification-observations']}
    point = checkpoint(config, control=env['ledger'])
    saved = snapshot(config, path / 'backup', control=env['ledger'], expected_checkpoint=point)
    receipt = restore(path / 'backup', path / 'restored', authoritative_checkpoint=point,
                       expected_manifest_sha256=saved['manifest_sha256'])
    from .control import StopLedger
    from keel_muse.session import NativeSession
    restored_control = StopLedger(path / 'restored' / 'control', workspace)
    current = checkpoint(receipt['inventory'], control=restored_control)
    recovered = RecoveryJournal.open_readonly(path / 'restored' / 'journal', workspace).snapshot()['state']
    restored_sources = SourceStore(path / 'restored' / 'sources', workspace, clock=source.clock)
    with closing(sqlite3.connect(path / 'restored' / 'sources' / 'sources.sqlite3')) as db:
        revocations = db.execute('SELECT COUNT(*) FROM source_approval_revocations').fetchone()[0]
    checks = {
        'configured_ten_store_checkpoint_matches': current == point and len(point['stores']) == 10,
        'runtime_native_binding_preserved': point['cross_bindings'] == {'runtime': 'native'},
        'global_429_preserved': restored_control.snapshot()['state']['rate_limited'] is True,
        'recovery_unknown_and_revocation_preserved': recovered['jobs']['job']['phase'] == 'UNKNOWN' and
             recovered['jobs']['job']['approval_active'] is False and recovered['rate_limited'],
        'native_uncertainty_preserved': NativeSession(path / 'restored' / 'native', workspace).status()['status'] == 'UNKNOWN',
        'source_attachment_bytes_preserved': read_file(restored_sources.home, 'attachments/objects/' + attachment_hash) == ATTACHMENT,
        'source_approval_revocation_preserved': revocations == 1,
        'temporal_history_preserved': TemporalMemory(path / 'restored' / 'memory', clock=lambda: 100).history() == memory.history(),
        'coordinator_stop_preserved': Coordinator.open_readonly(path / 'restored' / 'coordinator', workspace).snapshot()['state']['rate_limited'],
        'offline_no_controller_activation': receipt['controller_started'] is False and
             receipt['activation'] == 'OFFLINE_REVALIDATION_REQUIRED',
        'external_state_not_claimed': receipt['external_service_state_restored'] is False,
    }
    return {'schema': 'keel.operational.recovery-demo.v1', 'status': 'PASS' if all(checks.values()) else 'FAIL',
            'checks': checks, 'synthetic': True, 'configured_store_count': len(point['stores']),
            'supported_profiles_exercised': sorted({s['kind'] for s in stores}),
            'real_native_browser_actions': 0, 'real_model_calls': 0, 'real_canonical_writes': 0,
            'account_muse_integration': 'NOT_VERIFIED', 'actual_native_browser': 'NOT_RUN',
            'submission_authorized': False, 'checkpoint_sha256': point['checkpoint_sha256'], **FLAGS}
