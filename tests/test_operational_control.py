"""Concurrency, crash, provenance and clock tests for participating writers."""
import json
import os
import sqlite3
import threading
import pytest
from keel_muse.common import digest
from keel_operational.control import StopLedger, DB_NAME, KEY_NAME, LOCK_NAME, demo

class Clock:
    value = 1800000000.0
    def __call__(self): return self.value

def binding(aid='attempt-1', role='role-1', account='account-1', resource='form-1'):
    return {'schema': 'keel.operational.attempt.v1', 'attempt_id': aid, 'worker_id': 'worker-1',
        'workspace_id': 'workspace', 'account_id': account, 'role_id': role, 'resource_id': resource,
        'operation': 'prepare', 'input_sha256': digest(aid), 'source_sha256': digest('source')}

def provider(clock, **changes):
    def get(b):
        return {'schema': 'keel.operational.host-snapshot.v1', 'workspace_id': b['workspace_id'],
            'attempt_sha256': digest(b), 'source_sha256': b['source_sha256'], 'consent': True,
            'no_ai': False, 'approval_current': True, 'holds': [], 'unknown_attempt': False,
            'rate_limited': False, 'issued_at': clock(), 'expires_at': clock() + 60, **changes}
    return get

def ack(event):
    return {'schema': 'keel.operational.canonical-ack.v1', 'workspace_id': event['workspace_id'],
        'event_id': event['event_id'], 'event_sha256': digest(event), 'status': 'APPLIED',
        'canonical_record_sha256': digest(['canonical', event])}

def stop(eid='stop-1', kind='RATE_429', aid=None, **kwargs):
    b = binding(aid or 'attempt-1', **kwargs)
    return {'schema': 'keel.operational.stop.v1', 'event_id': eid, 'workspace_id': 'workspace',
        'kind': kind, 'attempt_id': aid, 'account_id': b['account_id'], 'role_id': b['role_id'],
        'resource_id': b['resource_id'], 'observation_sha256': digest(eid)}

@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    ledger = StopLedger.create(tmp_path / 'control', 'workspace', clock=clock, snapshot_provider=provider(clock))
    return ledger, clock

def issued(ledger, b=None):
    b = b or binding(); token = ledger.admit(b)['token']
    assert ledger.dispatch(b['attempt_id'], token)['status'] == 'ISSUED'
    return token

def test_demo(tmp_path):
    result = demo(tmp_path / 'demo')
    assert result['status'] == 'PASS' and len(result['checks']) == 5
    assert result['real_canonical_writes'] == 0 and not result['execution_authorized']

def test_passive_open_and_final_check_do_not_mutate(setup):
    ledger, clock = setup; token = issued(ledger); before = ledger.snapshot()
    reopened = StopLedger(ledger.home, 'workspace', clock=clock, snapshot_provider=provider(clock))
    assert reopened.snapshot() == before
    assert reopened.check_dispatch('attempt-1', token)['status'] == 'CURRENT'
    assert reopened.snapshot() == before

def test_two_phase_and_replay_fencing(setup):
    ledger, _ = setup; first = ledger.admit(binding())
    assert first['status'] == 'RESERVED' and ledger.admit(binding())['token'] is None
    assert ledger.dispatch('attempt-1', first['token'])['status'] == 'ISSUED'
    assert ledger.dispatch('attempt-1', first['token'])['status'] == 'WAIT'
    with pytest.raises(ValueError): ledger.admit({**binding(), 'input_sha256': digest('other')})
    with pytest.raises(ValueError): ledger.dispatch('attempt-1', '0' * 64)

@pytest.mark.parametrize('operation', ['admit', 'dispatch'])
def test_concurrent_admission_or_dispatch_exactly_one(setup, operation):
    ledger, _ = setup; results, errors = [], []; barrier = threading.Barrier(3)
    token = ledger.admit(binding())['token'] if operation == 'dispatch' else None
    def worker(index):
        try:
            barrier.wait()
            result = ledger.dispatch('attempt-1', token) if token else ledger.admit(binding(str(index)))
            results.append(result['status'])
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads: t.start()
    barrier.wait()
    for t in threads: t.join(10)
    assert not errors and sorted(results) == (['ISSUED', 'WAIT'] if token else ['BLOCKED', 'RESERVED'])

@pytest.mark.parametrize('changes', [dict(consent=False), dict(no_ai=True), dict(approval_current=False),
    dict(holds=['dedupe']), dict(unknown_attempt=True), dict(rate_limited=True), dict(consent=1),
    dict(attempt_sha256='0' * 64), dict(source_sha256='0' * 64), dict(expires_at=1800000000.0)])
