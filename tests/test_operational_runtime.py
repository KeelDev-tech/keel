import copy
import os
from pathlib import Path

import pytest

from keel_loki.common import digest
from keel_loki.recovery import RecoveryJournal
from keel_muse.browser import InjectedFixture
from keel_muse.coordinator import Coordinator
from keel_operational.control import StopLedger
from keel_operational.runtime import (OperationalSession, RuntimeError, fixture_environment,
    inspect_home, coordinator_sink, recovery_sink, combined_sink, DB_NAME, KEY_NAME)


@pytest.fixture
def env(tmp_path):
    return fixture_environment(tmp_path / 'environment')


def issue(session, fixture):
    proposal = session.next_request()
    assert proposal['status'] == 'PROPOSED', proposal
    issued = session.next_request(dispatch=True, native_permission=fixture.permission(proposal['native']['request']))
    assert issued['status'] == 'ISSUED', issued
    return issued


def observe(session, fixture):
    issued = issue(session, fixture)
    response = fixture(issued['native']['request'])
    result = session.observe(response, request_sha256=issued['native']['request_sha256'])
    return result, issued, response


def receipt(issued, status):
    request = issued['native']['request']
    return {'schema': 'keel.muse.native-receipt.v1', 'request_id': request['request_id'],
            'request_sha256': digest(request), 'status': status,
            **{k: request[k] for k in ('snapshot_id','snapshot_revision','target_ref','operation')}}


def reopen(session, options):
    return OperationalSession(session.store.home, options['workspace_id'],
                              **{k:v for k,v in options.items() if k not in ('workspace_id','session_id','scope_id')})


def test_full_preparation_actual_readback_and_passive_reopen(env):
    session, fixture, options = env['make']('full')
    status = session.status()
    steps = 0
    while status['status'] != 'SIMULATED':
        status, issued, response = observe(session, fixture)
        assert status['status'] in ('READY','SIMULATED')
        assert session.observe(response, request_sha256=issued['native']['request_sha256'])['native']['idempotent_observation']
        path = session.store.home / DB_NAME
        before = path.read_bytes(), path.stat().st_mtime_ns
        session = reopen(session, options)
        assert before == (path.read_bytes(), path.stat().st_mtime_ns)
        steps += 1
        assert steps <= 23
    assert steps == 23
    assert status['native']['readback']['status'] == 'EXACT_REPORTED_READBACK'
    assert status['native']['actions_reported_applied'] == 11
    assert status['execution_authorized'] is status['submission_authorized'] is False
    assert len(env['ledger'].snapshot()['state']['attempts']) == 23


def test_global_429_blocks_another_wrapped_session_and_reopen(env):
    a, fixture, _ = env['make']('a', scope='role-a')
    b, other, options = env['make']('b', scope='role-b')
    issued = issue(a, fixture)
    halted = a.observe(receipt(issued, 'rate_limited'), request_sha256=issued['native']['request_sha256'])
    assert halted['canonical_stop_deliveries_pending'] >= 1
    assert env['ledger'].snapshot()['state']['rate_limited'] is True
    reopened = reopen(b, options)
    report = reopened.next_request()
    assert report['status'] == 'BLOCKED'
    assert report['native']['request'] is None
    assert other.calls == []


def test_scoped_unknown_holds_same_role_and_resource(env):
    a, fixture, _ = env['make']('a', scope='same-role')
    observe(a, fixture)
    issued = issue(a, fixture)
    report = a.observe(receipt(issued, 'unknown'), request_sha256=issued['native']['request_sha256'])
    assert report['status'] == 'UNKNOWN'
    state = env['ledger'].snapshot()['state']
    assert state['rate_limited'] is False
    assert 'role:same-role' in state['holds']
    assert any(key.startswith('resource:') for key in state['holds'])


