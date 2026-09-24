import copy
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3

import pytest

from keel_loki.common import digest
from keel_muse.browser import InjectedFixture
from keel_muse.capabilities import fixture_manifest
from keel_muse.session import NativeSession, SessionError, NATIVE_GATE, DB_NAME, demo


SOURCE = digest('session-test-source')


def create(tmp_path, fixture=None, **changes):
    fixture = fixture or InjectedFixture()
    data = fixture.fixture
    args = dict(workspace_id='test-session', source_sha256=SOURCE, manifest=fixture_manifest(),
                contract=data['contract'], values=data['values'], host_approvals=data['approvals'],
                host_snapshot=fixture.host_snapshot(), attachments=data['attachments'],
                transport_mode='INJECTED', now=data['now'], clock=lambda: fixture.fixture['now'])
    args.update(changes)
    return NativeSession.create(tmp_path / 'session', **args), fixture


def current(fixture, **changes):
    value = dict(source_sha256=SOURCE, host_snapshot=fixture.host_snapshot(), now=fixture.fixture['now'])
    value.update(changes)
    return value


def issue(session, fixture):
    proposal = session.next_request(**current(fixture))
    assert proposal['status'] == 'PROPOSED'
    assert proposal['dispatch_issued_once'] is False
    assert proposal['request']
    result = session.next_request(**current(fixture), dispatch=True,
                                  native_permission=fixture.permission(proposal['request']))
    assert result['status'] == 'ISSUED'
    assert result['dispatch_issued_once'] is True
    return result


def complete_one(session, fixture):
    issued = issue(session, fixture)
    response = fixture(issued['request'])
    report = session.observe(response, request_sha256=issued['request_sha256'], **current(fixture))
    return report, issued, response


def effect_issued(session, fixture):
    report, _, _ = complete_one(session, fixture)
    assert report['status'] == 'READY'
    result = issue(session, fixture)
    assert result['request']['operation'] != 'accessibility_snapshot'
    return result


def test_full_durable_json_session_all_typed_fields_and_reopen(tmp_path):
    session, fixture = create(tmp_path)
    report = session.status()
    calls = 0
    while report['status'] not in ('SIMULATED', 'BLOCKED', 'UNKNOWN', 'PARTIAL'):
        report, issued, response = complete_one(session, fixture)
        calls += 1
        before = session.status()
        session = NativeSession(tmp_path / 'session', 'test-session', clock=lambda: fixture.fixture['now'])
        assert session.status() == before
        assert calls < 30
    assert report['status'] == 'SIMULATED'
    assert report['action_attempts'] == report['actions_reported_applied'] == 11
    assert report['issued_requests'] == calls == 23
    assert report['readback']['status'] == 'EXACT_REPORTED_READBACK'
    assert report['execution_authorized'] is report['submission_authorized'] is False
    assert report['native_permission_authenticated'] is report['human_approval_authenticated'] is False
    assert report['actual_native_browser'] == 'NOT_RUN'
    assert report['account_muse_integration'] == 'NOT_VERIFIED'
    assert not {'submit', 'evaluate', 'cdp', 'click'} & {r['operation'] for r in fixture.calls}


def test_status_is_read_only_including_open_and_expired_lease(tmp_path):
    session, fixture = create(tmp_path)
    effect_issued(session, fixture)
    db = tmp_path / 'session' / DB_NAME
    before = db.read_bytes(), db.stat().st_mtime_ns
    reopened = NativeSession(tmp_path / 'session', 'test-session', clock=lambda: fixture.fixture['now'])
    status = reopened.status()
    assert status['status'] == 'WAITING_OBSERVATION' and status['request'] is None
    assert before == (db.read_bytes(), db.stat().st_mtime_ns)


