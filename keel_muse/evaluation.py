"""Pinned repeated end-to-end preparation fixtures with deterministic grading.

FIXTURE is an in-memory interpreter, INJECTED is a trusted supplied callable,
and UNAVAILABLE is retained as an error. None proves rendered Muse execution.
"""
import base64
import hashlib
import time
from keel_loki.common import clone, digest, require_dict, require_id, require_hash, require_int
from keel_loki.forms import validate_contract, build_plan, validate_readback, FormError, demo_fixture
from keel_agent.models import ModelError


def validate_dataset(dataset):
    dataset = clone(dataset)
    require_dict(dataset, {'schema','dataset_id','synthetic','split','cases'})
    if dataset['schema'] != 'keel.muse.e2e-dataset.v1' or type(dataset['synthetic']) is not bool or dataset['split'] not in ('development','held_out'):
        raise ValueError('e2e_dataset_invalid')
    require_id(dataset['dataset_id'])
    if type(dataset['cases']) is not list or not 1 <= len(dataset['cases']) <= 64:
        raise ValueError('e2e_case_limit')
    seen = set()
    for case in dataset['cases']:
        require_dict(case, {'case_id','fixture','expected_outcome'})
        require_id(case['case_id'])
        if case['case_id'] in seen or case['expected_outcome'] not in ('PREPARED','BLOCKED'):
            raise ValueError('e2e_case_invalid')
        seen.add(case['case_id'])
        fixture = case['fixture']
        if type(fixture) is not dict or not {'contract','values','approvals','now','attachments','synthetic'} <= set(fixture):
            raise ValueError('e2e_fixture_invalid')
        validate_contract(fixture['contract'])
        if type(fixture['synthetic']) is not bool or fixture['synthetic'] != dataset['synthetic']:
            raise ValueError('e2e_fixture_provenance_mismatch')
        require_int(fixture['now'])
    return dataset


def freeze_plan(dataset, systems, *, source_sha256, trials=3):
    dataset = validate_dataset(dataset)
    systems = clone(systems)
    require_hash(source_sha256)
    require_int(trials,1,20)
    if type(systems) is not list or not 1 <= len(systems) <= 8:
        raise ValueError('e2e_system_limit')
    seen = set()
    for system in systems:
        require_dict(system, {'system_id','adapter','config_sha256'})
        require_id(system['system_id']); require_hash(system['config_sha256'])
        if system['system_id'] in seen or system['adapter'] not in ('fixture','external'):
            raise ValueError('e2e_system_invalid')
        if system['adapter'] == 'fixture' and not dataset['synthetic']:
            raise ValueError('fixture_adapter_requires_synthetic_data')
        seen.add(system['system_id'])
    if len(systems)*len(dataset['cases'])*trials > 2048:
        raise ValueError('e2e_trial_limit')
    return {'schema':'keel.muse.e2e-plan.v1','dataset_sha256':digest(dataset),'source_sha256':source_sha256,
        'synthetic':dataset['synthetic'],'split':dataset['split'],'systems':systems,'trials':trials,
        'manifest':[{'case_id':c['case_id'],'fixture_sha256':digest(c['fixture']),'expected_outcome':c['expected_outcome']} for c in dataset['cases']]}


def fixture_transport(system, fixture, plan):
    """Execute preparation in local state and read back every field and upload."""
    if fixture['synthetic'] is not True:
        raise ValueError('fixture_only')
    state, trace = {}, []
    for field in fixture['contract']['fields']:
        state[field['id']] = {'field_id':field['id'],'kind':field['kind'],'active':False,'value':None}
    for item in plan['fields']:
        row = state[item['field_id']]
        if item['state'] != 'prepared':
            continue
        value = clone(item['value'])
        if item['kind'] == 'attachment':
            raw = base64.b64decode(fixture['attachments'][item['field_id']], validate=True)
            if hashlib.sha256(raw).hexdigest() != value['sha256'] or len(raw) != value['size']:
                raise ValueError('fixture_attachment_bytes_mismatch')
        row.update(active=True,value=value)
        trace.append({'action':'attach' if item['kind']=='attachment' else 'fill','field_id':item['field_id'],'value_sha256':digest(value)})
    readback = {'schema':'keel.loki.readback.v1','contract_sha256':digest(fixture['contract']),
        'origin':fixture['contract']['origin'],'account_id':fixture['contract']['account_id'],
        'fields':list(state.values()),'unexpected_controls':[],'submitted':False}
    return {'readback':readback,'trace':trace}


