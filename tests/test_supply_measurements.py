"""No applicant payloads escape metadata snapshots; replay never gains authority."""
import copy
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engines'))
import muse_bridge as bridge
import queue_io
import supply_measurements as measured
from safe_io import atomic_json,utc_now
from tools.run_supply_replay import replay,validate_cases

@pytest.fixture
def home(tmp_path):
    for name in bridge.QUEUES:atomic_json(tmp_path/f'data/queues/{name}-queue.json',[])
    atomic_json(tmp_path/'data/application-ledger.json',[])
    for name in ('answer_bank','policy','applicant_profile'):atomic_json(tmp_path/f'data/{name}.json',{})
    (tmp_path/'data/employer-blocklist.md').write_text('')
    (tmp_path/'hidden_files').mkdir();(tmp_path/'hidden_files/measurement.key').write_bytes(os.urandom(32))
    with patch.object(queue_io,'_LOCK_PATH',str(tmp_path/'hidden_files/queue.lock')):yield tmp_path


def lead(number=1,**changes):
    return {'role_id':'PRIVATE-ROLE-'+str(number),'company':'PRIVATE-EMPLOYER','title':'PRIVATE-TITLE',
        'application_url':f'https://job-boards.greenhouse.io/private-board/jobs/{number}',
        'status':'PARKED-PENDING-VERIFICATION','fit_score':80,'action_band':'APPLY',**changes}


def put(home,records):atomic_json(home/'data/queues/standard-queue.json',records)

def snapshot(home,**kwargs):return measured.snapshot(home,key_file='hidden_files/measurement.key',**kwargs)


def test_snapshot_minimizes_payload_and_changes_no_workspace_bytes(home):
    put(home,[lead(unresolved=['PRIVATE-QUESTION'],queue_notes='PRIVATE-NOTE')])
    atomic_json(home/'data/answer_bank.json',{'answers':{'email':'PRIVATE-EMAIL'},'_provenance':{}})
    before={p:p.read_bytes() for p in home.rglob('*') if p.is_file()}
    value=snapshot(home);encoded=json.dumps(value)
    for secret in ('PRIVATE-ROLE','PRIVATE-EMPLOYER','PRIVATE-TITLE','PRIVATE-QUESTION','PRIVATE-NOTE','PRIVATE-EMAIL','private-board',str(home)):
        assert secret not in encoded
    assert all(p.read_bytes()==b for p,b in before.items())
    assert value['model_tokens'] is None and value['credits_used'] is None
    assert value['launchable_ready'] is None and value['network_reads']==0
    measured.validate(value)


def test_keyed_references_are_stable_and_change_with_key(home):
    put(home,[lead()]);a=snapshot(home);b=snapshot(home)
    assert a['roles'][0]['role_ref']==b['roles'][0]['role_ref']
    (home/'hidden_files/measurement.key').write_bytes(os.urandom(32));c=snapshot(home)
    assert c['roles'][0]['role_ref']!=a['roles'][0]['role_ref']
    with pytest.raises(ValueError):measured.compare(a,c)


@pytest.mark.parametrize('kind',['missing','short','symlink','outside'])
def test_key_path_fails_closed(home,kind):
    path=home/'hidden_files/measurement.key'
    if kind=='missing':path.unlink()
    elif kind=='short':path.write_bytes(b'short')
    elif kind=='symlink':path.unlink();path.symlink_to(home/'data/policy.json')
    with pytest.raises((ValueError,OSError)):
        measured.snapshot(home,key_file='/tmp/outside.key' if kind=='outside' else 'hidden_files/measurement.key')


def attempt(number=1):
    request={'request_number':1,'source_url':'https://boards-api.greenhouse.io/v1/boards/private-board/jobs',
        'observed_at':utc_now().isoformat(),'retry_index':0,'transport_class':'HTTP_429'}
    return {'observation_id':f'run:role-{number}','signal':'NONE','transport_class':'HTTP_429',
        'evidence':{'request_attempts':[request]}}


def test_shared_board_dispatch_is_counted_once(home):
    a=attempt();b=copy.deepcopy(a);b['observation_id']='run:role-2'
    put(home,[lead(verification_attempt=a),lead(2,verification_attempt=b)])
    value=snapshot(home)
    assert value['retained_dispatches']==1 and not value['dispatch_coverage_complete']
    assert 'boards-api' not in json.dumps(value)


def test_snapshot_comparison_counts_changes_without_double_counting(home):
    put(home,[lead()]);now=utc_now();a=snapshot(home,now=now)
    put(home,[lead(verification_attempt=attempt())]);b=snapshot(home,now=now+timedelta(seconds=1))
    window=measured.compare(a,b)
    assert window['new_retained_dispatches']==1 and window['observed_prepared_gains']==0
    assert window['causal_attribution'] is False
    c=snapshot(home,now=now+timedelta(seconds=2));assert measured.compare(b,c)['new_retained_dispatches']==0
    assert measured.compare(b,c)['gains_per_new_retained_dispatch'] is None


def test_retargets_and_policy_changes_prevent_false_yield(home):
    put(home,[lead()]);now=utc_now();a=snapshot(home,now=now)
    put(home,[lead(application_url='https://job-boards.greenhouse.io/private-board/jobs/999')]);b=snapshot(home,now=now+timedelta(seconds=1))
    assert measured.compare(a,b)['retargeted_roles']==1
    atomic_json(home/'data/policy.json',{'changed':True});c=snapshot(home,now=now+timedelta(seconds=2))
    with pytest.raises(ValueError):measured.compare(b,c)