def test_issued_request_never_redispatched_by_new_cli_invocation(tmp_path):
    session, fixture = create(tmp_path)
    first = effect_issued(session, fixture)
    session = NativeSession(tmp_path / 'session', 'test-session', clock=lambda: fixture.fixture['now'])
    for dispatch in (False, True):
        report = session.next_request(**current(fixture), dispatch=dispatch)
        assert report['status'] == 'WAITING_OBSERVATION'
        assert report['request'] is None and report['native_tool_name'] is None
        assert report['dispatch_issued_once'] is False
        assert report['request_sha256'] == first['request_sha256']
        assert report['action_attempts'] == 1


def test_identical_observation_idempotent_and_conflict_halts(tmp_path):
    session, fixture = create(tmp_path)
    report, issued, response = complete_one(session, fixture)
    again = session.observe(response, request_sha256=issued['request_sha256'], **current(fixture))
    assert again['idempotent_observation'] is True
    assert again['sequence'] == report['sequence']
    changed = copy.deepcopy(response)
    changed['snapshot_id'] = 'changed'
    conflict = session.observe(changed, request_sha256=issued['request_sha256'], **current(fixture))
    assert conflict['status'] == 'BLOCKED'
    assert conflict['reason'] == 'conflicting_replayed_observation'


@pytest.mark.parametrize('change', ['outer_hash', 'request_id', 'operation', 'snapshot_id', 'snapshot_revision', 'target_ref', 'inner_hash', 'bool_revision'])
def test_receipt_binding_rejects_every_changed_effect_scope(tmp_path, change):
    session, fixture = create(tmp_path)
    issued = effect_issued(session, fixture)
    receipt = fixture(issued['request'])
    pin = issued['request_sha256']
    if change == 'outer_hash': pin = '0' * 64
    elif change == 'inner_hash': receipt['request_sha256'] = '0' * 64
    elif change == 'snapshot_revision': receipt[change] += 1
    elif change == 'bool_revision': receipt['snapshot_revision'] = True
    else: receipt[change] = 'wrong'
    report = session.observe(receipt, request_sha256=pin, **current(fixture))
    assert report['status'] == 'UNKNOWN'
    assert report['actions_reported_applied'] == 0 and report['automatic_retry'] is False
    assert session.next_request(**current(fixture))['request'] is None


@pytest.mark.parametrize('outcome', ['denied', 'ask', 'unknown', 'rate_limited', 'human_takeover'])
def test_nonapplied_receipts_are_durable_terminal_no_retry(tmp_path, outcome):
    session, fixture = create(tmp_path)
    issued = effect_issued(session, fixture)
    receipt = fixture(issued['request'])
    receipt['status'] = outcome
    report = session.observe(receipt, request_sha256=issued['request_sha256'], **current(fixture))
    assert report['status'] == 'UNKNOWN'
    assert report['rate_limited'] is (outcome == 'rate_limited')
    reopened = NativeSession(tmp_path / 'session', 'test-session', clock=lambda: fixture.fixture['now'])
    assert reopened.recover(now=fixture.fixture['now'])['status'] == 'UNKNOWN'
    assert reopened.next_request(**current(fixture))['request'] is None


@pytest.mark.parametrize('flag,value', [('holds', ['new-hold']), ('consent', False), ('approval_current', False),
                                      ('no_ai', True), ('unknown_attempt', True), ('rate_limited', True)])
def test_each_effect_rechecks_fresh_canonical_keel_gate(tmp_path, flag, value):
    session, fixture = create(tmp_path)
    complete_one(session, fixture)
    proposal = session.next_request(**current(fixture))
    fixture.fixture['host_snapshot'][flag] = value
    blocked = session.next_request(**current(fixture), dispatch=True,
                                   native_permission=fixture.permission(proposal['request']))
    assert blocked['status'] == 'BLOCKED'
    assert blocked['action_attempts'] == 0 and blocked['request'] is None
    assert len(fixture.calls) == 1