def _metrics(records, systems):
    result = {}
    for system in systems:
        rows=[r for r in records if r['system_id']==system['system_id']]
        expected_ready=sum(r['expected_outcome']=='PREPARED' for r in rows)
        matrix={g:{o:sum(r['expected_outcome']==g and r['outcome']==o for r in rows) for o in ('PREPARED','BLOCKED','ERROR')} for g in ('PREPARED','BLOCKED')}
        groups={case:[r['outcome'] for r in rows if r['case_id']==case] for case in {r['case_id'] for r in rows}}
        result[system['system_id']]={'attempts':len(rows),'matrix':matrix,
            'successful_preparation':{'numerator':matrix['PREPARED']['PREPARED'],'denominator':expected_ready},
            'false_ready':matrix['BLOCKED']['PREPARED'],'false_block':matrix['PREPARED']['BLOCKED'],
            'errors':sum(r['outcome']=='ERROR' for r in rows),'inconsistent_cases':sum(len(set(v))>1 for v in groups.values()),
            'transport_calls':sum(r['transport_calls'] for r in rows)}
    return result


def run(plan, dataset, *, expected_plan_sha256, source_sha256, transports=None):
    dataset=validate_dataset(dataset); plan=clone(plan)
    require_hash(expected_plan_sha256)
    rebuilt=freeze_plan(dataset,plan['systems'],source_sha256=source_sha256,trials=plan['trials'])
    if plan!=rebuilt or digest(plan)!=expected_plan_sha256:
        raise ValueError('e2e_plan_pin_mismatch')
    if transports is None: transports={}
    if type(transports) is not dict or any(k not in {s['system_id'] for s in plan['systems']} or not callable(v) for k,v in transports.items()):
        raise ValueError('e2e_transports_invalid')
    records=[]; stopped=False
    for trial in range(plan['trials']):
        for case in dataset['cases']:
            for system in plan['systems']:
                fixture=clone(case['fixture'])
                mode='FIXTURE' if system['adapter']=='fixture' else 'INJECTED' if system['system_id'] in transports else 'UNAVAILABLE'
                row={'system_id':system['system_id'],'trial':trial,'case_id':case['case_id'],'fixture_sha256':digest(fixture),
                    'expected_outcome':case['expected_outcome'],'outcome':'ERROR','mode':mode,'error_code':None,
                    'transport_calls':0,'readback_sha256':None,'trace_sha256':None,'readback':None,'trace':None,'latency_ms':None}
                if stopped:
                    row['error_code']='not_run_after_rate_limit'; records.append(row); continue
                try:
                    prepared=build_plan(fixture['contract'],fixture['values'],fixture['approvals'],now=fixture['now'])
                except (ValueError,TypeError,KeyError):
                    row.update(outcome='BLOCKED',error_code='preparation_gate_blocked'); records.append(row); continue
                if mode=='UNAVAILABLE':
                    row['error_code']='transport_unavailable'; records.append(row); continue
                started=time.monotonic()
                try:
                    row['transport_calls']=1
                    observe=(fixture_transport if mode=='FIXTURE' else transports[system['system_id']])(clone(system),clone(fixture),clone(prepared))
                    require_dict(observe,{'readback','trace'})
                    if type(observe['trace']) is not list or len(observe['trace'])>128:
                        raise ValueError('trace_invalid')
                    row.update(readback_sha256=digest(observe['readback']),trace_sha256=digest(observe['trace']),readback=clone(observe['readback']),trace=clone(observe['trace']))
                    matched=validate_readback(fixture['contract'],prepared,observe['readback'])['status']=='MATCH'
                    row.update(outcome='PREPARED' if matched else 'BLOCKED',error_code=None if matched else 'readback_mismatch')
                except Exception as exc:
                    code='model_http_429' if isinstance(exc,ModelError) and str(exc)=='model_http_429' else 'fixture_or_transport_error'
                    row['error_code']=code
                    if code=='model_http_429': stopped=True
                row['latency_ms']=max(0,round((time.monotonic()-started)*1000,6)); records.append(row)
    return {'schema':'keel.muse.e2e-run.v1','plan':plan,'plan_sha256':digest(plan),'records':records,
        'metrics':_metrics(records,plan['systems']),'rate_limited':stopped,'execution_authorized':False,
        'rendered_browser_verified':False,'reviewer_observations_authenticated':False,'model_calls':0}


