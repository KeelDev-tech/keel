"""Census, protected supply cohorts, bounded reads, and interrupted-run holds."""
import copy
import os
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engines'))
import muse_bridge as bridge
import supply_recovery as recovery
import pipeline_service as pipeline
import queue_io
from safe_io import atomic_json, read_json, utc_now, digest

@pytest.fixture
def home(tmp_path):
    for name in bridge.QUEUES:
        atomic_json(tmp_path / f'data/queues/{name}-queue.json', [])
    atomic_json(tmp_path / 'data/application-ledger.json', [])
    for name in ('answer_bank', 'policy', 'applicant_profile'):
        atomic_json(tmp_path / f'data/{name}.json', {})
    (tmp_path / 'data/employer-blocklist.md').write_text('')
    with patch.object(queue_io, '_LOCK_PATH', str(tmp_path / 'hidden_files/queue.lock')):
        yield tmp_path

def row(i=1, **fields):
    return {'role_id': f'role-{i}', 'company': 'Fixture', 'title': f'Job {i}',
            'application_url': f'https://job-boards.greenhouse.io/fixture/jobs/{i}',
            'fit_score': 80, 'action_band': 'APPLY', 'status': 'PARKED-PENDING-VERIFICATION', **fields}

def put(home, records, name='standard'):
    atomic_json(home / f'data/queues/{name}-queue.json', records)

class Host:
    def observe(self, request):
        states = dict(task_state='CLEAR', attempt_state='CLEAR', form_state='UNKNOWN',
                      materials_state='UNKNOWN', approval_state='UNKNOWN')
        return {**request, **states, 'evidence_refs': {k: 'fixture-receipt' for k in states}}

def reader(fetcher=None, maximum=2):
    return pipeline.PublicBoardReader(fetcher=fetcher or (lambda *_: {'jobs': []}),
                                      max_requests=maximum, retry_timeouts=True)

def test_census_read_only_bound_and_unknown(home):
    put(home, [row()])
    before = {p: p.read_bytes() for p in (home/'data').rglob('*') if p.is_file()}
    report = bridge.census(home)
    assert report['queue_rows'] == 1 and report['launchable_ready'] is None
    assert report['network_reads'] == 0 and not report['execution_authorized']
    assert all(p.read_bytes() == b for p, b in before.items())
    assert set(report['manifest']['input_hashes']) == set(bridge.INPUTS)
    with patch.object(queue_io, '_LOCK_PATH', str(home/'wrong.lock')), pytest.raises(ValueError):
        bridge.census(home)

@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'malformed', 'missing'])
def test_bad_input_fails_closed(home, kind):
    path = home/'data/policy.json'; path.unlink()
    if kind == 'symlink': path.symlink_to(home/'data/answer_bank.json')
    elif kind == 'fifo': os.mkfifo(path)
    elif kind == 'malformed': path.write_text('[]')
    with pytest.raises((ValueError, OSError)):
        recovery.plan(home)

def test_snapshot_binding_is_untrusted(home):
    put(home, [row()]); state = bridge.read_state(home)
    request = bridge.request_for(state, row()); observation = Host().observe(request)
    payload = {'schema': bridge.SCHEMA, 'manifest_sha256': state['manifest']['sha256'],
               'observed_at': utc_now().isoformat(), 'observations': [observation]}
    atomic_json(home/'snapshot.json', payload)
    report = bridge.census(home, host_snapshot='snapshot.json')
    assert not report['host_snapshot']['authenticated'] and report['launchable_ready'] is None
    payload['observations'][0]['row_sha256'] = '0'*64
    atomic_json(home/'snapshot.json', payload)
    with pytest.raises(ValueError): bridge.census(home, host_snapshot='snapshot.json')

@pytest.mark.parametrize('change', [{'task_state':'ACTIVE'}, {'attempt_state':'UNKNOWN'}])
def test_host_holds_exclude_candidates(home, change):
    put(home, [row()])
    class HeldHost(Host):
        def observe(self, request): return {**super().observe(request), **change}
    result = recovery.plan(home, host_provider=HeldHost())
    assert result['selected_count'] == 0 and result['records'][0]['stage'] == 'held'

@pytest.mark.parametrize('fields', [{'fit_score':74}, {'unresolved':['work authorization']},
                                   {'d1_office_exclusion':True}, {'browser_task_id':'busy'},
                                   {'attempt_id':'owned'}, {'status':'SUBMITTED'}, {'action_band':'HOLD'},
                                   {'status':'CLOSED'}, {'status':'CLOSED-EXPIRED'},
                                   {'status':'closed'}, {'status':'Closed-Expired'}])