def test_canonical_gates_remain_independent(setup, changes):
    ledger, clock = setup; ledger.provider = provider(clock, **changes)
    with pytest.raises(ValueError): ledger.admit(binding())
    assert ledger.snapshot()['state']['attempts'] == {}

def test_json_is_never_a_callback(setup):
    ledger, clock = setup
    with pytest.raises(ValueError): StopLedger(ledger.home, 'workspace', snapshot_provider={'allow': True})
    with pytest.raises(ValueError): StopLedger(ledger.home, 'workspace', canonical_sink={'status': 'APPLIED'})
    passive = StopLedger(ledger.home, 'workspace', clock=clock)
    with pytest.raises(ValueError): passive.admit(binding())
    with pytest.raises(ValueError): passive.flush_outbox()
    assert not hasattr(passive, 'acknowledge') and not hasattr(passive, 'clear')

def test_clock_refreshed_after_provider(setup):
    ledger, clock = setup; original = provider(clock)
    def delayed(b):
        result = original(b); clock.value += 61; return result
    ledger.provider = delayed
    with pytest.raises(ValueError, match='not_fresh'): ledger.admit(binding())
    assert ledger.snapshot()['state']['attempts'] == {}

@pytest.mark.parametrize('operation', ['dispatch', 'check_dispatch'])
def test_clock_refreshed_during_dispatch_and_final_check(setup, operation):
    ledger, clock = setup; token = ledger.admit(binding())['token']
    if operation == 'check_dispatch': ledger.dispatch('attempt-1', token)
    def delayed(b):
        clock.value += 31; return provider(clock)(b)
    ledger.provider = delayed
    assert getattr(ledger, operation)('attempt-1', token)['status'] == 'BLOCKED'
    row = ledger.snapshot()['state']['attempts']['attempt-1']
    assert row['status'] == ('BLOCKED' if operation == 'dispatch' else 'UNKNOWN')

def test_unknown_holds_role_across_accounts_and_account_resource(setup):
    ledger, _ = setup; token = issued(ledger)
    ledger.finish('attempt-1', token, status='UNKNOWN')
    assert ledger.admit(binding('same-role', account='account-2', resource='other'))['status'] == 'BLOCKED'
    assert ledger.admit(binding('same-resource', role='role-2'))['status'] == 'BLOCKED'
    assert ledger.admit(binding('unrelated', role='role-2', resource='form-2'))['status'] == 'RESERVED'
    assert ledger.finish('attempt-1', token, status='RECORDED', receipt_sha256=digest('late'))['status'] == 'UNKNOWN'

def test_reservation_expiry_has_no_uncertain_effect(setup):
    ledger, clock = setup; ledger.admit(binding()); clock.value += 31; ledger.recover()
    state = ledger.snapshot()['state']
    assert state['attempts']['attempt-1']['status'] == 'BLOCKED' and not state['holds'] and not state['outbox']
    assert ledger.admit(binding('new'))['status'] == 'RESERVED'

def test_late_receipt_is_unknown(setup):
    ledger, clock = setup; token = issued(ledger); clock.value += 31
    assert ledger.finish('attempt-1', token, status='RECORDED', receipt_sha256=digest('receipt'))['status'] == 'UNKNOWN'

def test_workspace_429_propagates_across_instances_and_restart(setup):
    ledger, clock = setup; token = issued(ledger)
    ledger.admit(binding('other', role='role-2', resource='form-2'))
    other = StopLedger(ledger.home, 'workspace', clock=clock, snapshot_provider=provider(clock))
    event = stop(); first = other.record_stop(event)
    assert other.record_stop(event) == first
    state = ledger.snapshot()['state']
    assert state['rate_limited'] and state['attempts']['attempt-1']['status'] == 'UNKNOWN'
    assert state['attempts']['other']['status'] == 'BLOCKED'
    assert ledger.check_dispatch('attempt-1', token)['status'] == 'BLOCKED'
    assert other.admit(binding('third', role='role-3', resource='form-3'))['reason'] == 'global_rate_429'
    with pytest.raises(ValueError): other.record_stop({**event, 'observation_sha256': digest('conflict')})

def test_late_429_after_success_still_stops(setup):
    ledger, _ = setup; token = issued(ledger)
    ledger.finish('attempt-1', token, status='RECORDED', receipt_sha256=digest('receipt'))
    ledger.finish('attempt-1', token, status='RATE_429', receipt_sha256=digest('429'))
    assert ledger.snapshot()['state']['rate_limited']