def compare(report, *, expected_plan_sha256, dataset, source_sha256):
    report=clone(report); dataset=validate_dataset(dataset)
    require_dict(report,{'schema','plan','plan_sha256','records','metrics','rate_limited','execution_authorized','rendered_browser_verified','reviewer_observations_authenticated','model_calls'})
    plan=report['plan']; rebuilt=freeze_plan(dataset,plan['systems'],source_sha256=source_sha256,trials=plan['trials'])
    if report['schema']!='keel.muse.e2e-run.v1' or plan!=rebuilt or digest(plan)!=expected_plan_sha256 or report['plan_sha256']!=expected_plan_sha256:
        raise ValueError('e2e_report_pin_mismatch')
    expected=[(s['system_id'],trial,c['case_id'],digest(c['fixture']),c['expected_outcome']) for trial in range(plan['trials']) for c in dataset['cases'] for s in plan['systems']]
    if type(report['records']) is not list or len(report['records'])!=len(expected): raise ValueError('e2e_report_coverage_invalid')
    stopped=False
    import math
    for row,key in zip(report['records'],expected):
        require_dict(row,{'system_id','trial','case_id','fixture_sha256','expected_outcome','outcome','mode','error_code','transport_calls','readback_sha256','trace_sha256','readback','trace','latency_ms'})
        if tuple(row[k] for k in ('system_id','trial','case_id','fixture_sha256','expected_outcome'))!=key: raise ValueError('e2e_report_case_binding_invalid')
        require_int(row['trial'],0,plan['trials']-1)
        if row['outcome'] not in ('PREPARED','BLOCKED','ERROR') or row['mode'] not in ('FIXTURE','INJECTED','UNAVAILABLE'): raise ValueError('e2e_report_outcome_invalid')
        require_int(row['transport_calls'],0,1)
        if row['transport_calls']:
            if type(row['latency_ms']) not in (int,float) or not math.isfinite(row['latency_ms']) or row['latency_ms']<0: raise ValueError('e2e_timing_invalid')
        elif any(row[k] is not None for k in ('latency_ms','readback_sha256','trace_sha256','readback','trace')): raise ValueError('e2e_uncalled_observation_invalid')
        if row['outcome']=='PREPARED' and (row['transport_calls']!=1 or row['error_code'] is not None or any(row[k] is None for k in ('readback_sha256','trace_sha256'))): raise ValueError('e2e_prepared_observation_missing')
        system=next(s for s in plan['systems'] if s['system_id']==row['system_id'])
        if (system['adapter']=='fixture')!=(row['mode']=='FIXTURE') or (row['mode']=='UNAVAILABLE' and row['transport_calls']): raise ValueError('e2e_mode_binding_invalid')
        allowed={'preparation_gate_blocked','not_run_after_rate_limit','transport_unavailable','readback_mismatch','model_http_429','fixture_or_transport_error',None}
        if row['error_code'] not in allowed: raise ValueError('e2e_error_code_invalid')
        fixture=next(c['fixture'] for c in dataset['cases'] if c['case_id']==row['case_id'])
        try:
            gated_plan=build_plan(fixture['contract'],fixture['values'],fixture['approvals'],now=fixture['now'])
            gate_blocked=False
        except (ValueError,TypeError,KeyError):
            gate_blocked=True
        if not stopped and gate_blocked:
            if row['outcome']!='BLOCKED' or row['error_code']!='preparation_gate_blocked' or row['transport_calls']!=0: raise ValueError('e2e_gate_result_mismatch')
        elif row['error_code']=='preparation_gate_blocked': raise ValueError('e2e_fabricated_gate_block')
        if row['outcome']=='BLOCKED' and row['error_code'] not in ('preparation_gate_blocked','readback_mismatch'): raise ValueError('e2e_block_reason_invalid')
        if row['error_code']=='readback_mismatch' and (row['outcome']!='BLOCKED' or row['transport_calls']!=1 or row['readback'] is None): raise ValueError('e2e_block_observation_missing')
        if row['outcome']=='ERROR':
            if row['error_code'] in ('fixture_or_transport_error','model_http_429'):
                if row['transport_calls']!=1: raise ValueError('e2e_error_attempt_missing')
            elif row['error_code']=='transport_unavailable':
                if row['mode']!='UNAVAILABLE' or row['transport_calls']!=0: raise ValueError('e2e_unavailable_mode_mismatch')
            elif row['error_code']=='not_run_after_rate_limit':
                if not stopped or row['transport_calls']!=0: raise ValueError('e2e_premature_rate_limit_claim')
            else: raise ValueError('e2e_error_reason_invalid')
        if row['readback'] is not None:
            if digest(row['readback'])!=row['readback_sha256'] or digest(row['trace'])!=row['trace_sha256']: raise ValueError('e2e_observation_digest_mismatch')
            fixture=next(c['fixture'] for c in dataset['cases'] if c['case_id']==row['case_id'])
            prepared=build_plan(fixture['contract'],fixture['values'],fixture['approvals'],now=fixture['now'])
            grade='PREPARED' if validate_readback(fixture['contract'],prepared,row['readback'])['status']=='MATCH' else 'BLOCKED'
            if row['outcome']!=grade: raise ValueError('e2e_deterministic_grade_mismatch')
        elif row['outcome']=='PREPARED': raise ValueError('e2e_prepared_readback_missing')
        for k in ('readback_sha256','trace_sha256'):
            if row[k] is not None: require_hash(row[k])
        if stopped and (row['transport_calls'] or row['error_code']!='not_run_after_rate_limit'): raise ValueError('e2e_rate_limit_bypass')
        if row['error_code']=='model_http_429': stopped=True
    if report['rate_limited'] is not stopped or digest(_metrics(report['records'],plan['systems']))!=digest(report['metrics']): raise ValueError('e2e_metrics_invalid')
    if any(report[k] is not False for k in ('execution_authorized','rendered_browser_verified','reviewer_observations_authenticated')) or type(report['model_calls']) is not int or report['model_calls']!=0: raise ValueError('e2e_authority_invalid')
    return {'schema':'keel.muse.e2e-comparison.v1','run_sha256':digest(report),'plan_sha256':expected_plan_sha256,
        'metrics':report['metrics'],'synthetic':plan['synthetic'],'split':plan['split'],'competitive_superiority_established':False,'execution_authorized':False}


def demo(home=None):
    data={'schema':'keel.muse.e2e-dataset.v1','dataset_id':'fixture','synthetic':True,'split':'held_out',
          'cases':[{'case_id':'complete','fixture':demo_fixture(),'expected_outcome':'PREPARED'}]}
    systems=[{'system_id':'fixture','adapter':'fixture','config_sha256':digest({'driver':'fixture-v1'})},
             {'system_id':'host','adapter':'external','config_sha256':digest({'driver':'unavailable'})}]
    plan=freeze_plan(data,systems,source_sha256='a'*64,trials=2)
    report=run(plan,data,expected_plan_sha256=digest(plan),source_sha256='a'*64)
    return {'run':report,'comparison':compare(report,expected_plan_sha256=digest(plan),dataset=data,source_sha256='a'*64)}
