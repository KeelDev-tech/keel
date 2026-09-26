#!/usr/bin/env python3
"""Source-bound offline acceptance across all eight capability extensions."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from keel_loki.common import atomic_json,digest
from keel_loki.demo import run_demo
from keel_loki.forms import demo_fixture,build_plan,expected_readback,validate_readback,inventory_blockers
from keel_loki.actions import ActionGateway
from keel_loki.lab import validate_report
from keel_loki.modelcheck import replay_trace
from keel_loki.__main__ import inspect_host
from tools.loki_inventory import inventory


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True)
    args=p.parse_args(argv);out=Path(args.out).absolute();out.mkdir(mode=0o700,exist_ok=False)
    before=inventory();events=[]
    def guard(event,args):
        if event in {'socket.__new__','socket.connect','subprocess.Popen','os.system','os.exec','os.posix_spawn'}:
            events.append(event);raise RuntimeError('offline acceptance prohibits network and process execution')
    sys.addaudithook(guard)
    demo=run_demo(out/'rehearsal');c=demo['components'];checks=[]
    def check(name,condition):checks.append({'name':name,'status':'PASS' if condition else 'FAIL'})
    def rejected(call):
        try:call()
        except (ValueError,TypeError,KeyError):return True
        return False
    f=demo_fixture();plan=build_plan(f['contract'],f['values'],f['approvals'],now=f['now'])
    check('complete_typed_form_inventory',len(plan['fields'])==12 and sum(x['state']=='prepared' for x in plan['fields'])==11)
    check('conditional_inactive_field_retained',sum(x['state']=='inactive' for x in plan['fields'])==1)
    observed=expected_readback(f['contract'],plan)
    check('fixture_readback_not_rendered_proof',validate_readback(f['contract'],plan,observed)['rendered_browser_validated'] is False)
    observed['fields'][0]['value']='wrong value'
    check('wrong_readback_rejected',validate_readback(f['contract'],plan,observed)['status']=='MISMATCH')
    missing=deepcopy(f['values']);missing.pop('full_name')
    check('missing_required_fact_rejected',rejected(lambda:build_plan(f['contract'],missing,f['approvals'],now=f['now'])))
    check('missing_browser_not_passed',c['forms']['status']=='PARTIAL' and c['forms']['rendered_browser']['status']=='NOT_RUN')
    g=ActionGateway(f['contract'],f['host_snapshot'],f['approvals']);grant=g.issue(f['values'],now=f['now'])
    g.consume(grant['capability'],grant['actions'][0],now=f['now'])
    check('action_replay_rejected',rejected(lambda:g.consume(grant['capability'],grant['actions'][0],now=f['now'])))
    h=deepcopy(f['host_snapshot']);h['holds']=['dedupe']
    check('holds_dominate_preparation',rejected(lambda:ActionGateway(f['contract'],h,f['approvals']).issue(f['values'],now=f['now'])))
    h=deepcopy(f['host_snapshot']);h['no_ai']=True
    check('no_ai_preserved',rejected(lambda:ActionGateway(f['contract'],h,f['approvals']).issue(f['values'],now=f['now'])))
    check('historical_fact_reconstruction',c['memory']['historical_view_preserved'])
    check('corrected_fact_requires_verification',c['memory']['corrected_revision_requires_verification'])
    check('dependent_packet_becomes_stale',c['memory']['projection']['stale_artifact_count']==1)
    check('source_bytes_bound_to_current_plan',c['integration']['initial']['bound_fact_fields']['motivation']['source_sha256']==c['integration']['source_bytes_sha256'])
    check('cross_module_stale_fact_rejected',c['integration']['stale_fact_rejected'])
    check('question_shared_dependency_ranking',c['questions']['shared_fact_question_ranked_first'])
    check('individual_approval_not_reused',c['questions']['individual_approval_deferred'])
    check('lab_injected_not_real',c['lab']['mode']=='INJECTED' and c['lab']['synthetic'] is True)
    changed=deepcopy(c['lab']);changed['records'].pop()
    check('lab_missing_case_rejected',rejected(lambda:validate_report(changed)))
    changed=deepcopy(c['lab']);changed['synthetic']=False
    check('lab_provenance_upgrade_rejected',rejected(lambda:validate_report(changed)))
    check('research_hypothesis_not_declared_proven',c['research']['research_proposition_proven'] is False)
    check('skill_replay_completed',c['skills']['replay']['status']=='PASS')
    check('skill_promoted_then_rolled_back',c['skills']['active_before_rollback'] and not c['skills']['active_after_rollback'])
    check('routing_actual_two_stage_simulation',c['routing']['escalation']['model_calls_attempted']==2 and c['routing']['escalation']['mode']=='INJECTED')
    check('routing_429_no_later_call',c['routing']['after_rate_limit']['model_calls_attempted']==0 and c['routing']['after_rate_limit']['session_state']['rate_limited'])
    check('calibration_explicit_finite_method',c['calibration']['fit']['method']=='finite_family_bonferroni_binomial_upper')
    check('calibration_does_not_authenticate_labels',c['calibration']['fit']['label_provenance_authenticated'] is False)
    model=c['formal']['model']
    check('formal_exploration_completed',model['status']=='VERIFIED_FINITE_MODEL'
          and model['exploration_complete'] and model['states_discovered']>0
          and model['counterexample'] is None and model['progress_failures']==[]
          and model['progress_states_checked']>0)
    check('formal_broken_models_detected',len(c['formal']['mutant_results'])==5
          and all(x=='COUNTEREXAMPLE' for x in c['formal']['mutant_results'].values()))
    check('TLC_not_conflated_with_python',c['formal']['model']['tla_plus']['status']=='NOT_RUN')
    changed=deepcopy(c['formal']['trace']);changed['steps'][0]['action']='finish'
    trace_rejected=False
    try:trace_rejected=replay_trace(changed).get('status')=='NONCONFORMING'
    except ValueError:trace_rejected=True
    check('invalid_implementation_trace_rejected',trace_rejected)
    for name,value in c['recovery']['checks'].items():check('recovery_'+name,value)
    conformance=c['recovery']['journal_conformance']['trace_check']
    check('committed_journal_trace_conforms',conformance['status']=='CONFORMING'
          and conformance['steps_verified']==5)
    check('retrieval_cites_pinned_source',len(c['retrieval']['matches'])==1 and c['retrieval']['matches'][0]['untrusted_text'])
    check('no_real_model_calls',demo['real_model_calls']==0)
    check('no_canonical_writes',demo['canonical_writes']==0)
    check('no_network_or_process_attempts',not events)
    after=inventory();check('source_unchanged',before==after)
    report={'schema':'keel.loki.acceptance.v1','status':'PASS' if all(x['status']=='PASS' for x in checks) else 'FAIL',
        'synthetic':True,'source_unchanged':before==after,'release_source_sha256':after['sha256'],
        'checks':checks,'checks_passed':sum(x['status']=='PASS' for x in checks),'checks_total':len(checks),
        'execution_authorized':False,'production_deployed':False,'real_local_model_inference':'NOT_RUN',
        'live_repository_integration':'NOT_VERIFIED','rendered_browser':'NOT_RUN','external_tlc_model_check':'NOT_RUN',
        'competitive_superiority_established':False,'real_model_calls':0,'canonical_writes':0,
        'network_or_process_attempts':events,'rehearsal':demo,'host_inspection':inspect_host()}
    atomic_json(out/'acceptance.json',report)
    print(json.dumps({k:report[k] for k in ('status','checks_passed','checks_total','release_source_sha256')}))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
