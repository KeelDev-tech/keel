"""Reconstruct paired metrics from every scheduled case, including failures.

Intervals resample tasks, retaining their repeated trials together. They are
conditional descriptive estimates, not proof of dataset independence, model
identity, security, generalization, or competitive superiority.
"""
import hashlib
import json
import math
import random
import re

from keel_eval.evaluation import validate_dataset, dataset_digest
from keel_trust.common import canonical, digest
from .experiment import BenchmarkError, validate_plan, plan_digest, schedule, ALL_ERROR_CODES, NO_CALL_ERROR_CODES

VERDICTS = ('PASS', 'FAIL', 'ABSTAIN', 'ERROR')
HASH = re.compile(r'[0-9a-f]{64}\Z')
ERROR = re.compile(r'[a-z][a-z0-9_]{0,127}\Z')
ROW_KEYS = {'system_id','trial','case_id','subject_sha256','expected_verdict','observed_verdict',
            'latency_ms','error_code','assessment_sha256','request_sha256','response_sha256','model_calls_attempted'}
RUN_KEYS = {'schema','plan','plan_sha256','dataset_sha256','source_sha256','synthetic','split',
            'mode','records','wall_ms','stop_reason','model_calls_attempted','execution_authorized',
            'model_quality_validated','production_deployed'}


def require(ok, code):
    if not ok: raise BenchmarkError(code)


def _snapshot(value):
    # Reports can contain 10,000 fixed-width rows; use a larger bounded envelope
    # than the 2 MiB model-input limit, while rejecting recursive/exotic objects.
    try:
        stack, count = [(value,0)], 0
        while stack:
            item, depth = stack.pop(); count += 1
            require(count <= 500000 and depth <= 32, 'report_structure_limit')
            if type(item) is dict:
                require(len(item)<=10000 and all(type(k) is str for k in item),'report_object_invalid')
                stack.extend((v,depth+1) for v in item.values())
            elif type(item) is list:
                require(len(item)<=10000,'report_array_limit')
                stack.extend((v,depth+1) for v in item)
            elif type(item) is str: require(len(item)<=2097152,'report_string_limit')
            elif type(item) is float: require(math.isfinite(item),'report_number_invalid')
            else: require(item is None or type(item) in (bool,int),'report_value_invalid')
        raw=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
        require(len(raw)<=16*1024*1024,'report_byte_limit')
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    except (ValueError,TypeError,RecursionError,UnicodeError) as exc:
        if isinstance(exc,BenchmarkError): raise
        raise BenchmarkError('report_json_invalid') from None


def _hash(value): return type(value) is str and HASH.fullmatch(value) is not None
def _time(value): return type(value) in (int,float) and math.isfinite(value) and 0<=value<=86580000
def _rate(n,d): return {'numerator':n,'denominator':d,'value':n/d if d else None}