def test_protected_records_are_not_selected(home, fields):
    put(home, [row(**fields)])
    result = recovery.plan(home)
    assert result['selected_count'] == 0

def test_duplicate_homes_break_conservation(home):
    put(home, [row()]); put(home, [row()], 'needs_input')
    result = recovery.plan(home)
    assert not result['conservation_pass'] and result['selected_count'] == 0

def test_question_classification_is_positive_and_copy_only():
    item = row(unresolved=[], status_reason='needs_input: verify posting')
    original = copy.deepcopy(item)
    recovery.verification_only(item)
    assert item == original
    assert not recovery.verification_only(row(unresolved=['office days'], status_reason='verify posting'))

def test_role_and_request_bounds(home):
    put(home, [row(i) for i in range(1, 41)])
    result = recovery.plan(home, max_requests=2)
    assert result['selected_count'] == 25 and result['planned_sources'] == ['greenhouse:fixture']
    for arguments in ({'limit':26}, {'max_requests':11}, {'limit':True}, {'max_requests':0}):
        with pytest.raises(ValueError): recovery.plan(home, **arguments)

def test_changed_plan_and_missing_provider_do_not_read_or_write(home):
    put(home, [row()]); before = recovery.plan(home, max_requests=2)
    calls=[]; bounded=reader(lambda *args: calls.append(args) or {'jobs':[]})
    with pytest.raises(ValueError, match='authoritative'):
        recovery.canary(home, plan_id=before['plan_id'], max_requests=2, live=True, reader=bounded)
    put(home, [row(2)])
    with pytest.raises(ValueError, match='changed'):
        recovery.canary(home, plan_id=before['plan_id'], max_requests=2, reader=bounded)
    assert not calls and not (home/'data/supply-recovery/history.json').exists()

def test_dry_canary_is_bounded_and_no_promotion(home):
    put(home, [row(), row(2, fit_score=50)])
    before = recovery.plan(home, max_requests=2)
    original = (home/'data/queues/standard-queue.json').read_bytes()
    report = recovery.canary(home, plan_id=before['plan_id'], max_requests=2, reader=reader())
    assert report['verification']['requests'] == 1
    assert report['window']['reader_dispatches'] == 1 and report['promoted_to_ready'] == 0
    assert (home/'data/queues/standard-queue.json').read_bytes() == original
    assert not (home/'data/supply-recovery/history.json').exists()

def test_crash_journal_blocks_replay(home):
    put(home, [row()]); host = Host()
    before = recovery.plan(home, max_requests=2, host_provider=host)
    with patch.object(pipeline, 'verify', side_effect=RuntimeError('interrupted')):
        with pytest.raises(RuntimeError):
            recovery.canary(home, plan_id=before['plan_id'], max_requests=2, live=True,
                            host_provider=host, reader=reader(), run_id='crash')
    status = recovery.recovery_status(home)
    assert status['pending'][0]['state'] == 'RECONCILIATION_REQUIRED'
    assert not status['pending'][0]['authority_to_replay']
    before = recovery.plan(home, max_requests=2, host_provider=host)
    with pytest.raises(ValueError, match='unfinished'):
        recovery.canary(home, plan_id=before['plan_id'], max_requests=2, host_provider=host, reader=reader())

def test_live_only_writes_selected_posting_observations(home):
    selected = row(); held = row(2, fit_score=50); put(home, [selected, held]); host = Host()
    before = recovery.plan(home, max_requests=2, host_provider=host)
    with patch.object(pipeline, 'flush_outbox', return_value={'emitted':0,'pending':0}):
        result = recovery.canary(home, plan_id=before['plan_id'], max_requests=2, live=True,
                                 host_provider=host, reader=reader(), run_id='bounded')
    after = read_json(home/'data/queues/standard-queue.json')
    assert after[1] == held and after[0]['status'] == selected['status']
    assert result['verification']['promoted_to_ready'] == 0
    assert recovery.recovery_status(home)['pending'] == []
    assert recovery.plan(home)['feedback']['reconciled_windows'] == 1

def test_changed_policy_during_dispatch_stops_commit(home):
    put(home, [row()]); host = Host(); before = recovery.plan(home, max_requests=2, host_provider=host)
    original = (home/'data/queues/standard-queue.json').read_bytes()
    def change(*_):
        atomic_json(home/'data/policy.json', {'changed':True}); return {'jobs':[]}
    with patch.object(pipeline, 'flush_outbox', return_value={'emitted':0,'pending':0}):
        recovery.canary(home, plan_id=before['plan_id'], max_requests=2, live=True,
                        host_provider=host, reader=reader(change), run_id='policy-change')
    assert (home/'data/queues/standard-queue.json').read_bytes() == original

