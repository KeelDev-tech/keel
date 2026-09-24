#!/usr/bin/env python3
"""Source-bound offline acceptance for the executable Muse integration candidate."""
import argparse
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from keel_muse.common import atomic_json,digest,new_home,outside_source
from keel_muse.demo import run_demo
from keel_muse.review import validate_projection
from keel_muse.capabilities import validate_manifest,fixture_manifest
from keel_muse.backup import checkpoint,restore,snapshot
from keel_muse.coordinator import Coordinator
from tools.muse_inventory import inventory
from tools.bench_inventory import read_file


def collect_checks(demo,home,*,source_unchanged,network_events):
    """Derive assertions from executed fixture outputs and independent negatives."""
    c=demo['components'];checks=[]
    def check(name,condition):
        if type(condition) is not bool:raise ValueError('acceptance_boolean_required')
        checks.append({'name':name,'status':'PASS' if condition else 'FAIL'})
    def rejected(call):
        try:call()
        except (ValueError,TypeError,KeyError):return True
        return False
    capability=c['capabilities']
    check('native_capability_not_inferred_from_python',capability['native_adapter']['status']=='UNAVAILABLE' and capability['native_tool_calls']==0)
    manifest=fixture_manifest();manifest['operations'].append('submit')
    check('submission_operation_not_in_manifest',rejected(lambda:validate_manifest(manifest)))
    capture=c['sources']['capture']
    check('real_fixture_source_records_captured',capture['status']=='CAPTURED' and capture['missing_families']==['approval'])
    check('no_fabricated_human_decision',c['sources']['actual_human_decisions']==0 and c['sources']['approval_genuinely_absent'] is True)
    context=c['context']['compilation']
    check('complete_context_covers_every_form_field',context['status']=='COMPILED' and context['coverage_complete'] is True and context['field_count']==12)
    check('missing_evidence_blocks_context',c['context']['missing_field_blocks'] is True)
    check('overflow_preserves_mandatory_policy',c['context']['overflow_blocks_without_truncation'] is True and context['policy_truncated'] is False)
    check('human_only_values_stay_out_of_model_context',c['context']['human_values_kept_host_only'] is True)
    check('bridge_rejects_stale_state',c['bridge']['stale_state_rejected'] is True)
    native=c['browser']['preparation']
    check('native_fixture_complete_readback',native['status']=='SIMULATED' and native['field_count']==12 and native['active_field_count']==11 and native['readback']['status']=='EXACT_REPORTED_READBACK')
    check('native_fixture_effects_counted',native['action_attempts']==11 and native['actions_reported_applied']==11 and c['browser']['fixture_request_count']==23)
    check('native_fixture_not_account_execution',c['browser']['transport_mode']=='INJECTED' and c['browser']['real_browser_actions']==0)
    coordinator=c['coordinator']
    check('coordinator_has_measured_checks',type(coordinator['checks']) is dict and len(coordinator['checks'])>=6)
    for name,value in coordinator['checks'].items():check('coordinator_'+name,value is True)
    projection=c['review']
    check('review_projection_recomputes',digest(validate_projection(projection))==digest(projection))
    changed=deepcopy(projection);changed['metrics']['applications']+=1
    check('review_metric_tampering_rejected',rejected(lambda:validate_projection(changed)))
    check('review_does_not_mint_authority',projection['human_identity_authenticated'] is False and projection['execution_authorized'] is False)
    html=read_file(home/'dashboard','review.html')
    check('dashboard_bytes_match_recorded_artifact',hashlib.sha256(html).hexdigest()==c['dashboard']['dashboard']['sha256'] and len(html)>1000)
    class CSP(HTMLParser):
        def __init__(self):super().__init__();self.values=[]
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            if tag=='meta' and attrs.get('http-equiv','').lower()=='content-security-policy':self.values.append(attrs.get('content',''))
    parsed=CSP();parsed.feed(html.decode('utf-8'))
    check('dashboard_network_disabled_in_csp',len(parsed.values)==1 and "connect-src 'none'" in parsed.values[0] and c['dashboard']['browser_render_verified'] is False)
    measurements=c['evaluation']['comparison']['metrics']
    check('e2e_fixture_attempts_really_prepare',measurements['fixture']['attempts']==2 and measurements['fixture']['successful_preparation']=={'numerator':2,'denominator':2})
    check('e2e_missing_host_remains_error',measurements['host']['attempts']==2 and measurements['host']['errors']==2 and measurements['host']['transport_calls']==0)
    check('e2e_competitive_claim_stays_unproven',c['evaluation']['comparison']['competitive_superiority_established'] is False)
    repair=c['repair'];corrected=repair['replay']['rows'][0]
    check('repair_changes_actual_driver_state',corrected['baseline']['outcome']=='BLOCKED' and corrected['candidate']['outcome']=='PREPARED' and len(corrected['candidate']['repair_actions'])==1 and corrected['candidate']['before_driver_sha256']!=corrected['candidate']['after_driver_sha256'])
    check('repair_preserves_unknown_required_blocker',repair['replay']['rows'][1]['candidate']['outcome']=='BLOCKED')
    check('repair_recommendation_not_self_promotion',repair['recommendation']['beneficial']==1 and repair['recommendation']['false_ready']==0 and repair['recommendation']['promoted'] is False)
    backup=c['backup']
    check('real_recovery_backup_restored_429',backup['rate_limit_preserved'] is True and backup['restore']['controller_started'] is False)
    check('backup_requires_independent_checkpoint',rejected(lambda:restore(home/'backup'/'backup',home/'forbidden-restore',authoritative_checkpoint=None,expected_manifest_sha256=backup['backup']['manifest_sha256'])))
    current=checkpoint(home/'coordinator','fixture',profile='coordinator')
    saved=snapshot(home/'coordinator',home/'coordinator-backup','fixture',profile='coordinator',expected_checkpoint=current)
    restored=restore(home/'coordinator-backup',home/'coordinator-restored',authoritative_checkpoint=current,expected_manifest_sha256=saved['manifest_sha256'])
    copied=Coordinator.open_readonly(home/'coordinator-restored','fixture').snapshot()
    original=Coordinator.open_readonly(home/'coordinator','fixture').snapshot()
    check('coordinator_backup_preserves_full_state_and_inbox',copied==original and copied['state']['rate_limited'] is True and copied['state']['tasks']['second']['status']=='UNKNOWN')
    check('coordinator_restore_does_not_start_controller',restored['controller_started'] is False)
    integrated=c['integration']
    check('integration_has_measured_checks',type(integrated['checks']) is dict and len(integrated['checks'])>=6)
    for name,value in integrated['checks'].items():check('integration_'+name,value is True)
    check('reconciliation_has_measured_checks',type(c['reconciliation']['checks']) is dict and len(c['reconciliation']['checks'])>0)
    for name,value in c['reconciliation']['checks'].items():check('reconciliation_'+name,value is True)
    session=c['session']
    check('serializable_native_session_completes_fixture',session['preparation']['status']=='SIMULATED' and session['preparation']['action_attempts']==11 and session['preparation']['actions_reported_applied']==11)
    check('native_session_counts_every_issued_request',session['preparation']['issued_requests']==23)
    check('native_session_observation_replay_idempotent',session['idempotent_observation'] is True)
    check('native_session_restart_preserves_uncertain_effect',session['recovered_effect_status']=='UNKNOWN')
    check('native_session_no_account_or_browser_claim',session['actual_native_browser']=='NOT_RUN' and session['account_muse_integration']=='NOT_VERIFIED')
    check('no_real_model_calls',demo['real_model_calls']==0)
    check('no_canonical_writes',demo['canonical_writes']==0)
    check('no_network_or_process_attempts',not network_events)
    check('source_unchanged',source_unchanged)
    if not checks or len({r['name'] for r in checks})!=len(checks):raise ValueError('unique_nonempty_acceptance_checks_required')
    return checks


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    out=new_home(outside_source(args.out,ROOT))
    before=inventory();events=[]
    # Same offline audit boundary as the predecessor acceptance runner. This
    # Python guard is not an OS sandbox; it records prohibited attempts even
    # when a component catches the raised exception.
    def guard(event,args):
        if event in {'socket.__new__','socket.connect','subprocess.Popen','os.system','os.exec','os.posix_spawn'}:
            events.append(event);raise RuntimeError('offline acceptance prohibits network and process execution')
    sys.addaudithook(guard)
    demo=run_demo(out/'rehearsal')
    after=inventory()
    checks=collect_checks(demo,out/'rehearsal',source_unchanged=before==after,network_events=events)
    final_inventory=inventory()
    unchanged=before==after==final_inventory
    # Backup checks above also execute real code and must leave source intact.
    next(row for row in checks if row['name']=='source_unchanged')['status']='PASS' if unchanged else 'FAIL'
    report={'schema':'keel.muse.acceptance.v1','status':'PASS' if all(r['status']=='PASS' for r in checks) else 'FAIL',
        'synthetic':True,'source_unchanged':unchanged,'release_source_sha256':final_inventory['sha256'],
        'source_sha256':final_inventory['sha256'],'checks':checks,'checks_total':len(checks),
        'checks_passed':sum(r['status']=='PASS' for r in checks),'execution_authorized':False,
        'production_deployed':False,'real_local_model_inference':'NOT_RUN','actual_native_browser':'NOT_RUN',
        'account_muse_integration':'NOT_VERIFIED','live_repository_integration':'NOT_VERIFIED',
        'rendered_browser':'NOT_RUN','external_tlc_model_check':'NOT_RUN','competitive_superiority_established':False,
        'real_model_calls':0,'canonical_writes':0,'network_or_process_attempts':events,'rehearsal':demo}
    atomic_json(out/'acceptance.json',report)
    print(json.dumps({k:report[k] for k in ('status','checks_passed','checks_total','release_source_sha256')}))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
