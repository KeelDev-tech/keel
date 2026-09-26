from copy import deepcopy
import json
from pathlib import Path

import pytest

from keel_machine import ComputationCache, GraphRunner, GuardDecision, MachineError, Operation, validate_plan
from keel_machine.builtins import demo, demo_plan, object_contract, registered_operations
from keel_machine.common import digest,canonical


@pytest.fixture
def runtime(tmp_path):
    now=[1000];allowed=[True];calls=[]
    cache=ComputationCache(tmp_path/'cache.sqlite3',clock=lambda:now[0])
    def guard(request,stamp):
        calls.append(deepcopy(request))
        return GuardDecision(allowed[0],digest(request),stamp+30)
    return GraphRunner(cache,registered_operations(),guard,clock=lambda:now[0]),now,allowed,calls


def test_incremental_graph_reuses_only_unchanged_branches(runtime):
    runner,_,_,_=runtime
    first=runner.run(demo_plan())
    repeated=runner.run(demo_plan('again'))
    changed=runner.run(demo_plan('changed','Different synthetic lexical input.'))
    assert first['status']==repeated['status']==changed['status']=='COMPLETED'
    assert (first['computed_nodes'],repeated['computed_nodes'],changed['computed_nodes'])==(3,0,2)
    assert repeated['cache_hits']==3 and changed['cache_hits']==1
    assert changed['nodes']['right']['status']=='CACHED'
    assert not changed['execution_authorized']


def test_source_revision_invalidates_descendants_even_if_text_unchanged(runtime):
    runner,_,_,_=runtime;runner.run(demo_plan())
    altered=demo_plan();altered['nodes'][0]['revisions']['document']='f'*64
    report=runner.run(altered)
    assert report['computed_nodes']==2 and report['cache_hits']==1


def test_cache_hit_rechecks_current_host_gate(runtime):
    runner,_,allowed,calls=runtime;runner.run(demo_plan());calls.clear();allowed[0]=False
    report=runner.run(demo_plan())
    assert report['status']=='HELD' and not report['outputs'] and report['computed_nodes']==0
    assert len(calls)==2


def test_rechecks_all_results_after_later_node_revokes_evidence(runtime):
    runner,_,_,_=runtime
    def guard(req,now):
        return GuardDecision(req['phase']!='return' or req['node_id']!='left',digest(req),now+10)
    runner.guard=guard
    report=runner.run(demo_plan())
    assert report['status']=='HELD' and not report['outputs']
    assert report['nodes']['left']['reason']=='final_admission_failed'


def test_final_graph_admission_catches_later_callback_revocation(runtime):
    runner,_,_,_=runtime;revoked=[False];whole=[]
    def guard(request,now):
        if request['phase']=='return' and request['node_id']=='summary':
            revoked[0]=True
        if request['phase']=='graph-return':
            whole.append(deepcopy(request))
            return GuardDecision(not revoked[0],digest(request),now+10)
        return GuardDecision(True,digest(request),now+10)
    runner.guard=guard
    report=runner.run(demo_plan())
    assert report['status']=='HELD' and not report['outputs']
    assert report['graph_hold']=='final_graph_admission_failed'
    assert len(whole)==1 and set(whole[0]['nodes'])=={'left','right','summary'}
    assert all(set(row)=={'operation','revisions','cache_key_sha256','output_sha256'}
               for row in whole[0]['nodes'].values())


def test_json_rejects_large_keys_and_aggregate_text_before_serialization(monkeypatch):
    from keel_machine import common
    def unexpected(*args,**kwargs):pytest.fail('must reject before JSON serialization')
    monkeypatch.setattr(common.json,'dumps',unexpected)
    for value in ({'k'*5000:None},['x'*200000,'x'*200000]):
        with pytest.raises(MachineError):canonical(value)


@pytest.mark.parametrize('change',[{'account_id':'different'},{'scope':'different'},{'purpose':'analysis'}])
def test_cache_namespace_separation(runtime,change):
    runner,_,_,_=runtime;runner.run(demo_plan())
    plan=demo_plan();plan.update(change)
    assert runner.run(plan)['computed_nodes']==3


def test_cache_hit_does_not_spend_compute_budget(runtime):
    runner,_,_,_=runtime;runner.run(demo_plan())
    plan=demo_plan();plan['max_compute_nodes']=1
    report=runner.run(plan)
    assert report['status']=='COMPLETED' and report['computed_nodes']==0


def test_budget_exhaustion_never_returns_partial_outputs(runtime):
    runner,_,_,_=runtime
    plan=demo_plan();plan['max_compute_nodes']=1
    report=runner.run(plan)
    assert report['status']=='HELD' and report['computed_nodes']==1 and not report['outputs']