def test_unknown_requires_exact_issued_attempt(setup):
    ledger, _ = setup
    with pytest.raises(ValueError): ledger.record_stop(stop(kind='UNKNOWN', aid='missing'))
    token = ledger.admit(binding())['token']
    with pytest.raises(ValueError): ledger.record_stop(stop(kind='UNKNOWN', aid='attempt-1'))
    ledger.dispatch('attempt-1', token)
    with pytest.raises(ValueError): ledger.record_stop(stop(kind='UNKNOWN', aid='attempt-1', role='role-2'))
    assert ledger.snapshot()['state']['attempts']['attempt-1']['status'] == 'ISSUED'

def test_recorded_is_only_observation_and_conflicts_reject(setup):
    ledger, _ = setup; token = issued(ledger); receipt = digest('receipt')
    result = ledger.finish('attempt-1', token, status='RECORDED', receipt_sha256=receipt)
    assert result['status'] == 'RECORDED' and result['execution_authorized'] is False
    assert ledger.finish('attempt-1', token, status='RECORDED', receipt_sha256=receipt)['duplicate']
    with pytest.raises(ValueError): ledger.finish('attempt-1', token, status='RECORDED', receipt_sha256=digest('other'))
    assert ledger.admit(binding('next'))['status'] == 'RESERVED'

def test_wrong_ack_stays_pending_and_retry_is_idempotent(setup):
    ledger, _ = setup; ledger.record_stop(stop())
    ledger.sink = lambda e: {**ack(e), 'event_sha256': digest('wrong')}
    assert ledger.flush_outbox()['deliveries'][0]['status'] == 'PENDING'
    calls = []
    def sink(e): calls.append(e['event_id']); return ack(e)
    ledger.sink = sink
    assert ledger.flush_outbox()['deliveries'][0]['status'] == 'ACKNOWLEDGED'
    assert ledger.flush_outbox()['deliveries'] == [] and calls == ['stop-1']
    assert ledger.snapshot()['state']['rate_limited']

def test_sink_exception_preserves_pending_and_no_sensitive_error(setup):
    ledger, _ = setup; ledger.record_stop(stop())
    def sink(e): raise RuntimeError('sensitive provider error omitted')
    ledger.sink = sink; ledger.flush_outbox()
    row = ledger.snapshot()['state']['outbox']['stop-1']
    assert row['status'] == 'PENDING' and row['last_error'] == 'RuntimeError'

def test_crash_after_sink_before_ack_replays_same_event_id(setup):
    ledger, clock = setup; ledger.record_stop(stop()); observed = {}
    def crash(e): observed.setdefault(e['event_id'], digest(e)); raise SystemExit('crash')
    ledger.sink = crash
    with pytest.raises(SystemExit): ledger.flush_outbox()
    assert ledger.snapshot()['state']['outbox']['stop-1']['status'] == 'DELIVERING'
    def retry(e): assert observed[e['event_id']] == digest(e); return ack(e)
    ledger.sink = retry
    assert ledger.flush_outbox()['deliveries'] == []
    clock.value += 31
    assert ledger.flush_outbox()['deliveries'][0]['status'] == 'ACKNOWLEDGED' and len(observed) == 1

def test_actual_process_crash_after_durable_intent(setup):
    ledger, clock = setup; token = ledger.admit(binding())['token']; pid = os.fork()
    if pid == 0:
        try:
            child = StopLedger(ledger.home, 'workspace', clock=clock, snapshot_provider=provider(clock))
            child.dispatch('attempt-1', token); os._exit(0)
        except BaseException: os._exit(7)
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0
    assert ledger.snapshot()['state']['attempts']['attempt-1']['status'] == 'ISSUED'
    clock.value += 31; ledger.recover()
    assert ledger.snapshot()['state']['attempts']['attempt-1']['status'] == 'UNKNOWN'
    assert ledger.dispatch('attempt-1', token)['status'] == 'WAIT'

def test_maintenance_excludes_writer_and_passive_snapshot(setup):
    ledger, _ = setup; waiting, completed = threading.Event(), threading.Event(); errors = []
    def writer():
        waiting.set()
        try: ledger.record_stop(stop()); completed.set()
        except Exception as exc: errors.append(exc)
    with ledger.maintenance() as checkpoint:
        thread = threading.Thread(target=writer); thread.start(); assert waiting.wait(2)
        assert not completed.wait(.05) and ledger.snapshot() == checkpoint
    thread.join(10)
    assert not errors and completed.is_set() and ledger.snapshot()['state']['rate_limited']

