"""Real SQLite/files, coherent barriers, preservation and failed restores."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from keel_agent.state import LocalState
from keel_loki.common import digest
from keel_loki.recovery import RecoveryJournal
from keel_loki.temporal import TemporalMemory
from keel_muse.browser import InjectedFixture
from keel_muse.capabilities import fixture_manifest
from keel_muse.session import NativeSession
from keel_sources.store import SourceStore
from keel_workflow.reviews import ReviewStore
from keel_operational.control import StopLedger
from keel_operational import recovery as r


def fixture(tmp_path, *, workspace='workspace'):
    control = StopLedger.create(tmp_path / 'control', workspace, clock=lambda: 100)
    SourceStore(tmp_path / 'sources', workspace)
    config = {'schema': r.SCHEMA, 'workspace_id': workspace, 'source_sha256': digest('source'),
              'writer_coordination': 'control_barrier',
              'stores': [{'store_id': 'control', 'kind': 'control', 'home': str(tmp_path / 'control')},
                         {'store_id': 'sources', 'kind': 'sources', 'home': str(tmp_path / 'sources')}],
              'required_store_ids': ['control', 'sources'], 'canonical_store_ids': ['sources'],
              'external_state': ['muse-native-browser', 'employer-service']}
    return config, control


def add(config, store_id, kind, home, **extra):
    config['stores'].append({'store_id': store_id, 'kind': kind, 'home': str(home), **extra})
    config['required_store_ids'].append(store_id)


def save(tmp_path, config=None, control=None):
    if config is None:
        config, control = fixture(tmp_path)
    point = r.checkpoint(config, control=control)
    saved = r.snapshot(config, tmp_path / 'backup', control=control, expected_checkpoint=point)
    return config, control, point, saved


def restore(tmp_path, point, saved):
    return r.restore(tmp_path / 'backup', tmp_path / 'restored', authoritative_checkpoint=point,
                     expected_manifest_sha256=saved['manifest_sha256'])


def test_configured_state_restores_to_new_offline_private_directory(tmp_path):
    config, control, point, saved = save(tmp_path)
    result = restore(tmp_path, point, saved)
    assert result['activation'] == 'OFFLINE_REVALIDATION_REQUIRED'
    assert result['configured_state_complete'] is True
    assert result['native_browser_session_restored'] is False
    assert result['controller_started'] is result['execution_authorized'] is False
    assert result['checkpoint_authority_authenticated'] is False
    assert (tmp_path / 'restored' / 'OFFLINE_RESTORE.json').is_file()
    restored_control = StopLedger(tmp_path / 'restored' / 'control', 'workspace')
    assert restored_control.snapshot() == control.snapshot()
    assert r.checkpoint(result['inventory'], control=restored_control) == point
    assert 'control/control.lock' not in saved['manifest']['files']
    assert (tmp_path / 'restored' / 'control' / 'control.lock').read_bytes() == b''
    for path in (tmp_path / 'restored').rglob('*'):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_429_unknown_and_revoked_approval_preserved_in_actual_recovery_journal(tmp_path):
    config, control = fixture(tmp_path)
    journal = RecoveryJournal(tmp_path / 'journal', 'workspace', now=100)
    journal.register('job', 'a' * 64, now=101)
    journal.approve('job', 'a' * 64, 'b' * 64, now=102)
    lease = journal.claim('job', 'worker', now=103)
    journal.start('job', 'worker', lease['fence'], now=104)
    journal.revoke('job', now=105)
    journal.record_429(now=106)
    add(config, 'journal', 'recovery', tmp_path / 'journal')
    _, _, point, saved = save(tmp_path, config, control)
    restore(tmp_path, point, saved)
    state = RecoveryJournal.open_readonly(tmp_path / 'restored' / 'journal', 'workspace').snapshot()['state']
    assert state['rate_limited'] is True
    assert state['jobs']['job']['phase'] == 'UNKNOWN'
    assert state['jobs']['job']['approval_active'] is False


def test_native_issued_action_survives_without_redispatch(tmp_path):
    config, control = fixture(tmp_path)
    browser = InjectedFixture(); data = browser.fixture
    session = NativeSession.create(tmp_path / 'native', workspace_id='workspace',
        source_sha256=config['source_sha256'], manifest=fixture_manifest(), contract=data['contract'],
        values=data['values'], host_approvals=data['approvals'], host_snapshot=browser.host_snapshot(),
        attachments=data['attachments'], transport_mode='INJECTED', now=data['now'], clock=lambda: data['now'])
    args = dict(source_sha256=config['source_sha256'], host_snapshot=browser.host_snapshot(), now=data['now'])
    for index in range(2):
        proposal = session.next_request(**args)
        issued = session.next_request(**args, dispatch=True, native_permission=browser.permission(proposal['request']))
        if index == 0:
            session.observe(browser(issued['request']), request_sha256=issued['request_sha256'], **args)
    assert issued['request']['operation'] != 'accessibility_snapshot'
    add(config, 'native', 'native_session', tmp_path / 'native')
    _, _, point, saved = save(tmp_path, config, control)
    restore(tmp_path, point, saved)
    reopened = NativeSession(tmp_path / 'restored' / 'native', 'workspace', clock=lambda: data['now'])
    assert reopened.status() == session.status()
    result = reopened.next_request(**args, dispatch=True)
    assert result['request'] is None and result['dispatch_issued_once'] is False
    assert len(browser.calls) == 1


def test_actual_source_descriptor_attachment_objects_and_history_preserved(tmp_path, monkeypatch):
    # Preserve the predecessor attachment producer and unchanged audit guard:
    # its descriptor-relative mkdir events are interpreted against cwd.
    monkeypatch.chdir(tmp_path)
    from keel_muse.sources import make_demo_inputs
    data = make_demo_inputs(tmp_path / 'evidence')
    data['producer'].capture(data['bindings'], expected_bindings_sha256=digest(data['bindings']),
        flow=data['flow'], expected_flow_sha256=digest(data['flow']), expected_head_sha256=data['expected_head_sha256'])
    workspace = data['scope']['workspace_id']
    config, control = fixture(tmp_path, workspace=workspace)
    config['stores'][1]['home'] = str(data['store'].home)
    _, _, point, saved = save(tmp_path, config, control)
    restore(tmp_path, point, saved)
    objects = [name for name in saved['manifest']['files'] if '/attachments/objects/' in name]
    assert len(objects) == 1
    assert hashlib.sha256((tmp_path / 'restored' / objects[0]).read_bytes()).hexdigest() == objects[0].rsplit('/', 1)[1]
    restored = SourceStore(tmp_path / 'restored' / 'sources', workspace, clock=data['clock'])
    assert restored.export_snapshot() == data['store'].export_snapshot()


def test_missing_or_corrupted_attachment_cannot_claim_complete(tmp_path):
    config, control = fixture(tmp_path)
    objects = tmp_path / 'sources' / 'attachments' / 'objects'; objects.mkdir(mode=0o700)
    path = objects / ('a' * 64); path.write_bytes(b'not matching hash'); path.chmod(0o600)
    with pytest.raises(ValueError, match='attachment_content_address_mismatch'):
        r.checkpoint(config, control=control)


def test_actual_temporal_history_and_local_agent_tables_preserved(tmp_path):
    config, control = fixture(tmp_path)
    memory = TemporalMemory(tmp_path / 'memory', clock=lambda: 100)
    claim = {'revision_id': 'rev-1', 'subject_id': 'person', 'key': 'name', 'value': 'Original',
             'source': {'source_id': 'record', 'sha256': 'a' * 64, 'classification': 'personal',
                        'allowed_scopes': ['role'], 'permitted_uses': ['application_fact']},
             'scope': 'role', 'permitted_uses': ['application_fact'], 'valid_from': 10,
             'valid_until': None, 'supersedes': []}
    memory.record_claim(claim, 'one')
    agent_home = tmp_path / 'agent'; agent_home.mkdir(mode=0o700)
    state = LocalState(agent_home / 'agent.sqlite3', 'workspace')
    ReviewStore(agent_home / 'reviews.sqlite3')
    with sqlite3.connect(state.path) as db:
        db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?)',
                   ('approval', '{}', 100, 200, 101, None, None))
    add(config, 'memory', 'temporal', memory.home)
    add(config, 'agent', 'agent', agent_home)
    _, _, point, saved = save(tmp_path, config, control)
    restore(tmp_path, point, saved)
    assert TemporalMemory(tmp_path / 'restored' / 'memory', clock=lambda: 100).history() == memory.history()
    with sqlite3.connect(tmp_path / 'restored' / 'agent' / 'agent.sqlite3') as db:
        assert db.execute('SELECT revoked_at FROM approvals').fetchone() == (101.0,)


def test_stale_external_authority_and_wrong_manifest_rejected_before_restore(tmp_path):
    config, control, point, saved = save(tmp_path)
    scope = {'workspace_id': 'workspace', 'role_id': 'role', 'application_id': 'app', 'action': 'PREPARE'}
    SourceStore(tmp_path / 'sources', 'workspace').register_scope(scope)
    current = r.checkpoint(config, control=control)
    with pytest.raises(ValueError, match='authoritative_current_checkpoint_mismatch'):
        restore(tmp_path, current, saved)
    with pytest.raises(ValueError, match='backup_manifest_pin_mismatch'):
        r.restore(tmp_path / 'backup', tmp_path / 'restored', authoritative_checkpoint=point,
                  expected_manifest_sha256='f' * 64)
    with pytest.raises(ValueError):
        r.restore(tmp_path / 'backup', tmp_path / 'restored', authoritative_checkpoint=None,
                  expected_manifest_sha256=saved['manifest_sha256'])
    assert not (tmp_path / 'restored').exists()


def test_source_change_after_checkpoint_blocks_snapshot(tmp_path):
    config, control = fixture(tmp_path)
    point = r.checkpoint(config, control=control)
    SourceStore(tmp_path / 'sources', 'workspace').register_scope(
        {'workspace_id': 'workspace', 'role_id': 'role', 'application_id': 'app', 'action': 'PREPARE'})
    with pytest.raises(ValueError, match='backup_checkpoint_mismatch'):
        r.snapshot(config, tmp_path / 'backup', control=control, expected_checkpoint=point)
    assert not (tmp_path / 'backup').exists()


def test_unmanaged_mutation_during_capture_is_detected(tmp_path, monkeypatch):
    config, control = fixture(tmp_path)
    original = r._capture; count = [0]
    def change_after_first(*args):
        captured = original(*args); count[0] += 1
        if count[0] == 1:
            SourceStore(tmp_path / 'sources', 'workspace').register_scope(
                {'workspace_id': 'workspace', 'role_id': 'role', 'application_id': 'app', 'action': 'PREPARE'})
        return captured
    monkeypatch.setattr(r, '_capture', change_after_first)
    with pytest.raises(ValueError, match='unmanaged_writer_or_changed_checkpoint'):
        r.checkpoint(config, control=control)


def test_shared_writer_barrier_produces_coherent_cross_store_cut(tmp_path):
    config, control = fixture(tmp_path)
    for store_id in ('left', 'right'):
        home = tmp_path / store_id; home.mkdir(mode=0o700)
        path = home / 'state.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE state (value INTEGER)'); db.execute('INSERT INTO state VALUES (0)')
        path.chmod(0o600)
        add(config, store_id, 'canonical_sqlite', home, database='state.sqlite3', key=None)
    first_written, finish_write, entered = threading.Event(), threading.Event(), threading.Event()
    def writer():
        with control.writer():
            with sqlite3.connect(tmp_path / 'left' / 'state.sqlite3') as db:
                db.execute('UPDATE state SET value=1')
            first_written.set()
            assert finish_write.wait(5)
            with sqlite3.connect(tmp_path / 'right' / 'state.sqlite3') as db:
                db.execute('UPDATE state SET value=1')
    def reader():
        entered.set()
        return r.checkpoint(config, control=control)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer); assert first_written.wait(5)
        reading = pool.submit(reader); assert entered.wait(5)
        assert not reading.done()
        finish_write.set(); writing.result(timeout=5)
        point = reading.result(timeout=5)
    assert point['stores']['left']['files']['state.sqlite3'] == point['stores']['right']['files']['state.sqlite3']


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'symlink', 'hardlink', 'unsafe_mode'])
def test_invalid_storage_never_silently_omitted(tmp_path, mutation):
    config, control = fixture(tmp_path)
    home = tmp_path / 'sources'; identity = home / 'store.identity'
    if mutation == 'missing': identity.unlink()
    if mutation == 'extra':
        (home / 'forgotten-state.json').write_text('{}'); (home / 'forgotten-state.json').chmod(0o600)
    if mutation == 'symlink':
        target = tmp_path / 'identity'; identity.rename(target); identity.symlink_to(target)
    if mutation == 'hardlink': os.link(identity, tmp_path / 'alias')
    if mutation == 'unsafe_mode': identity.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        r.checkpoint(config, control=control)


@pytest.mark.parametrize('change', ['no_canonical', 'missing_required', 'omitted_required', 'overlap', 'wrong_workspace'])
def test_inventory_scope_and_completeness_are_explicit(tmp_path, change):
    config, control = fixture(tmp_path)
    if change == 'no_canonical': config['canonical_store_ids'] = []
    if change == 'missing_required': config['required_store_ids'].append('missing')
    if change == 'omitted_required': config['required_store_ids'].remove('sources')
    if change == 'overlap': config['stores'][1]['home'] = str(tmp_path / 'control' / 'sub')
    if change == 'wrong_workspace': config['workspace_id'] = 'another'
    with pytest.raises(ValueError): r.checkpoint(config, control=control)


def test_exact_canonical_files_pins_are_required_and_restored(tmp_path):
    config, control = fixture(tmp_path)
    home = tmp_path / 'canonical'; home.mkdir(mode=0o700)
    body = b'{"status":"UNKNOWN","approval_revoked":true}'
    path = home / 'attempts.json'; path.write_bytes(body); path.chmod(0o600)
    add(config, 'canonical', 'canonical_files', home, files={'attempts.json': hashlib.sha256(body).hexdigest()})
    config['canonical_store_ids'].append('canonical')
    _, _, point, saved = save(tmp_path, config, control)
    restore(tmp_path, point, saved)
    assert (tmp_path / 'restored' / 'canonical' / 'attempts.json').read_bytes() == body
    path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='canonical_file_pin_mismatch'): r.checkpoint(config, control=control)


def test_restore_never_overwrites_existing_directory_or_accepts_tampering(tmp_path):
    _, _, point, saved = save(tmp_path)
    destination = tmp_path / 'restored'; destination.mkdir(mode=0o700)
    sentinel = destination / 'existing'; sentinel.write_bytes(b'keep')
    with pytest.raises(FileExistsError): restore(tmp_path, point, saved)
    assert sentinel.read_bytes() == b'keep'
    (tmp_path / 'backup' / 'control' / 'control.key').write_bytes(b'x' * 32)
    with pytest.raises(ValueError, match='backup_file_hash_mismatch'): restore(tmp_path, point, saved)


def test_opaque_sqlite_schema_and_blob_data_roundtrip_without_semantic_authority(tmp_path):
    config, control = fixture(tmp_path)
    home = tmp_path / 'canonical'; home.mkdir(mode=0o700)
    path = home / 'canonical.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE opaque (id TEXT PRIMARY KEY, value BLOB)')
        db.execute('INSERT INTO opaque VALUES (?,?)', ('attempt', b'\x00\xffUNKNOWN'))
    path.chmod(0o600)
    add(config, 'opaque', 'canonical_sqlite', home, database='canonical.db', key=None)
    _, _, point, saved = save(tmp_path, config, control)
    restore(tmp_path, point, saved)
    with sqlite3.connect(tmp_path / 'restored' / 'opaque' / 'canonical.db') as db:
        assert db.execute('SELECT value FROM opaque').fetchone() == (b'\x00\xffUNKNOWN',)
    assert saved['manifest']['inventory_completeness_authenticated'] is False


def test_backup_must_be_outside_all_runtime_stores(tmp_path):
    config, control = fixture(tmp_path)
    point = r.checkpoint(config, control=control)
    with pytest.raises(ValueError, match='backup_must_be_outside_runtime'):
        r.snapshot(config, tmp_path / 'sources' / 'backup', control=control, expected_checkpoint=point)


def test_logical_checkpoint_pins_sqlite_migration_metadata(tmp_path):
    config, control = fixture(tmp_path)
    home = tmp_path / 'canonical'; home.mkdir(mode=0o700)
    path = home / 'state.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE state (value INTEGER)')
        db.execute('PRAGMA user_version=1')
    path.chmod(0o600)
    add(config, 'canonical', 'canonical_sqlite', home, database='state.sqlite3', key=None)
    old = r.checkpoint(config, control=control)
    with control.writer(), sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=2'); db.execute('PRAGMA application_id=123')
    current = r.checkpoint(config, control=control)
    assert old != current
    with pytest.raises(ValueError, match='backup_checkpoint_mismatch'):
        r.snapshot(config, tmp_path / 'backup', control=control, expected_checkpoint=old)


def test_restore_cannot_mutate_original_backup_tree(tmp_path):
    _, _, point, saved = save(tmp_path)
    with pytest.raises(ValueError, match='restore_destination_must_be_outside_backup'):
        r.restore(tmp_path / 'backup', tmp_path / 'backup' / 'control' / 'nested',
                  authoritative_checkpoint=point, expected_manifest_sha256=saved['manifest_sha256'])
    assert not (tmp_path / 'backup' / 'control' / 'nested').exists()
    assert restore(tmp_path, point, saved)['configured_state_complete'] is True


@pytest.mark.parametrize('name', ['state#1.db', 'state?.db', 'state%.db'])
def test_opaque_sqlite_special_filename_characters_are_literal(tmp_path, name):
    config, control = fixture(tmp_path)
    home = tmp_path / 'canonical'; home.mkdir(mode=0o700)
    path = home / name
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE state (value TEXT)'); db.execute("INSERT INTO state VALUES ('kept')")
    path.chmod(0o600)
    add(config, 'canonical', 'canonical_sqlite', home, database=name, key=None)
    _, _, point, saved = save(tmp_path, config, control)
    restore(tmp_path, point, saved)
    with sqlite3.connect(tmp_path / 'restored' / 'canonical' / name) as db:
        assert db.execute('SELECT value FROM state').fetchone() == ('kept',)


def test_ten_store_recovery_demo_exercises_actual_state(tmp_path):
    result = r.demo(tmp_path / 'demo')
    assert result['status'] == 'PASS' and len(result['checks']) >= 11
    assert all(result['checks'].values())
    assert result['configured_store_count'] == 10
    assert result['real_native_browser_actions'] == result['real_model_calls'] == 0


def test_interrupted_restore_is_fenced_before_first_runtime_file(tmp_path, monkeypatch):
    _, _, point, saved = save(tmp_path)
    def interrupted(root, files):
        assert (root / 'OFFLINE_RESTORE.json').is_file()
        assert not (root / 'RESTORE_VERIFIED.json').exists()
        raise OSError('simulated process interruption during file publication')
    monkeypatch.setattr(r, '_publish', interrupted)
    with pytest.raises(OSError, match='simulated process interruption'):
        restore(tmp_path, point, saved)
    assert (tmp_path / 'restored' / 'OFFLINE_RESTORE.json').is_file()
    assert not (tmp_path / 'restored' / 'RESTORE_VERIFIED.json').exists()


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='Linux process-exit recovery rehearsal')
def test_process_exit_mid_restore_leaves_offline_hold_and_no_completion_claim(tmp_path):
    _, _, point, saved = save(tmp_path)
    pid = os.fork()
    if pid == 0:
        original = r._publish
        def exit_after_partial(root, files):
            original(root, {next(iter(files)): next(iter(files.values()))})
            os._exit(73)
        r._publish = exit_after_partial
        try:
            restore(tmp_path, point, saved)
        finally:
            os._exit(74)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 73
    assert (tmp_path / 'restored' / 'OFFLINE_RESTORE.json').is_file()
    assert not (tmp_path / 'restored' / 'RESTORE_VERIFIED.json').exists()
    result = r.restore(tmp_path / 'backup', tmp_path / 'retry-new-destination',
                       authoritative_checkpoint=point, expected_manifest_sha256=saved['manifest_sha256'])
    assert result['configured_state_complete'] is True and result['controller_started'] is False
