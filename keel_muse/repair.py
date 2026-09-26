"""Counterexample-bound procedural repair candidates, never self-modification.

Only a fixture driver's locator/upload observation path can be retried. Facts,
values, policy, approvals, revisions and benchmark labels are immutable inputs.
A successful replay yields a recommendation for human integration, not promotion.
"""
from keel_loki.common import clone,digest,require_dict,require_id,require_hash
from keel_loki.forms import build_plan,validate_readback,demo_fixture

FIXES={'missing_locator':'rebind_known_locator','bad_upload':'recheck_attachment_bytes','stale_form':'refresh_bound_snapshot'}


def freeze_fixtures(cases):
    cases=clone(cases)
    if type(cases) is not list or not 2<=len(cases)<=32: raise ValueError('repair_fixture_count_invalid')
    seen=set()
    for case in cases:
        require_dict(case,{'case_id','fixture','fault','field_id','expected_outcome'})
        require_id(case['case_id']);require_id(case['field_id'])
        if case['case_id'] in seen or case['fault'] not in (*FIXES,'none','unknown_required') or case['expected_outcome'] not in ('PREPARED','BLOCKED'): raise ValueError('repair_fixture_invalid')
        if case['fixture'].get('synthetic') is not True: raise ValueError('repair_fixtures_must_be_synthetic')
        seen.add(case['case_id'])
    if len({digest({k:v for k,v in c.items() if k!='case_id'}) for c in cases})!=len(cases): raise ValueError('repair_fixture_duplicates')
    return {'schema':'keel.muse.repair-fixtures.v1','split':'held_out','cases':cases,'fixtures_sha256':digest(cases)}


def propose(failure_trace,*,frozen_fixtures_sha256,source_sha256):
    trace=clone(failure_trace)
    require_dict(trace,{'trace_id','failure_code','field_id','form_sha256','counterexample'})
    require_id(trace['trace_id']);require_id(trace['field_id']);require_hash(trace['form_sha256'])
    require_hash(frozen_fixtures_sha256);require_hash(source_sha256)
    if trace['failure_code'] not in FIXES or type(trace['counterexample']) is not dict or not trace['counterexample']:
        raise ValueError('repair_unrecognized_or_missing_counterexample')
    # The counterexample is supplied observation data, not authenticated truth.
    return {'schema':'keel.muse.repair-candidate.v1','candidate_id':'repair-'+digest(trace)[:20],
        'version':1,'status':'QUARANTINED','action':FIXES[trace['failure_code']],
        'trigger':trace['failure_code'],'field_id':trace['field_id'],'form_sha256':trace['form_sha256'],
        'failure_trace_sha256':digest(trace),'counterexample_sha256':digest(trace['counterexample']),
        'fixtures_sha256':frozen_fixtures_sha256,'source_sha256':source_sha256,
        'execution_authorized':False}


def _observe(case,candidate=None):
    import base64,hashlib
    fixture=clone(case['fixture'])
    try:
        plan=build_plan(fixture['contract'],fixture['values'],fixture['approvals'],now=fixture['now'])
    except (ValueError,TypeError,KeyError):
        return {'outcome':'BLOCKED','reason':'preparation_gate_blocked','readback_sha256':None,'trace_sha256':None,
                'repair_actions':[],'before_driver_sha256':None,'after_driver_sha256':None}
    inventory={field['id']:field for field in fixture['contract']['fields']}
    driver={'locators':{field_id:field_id for field_id in inventory},
            'upload_buffers':clone(fixture['attachments']),
            'snapshot_sha256':digest(fixture['contract']),'unexpected_controls':[]}
    fault,target=case['fault'],case['field_id']
    # Corrupt the driver's actual operational state before both runs.
    if fault=='missing_locator':driver['locators'][target]=None
    elif fault=='bad_upload':driver['upload_buffers'][target]=base64.b64encode(b'wrong upload bytes').decode()
    elif fault=='stale_form':driver['snapshot_sha256']='0'*64
    elif fault=='unknown_required':driver['unexpected_controls']=['unapproved_required_field']
    before=digest(driver);actions=[]
    applicable=(candidate is not None and candidate['trigger']==fault and candidate['field_id']==target
                and candidate['form_sha256']==digest(fixture['contract']) and target in inventory)
    if applicable:
        action=candidate['action']
        if action=='rebind_known_locator':
            driver['locators'][target]=inventory[target]['id']
        elif action=='recheck_attachment_bytes' and target in fixture['attachments']:
            driver['upload_buffers'][target]=fixture['attachments'][target]
        elif action=='refresh_bound_snapshot':
            driver['snapshot_sha256']=digest(fixture['contract'])
        actions.append({'action':action,'field_id':target,'before_sha256':before,'after_sha256':digest(driver)})
    after=digest(driver)
    state={field_id:{'field_id':field_id,'kind':field['kind'],'active':False,'value':None} for field_id,field in inventory.items()}
    trace=[]
    for step in plan['fields']:
        if step['state']!='prepared':continue
        field_id=step['field_id'];locator=driver['locators'].get(field_id)
        if locator not in state:continue
        value=clone(step['value'])
        if step['kind']=='attachment':
            try:raw=base64.b64decode(driver['upload_buffers'][field_id],validate=True)
            except (ValueError,KeyError):raw=b''
            # Observed attachment identity comes from uploaded bytes, not plan
            # metadata. Wrong bytes stay wrong until the reload action runs.
            value['sha256']=hashlib.sha256(raw).hexdigest();value['size']=len(raw)
        state[locator].update(active=True,value=value)
        trace.append({'action':'attach' if step['kind']=='attachment' else 'fill',
                      'field_id':locator,'value_sha256':digest(value)})
    readback={'schema':'keel.loki.readback.v1','contract_sha256':driver['snapshot_sha256'],
        'origin':fixture['contract']['origin'],'account_id':fixture['contract']['account_id'],
        'fields':list(state.values()),'unexpected_controls':driver['unexpected_controls'],'submitted':False}
    matched=validate_readback(fixture['contract'],plan,readback)['status']=='MATCH'
    return {'outcome':'PREPARED' if matched else 'BLOCKED','reason':'matched' if matched else 'readback_mismatch',
            'readback_sha256':digest(readback),'trace_sha256':digest(trace),'repair_actions':actions,
            'before_driver_sha256':before,'after_driver_sha256':after}