def test_missing_native_issuance_after_shared_commit_is_not_replayed(env, monkeypatch):
    session, fixture, options = env['make']('crash')
    proposed = session.next_request()
    original = session.native.next_request
    def die(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get('dispatch'):
            raise SystemExit('injected process death after native commit')
        return result
    monkeypatch.setattr(session.native, 'next_request', die)
    with pytest.raises(SystemExit):
        session.next_request(dispatch=True, native_permission=fixture.permission(proposed['native']['request']))
    session = reopen(session, options)
    again = session.next_request(dispatch=True)
    assert again['status'] == 'WAITING_OBSERVATION'
    assert again['native']['request'] is None
    assert fixture.calls == []
    recovered = session.recover()
    assert recovered['status'] == 'UNKNOWN'
    assert env['ledger'].snapshot()['state']['holds']


def test_admission_output_loss_never_readmits_without_token(env, monkeypatch):
    session, fixture, options = env['make']('lost-token')
    proposed = session.next_request()
    original = session.ledger.admit
    def die(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit('after admit before token store')
    monkeypatch.setattr(session.ledger, 'admit', die)
    with pytest.raises(SystemExit):
        session.next_request(dispatch=True, native_permission=fixture.permission(proposed['native']['request']))
    session = reopen(session, options)
    assert session.next_request(dispatch=True)['native']['request'] is None
    assert len(env['ledger'].snapshot()['state']['attempts']) == 1
    assert session.native.status()['issued_requests'] == 0
    session.recover()
    assert session.next_request()['native']['request'] is None


def test_observation_native_commit_then_crash_can_record_same_observation(env, monkeypatch):
    session, fixture, options = env['make']('observation-gap')
    issued = issue(session, fixture)
    response = fixture(issued['native']['request'])
    def die(*args, **kwargs):
        raise SystemExit('before shared observation commit')
    monkeypatch.setattr(session.ledger, 'finish', die)
    with pytest.raises(SystemExit):
        session.observe(response, request_sha256=issued['native']['request_sha256'])
    session = reopen(session, options)
    assert session.next_request()['native']['request'] is None
    result = session.observe(response, request_sha256=issued['native']['request_sha256'])
    assert result['status'] == 'READY'
    assert result['native']['idempotent_observation'] is True
    assert len(fixture.calls) == 1


@pytest.mark.parametrize('kind', ['source','manifest','tool_schemas','host_config','report','pin'])
def test_qualification_drift_before_dispatch_blocks_without_new_intent(env, kind):
    session, fixture, _ = env['make']('drift')
    proposal = session.next_request()
    bundle = env['current']['bundle']
    if kind == 'source': env['current']['source'] = 'b' * 64
    elif kind == 'manifest': bundle['manifest']['adapter_id'] = 'changed'
    elif kind == 'tool_schemas': next(iter(bundle['tool_schemas'].values()))['description'] = 'changed'
    elif kind == 'host_config': bundle['host_config']['adapter_version'] = 'changed'
    elif kind == 'report': bundle['report']['status'] = 'QUALIFIED_REPORTED'
    else: bundle['expected_report_sha256'] = digest('untrusted-new-pin')
    result = session.next_request(dispatch=True, native_permission=fixture.permission(proposal['native']['request']))
    assert result['status'] == 'BLOCKED'
    assert result['native']['request'] is None
    assert env['ledger'].snapshot()['state']['attempts'] == {}


@pytest.mark.parametrize('field,value', [('consent',False),('no_ai',True),('approval_current',False),
                                        ('holds',['dedupe-hold']),('unknown_attempt',True),('rate_limited',True)])
def test_existing_native_keel_gates_remain_required(env, field, value):
    session, fixture, _ = env['make']('gate')
    proposal = session.next_request()
    fixture.fixture['host_snapshot'][field] = value
    result = session.next_request(dispatch=True, native_permission=fixture.permission(proposal['native']['request']))
    assert result['status'] == 'BLOCKED'
    assert result['native']['dispatch_issued_once'] is False
    assert env['ledger'].snapshot()['state']['attempts'] == {}


def test_late_429_tightens_after_source_drift_and_prior_completion(env):
    session, fixture, _ = env['make']('late')
    _, issued, _ = observe(session, fixture)
    env['current']['source'] = 'f' * 64
    result = session.observe(receipt(issued,'rate_limited'), request_sha256=issued['native']['request_sha256'])
    assert result['native']['rate_limited'] is True
    assert env['ledger'].snapshot()['state']['rate_limited'] is True


def test_forged_rate_receipt_cannot_invent_global_stop(env):
    session, fixture, _ = env['make']('forged')
    issued = issue(session, fixture)
    response = receipt(issued,'rate_limited'); response['request_id'] = 'other-request'
    result = session.observe(response, request_sha256=issued['native']['request_sha256'])
    assert env['ledger'].snapshot()['state']['rate_limited'] is False
    assert result['native']['rate_limited'] is False
    assert result['native']['request'] is None


def test_real_canonical_sinks_write_and_duplicate_without_new_effects(env):
    home = env['home']
    for name in ('coordinator','recovery'): (home/name).mkdir(mode=0o700)
    coordinator = Coordinator(home/'coordinator','fixture-workspace', {'noop':lambda _:None}, lambda _:None, clock=lambda:1000)
    journal = RecoveryJournal(home/'recovery','fixture-workspace',now=1000)
    journal.register('existing-job',digest('revision'),now=1000)
    sink = combined_sink(coordinator_sink(coordinator),recovery_sink(journal,{'fixture-role':['existing-job']},clock=lambda:1000))
    ledger = StopLedger(home/'control','fixture-workspace',snapshot_provider=env['gate'],canonical_sink=sink,clock=lambda:1000)
    session, fixture, _ = env['make']('sink', shared=ledger)
    observe(session,fixture)
    issued = issue(session,fixture)
    result = session.observe(receipt(issued,'rate_limited'),request_sha256=issued['native']['request_sha256'])
    assert result['canonical_stop_deliveries_pending'] == 0
    assert coordinator.snapshot()['state']['rate_limited'] is True
    assert journal.snapshot()['state']['rate_limited'] is True
    events = [r['event'] for r in ledger.snapshot()['state']['outbox'].values()]
    before = coordinator.snapshot()['checkpoint_sha256'],journal.snapshot()['checkpoint_sha256']
    for event in events: sink(event)
    assert before == (coordinator.snapshot()['checkpoint_sha256'],journal.snapshot()['checkpoint_sha256'])


def test_sink_failure_is_pending_and_never_acknowledged_by_input(env):
    def broken(event): raise OSError('canonical host unavailable')
    ledger = StopLedger(env['home']/'control','fixture-workspace',snapshot_provider=env['gate'],canonical_sink=broken,clock=lambda:1000)
    session, fixture, _ = env['make']('sink-fail',shared=ledger)
    issued = issue(session,fixture)
    result = session.observe(receipt(issued,'rate_limited'),request_sha256=issued['native']['request_sha256'])
    assert result['canonical_stop_deliveries_pending'] > 0
    assert all(row['ack'] is None for row in ledger.snapshot()['state']['outbox'].values())
    with pytest.raises(TypeError):
        session.observe({},request_sha256=issued['native']['request_sha256'],acknowledged=True)


def test_clock_expires_qualification_during_final_provider(env):
    from keel_operational.qualification import freeze_plan, grade
    bundle = env['current']['bundle']
    plan = freeze_plan(**env['inputs'], ttl=1)
    report = grade(plan,bundle['report']['records'],expected_plan_sha256=digest(plan),
                  **{k:env['inputs'][k] for k in ('manifest','tool_schemas','host_config','source_sha256','now')})
    bundle.update(plan=plan,expected_plan_sha256=digest(plan),report=report,expected_report_sha256=digest(report))
    session,fixture,_ = env['make']('expires')
    proposal = session.next_request()
    count = [0]
    def provider(binding):
        count[0] += 1
        if count[0] == 3: env['current']['now'] = 1001
        return env['gate'](binding)
    session.ledger.provider = provider
    result = session.next_request(dispatch=True,native_permission=fixture.permission(proposal['native']['request']))
    assert result['native']['request'] is None
    assert result['native']['dispatch_issued_once'] is False
    assert result['status'] == 'UNKNOWN'


def test_private_metadata_tamper_rejected_and_binding_pinned(env):
    session,_,_ = env['make']('integrity')
    snapshot = inspect_home(session.store.home,'fixture-workspace')
    assert snapshot['checkpoint_sha256'] == digest(snapshot['state'])
    import sqlite3
    db=sqlite3.connect(session.store.home/DB_NAME)
    db.execute("UPDATE operational_runtime SET payload='{}'");db.commit();db.close()
    with pytest.raises(RuntimeError, match='integrity'):
        inspect_home(session.store.home,'fixture-workspace')


def test_nondefault_fixture_clock(tmp_path):
    env=fixture_environment(tmp_path/'current-clock',now=1800000000)
    session,fixture,_=env['make']('current')
    assert issue(session,fixture)['status']=='ISSUED'


def test_offline_restore_marker_blocks_new_dispatch(env):
    session,fixture,_=env['make']('restored')
    (session.store.home.parent/'OFFLINE_RESTORE.json').write_text('{}')
    report=session.next_request()
    assert report['status']=='BLOCKED'
    assert report['native']['request'] is None
    assert env['ledger'].snapshot()['state']['attempts']=={}


def test_short_control_snapshot_deadline_cannot_outlive_final_source_provider(env):
    session,fixture,_=env['make']('short-host-deadline')
    proposal=session.next_request()
    calls=[0]
    def provider(binding):
        calls[0]+=1
        value=env['gate'](binding)
        if calls[0]==3:
            value['expires_at']=1001
            env['current']['advance_after_control']=True
        return value
    def source():
        if env['current'].get('advance_after_control'):
            env['current']['now']=1001
        return env['current']['source']
    session.ledger.provider=provider
    session.source_provider=source
    result=session.next_request(dispatch=True,native_permission=fixture.permission(proposal['native']['request']))
    assert result['native']['request'] is None
    assert result['status']=='UNKNOWN'


def test_stop_arriving_during_final_source_read_withholds_request(env):
    session,fixture,_=env['make']('concurrent-stop')
    proposal=session.next_request()
    calls=[0]
    def provider(binding):
        calls[0]+=1
        if calls[0]==3: env['current']['stop_after_control']=True
        return env['gate'](binding)
    def source():
        if env['current'].pop('stop_after_control',False):
            session.ledger.record_stop({'schema':'keel.operational.stop.v1','event_id':'external-stop',
                'workspace_id':'fixture-workspace','kind':'RATE_429','attempt_id':None,
                'account_id':'external-account','role_id':'external-role','resource_id':'external-resource',
                'observation_sha256':digest('actual-host-rate-observation')})
        return env['current']['source']
    session.ledger.provider=provider
    session.source_provider=source
    result=session.next_request(dispatch=True,native_permission=fixture.permission(proposal['native']['request']))
    assert result['native']['request'] is None
    assert result['shared_rate_limited'] is True


@pytest.mark.skipif(not hasattr(os,'fork'),reason='POSIX process-crash test')
def test_real_process_exit_after_native_commit_cannot_reissue(env):
    session,fixture,options=env['make']('process-exit')
    proposed=session.next_request()
    pid=os.fork()
    if pid==0:
        original=session.native.next_request
        def exit_after_commit(*args,**kwargs):
            value=original(*args,**kwargs)
            if kwargs.get('dispatch'): os._exit(77)
            return value
        session.native.next_request=exit_after_commit
        session.next_request(dispatch=True,native_permission=fixture.permission(proposed['native']['request']))
        os._exit(78)
    _,status=os.waitpid(pid,0)
    assert os.waitstatus_to_exitcode(status)==77
    session=reopen(session,options)
    assert session.native.status()['issued_requests']==1
    assert session.next_request(dispatch=True)['native']['request'] is None
    assert session.recover()['status']=='UNKNOWN'