@pytest.mark.parametrize('change', ['source', 'policy_revision', 'approval_revoked', 'clock_regressed', 'snapshot_stale'])
def test_changes_between_proposal_and_issue_fail_closed(tmp_path, change):
    session, fixture = create(tmp_path)
    complete_one(session, fixture)
    proposal = session.next_request(**current(fixture))
    args = current(fixture)
    if change == 'source': args['source_sha256'] = '0' * 64
    elif change == 'policy_revision': args['host_snapshot']['revisions']['policy'] = '0' * 64
    elif change == 'approval_revoked': args['host_snapshot']['revoked_approval_ids'] = [proposal['request']['action']['approval_id']]
    elif change == 'clock_regressed': args['now'] -= 1
    else: args['now'] += 11
    report = session.next_request(**args, dispatch=True, native_permission=fixture.permission(proposal['request']))
    assert report['status'] == 'BLOCKED' and report['action_attempts'] == 0


def test_source_changed_after_issued_effect_records_unknown(tmp_path):
    session, fixture = create(tmp_path)
    issued = effect_issued(session, fixture)
    receipt = fixture(issued['request'])
    report = session.observe(receipt, request_sha256=issued['request_sha256'],
                             **current(fixture, source_sha256='0' * 64))
    assert report['status'] == 'UNKNOWN' and report['reason'] == 'running_source_changed'


def test_issued_deadline_and_late_applied_receipt_never_resume(tmp_path):
    session, fixture = create(tmp_path)
    issued = effect_issued(session, fixture)
    receipt = fixture(issued['request'])
    expired = current(fixture, now=issued['lease_until'])
    assert session.next_request(**expired)['status'] == 'UNKNOWN'
    assert session.observe(receipt, request_sha256=issued['request_sha256'], **expired)['status'] == 'UNKNOWN'
    assert session.recover(now=issued['lease_until'])['status'] == 'UNKNOWN'


def test_dispatch_lease_bounded_by_policy_and_accessibility_freshness(tmp_path):
    session, fixture = create(tmp_path)
    complete_one(session, fixture)
    fixture.fixture['host_snapshot']['expires_at'] = fixture.fixture['now'] + 3
    issued = issue(session, fixture)
    assert issued['lease_until'] == fixture.fixture['now'] + 3
    assert issued['lease_until'] <= fixture.latest['observed_at'] + 10


def test_no_issue_at_accessibility_freshness_boundary(tmp_path):
    session, fixture = create(tmp_path)
    complete_one(session, fixture)
    proposal = session.next_request(**current(fixture))
    report = session.next_request(**current(fixture, now=fixture.fixture['now'] + 10), dispatch=True,
                                  native_permission=fixture.permission(proposal['request']))
    assert report['status'] == 'BLOCKED' and report['action_attempts'] == 0
    assert report['dispatch_issued_once'] is False and report['request'] is None


def test_explicit_recovery_abandons_unissued_and_read_only_nonce(tmp_path):
    session, fixture = create(tmp_path)
    first = issue(session, fixture)
    recovered = session.recover(now=fixture.fixture['now'])
    assert recovered['status'] == 'READY' and recovered['pending_state'] is None
    second = session.next_request(**current(fixture))
    assert second['request_sha256'] != first['request_sha256']
    assert session.recover(now=fixture.fixture['now'])['status'] == 'READY'


def test_real_child_process_exit_after_committed_intent_is_unknown(tmp_path):
    session, fixture = create(tmp_path)
    complete_one(session, fixture)
    pid = os.fork()
    if pid == 0:
        try:
            child = NativeSession(tmp_path / 'session', 'test-session', clock=lambda: fixture.fixture['now'])
            issue(child, fixture)
            os._exit(0)  # Abrupt exit: no observation, cleanup or Python finalizer.
        except BaseException:
            os._exit(17)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    resumed = NativeSession(tmp_path / 'session', 'test-session', clock=lambda: fixture.fixture['now'])
    assert resumed.status()['status'] == 'WAITING_OBSERVATION'
    report = resumed.recover(now=fixture.fixture['now'])
    assert report['status'] == 'UNKNOWN' and report['action_attempts'] == 1
    assert resumed.next_request(**current(fixture))['request'] is None