def test_clock_refreshed_after_barrier_wait(setup):
    ledger, clock = setup; token = ledger.admit(binding())['token']; started = threading.Event(); result = []
    def waiter(): started.set(); result.append(ledger.dispatch('attempt-1', token))
    with ledger.maintenance():
        thread = threading.Thread(target=waiter); thread.start(); assert started.wait(2); clock.value += 31
    thread.join(10)
    assert result[0]['status'] == 'BLOCKED'
    assert ledger.snapshot()['state']['attempts']['attempt-1']['issued_at'] is None

def test_sink_local_writes_are_inside_barrier(setup):
    ledger, _ = setup; ledger.record_stop(stop())
    sink_started, sink_release, checkpoint_entered = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def sink(e):
        sink_started.set(); assert sink_release.wait(5); return ack(e)
    ledger.sink = sink
    def deliver():
        try: ledger.flush_outbox()
        except Exception as exc: errors.append(exc)
    def checkpoint():
        try:
            with ledger.maintenance(): checkpoint_entered.set()
        except Exception as exc: errors.append(exc)
    deliver_thread = threading.Thread(target=deliver); deliver_thread.start(); assert sink_started.wait(2)
    backup_thread = threading.Thread(target=checkpoint); backup_thread.start()
    assert not checkpoint_entered.wait(.05)
    sink_release.set(); deliver_thread.join(10); backup_thread.join(10)
    assert checkpoint_entered.is_set() and not errors

def test_writer_barrier_nesting(setup):
    ledger, _ = setup
    with ledger.writer():
        with ledger.writer(): assert ledger.admit(binding())['status'] == 'RESERVED'

def test_clock_rollback_does_not_mutate(setup):
    ledger, clock = setup; before = ledger.snapshot(); clock.value -= 1
    with pytest.raises(ValueError, match='rollback'): ledger.record_stop(stop())
    assert ledger.snapshot() == before

def test_integrity_tamper_and_missing_key_fail_closed(setup):
    ledger, _ = setup
    with sqlite3.connect(ledger.home / DB_NAME) as db:
        state = json.loads(db.execute('SELECT payload FROM control').fetchone()[0]); state['rate_limited'] = True
        db.execute('UPDATE control SET payload=?', (json.dumps(state),))
    with pytest.raises(ValueError, match='integrity'): ledger.snapshot()
    (ledger.home / KEY_NAME).unlink()
    with pytest.raises((ValueError, OSError)): StopLedger(ledger.home, 'workspace')
    assert not (ledger.home / KEY_NAME).exists()

def test_live_barrier_replacement_detected(setup):
    ledger, _ = setup; path = ledger.home / LOCK_NAME; path.rename(ledger.home / 'retired.lock')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
    with pytest.raises(ValueError, match='replaced'): ledger.record_stop(stop())

def test_private_modes_required(setup):
    ledger, _ = setup; (ledger.home / KEY_NAME).chmod(0o644)
    with pytest.raises(ValueError): ledger.snapshot()

def test_workspace_separation(setup):
    ledger, _ = setup
    with pytest.raises(ValueError): StopLedger(ledger.home, 'other')
    with pytest.raises(ValueError): ledger.record_stop({**stop(), 'workspace_id': 'other'})
    with pytest.raises(ValueError): ledger.admit({**binding(), 'workspace_id': 'other'})


def test_late_unknown_after_recorded_is_conservatively_held(setup):
    ledger, _ = setup; token = issued(ledger)
    ledger.finish('attempt-1', token, status='RECORDED', receipt_sha256=digest('receipt'))
    assert ledger.finish('attempt-1', token, status='UNKNOWN')['status'] == 'UNKNOWN'
    assert ledger.admit(binding('another'))['status'] == 'BLOCKED'


def test_clock_refreshed_after_sqlite_write_lock_wait(setup):
    ledger, clock = setup; token = ledger.admit(binding())['token']
    started, result = threading.Event(), []
    def waiter(): started.set(); result.append(ledger.dispatch('attempt-1', token))
    db = sqlite3.connect(ledger.home / DB_NAME, isolation_level=None)
    db.execute('BEGIN IMMEDIATE')
    try:
        thread = threading.Thread(target=waiter); thread.start(); assert started.wait(2)
        clock.value += 31
    finally:
        db.execute('ROLLBACK'); db.close()
    thread.join(10)
    assert result[0]['status'] == 'BLOCKED'
    assert ledger.snapshot()['state']['attempts']['attempt-1']['issued_at'] is None