@pytest.mark.parametrize('field,value',[('credits_used',1),('model_tokens',100),('launchable_ready',1),('retained_dispatches',4)])
def test_even_resealed_invalid_claims_are_rejected(home,field,value):
    v=snapshot(home);v[field]=value;measured.seal(v)
    with pytest.raises(ValueError):measured.validate(v)


def test_unknown_reason_strings_cannot_escape(home):
    put(home,[lead()])
    import supply_recovery
    original=supply_recovery._inventory
    def injected(*args,**kwargs):
        records,entries=original(*args,**kwargs);records[0]['reason_codes'].append('PRIVATE-SECRET');return records,entries
    with patch.object(supply_recovery,'_inventory',side_effect=injected):v=snapshot(home)
    assert 'PRIVATE-SECRET' not in json.dumps(v)
    assert 'unclassified_blocker' in v['roles'][0]['reason_codes']


def test_snapshot_failures_export_only_fixed_recipes(home):
    put(home,[lead(unresolved=['PRIVATE-ANSWER'])]);cases=measured.failure_cases(snapshot(home))
    assert cases['cases'][0]['scenario']=='questions'
    assert 'PRIVATE' not in json.dumps(cases)
    cases['cases'][0]['target']='https://attacker.invalid'
    with pytest.raises(ValueError):validate_cases(cases)


def test_recipe_replay_checks_production_path_with_synthetic_data(home,tmp_path):
    put(home,[lead(d1_office_exclusion=True)]);cases=measured.failure_cases(snapshot(home))
    value=replay(cases,tmp_path/'replay',prohibited_events=[])
    assert value['status']=='PASS' and value['cases_passed']==1
    assert not value['incident_reconstructed'] and not value['input_authenticated']
    assert not value['live_muse_verified'] and not value['submission_authorized']


def test_cli_replay_runs_without_site_packages(home,tmp_path):
    put(home,[lead(unresolved=['PRIVATE-ANSWER'])]);cases=measured.failure_cases(snapshot(home))
    path=tmp_path/'cases.json';atomic_json(path,cases)
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,'-S',str(root/'tools/run_supply_replay.py'),'--cases',str(path),
        '--out',str(tmp_path/'replayed')],cwd=tmp_path,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['cases_passed']==1


def test_valid_preparation_gain_has_observed_denominator_and_no_authority(home):
    import ready_gate
    now=utc_now();put(home,[lead()]);before=snapshot(home,now=now)
    live={'identity':['greenhouse','private-board','1'],'verdict':'live','observed_at':now.isoformat()}
    observation=attempt();observation.update(signal='LIVE',transport_class='OK',**live)
    item=lead(posting_verification=live,verification_attempt=observation)
    put(home,[item]);(home/'resume.txt').write_text('Synthetic material')
    packet=ready_gate.seal_packet({'role_id':item['role_id'],'company':item['company'],'title':item['title'],
        'ats_url':item['application_url'],'brief':'Synthetic preparation','upload_files':[str(home/'resume.txt')],
        'scope':'preparation_only','execution_authorized':False},item,{},workspace=home)
    atomic_json(home/'data/launch-packets/buffer/PRIVATE-ROLE-1.json',packet)
    after=snapshot(home,now=now+timedelta(seconds=1));window=measured.compare(before,after)
    assert window['observed_prepared_gains']==1 and window['new_retained_dispatches']==1
    assert window['gains_per_new_retained_dispatch']==1
    assert not window['dispatch_coverage_complete'] and after['launchable_ready'] is None


def test_terminal_status_does_not_invent_an_ownership_replay(home):
    put(home,[lead(status='SUBMITTED')]);assert measured.failure_cases(snapshot(home))['cases']==[]
    put(home,[lead(attempt_id='PRIVATE-ATTEMPT')]);cases=measured.failure_cases(snapshot(home))
    assert cases['cases'][0]['scenario']=='ownership_change' and 'PRIVATE-ATTEMPT' not in json.dumps(cases)


def test_tampering_unknown_fields_and_unordered_windows_fail(home):
    put(home,[lead()]);a=snapshot(home);tampered=copy.deepcopy(a);tampered['payload']='private'
    measured.seal(tampered)
    with pytest.raises(ValueError):measured.validate(tampered)
    with pytest.raises(ValueError):measured.compare(a,a)
    tampered=copy.deepcopy(a);tampered['roles'][0]['stage']='ready'
    with pytest.raises(ValueError):measured.validate(tampered)


def test_duplicate_homes_cannot_produce_conserved_window(home):
    put(home,[lead()]);atomic_json(home/'data/queues/strategic-queue.json',[lead()]);now=utc_now()
    a=snapshot(home,now=now);b=snapshot(home,now=now+timedelta(seconds=1))
    assert not a['conservation_pass']
    with pytest.raises(ValueError):measured.compare(a,b)


def test_replay_is_bounded_and_rejects_unknown_scenarios(home):
    put(home,[lead(unresolved=['private'])]);cases=measured.failure_cases(snapshot(home))
    cases['cases'][0]['scenario']='submit'
    with pytest.raises(ValueError):validate_cases(cases)
    cases=measured.failure_cases(snapshot(home));cases['cases']*=26
    with pytest.raises(ValueError):validate_cases(cases)


def test_boolean_counter_cannot_masquerade_as_one_role(home):
    put(home,[lead()]);value=snapshot(home);value['stage_counts']={'verification':True};measured.seal(value)
    with pytest.raises(ValueError):measured.validate(value)