def test_host_native_delegation_never_requires_invented_sentinel_allow(tmp_path):
    session, fixture = create(tmp_path, transport_mode='HOST_NATIVE_REPORTED', native_gate=NATIVE_GATE)
    proposal = session.next_request(**current(fixture))
    issued = session.next_request(**current(fixture), dispatch=True)
    assert issued['dispatch_issued_once'] is True
    assert issued['native_gate'] == NATIVE_GATE
    assert issued['native_permission_authenticated'] is False
    assert issued['account_muse_integration'] == 'NOT_VERIFIED'
    response = fixture(issued['request'])
    response['provenance'] = 'HOST_NATIVE_REPORTED'
    for row in response['fields']: row['value_provenance'] = 'accessibility'
    assert session.observe(response, request_sha256=issued['request_sha256'], **current(fixture))['status'] == 'READY'
    proposal = session.next_request(**current(fixture))
    rejected = session.next_request(**current(fixture), dispatch=True,
                                    native_permission=fixture.permission(proposal['request']))
    assert rejected['status'] == 'BLOCKED' and rejected['action_attempts'] == 0


@pytest.mark.parametrize('change', ['missing_gate', 'mapping_required', 'snapshot_binding_absent', 'attachments_forged', 'approval_absent'])
def test_invalid_creation_leaves_no_session(tmp_path, change):
    fixture = InjectedFixture()
    args = {'transport_mode': 'HOST_NATIVE_REPORTED', 'native_gate': NATIVE_GATE}
    if change == 'missing_gate': args['native_gate'] = None
    elif change == 'mapping_required':
        args['manifest'] = fixture_manifest(); args['manifest']['mapping_status'] = 'HOST_MAPPING_REQUIRED'
    elif change == 'snapshot_binding_absent':
        args['manifest'] = fixture_manifest(); args['manifest']['snapshot_action_binding'] = False
    elif change == 'attachments_forged': args['attachments'] = {'resume': 'Zm9yZ2Vk'}
    else: args['host_approvals'] = []
    with pytest.raises(ValueError): create(tmp_path, fixture, **args)
    assert not (tmp_path / 'session').exists()


def test_missing_attachment_hash_stays_partial(tmp_path):
    session, fixture = create(tmp_path, InjectedFixture(attachment_hashes=False))
    report = session.status()
    for _ in range(30):
        if report['status'] in ('PARTIAL', 'BLOCKED', 'UNKNOWN', 'SIMULATED'): break
        report, _, _ = complete_one(session, fixture)
    assert report['status'] == 'PARTIAL'
    assert report['readback']['unverified_attachment_fields'] == ['resume']


def test_unkeyed_database_tamper_detected_without_overwrite(tmp_path):
    session, fixture = create(tmp_path)
    db_path = tmp_path / 'session' / DB_NAME
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE session SET payload=replace(payload, 'READY', 'ISSUED')")
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    with pytest.raises(SessionError, match='integrity'): session.status()
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize('kind', ['workspace', 'symlink', 'hardlink', 'permissions'])
def test_unsafe_storage_or_workspace_rejected(tmp_path, kind):
    session, fixture = create(tmp_path)
    home = tmp_path / 'session'
    db = home / DB_NAME
    workspace = 'test-session'
    if kind == 'workspace': workspace = 'wrong-workspace'
    elif kind == 'symlink':
        other = home / 'other.sqlite'; db.rename(other); db.symlink_to(other)
    elif kind == 'hardlink': os.link(db, home / 'other.sqlite')
    else: db.chmod(0o644)
    with pytest.raises(ValueError): NativeSession(home, workspace)


def test_demo_exercises_handshake_without_native_browser_claim(tmp_path):
    report = demo(tmp_path / 'demo')
    assert report['preparation']['status'] == 'SIMULATED'
    assert report['recovered_effect_status'] == 'UNKNOWN'
    assert report['idempotent_observation'] is True
    assert report['real_browser_actions'] == report['real_model_calls'] == report['external_network_calls'] == 0
    assert report['actual_native_browser'] == 'NOT_RUN'