def test_invalid_journal_fails_closed(home):
    put(home, [row()])
    run = recovery._seal({'run_id':'bad', 'state':'STARTED','started_at':utc_now().isoformat()})
    atomic_json(home/'data/supply-recovery/history.json', {'schema':recovery.SCHEMA,'runs':[run],'last_selected':{}})
    with pytest.raises(ValueError): recovery.recovery_status(home)

def test_feedback_requires_two_windows_and_matching_policy(home):
    manifest = bridge.read_state(home)['manifest']; policy = digest({'floor':manifest['main_fit_floor'],'code':manifest['code_manifest_sha256']})
    run = {'state':'COMPLETE','policy_sha256':policy,'window':{'conservation_pass':True,'policy_unchanged':True,
           'sources':{'greenhouse:fixture':{'dispatches':2,'observed_prepared_gains':1}}}}
    assert not recovery._feedback({'runs':[run]}, manifest)['enabled']
    assert recovery._feedback({'runs':[run,copy.deepcopy(run)]}, manifest)['observed_yield_by_source']['greenhouse:fixture'] == .5
    stale = {**run,'policy_sha256':'0'*64}
    assert not recovery._feedback({'runs':[run,stale]}, manifest)['enabled']

def test_retargeted_roles_not_counted_as_preparation_gains(home):
    put(home, [row()]); before = recovery.plan(home); after = copy.deepcopy(before)
    after['records'][0].update(prepared_artifact=True, posting_identity=['greenhouse','fixture','999'])
    window = recovery.compare(before, after, {'greenhouse:fixture':1})
    assert window['observed_prepared_gains'] == 0 and window['retargeted_roles'] == 1
    assert window['credits_used'] is None and window['prepared_gains_per_model_token'] is None

def test_unattributed_requests_are_counted():
    assert recovery._dispatch_sources([{'source_url':'https://example.com/unknown'}]) == {'unattributed':1}

def test_host_ownership_changed_during_read_stops_commit(home):
    put(home, [row()]); busy = [False]
    class ChangingHost(Host):
        def observe(self, request):
            return {**super().observe(request), 'task_state':'ACTIVE' if busy[0] else 'CLEAR'}
    host = ChangingHost(); before = recovery.plan(home, max_requests=2, host_provider=host)
    original = (home/'data/queues/standard-queue.json').read_bytes()
    def change(*_): busy[0] = True; return {'jobs':[]}
    with patch.object(pipeline, 'flush_outbox', return_value={'emitted':0,'pending':0}):
        recovery.canary(home, plan_id=before['plan_id'], max_requests=2, live=True,
                        host_provider=host, reader=reader(change), run_id='ownership-change')
    assert (home/'data/queues/standard-queue.json').read_bytes() == original

def test_selected_outbox_does_not_acknowledge_other_roles(home):
    records = [row(i, verification_event_pending={'event_id':f'event-{i}',
                'event_type':'verification_attempt','source':'fixture','details':{'fixture':i}}) for i in (1,2)]
    put(home, records); calls=[]
    def logger(event_type, **values): calls.append(values['role_id']); return {'event_id':values['event_id']}
    pipeline.flush_outbox(home, logger=logger, role_ids=['role-1'])
    after = read_json(home/'data/queues/standard-queue.json')
    assert calls == ['role-1'] and 'verification_event_pending' not in after[0]
    assert after[1] == records[1]

def test_unknown_ledger_history_holds_role(home):
    put(home, [row()]); atomic_json(home/'data/application-ledger.json', [{'role_id':'role-1','status':'UNKNOWN_OUTCOME'}])
    assert recovery.plan(home)['selected_count'] == 0

def test_prepared_packet_counts_without_ready_promotion(home):
    import ready_gate
    now = utc_now()
    item = row(posting_verification={'identity':['greenhouse','fixture','1'],
               'verdict':'live','observed_at':now.isoformat()})
    put(home, [item]); (home/'resume.txt').write_text('Synthetic applicant material')
    packet = ready_gate.seal_packet({'role_id':item['role_id'], 'company':item['company'],
               'title':item['title'], 'ats_url':item['application_url'],
               'brief':'Synthetic preparation', 'upload_files':[str(home/'resume.txt')],
               'scope':'application','execution_authorized':False}, item, {}, workspace=home)
    atomic_json(home/'data/launch-packets/buffer/role-1.json', packet)
    result = recovery.plan(home)
    assert result['records'][0]['stage'] == 'prepared'
    assert result['prepared_artifacts'] == 1 and result['promoted_to_ready'] == 0
    assert result['launchable_ready'] is None