def evaluate(candidate,frozen,*,expected_candidate_sha256,expected_fixtures_sha256,source_sha256):
    candidate=clone(candidate);frozen=clone(frozen)
    require_hash(expected_candidate_sha256);require_hash(expected_fixtures_sha256);require_hash(source_sha256)
    require_dict(candidate,{'schema','candidate_id','version','status','action','trigger','field_id','form_sha256','failure_trace_sha256','counterexample_sha256','fixtures_sha256','source_sha256','execution_authorized'})
    if candidate['schema']!='keel.muse.repair-candidate.v1' or candidate['status']!='QUARANTINED' or candidate['execution_authorized'] is not False or candidate['trigger'] not in FIXES or candidate['action']!=FIXES[candidate['trigger']]: raise ValueError('repair_candidate_invalid')
    rebuilt=freeze_fixtures(frozen['cases'])
    if frozen!=rebuilt or digest(candidate)!=expected_candidate_sha256 or frozen['fixtures_sha256']!=expected_fixtures_sha256 or candidate['fixtures_sha256']!=expected_fixtures_sha256 or candidate['source_sha256']!=source_sha256: raise ValueError('repair_pin_mismatch')
    rows=[]
    for case in frozen['cases']:
        baseline=_observe(case); observed=_observe(case,candidate)
        rows.append({'case_id':case['case_id'],'case_sha256':digest(case),'expected_outcome':case['expected_outcome'],'baseline':baseline,'candidate':observed})
    return {'schema':'keel.muse.repair-replay.v1','candidate':candidate,'candidate_sha256':expected_candidate_sha256,
        'fixtures_sha256':expected_fixtures_sha256,'source_sha256':source_sha256,'rows':rows,
        'replay_sha256':digest(rows),'mode':'FIXTURE','synthetic':True,'actual_browser_verified':False,
        'counterexample_authenticated':False,'promotion_recommended':False,'execution_authorized':False}


def recommend(report,*,expected_report_sha256,candidate,frozen,source_sha256):
    """Re-execute the frozen fixture replay; ignore caller performance claims."""
    report=clone(report);require_hash(expected_report_sha256)
    if digest(report)!=expected_report_sha256: raise ValueError('repair_report_pin_mismatch')
    verified=evaluate(candidate,frozen,expected_candidate_sha256=digest(candidate),expected_fixtures_sha256=frozen['fixtures_sha256'],source_sha256=source_sha256)
    if verified!=report: raise ValueError('repair_replay_mismatch')
    rows=report['rows']; beneficial=harmful=false_ready=0
    for row in rows:
        gold=row['expected_outcome'];before=row['baseline']['outcome'];after=row['candidate']['outcome']
        beneficial+=before!=gold and after==gold
        harmful+=before==gold and after!=gold
        false_ready+=gold=='BLOCKED' and after=='PREPARED'
    return {'schema':'keel.muse.repair-recommendation.v1','report_sha256':expected_report_sha256,
        'candidate_sha256':report['candidate_sha256'],'case_count':len(rows),'beneficial':beneficial,'harmful':harmful,
        'false_ready':false_ready,'recommendation':'REVIEW_FOR_INTEGRATION' if beneficial>0 and harmful==0 and false_ready==0 else 'KEEP_QUARANTINED',
        'promoted':False,'facts_or_policy_modified':False,'execution_authorized':False}


def demo(home=None):
    fixture=demo_fixture()
    frozen=freeze_fixtures([{'case_id':'locator','fixture':fixture,'fault':'missing_locator','field_id':'full_name','expected_outcome':'PREPARED'},
        {'case_id':'unknown','fixture':fixture,'fault':'unknown_required','field_id':'full_name','expected_outcome':'BLOCKED'}])
    trace={'trace_id':'fixture-failure','failure_code':'missing_locator','field_id':'full_name','form_sha256':digest(fixture['contract']),
           'counterexample':{'observed_field':'full_name','observed_value':None}}
    candidate=propose(trace,frozen_fixtures_sha256=frozen['fixtures_sha256'],source_sha256='a'*64)
    report=evaluate(candidate,frozen,expected_candidate_sha256=digest(candidate),expected_fixtures_sha256=frozen['fixtures_sha256'],source_sha256='a'*64)
    return {'candidate':candidate,'replay':report,'recommendation':recommend(report,expected_report_sha256=digest(report),candidate=candidate,frozen=frozen,source_sha256='a'*64)}