@pytest.mark.parametrize('mutation',[
    lambda p:p['nodes'][0].update(needs=['summary']),
    lambda p:p['nodes'][0].update(needs=['missing']),
    lambda p:p['nodes'][0].update(needs=[{}]),
    lambda p:p['nodes'].append(deepcopy(p['nodes'][0])),
    lambda p:p.update(max_compute_nodes=True),
    lambda p:p.update(max_seconds=float('inf')),
    lambda p:p.update(purpose='submission'),
    lambda p:p['nodes'][0].update(shell='echo forbidden')])
def test_invalid_graph_never_runs(runtime,mutation):
    runner,_,_,calls=runtime;plan=demo_plan();mutation(plan)
    with pytest.raises(MachineError):runner.run(plan)
    assert not calls


def test_missing_or_changed_contract_stops_node(runtime):
    runner,_,_,_=runtime;plan=demo_plan();plan['nodes'][0]['arguments']['text']=123
    report=runner.run(plan)
    assert report['status']=='HELD' and report['nodes']['left']['reason']=='graph_input_contract_failed'
    assert report['nodes']['summary']['status']=='BLOCKED'


def test_mutated_operation_contract_invalidates_pin(runtime):
    runner,_,_,_=runtime;runner.run(demo_plan())
    runner.operations['tokenize_text'].input_contract['required']=[]
    report=runner.run(demo_plan())
    assert report['status']=='HELD' and not report['outputs'] and report['computed_nodes']==0


def test_invalid_cached_output_contract_is_rejected(runtime,monkeypatch):
    runner,_,_,_=runtime
    monkeypatch.setattr(runner.cache,'get',lambda key:{'status':'HIT','value':123})
    report=runner.run(demo_plan())
    assert report['status']=='HELD' and not report['outputs']


def test_forged_guard_binding_rejected(runtime):
    runner,_,_,_=runtime
    runner.guard=lambda req,now:GuardDecision(True,'a'*64,now+10)
    report=runner.run(demo_plan())
    assert report['computed_nodes']==0 and report['status']=='HELD'


def test_expiring_guard_is_rechecked_after_callback(runtime):
    runner,now,_,_=runtime
    def guard(req,stamp):
        now[0]+=2
        return GuardDecision(True,digest(req),stamp+1)
    runner.guard=guard
    assert runner.run(demo_plan())['computed_nodes']==0


def test_deadline_rejects_late_results(runtime):
    runner,_,_,_=runtime
    ticks=iter([0,0,100,100,100,100])
    runner.monotonic=lambda:next(ticks)
    result=runner.run(demo_plan())
    assert result['status']=='HELD' and not result['outputs']


def test_callback_error_prose_not_retained(runtime):
    runner,_,_,_=runtime
    def guard(req,now):raise MachineError('synthetic_private_value')
    runner.guard=guard
    result=runner.run(demo_plan())
    assert 'synthetic_private_value' not in json.dumps(result)


def test_unregistered_operation_rejected(runtime):
    runner,_,_,_=runtime;plan=demo_plan();plan['nodes'][0]['operation']='arbitrary.import'
    with pytest.raises(MachineError,match='unregistered'):runner.run(plan)


def test_integrated_machine_demo(tmp_path):
    result=demo(tmp_path/'fresh')
    assert result['status']=='DEMO_PASSED' and all(result['checks'].values())
    assert result['model_calls']==result['network_calls']==result['external_actions']==0
    with pytest.raises(FileExistsError):demo(tmp_path/'fresh')


def _large_but_bounded_output(payload):
    return ['x'*65000 for _ in range(3)]


def test_aggregate_result_limit_holds_without_partial_output(runtime):
    runner,_,_,_=runtime
    empty=object_contract({})
    op=Operation('bounded_output',_large_but_bounded_output,
        object_contract({'arguments':empty,'config':empty,'dependencies':empty}),
        {'type':'array','items':{'type':'string','maxLength':65000},'maxItems':3})
    runner.operations={'bounded_output':op}
    plan=demo_plan()
    plan['nodes']=[{'id':name,'operation':'bounded_output','arguments':{},'config':{},
                    'needs':[],'revisions':{}} for name in ('first','second')]
    plan['outputs']=['first','second']
    report=runner.run(plan)
    assert all(row['status'] in ('COMPUTED','CACHED') for row in report['nodes'].values())
    assert report['status']=='HELD' and report['outputs']=={}
    assert report['graph_hold']=='graph_result_too_large'
    assert len(canonical(report))<262144