@pytest.mark.parametrize('change', [
    {'observed_at':(utc_now()-timedelta(minutes=10)).isoformat()},
    {'observed_at':(utc_now()+timedelta(minutes=10)).isoformat()},
    {'attempt_state':'invented-clearance'},
    {'approval_state':'APPROVED','approval_expires_at':(utc_now()-timedelta(seconds=1)).isoformat()},
])
def test_invalid_host_evidence_never_authorizes(home, change):
    put(home, [row()]); state = bridge.read_state(home); request = bridge.request_for(state, row())
    class InvalidHost(Host):
        def observe(self, request): return {**super().observe(request), **change}
    assert bridge.observe(InvalidHost(), request)['scope'] == 'UNOBSERVED'


def modern_preparation(home):
    import packet_contract
    now = utc_now()
    item = row(materials={'resume': 'resume.txt'}, posting_verification={
        'identity': ['greenhouse', 'fixture', '1'], 'verdict': 'live',
        'observed_at': now.isoformat()})
    bank = {'answers': {'first_name': 'Synthetic', 'last_name': 'Applicant',
                        'email': 'synthetic@fixture.invalid'}}
    bank['_provenance'] = {key: packet_contract.answer_receipt(value, 'synthetic fixture', now=now)
                           for key, value in bank['answers'].items()}
    policy = {'main_fit_floor': 75}
    atomic_json(home/'data/answer_bank.json', bank)
    atomic_json(home/'data/policy.json', policy)
    (home/'resume.txt').write_text('Synthetic review material')
    put(home, [item, row(2)])
    packet = packet_contract.prepare(item, bank, policy, home, item['materials'],
                                     {'questions': []}, now=now)
    assert packet_contract.validate(packet, item, bank, policy, home, item['materials'], now=now)
    path = home/'data/launch-packets/role-1.json'
    atomic_json(path, packet)
    return item, packet, path, now


def test_modern_preparation_is_counted_without_ready_or_execution(home):
    item, packet, path, now = modern_preparation(home)
    before = {p: p.read_bytes() for p in (home/'data').rglob('*') if p.is_file()}
    result = recovery.plan(home, now=now)
    records = {r['role_id']: r for r in result['records']}
    assert records['role-1']['stage'] == 'prepared'
    assert result['prepared_artifacts'] == 1
    assert [r['role_id'] for r in result['selected']] == ['role-2']
    assert result['promoted_to_ready'] == 0 and result['launchable_ready'] is None
    assert not result['execution_authorized'] and not result['submission_authorized']
    assert packet['execution_authorized'] is False
    assert all(p.read_bytes() == body for p, body in before.items())


@pytest.mark.parametrize('changed', ['source', 'attachment', 'policy', 'bank', 'profile',
                                     'packet', 'expiry', 'entry'])
def test_invalid_modern_preparation_does_not_block_verification_neighbor(home, changed):
    item, packet, path, now = modern_preparation(home)
    if changed == 'source': (home/'resume.txt').write_text('Changed synthetic material')
    elif changed == 'attachment': Path(packet['upload_files'][0]).write_text('Changed copy')
    elif changed == 'policy': atomic_json(home/'data/policy.json', {'main_fit_floor': 75, 'changed': True})
    elif changed == 'bank': atomic_json(home/'data/answer_bank.json', {'answers': {}})
    elif changed == 'profile': atomic_json(home/'data/applicant_profile.json', {'changed': True})
    elif changed == 'entry': put(home, [{**item, 'title': 'Changed title'}, row(2)])
    else:
        if changed == 'packet': packet['brief'] = 'Tampered brief'
        else:
            packet['expires_at'] = (now-timedelta(seconds=1)).isoformat()
            packet['integrity_sha256'] = digest({k:v for k,v in packet.items() if k != 'integrity_sha256'})
        atomic_json(path, packet)
    before = {p: p.read_bytes() for p in (home/'data').rglob('*') if p.is_file()}
    result = recovery.plan(home, now=now)
    assert result['records'][0]['stage'] == 'materials'
    assert result['prepared_artifacts'] == 0
    assert [r['role_id'] for r in result['selected']] == ['role-2']
    assert result['promoted_to_ready'] == 0 and not result['execution_authorized']
    assert all(p.read_bytes() == body for p, body in before.items())