def validate_run(dataset,plan,report,*,expected_plan_sha256):
    dataset=validate_dataset(dataset)
    plan=validate_plan(plan,dataset,source_sha256=plan.get('source_sha256') if type(plan) is dict else None)
    report, report_hash=_snapshot(report)
    require(_hash(expected_plan_sha256) and plan_digest(plan)==expected_plan_sha256,'plan_pin_mismatch')
    require(type(report) is dict and set(report)==RUN_KEYS,'run_schema_invalid')
    require(report['schema']=='keel.bench.run.v1','run_schema_invalid')
    require(report['plan']==plan and report['plan_sha256']==expected_plan_sha256,'run_plan_mismatch')
    require(report['dataset_sha256']==dataset_digest(dataset)==plan['dataset_sha256'],'run_dataset_mismatch')
    require(report['source_sha256']==plan['source_sha256'],'run_source_mismatch')
    require(type(report['synthetic']) is bool and report['synthetic']==dataset['synthetic']
            and report['split']==dataset['split'],'run_dataset_metadata_mismatch')
    require(report['mode'] in ('BASELINE_ONLY','INJECTED','LOCAL_LOOPBACK'),'run_mode_invalid')
    require(all(report[k] is False for k in ('execution_authorized','model_quality_validated','production_deployed')),
            'run_authority_claim_forbidden')
    require(_time(report['wall_ms']),'run_time_invalid')
    require(report['stop_reason'] in (None,'model_http_429','model_call_budget_exhausted',
            'wall_budget_exhausted','wall_budget_insufficient_for_deadline'),'run_stop_reason_invalid')
    rows=report['records']; systems={s['system_id']:s for s in plan['systems']}
    cases={c['case_id']:c for c in dataset['cases']}
    require(type(rows) is list and len(rows)==len(systems)*plan['trials']*len(cases),'run_coverage_invalid')
    seen=set(); calls=0; rate_limited=False
    require([(r.get('system_id'),r.get('trial'),r.get('case_id')) if type(r) is dict else None for r in rows]
            ==schedule(plan,dataset),'run_schedule_mismatch')
    for row in rows:
        require(type(row) is dict and set(row)==ROW_KEYS,'run_record_schema_invalid')
        require(type(row['system_id']) is str and row['system_id'] in systems
                and type(row['case_id']) is str and row['case_id'] in cases
                and type(row['trial']) is int and 0<=row['trial']<plan['trials'],'run_record_identity_invalid')
        key=(row['system_id'],row['trial'],row['case_id'])
        require(key not in seen,'duplicate_run_record');seen.add(key)
        case=cases[row['case_id']]; system=systems[row['system_id']]
        require(row['subject_sha256']==digest(case['subject']) and row['expected_verdict']==case['expected_verdict'],
                'run_record_label_or_subject_mismatch')
        require(row['observed_verdict'] in VERDICTS,'run_verdict_invalid')
        require(row['latency_ms'] is None or _time(row['latency_ms']),'run_latency_invalid')
        require(type(row['model_calls_attempted']) is int and row['model_calls_attempted'] in (0,1),'run_calls_invalid')
        prior_calls=calls;calls+=row['model_calls_attempted']
        for name in ('assessment_sha256','request_sha256','response_sha256'):
            require(row[name] is None or _hash(row[name]),'run_exchange_hash_invalid')
        if row['observed_verdict']=='ERROR':
            require(type(row['error_code']) is str and row['error_code'] in ALL_ERROR_CODES and row['assessment_sha256'] is None,
                    'run_error_record_invalid')
        else:
            require(row['error_code'] is None and _hash(row['assessment_sha256']) and row['latency_ms'] is not None,
                    'run_assessment_record_invalid')
        if system['kind']=='abstain_baseline':
            require(row['observed_verdict'] in ('ABSTAIN','ERROR') and row['model_calls_attempted']==0
                    and row['request_sha256'] is None and row['response_sha256'] is None,'baseline_record_invalid')
            require(row['error_code'] in (None,'wall_budget_exhausted'),'baseline_error_invalid')
        elif row['observed_verdict']!='ERROR':
            require(row['model_calls_attempted']==1 and _hash(row['request_sha256']) and _hash(row['response_sha256']),
                    'model_exchange_missing')
        if row['model_calls_attempted']==0:
            require(row['request_sha256'] is None and row['response_sha256'] is None,'unattempted_exchange_forbidden')
            if row['observed_verdict']=='ERROR':
                require(row['error_code'] in NO_CALL_ERROR_CODES and row['latency_ms'] is None,'uncalled_error_invalid')
        else:
            require(_hash(row['request_sha256']) and row['latency_ms'] is not None,'attempted_exchange_missing')
            require(row['error_code'] not in NO_CALL_ERROR_CODES,'attempted_skip_invalid')
        if system['kind']=='local_model' and rate_limited:
            require(row['observed_verdict']=='ERROR' and row['error_code']=='not_run_after_rate_limit'
                    and row['model_calls_attempted']==0,'rate_limit_stop_violated')
        if row['error_code']=='not_run_after_rate_limit':
            require(rate_limited and system['kind']=='local_model','rate_limit_skip_without_trigger')
        if row['error_code']=='model_http_429':
            require(system['kind']=='local_model' and row['model_calls_attempted']==1,'rate_limit_trigger_invalid')
            rate_limited=True
        if row['error_code']=='local_model_not_enabled':
            require(system['kind']=='local_model' and report['mode']=='BASELINE_ONLY','disabled_model_error_invalid')
        if row['error_code']=='model_call_budget_exhausted':
            require(system['kind']=='local_model' and prior_calls>=plan['limits']['max_model_calls'],'call_budget_error_invalid')
        if report['mode']=='BASELINE_ONLY' and system['kind']=='local_model':
            require(row['model_calls_attempted']==0 and row['observed_verdict']=='ERROR','baseline_mode_model_claim')
    require(type(report['model_calls_attempted']) is int and report['model_calls_attempted']==calls
            and calls<=plan['limits']['max_model_calls'],'run_call_total_mismatch')
    require((report['stop_reason']=='model_http_429')==rate_limited,'rate_limit_stop_reason_mismatch')
    budget_codes={'model_call_budget_exhausted','wall_budget_exhausted','wall_budget_insufficient_for_deadline'}
    first_budget=next((r['error_code'] for r in rows if r['error_code'] in budget_codes),None)
    if not rate_limited:require(report['stop_reason']==first_budget,'budget_stop_reason_mismatch')
    return dataset,plan,report,report_hash


