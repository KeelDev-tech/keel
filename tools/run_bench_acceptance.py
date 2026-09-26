#!/usr/bin/env python3
"""Source-bound offline rehearsal; scripted transports never become real models."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from keel_agent.models import HTTPResult
from keel_bench.demo import run_demo,scripted_pass
from keel_bench.experiment import BenchmarkError,plan_digest,run_experiment
from keel_bench.comparison import analyze_run
from keel_bench.datasets import audit_partition
from keel_bench.host import run_host_trial,inspect_host
from tools.bench_inventory import inventory


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True)
    args=p.parse_args(argv);out=Path(args.out).absolute();out.mkdir(mode=0o700,parents=True,exist_ok=False)
    before=inventory();events=[]
    def guard(event,args):
        if event in {'socket.__new__','socket.connect','subprocess.Popen','os.system','os.exec','os.posix_spawn'}:
            events.append(event);raise RuntimeError('offline_rehearsal_forbids_external_actions')
    sys.addaudithook(guard)
    dataset=json.loads((ROOT/'fixtures/grounding_eval/dataset.json').read_bytes())
    demo=run_demo(dataset,source_sha256=before['sha256'])
    plan,run,comp=demo['plan'],demo['run'],demo['comparison'];pin=plan_digest(plan)
    checks=[]
    def check(name,value):checks.append({'name':name,'status':'PASS' if value else 'FAIL'})
    check('complete_repeated_grid',len(run['records'])==54)
    check('scripted_transport_not_inference',run['mode']=='INJECTED' and demo['real_model_calls']==0)
    metrics={s['system_id']:s['metrics'] for s in comp['systems']}
    bad=metrics['scripted-always-pass'];base=metrics['abstain-baseline']
    check('false_pass_denominator',bad['false_pass_on_nonpass_labels']=={'numerator':21,'denominator':21,'value':1.0})
    check('abstain_baseline_utility_cost_visible',base['withheld_supported_cases']['value']==1.0)
    check('task_clusters_not_independent_repeated_trials',comp['paired_comparisons'][0]['tasks']==9 and not comp['repeated_trials_are_independent'])
    check('no_competitive_certification',not comp['state_of_the_art_established'])
    def rejected(changed):
        try:analyze_run(dataset,plan,changed,expected_plan_sha256=pin,bootstrap_samples=100)
        except BenchmarkError:return True
        return False
    changed=deepcopy(run);changed['records'].pop()
    check('missing_trial_rejected',rejected(changed))
    changed=deepcopy(run);changed['records'].reverse()
    check('changed_schedule_rejected',rejected(changed))
    changed=deepcopy(run);changed['records'][0]['expected_verdict']='untrusted-label'
    check('changed_labels_rejected',rejected(changed))
    changed=deepcopy(run);changed['source_sha256']='0'*64
    check('changed_source_pin_rejected',rejected(changed))
    disabled=run_experiment(dataset,plan,expected_plan_sha256=pin,source_sha256=before['sha256'])
    dcomp=analyze_run(dataset,plan,disabled,expected_plan_sha256=pin,bootstrap_samples=100)
    dm=next(s['metrics'] for s in dcomp['systems'] if s['kind']=='local_model')
    check('models_disabled_by_default',disabled['model_calls_attempted']==0)
    check('disabled_cases_count_as_errors',dm['errors']['value']==1 and dm['validated_response_coverage']['value']==0)
    limited=deepcopy(plan);limited['limits']['max_model_calls']=2
    lr=run_experiment(dataset,limited,expected_plan_sha256=plan_digest(limited),source_sha256=before['sha256'],
                       allow_model_calls=True,transport=scripted_pass)
    lm=next(s['metrics'] for s in analyze_run(dataset,limited,lr,expected_plan_sha256=plan_digest(limited),bootstrap_samples=100)['systems'] if s['kind']=='local_model')
    check('call_budget_stops_without_omission',lr['model_calls_attempted']==2 and len(lr['records'])==54 and lm['errors']['numerator']==25)
    rate=run_experiment(dataset,plan,expected_plan_sha256=pin,source_sha256=before['sha256'],allow_model_calls=True,
        transport=lambda *_:HTTPResult(429,{},b''))
    analyze_run(dataset,plan,rate,expected_plan_sha256=pin,bootstrap_samples=100)
    check('global_rate_limit_stop',rate['model_calls_attempted']==1 and rate['stop_reason']=='model_http_429')
    changed=deepcopy(rate);changed['stop_reason']=None
    check('erased_rate_limit_rejected',rejected(changed))
    held=deepcopy(dataset);held['split']='held_out'
    for c in held['cases']:c['case_id']='renamed-'+c['case_id']
    leakage=audit_partition(dataset,held)
    check('renaming_cannot_hide_partition_overlap',leakage['status']=='EXACT_OVERLAP_DETECTED')
    check('partition_audit_not_independence_proof',not leakage['heldout_independence_verified'])
    host=run_host_trial(out/'synthetic-host')
    check('actual_local_source_answer_packet_chain',all(host['checks'][k]['status']=='PASS' for k in ('evidence','scoped_answer','packet','answer_packet_binding')))
    check('unrun_browser_stays_unverified',host['status']=='PARTIAL' and host['browser']['status']=='NOT_RUN' and not host['rendered_browser_verified'])
    check('partial_form_stays_explicit',not host['full_application_trial'] and not host['whole_form_prepared'])
    check('no_submission_or_authority',not host['external_submission'] and not host['local_submission'] and not host['execution_authorized'])
    inspection=inspect_host()
    check('dependency_presence_not_runtime_pass',not inspection['node']['runnability_verified'] and inspection['rendered_browser']=='NOT_RUN')
    check('no_network_or_process_attempts',not events)
    after=inventory();check('source_unchanged',before==after)
    report={'schema':'keel.bench.acceptance.v1','status':'PASS' if all(x['status']=='PASS' for x in checks) else 'FAIL',
        'synthetic':True,'source_unchanged':before==after,'release_source_sha256':after['sha256'],
        'checks':checks,'checks_passed':sum(x['status']=='PASS' for x in checks),'checks_total':len(checks),
        'execution_authorized':False,'production_deployed':False,'real_local_model_inference':'NOT_RUN',
        'live_repository_integration':'NOT_VERIFIED','real_benchmark_trials':'NOT_RUN','competitive_superiority_established':False,
        'real_model_calls':0,'rendered_browser':'NOT_RUN','browser_actions':0,'canonical_writes':0,
        'network_or_process_attempts':events,'synthetic_comparison':comp,'scripted_transport_calls':run['model_calls_attempted'],
        'synthetic_host_trial':host,'host_inspection':inspection}
    (out/'acceptance.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    print(json.dumps({k:report[k] for k in ('status','checks_passed','checks_total','release_source_sha256')}))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
