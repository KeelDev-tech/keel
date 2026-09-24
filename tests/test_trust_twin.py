"""Replay, decisions and twin behavior across concrete failure scenarios."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest
from tools.make_trust_demo import NOW,make_document,research
from tools.make_flow_demo import make_snapshot
from keel_flow.board import build
from keel_trust.common import ContractError,digest
from keel_trust.twin import capture,simulate,drift,metrics
from keel_trust.replay import run,compare
from keel_trust.decisions import operator_brief,research_plan
from keel_trust.__main__ import main


def scenario(model,kind=None,target=None,value=None):
    return {'schema_version':1,'scenario_id':'synthetic-scenario','base_sha256':model['source_sha256'],
            'assumption_ref':'fixture://hypothesis-not-a-measurement',
            'changes':[] if kind is None else [{'kind':kind,'target':target,'value':value}]}


def test_capture_and_noop_simulation_are_reproducible_and_immutable():
    source=make_snapshot();old=deepcopy(source);model=capture(source,now=NOW);saved=deepcopy(model)
    result=simulate(model,scenario(model));assert source==old and model==saved
    assert result['changed_metrics']=={} and result['mode']=='SIMULATION_ONLY'
    assert not result['execution_authorized'] and result['live_writes']==0
    assert result['forecast_calibration']=='NOT_ESTABLISHED' and not model['live_sync_connected']
    assert result==simulate(model,scenario(model))


@pytest.mark.parametrize('kind,target,value,key,expected',[
    ('CAPACITY_MULTIPLIER','capacity',2,'runway_seconds',150.0),
    ('VERIFY_MULTIPLIER','synthetic-backlog',2,'first_refill_seconds',1500.0),
    ('PREPARE_MULTIPLIER','synthetic-backlog',2,'first_refill_seconds',1500.0),
    ('RELEASE_DELAY_SECONDS','synthetic-backlog',600,'first_refill_seconds',1800.0),
    ('RATE_HOLD','source-a',True,'discovery_minutes',0),
    ('INVALIDATE_PACKET','role-1','answers','executable_ready',0),
    ('UNKNOWN_ATTEMPT','role-1',True,'held_applications',2),
    ('OBSERVATION_AGE_SECONDS','snapshot',91,'estimate_current',False)])
def test_scenario_runs_actual_pipeline_rules(kind,target,value,key,expected):
    model=capture(make_snapshot(),now=NOW);result=simulate(model,scenario(model,kind,target,value))
    assert result['scenario'][key]==expected
    assert result['baseline'][key]!=expected
    assert result['constraints_preserved']=={'ready_floor':5,'check_seconds':30,'spawn_throttle_seconds':600,'consent_and_fit_gates_changed':False}
    assert not result['provider_acceptance_verified']


@pytest.mark.parametrize('kind,target,value',[
    ('FIT_OVERRIDE','role-1',99),('CLEAR_CONSENT','role-5',True),('RATE_HOLD','source-a',False),
    ('CAPACITY_MULTIPLIER','capacity',0),('CAPACITY_MULTIPLIER','capacity',5),
    ('RELEASE_DELAY_SECONDS','synthetic-backlog',-1),('UNKNOWN_ATTEMPT','synthetic-backlog:0',True),
    ('INVALIDATE_PACKET','missing','answers'),('INVALIDATE_PACKET','role-1','consent'),
    ('OBSERVATION_AGE_SECONDS','snapshot',-1),('PREPARE_MULTIPLIER','missing',2)])
def test_scenario_cannot_clear_gates_or_invent_records(kind,target,value):
    model=capture(make_snapshot(),now=NOW)
    with pytest.raises(ContractError):simulate(model,scenario(model,kind,target,value))


@pytest.mark.parametrize('change',['source','baseline','source_revision','version','authority','sync','calibration','status','scenario_binding','duplicate_change'])
def test_tampered_or_ambiguous_model_refused(change):
    model=capture(make_snapshot(),now=NOW);sc=scenario(model)
    if change=='source':model['export']['leads'][0]['fit_score']=99
    if change=='baseline':model['baseline']['readiness']['executable_ready']=99
    if change=='source_revision':model['source_revision']='modified'
    if change=='version':model['model_version']='unknown'
    if change=='authority':model['execution_authorized']=True
    if change=='sync':model['live_sync_connected']=True
    if change=='calibration':model['calibration']='PROVEN'
    if change=='status':model['status']='UNVERIFIED_EXPORT_MODEL'
    if change=='scenario_binding':sc['base_sha256']='c'*64
    if change=='duplicate_change':sc['changes']=[{'kind':'CAPACITY_MULTIPLIER','target':'capacity','value':2}]*2
    with pytest.raises(ContractError):simulate(model,sc)


def test_drift_distinguishes_changed_stale_and_unchanged_observations():
    source=make_snapshot();model=capture(source,now=NOW)
    assert drift(model,source,now=NOW)['status']=='NO_CHANGE_IN_EXPORT'
    source['leads'][0]['packet_present']=False
    result=drift(model,source,now=NOW)
    assert result['status']=='OBSERVED_CHANGE' and result['changed_roles']==[{'role_id':'role-1','fields':['packet_present']}]
    assert result['before']['executable_ready']==1 and result['after']['executable_ready']==0
    assert not result['forecast_error_estimated']
    assert drift(model,source,now=NOW+timedelta(seconds=91))['status']=='CURRENT_EXPORT_UNVERIFIED'
    with pytest.raises(ContractError):drift(model,source,now=NOW-timedelta(seconds=1))


def test_unverified_model_cannot_be_freshened_by_age_scenario():
    source=make_snapshot();source['observed_at']=(NOW-timedelta(seconds=91)).isoformat()
    model=capture(source,now=NOW);assert model['status']=='UNVERIFIED_EXPORT_MODEL'
    result=simulate(model,scenario(model,'OBSERVATION_AGE_SECONDS','snapshot',0))
    assert not result['scenario']['estimate_current'] and result['scenario']['forecast_state']=='UNKNOWN'


def test_scenario_cli_roundtrip(tmp_path,capsys):
    source=tmp_path/'flow.json';source.write_text(json.dumps(make_snapshot()));model=tmp_path/'model.json'
    assert main(['capture',str(source),'--now',NOW.isoformat(),'--out',str(model)])==0
    sc=tmp_path/'scenario.json';sc.write_text(json.dumps(scenario(json.loads(model.read_text()),'RATE_HOLD','source-a',True)))
    assert main(['simulate',str(model),'--scenario',str(sc)])==0
    result=json.loads(capsys.readouterr().out);assert result['scenario']['discovery_minutes']==0
    assert main(['drift',str(model),'--current',str(source),'--now',NOW.isoformat()])==0
    assert json.loads(capsys.readouterr().out)['status']=='NO_CHANGE_IN_EXPORT'


def test_operator_budget_respects_dependencies_and_leaves_consent_unanswered():
    doc=make_document();flow=build(doc['flow_export'],now=NOW)
    small=operator_brief(flow,doc['question_costs'],budget_minutes=2,now=NOW)
    assert small['minutes']==2 and small['remaining_minutes']==0
    assert small['roles_unlocked_if_all_selected_answered']==['role-4']
    large=operator_brief(flow,doc['question_costs'],budget_minutes=4,now=NOW)
    assert large['roles_unlocked_if_all_selected_answered']==['role-4','role-5']
    assert all(not r['resolved'] and r['operator_reply_required'] for r in large['selected'])
    assert large['answers_generated']==0


def test_full_question_bundle_can_win_over_partial_progress():
    doc=make_document();flow=build(doc['flow_export'],now=NOW)
    for row in flow['questions']['groups']:row['urgent']=False
    flow['questions']['dependency_bundles']=[{'role_id':'bundle-role','required_group_ids':[r['group_id'] for r in flow['questions']['groups']]}]
    result=operator_brief(flow,doc['question_costs'],budget_minutes=4,now=NOW)
    assert result['roles_unlocked_if_all_selected_answered']==['bundle-role'] and result['minutes']==4


@pytest.mark.parametrize('budget',[0,1,2,3,4,5,240])
def test_operator_budget_conserves_minutes_and_deduplicates_roles(budget):
    doc=make_document();result=operator_brief(build(doc['flow_export'],now=NOW),doc['question_costs'],budget_minutes=budget,now=NOW)
    assert result['minutes']==sum(r['estimated_minutes'] for r in result['selected'])
    assert result['minutes']+result['remaining_minutes']==budget
    assert len(result['roles_unlocked_if_all_selected_answered'])==len(set(result['roles_unlocked_if_all_selected_answered']))


def test_missing_estimates_urgent_overflow_and_stale_data_stay_visible():
    doc=make_document();flow=build(doc['flow_export'],now=NOW)
    missing=operator_brief(flow,[],budget_minutes=20,now=NOW)
    assert not missing['selected'] and all(r['reason']=='TIME_ESTIMATE_MISSING' for r in missing['deferred'])
    overflow=operator_brief(flow,doc['question_costs'],budget_minutes=0,now=NOW)
    assert overflow['urgent_deferred_count']==1
    stale=operator_brief(flow,doc['question_costs'],budget_minutes=20,now=NOW+timedelta(seconds=91))
    assert stale['status']=='REFRESH_EXPORT' and not stale['selected']


@pytest.mark.parametrize('field,value,reason',[('permitted',False,'NOT_PERMITTED'),('fit_score',74,'FIT_BELOW_75'),
    ('affects_decision',False,'NO_DECISION_IMPACT'),('probability_of_change',None,'ESTIMATE_MISSING'),
    ('estimate_ref',None,'ESTIMATE_MISSING'),('observed_at',(NOW-timedelta(seconds=91)).isoformat(),'STALE_OBSERVATION'),
    ('decision_value',0,'ZERO_ESTIMATED_VALUE')])
def test_research_only_budgets_permitted_current_decision_checks(field,value,reason):
    row=research();row[field]=value;result=research_plan([row],budget_minutes=5,now=NOW)
    assert not result['selected'] and result['excluded'][0]['reason']==reason
    assert result['http_requests']==0


def test_research_deduplicates_uncertainty_and_429_is_global_stop():
    checks=[research(),research('same-question'),research('second-topic',uncertainty_key='different',estimated_minutes=4)]
    result=research_plan(checks,budget_minutes=5,now=NOW)
    assert len(result['selected'])==1 and result['minutes']==2 and result['remaining_minutes']==3
    assert {r['reason'] for r in result['excluded']}=={'DUPLICATE_UNCERTAINTY','BUDGET_EXHAUSTED'}
    checks[-1]['rate_limited']=True;stopped=research_plan(checks,budget_minutes=5,now=NOW)
    assert stopped['status']=='RATE_LIMIT_RECONCILIATION_REQUIRED' and stopped['minutes']==0 and not stopped['selected']


def incident():
    return {'schema_version':1,'case_id':'incident-1','incident_ref':'fixture://ready-does-not-mean-executable',
            'as_of':NOW.isoformat(),'flow_export':make_snapshot(),
            'expected':{'executable_ready':1,'estimate_current':True,'forecast_state':'STARVATION_RISK','held_applications':1}}


def test_replay_requires_real_cases_and_expected_observations():
    assert run([])['status']=='NOT_RUN'
    case=incident();assert run([case])['status']=='PASS'
    case['expected']={}
    with pytest.raises(ContractError):run([case])


@pytest.mark.parametrize('change,status',[('wrong_value','FAIL'),('unknown_metric','ERROR'),('invalid_export','ERROR'),('bool_as_count','FAIL')])
def test_replay_detects_regression_and_bad_fixtures(change,status):
    case=incident()
    if change=='wrong_value':case['expected']['executable_ready']=5
    if change=='unknown_metric':case['expected']['fantasy']=1
    if change=='invalid_export':case['flow_export']['complete']='yes'
    if change=='bool_as_count':case['expected']['executable_ready']=True
    report=run([case]);assert report['status']=='FAIL' and report['results'][0]['status']==status


def test_candidate_comparison_detects_regression_and_never_authorizes_deployment():
    def broken(source,*,now):
        report=build(source,now=now);report['readiness']['executable_ready']=5;return report
    result=compare([incident()],baseline=build,candidate=broken)
    assert result['status']=='REVIEW_REQUIRED' and result['regressions']==['incident-1']
    assert not result['deployment_authorized'] and not result['performance_improvement_proven']
    assert compare([],baseline=build,candidate=build)['status']=='NOT_RUN'
    assert compare([incident()],baseline=build,candidate=build)['status']=='NO_REGRESSION_IN_FIXTURES'


def test_replay_fails_on_reported_side_effects():
    def unsafe(source,*,now):
        report=build(source,now=now);report['effects']['queue_writes']=1;return report
    result=run([incident()],evaluator=unsafe)
    assert result['status']=='FAIL' and result['results'][0]['status']=='ERROR'


def test_replay_cli_has_nonzero_status_for_fail_and_empty(tmp_path,capsys):
    path=tmp_path/'incidents.json';case=incident();path.write_text(json.dumps([case]))
    assert main(['replay',str(path)])==0
    case['expected']['executable_ready']=99;path.write_text(json.dumps([case]));assert main(['replay',str(path)])==1
    path.write_text('[]');assert main(['replay',str(path)])==1