def _metrics(rows,trials):
    matrix={g:{v:0 for v in VERDICTS} for g in VERDICTS[:-1]}
    cases={}
    for r in rows:
        matrix[r['expected_verdict']][r['observed_verdict']]+=1
        cases.setdefault(r['case_id'],[]).append(r)
    n=len(rows);pos=sum(matrix['PASS'].values());neg=n-pos
    errors=sum(matrix[g]['ERROR'] for g in matrix)
    correct=sum(matrix[g][g] for g in matrix)
    false_pass=matrix['FAIL']['PASS']+matrix['ABSTAIN']['PASS']
    matched={cid:sum(r['observed_verdict']==r['expected_verdict'] for r in values) for cid,values in cases.items()}
    times=sorted(r['latency_ms'] for r in rows if r['latency_ms'] is not None)
    return {'confusion_matrix':matrix,'exact_label_agreement':_rate(correct,n),
        'false_pass_on_nonpass_labels':_rate(false_pass,neg),
        'false_block_on_pass_labels':_rate(matrix['PASS']['FAIL'],pos),
        'withheld_supported_cases':_rate(pos-matrix['PASS']['PASS'],pos),
        'abstentions':_rate(sum(matrix[g]['ABSTAIN'] for g in matrix),n),'errors':_rate(errors,n),
        'validated_response_coverage':_rate(n-errors,n),
        'tasks_all_trials_match':_rate(sum(v==trials for v in matched.values()),len(cases)),
        'tasks_at_least_one_trial_matches':_rate(sum(v>0 for v in matched.values()),len(cases)),
        'latency':{'count':len(times),'missing_count':n-len(times),
            'mean_ms':sum(times)/len(times) if times else None,
            'p50_ms':times[math.ceil(.5*len(times))-1] if times else None,
            'p95_ms':times[math.ceil(.95*len(times))-1] if times else None,
            'method':'nearest_rank','basis':'measured_harness_wall_time_unattested'},
        'model_calls_attempted':sum(r['model_calls_attempted'] for r in rows)}


def _paired(left,right,case_ids,*,seed,samples):
    def bycase(rows):
        result={}
        for r in rows:result.setdefault(r['case_id'],[]).append(r)
        return result
    a,b=bycase(left),bycase(right)
    deltas=[]
    for cid in case_ids:
        x=sum(r['observed_verdict']==r['expected_verdict'] for r in a[cid])/len(a[cid])
        y=sum(r['observed_verdict']==r['expected_verdict'] for r in b[cid])/len(b[cid])
        deltas.append(x-y)
    mean=sum(deltas)/len(deltas)
    interval=None
    if len(deltas)>=2:
        rng=random.Random(seed)
        draws=sorted(sum(deltas[rng.randrange(len(deltas))] for _ in deltas)/len(deltas) for _ in range(samples))
        interval=[draws[math.floor(.025*(samples-1))],draws[math.ceil(.975*(samples-1))]]
    return {'exact_agreement_delta_left_minus_right':mean,
        'task_wins':sum(d>0 for d in deltas),'task_ties':sum(d==0 for d in deltas),'task_losses':sum(d<0 for d in deltas),
        'tasks':len(deltas),'task_cluster_bootstrap_95_interval':interval,'bootstrap_samples':samples,
        'bootstrap_seed':seed,'interval_method':'paired_task_cluster_percentile_order_statistic',
        'independent_tasks_verified':False,'statistical_superiority_established':False}


def analyze_run(dataset,plan,report,*,expected_plan_sha256,bootstrap_samples=2000):
    require(type(bootstrap_samples) is int and 100<=bootstrap_samples<=10000,'bootstrap_sample_count_invalid')
    dataset,plan,report,report_hash=validate_run(dataset,plan,report,expected_plan_sha256=expected_plan_sha256)
    groups={s['system_id']:[r for r in report['records'] if r['system_id']==s['system_id']] for s in plan['systems']}
    systems=[{'system_id':sid,'kind':next(s['kind'] for s in plan['systems'] if s['system_id']==sid),
              'metrics':_metrics(rows,plan['trials'])} for sid,rows in groups.items()]
    ids=sorted(groups); comparisons=[]
    for i,left in enumerate(ids):
        for right in ids[i+1:]:
            pair_seed=int(hashlib.sha256((str(plan['seed'])+'\0'+left+'\0'+right).encode()).hexdigest()[:16],16)
            comparisons.append({'left':left,'right':right,**_paired(groups[left],groups[right],
                sorted(c['case_id'] for c in dataset['cases']),seed=pair_seed,samples=bootstrap_samples)})
    return {'schema':'keel.bench.comparison.v1','status':'ANALYZED','run_sha256':report_hash,
        'plan_sha256':expected_plan_sha256,'dataset_sha256':plan['dataset_sha256'],'source_sha256':plan['source_sha256'],
        'mode':report['mode'],'synthetic':dataset['synthetic'],'split':dataset['split'],
        'workload_scope':'evidence_review','human_intervention':'NOT_MEASURED','energy_and_hardware_cost':'NOT_MEASURED',
        'systems':systems,'paired_comparisons':comparisons,'record_count':len(report['records']),
        'repeated_trials_are_independent':False,'hardware_control_verified':False,
        'source_and_model_identity_authenticated':False,'heldout_independence_verified':False,
        'label_authenticity_verified':False,'report_authenticity_verified':False,
        'generalization_established':False,'state_of_the_art_established':False,
        'execution_authorized':False,'production_deployed':False,
        'boundary':'Descriptive comparisons on the same supplied tasks and trial plan. Repeated trials stay within task clusters; '
                   'small or dependent task sets can give misleadingly narrow intervals. No production or competitive certification.'}