@pytest.mark.parametrize('later', ['recover', 'expiry', 'changed_source', 'after_applied', 'current'])
def test_late_bound_429_always_tightens_stop_without_replay(tmp_path, later):
    session, fixture = create(tmp_path)
    issued = effect_issued(session, fixture)
    receipt = fixture(issued['request'])
    args = current(fixture)
    if later == 'recover': session.recover(now=fixture.fixture['now'])
    elif later == 'expiry': args['now'] = issued['lease_until']
    elif later == 'changed_source': args['source_sha256'] = '0' * 64
    elif later == 'after_applied':
        session.observe(receipt, request_sha256=issued['request_sha256'], **args)
    receipt['status'] = 'rate_limited'
    report = session.observe(receipt, request_sha256=issued['request_sha256'], **args)
    assert report['status'] == 'UNKNOWN' and report['rate_limited'] is True
    assert report['halt_event']['rate_limited'] is True
    assert report['automatic_retry'] is report['global_host_hold_written'] is False
    assert report['request'] is None
    again = session.observe(receipt, request_sha256=issued['request_sha256'], **args)
    assert again['idempotent_observation'] is True and again['sequence'] == report['sequence']


def test_unbound_429_cannot_claim_a_native_rate_limit(tmp_path):
    session, fixture = create(tmp_path)
    issued = effect_issued(session, fixture)
    receipt = fixture(issued['request']); receipt['status'] = 'rate_limited'
    receipt['target_ref'] = 'wrong-ref'
    result = session.observe(receipt, request_sha256=issued['request_sha256'], **current(fixture))
    assert result['status'] == 'UNKNOWN' and result['rate_limited'] is False


def test_snapshot_429_blocks_and_sticks_even_without_an_effect(tmp_path):
    session, fixture = create(tmp_path)
    issued = issue(session, fixture)
    request = issued['request']
    receipt = {k: request[k] for k in ('request_id', 'operation', 'snapshot_id', 'snapshot_revision', 'target_ref')}
    receipt.update(schema='keel.muse.native-receipt.v1', request_sha256=issued['request_sha256'], status='rate_limited')
    report = session.observe(receipt, request_sha256=issued['request_sha256'], **current(fixture))
    assert report['status'] == 'BLOCKED' and report['rate_limited'] is True
    assert report['action_attempts'] == 0 and report['request'] is None


@pytest.mark.parametrize('stage', ['lock', 'commit'])
def test_live_clock_after_storage_delay_withholds_stale_request(tmp_path, monkeypatch, stage):
    session, fixture = create(tmp_path)
    complete_one(session, fixture)
    proposal = session.next_request(**current(fixture))
    instant = [fixture.fixture['now']]
    session.clock = lambda: instant[0]
    original = session._connection
    @contextmanager
    def delayed(*, readonly=False):
        with original(readonly=readonly) as result:
            if stage == 'lock': instant[0] += 11
            yield result
        if stage == 'commit': instant[0] += 11
    monkeypatch.setattr(session, '_connection', delayed)
    result = session.next_request(**current(fixture), dispatch=True,
                                  native_permission=fixture.permission(proposal['request']))
    assert result['status'] == ('UNKNOWN' if stage == 'commit' else 'BLOCKED')
    assert result['request'] is None and result['dispatch_issued_once'] is False
    assert result['action_attempts'] == (1 if stage == 'commit' else 0)
    assert len(fixture.calls) == 1


def test_injected_permission_expiry_also_bounds_committed_lease(tmp_path):
    session, fixture = create(tmp_path)
    complete_one(session, fixture)
    proposal = session.next_request(**current(fixture))
    permission = fixture.permission(proposal['request'])
    permission['expires_at'] = fixture.fixture['now'] + 1
    issued = session.next_request(**current(fixture), dispatch=True, native_permission=permission)
    assert issued['lease_until'] == fixture.fixture['now'] + 1
