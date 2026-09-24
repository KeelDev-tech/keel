from copy import deepcopy
import pytest
from keel_agent.models import ModelError
from keel_loki.common import digest
from keel_loki.forms import demo_fixture
from keel_muse import evaluation as e


def inputs():
    dataset={'schema':'keel.muse.e2e-dataset.v1','dataset_id':'test','synthetic':True,'split':'held_out',
        'cases':[{'case_id':'one','fixture':demo_fixture(),'expected_outcome':'PREPARED'}]}
    systems=[{'system_id':'fixture','adapter':'fixture','config_sha256':'b'*64},{'system_id':'external','adapter':'external','config_sha256':'c'*64}]
    plan=e.freeze_plan(dataset,systems,source_sha256='a'*64,trials=2)
    return dataset,plan


def test_fixture_executes_and_unavailable_stays_error():
    result=e.demo()
    metrics=result['comparison']['metrics']
    assert metrics['fixture']['successful_preparation']['numerator']==2
    assert metrics['host']['errors']==2
    assert result['run']['rendered_browser_verified'] is False


def test_changed_attachment_bytes_fail_despite_matching_metadata():
    data,plan=inputs();data['cases'][0]['fixture']['attachments']['resume']='YmFk'
    plan=e.freeze_plan(data,plan['systems'],source_sha256='a'*64)
    result=e.run(plan,data,expected_plan_sha256=digest(plan),source_sha256='a'*64)
    assert result['records'][0]['outcome']=='ERROR'


def test_callback_pass_claim_does_not_grade_success():
    data,plan=inputs()
    result=e.run(plan,data,expected_plan_sha256=digest(plan),source_sha256='a'*64,transports={'external':lambda *args:{'PASS':True}})
    assert result['metrics']['external']['errors']==2


def test_repeated_outcomes_and_readback_are_measured():
    data,plan=inputs();counter=[0]
    def transport(system,fixture,prepared):
        response=e.fixture_transport(system,fixture,prepared)
        counter[0]+=1
        if counter[0]==2:response['readback']['fields'][0]['value']='Wrong'
        return response
    report=e.run(plan,data,expected_plan_sha256=digest(plan),source_sha256='a'*64,transports={'external':transport})
    comparison=e.compare(report,expected_plan_sha256=digest(plan),dataset=data,source_sha256='a'*64)
    assert comparison['metrics']['external']['inconsistent_cases']==1


@pytest.mark.parametrize('mutation',['delete','label','grade','timing','source','mode'])
def test_report_tampering_rejected(mutation):
    data,plan=inputs();report=e.run(plan,data,expected_plan_sha256=digest(plan),source_sha256='a'*64)
    if mutation=='delete':report['records'].pop()
    elif mutation=='label':report['records'][0]['expected_outcome']='BLOCKED'
    elif mutation=='grade':report['records'][0]['outcome']='BLOCKED';report['metrics']=e._metrics(report['records'],plan['systems'])
    elif mutation=='timing':report['records'][0]['latency_ms']=True
    elif mutation=='source':report['plan']['source_sha256']='d'*64
    else:report['records'][0]['mode']='INJECTED'
    with pytest.raises(ValueError):e.compare(report,expected_plan_sha256=digest(plan),dataset=data,source_sha256='a'*64)


def test_global_429_preserves_remaining_attempt_denominators():
    data,plan=inputs()
    def limited(*args):raise ModelError('model_http_429')
    report=e.run(plan,data,expected_plan_sha256=digest(plan),source_sha256='a'*64,transports={'external':limited})
    assert len(report['records'])==4 and report['rate_limited'] is True
    assert all(r['error_code']=='not_run_after_rate_limit' for r in report['records'][2:])
    e.compare(report,expected_plan_sha256=digest(plan),dataset=data,source_sha256='a'*64)


def test_blocked_approval_never_calls_transport():
    data,plan=inputs();data['cases'][0]['fixture']['approvals'][0]['revoked']=True
    plan=e.freeze_plan(data,plan['systems'],source_sha256='a'*64)
    report=e.run(plan,data,expected_plan_sha256=digest(plan),source_sha256='a'*64,transports={'external':lambda *args:pytest.fail('blocked')})
    assert all(r['outcome']=='BLOCKED' and r['transport_calls']==0 for r in report['records'])


def test_fabricated_preparation_block_is_rejected():
    data,plan=inputs();report=e.run(plan,data,expected_plan_sha256=digest(plan),source_sha256='a'*64)
    row=report['records'][0]
    row.update(outcome='BLOCKED',error_code='preparation_gate_blocked',transport_calls=0,
               readback=None,trace=None,readback_sha256=None,trace_sha256=None,latency_ms=None)
    report['metrics']=e._metrics(report['records'],plan['systems'])
    with pytest.raises(ValueError):e.compare(report,expected_plan_sha256=digest(plan),dataset=data,source_sha256='a'*64)
