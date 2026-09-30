"""Read-only pseudonymous supply measurements; no payload capture or authority."""
import hashlib
import hmac
import math
from collections import Counter
from pathlib import Path
import re

import muse_bridge as bridge
import ready_gate
import supply_recovery as recovery
from safe_io import aware_time, canonical, digest, MAX_JSON_BYTES, utc_now

SCHEMA = 'keel.supply-measurements.v1'
CASE_SCHEMA = 'keel.supply-replay-cases.v1'
HEX = re.compile(r'[a-f0-9]{64}\Z')
TRANSPORTS = {'OK','TIMEOUT','DNS_FAILURE','CONNECTION_RESET','HTTP_429','HTTP_5XX','SOURCE_UNAVAILABLE'}
REASONS = {'unknown_fit','below_fit_floor','not_apply_band','blocklisted_employer','blocklist_unconfirmed',
    'invalid_gates','duplicate_queue_home','rejected_queue','active_or_terminal_or_owned','ledger_hold',
    'posting_identity_missing','posting_identity_duplicate','verification_cooldown_invalid',
    'verification_cooldown','verification_event_pending','host_runtime_hold','host_approval_refused',
    'question_or_classification_unconfirmed','posting_check_required','scoped_material_packet_required',
    'ready_transition_not_observed','unclassified_blocker'}
REASONS |= {'explicit_hold:'+f for f in ready_gate.HOLD_FIELDS}
REASONS |= {'explicit_gate:'+f for f in ready_gate.HOLD_FIELDS}
REASONS |= {'unanswered_questions:'+f for f in ready_gate.QUESTION_FIELDS}
SCENARIOS = {'http_429','questions','ledger_hold','office_hold','ownership_change'}


def token(key, purpose, value):
    return hmac.new(key, canonical([purpose,value]), hashlib.sha256).hexdigest()


def _key(root, path):
    path=Path(path)
    if not path.is_absolute():path=root/path
    try:relative=path.relative_to(root).as_posix()
    except ValueError as exc:raise ValueError('measurement key must be inside workspace') from exc
    key=bridge.secure_bytes(root,relative,limit=32)
    if len(key)!=32:raise ValueError('measurement key must contain exactly 32 random bytes')
    return key


def seal(value):
    value['integrity_sha256']=digest({k:v for k,v in value.items() if k!='integrity_sha256'})
    return value


def snapshot(workspace, *, key_file, now=None):
    state=bridge.read_state(workspace);root=state['root'];key=_key(root,key_file);now=now or utc_now()
    records,by_id=recovery._inventory(state,now=now)
    code=digest({'supply':state['code']['sha256'],
        'measurements':hashlib.sha256(bridge.secure_bytes(Path(__file__).resolve().parent,'supply_measurements.py')).hexdigest()})
    policy={name:value for name,value in state['manifest']['input_hashes'].items()
            if not name.startswith('data/queues/') and name!='data/application-ledger.json'}
    output=[]
    for record in records:
        entry=by_id[record['role_id']];attempt=entry.get('verification_attempt')
        refs=[];transport='UNKNOWN';signal='UNKNOWN'
        if isinstance(attempt,dict):
            transport=attempt.get('transport_class') if attempt.get('transport_class') in TRANSPORTS else 'UNKNOWN'
            signal=attempt.get('signal') if attempt.get('signal') in {'LIVE','NONE','AMBIGUOUS'} else 'UNKNOWN'
            run=attempt.get('observation_id');evidence=attempt.get('evidence')
            requests=evidence.get('request_attempts') if isinstance(evidence,dict) else None
            if isinstance(run,str) and ':' in run and isinstance(requests,list) and len(requests)<=10:
                for request in requests:
                    if (not isinstance(request,dict) or type(request.get('request_number')) is not int
                            or request['request_number']<1 or not isinstance(request.get('source_url'),str)):
                        continue
                    try:aware_time(request.get('observed_at'))
                    except (TypeError,ValueError):continue
                    refs.append(token(key,'dispatch',[str(root),run.rsplit(':',1)[0],request]))
        score=entry.get('fit_score')
        bucket=('UNKNOWN' if type(score) not in (int,float) or not math.isfinite(score)
                else 'MAIN' if score>=state['manifest']['main_fit_floor'] else 'BELOW')
        output.append({'role_ref':token(key,'role',[str(root),record['role_id']]),
            'target_ref':token(key,'target',[str(root),record['posting_identity']]),
            'stage':record['stage'],'fit_bucket':bucket,
            'reason_codes':sorted({r if r in REASONS else 'unclassified_blocker' for r in record['reason_codes']}),
            'prepared_artifact':bool(record['prepared_artifact']),
            'local_ownership_marker':bool(entry.get('browser_task_id') or entry.get('attempt_id')),
            'verification_signal':signal,'transport_class':transport,'dispatch_refs':sorted(set(refs))})
    report={'schema':SCHEMA,'observed_at':now.isoformat(),'key_scope':token(key,'scope',['v1',str(root)]),
        'workspace_ref':token(key,'workspace',str(root)),'code_sha256':code,
        'policy_ref':token(key,'policy',[state['manifest']['main_fit_floor'],policy]),
        'roles':sorted(output,key=lambda r:r['role_ref']),'queue_rows':len(state['entries']),
        'conservation_pass':len(output)==len(state['entries']),
        'stage_counts':dict(Counter(r['stage'] for r in output)),
        'blocker_counts':dict(Counter(c for r in output for c in r['reason_codes'])),
        'fit_counts':dict(Counter(r['fit_bucket'] for r in output)),
        'retained_dispatches':len({ref for r in output for ref in r['dispatch_refs']}),
        'dispatch_coverage_complete':False,'model_tokens':None,'credits_used':None,
        'launchable_ready':None,'execution_authorized':False,'submission_authorized':False,'network_reads':0}
    seal(report)
    if len(canonical(report))>MAX_JSON_BYTES:raise ValueError('measurement snapshot exceeds export budget')
    return report


def validate(value):
    fields={'schema','observed_at','key_scope','workspace_ref','code_sha256','policy_ref','roles','queue_rows',
        'conservation_pass','stage_counts','blocker_counts','fit_counts','retained_dispatches','dispatch_coverage_complete','model_tokens',
        'credits_used','launchable_ready','execution_authorized','submission_authorized','network_reads','integrity_sha256'}
    if not isinstance(value,dict) or set(value)!=fields or value['schema']!=SCHEMA:
        raise ValueError('invalid measurement contract')
    if value['integrity_sha256']!=digest({k:v for k,v in value.items() if k!='integrity_sha256'}):
        raise ValueError('measurement integrity changed')
    aware_time(value['observed_at'])
    for field in ('key_scope','workspace_ref','code_sha256','policy_ref'):
        if not isinstance(value[field],str) or not HEX.fullmatch(value[field]):raise ValueError('invalid measurement binding')
    for field in ('model_tokens','credits_used','launchable_ready'):
        if value[field] is not None:raise ValueError('unknown costs or authority cannot be invented')
    if (value['dispatch_coverage_complete'] is not False or value['execution_authorized'] is not False
            or value['submission_authorized'] is not False or type(value['network_reads']) is not int or value['network_reads']!=0):
        raise ValueError('measurement cannot grant authority or claim coverage')
    records=value['roles']
    if not isinstance(records,list) or len(records)>bridge.MAX_HOST_ROWS:raise ValueError('invalid measurement role count')
    seen=set()
    for r in records:
        if (not isinstance(r,dict) or set(r)!={'role_ref','target_ref','stage','fit_bucket','reason_codes',
            'prepared_artifact','local_ownership_marker','verification_signal','transport_class','dispatch_refs'}):raise ValueError('invalid measurement row')
        if any(not isinstance(r[k],str) or not HEX.fullmatch(r[k]) for k in ('role_ref','target_ref')):
            raise ValueError('invalid opaque reference')
        if r['role_ref'] in seen:raise ValueError('duplicate measurement role')
        seen.add(r['role_ref'])
        if (r['stage'] not in recovery.STAGES or r['fit_bucket'] not in {'UNKNOWN','MAIN','BELOW'}
                or type(r['prepared_artifact']) is not bool or type(r['local_ownership_marker']) is not bool or r['verification_signal'] not in {'UNKNOWN','LIVE','NONE','AMBIGUOUS'}
                or r['transport_class'] not in TRANSPORTS|{'UNKNOWN'}):raise ValueError('invalid measurement state')
        if (not isinstance(r['reason_codes'],list) or any(not isinstance(c,str) or c not in REASONS for c in r['reason_codes'])
                or len(set(r['reason_codes']))!=len(r['reason_codes'])):raise ValueError('unsafe measurement reason')
        if (not isinstance(r['dispatch_refs'],list) or len(r['dispatch_refs'])>10
                or any(not isinstance(ref,str) or not HEX.fullmatch(ref) for ref in r['dispatch_refs'])
                or len(set(r['dispatch_refs']))!=len(r['dispatch_refs'])):raise ValueError('invalid dispatch reference')
    for field in ('stage_counts','blocker_counts','fit_counts'):
        if not isinstance(value[field],dict) or any(type(n) is not int or n<0 for n in value[field].values()):
            raise ValueError('measurement counters must be nonnegative integers')
    if (type(value['queue_rows']) is not int or value['queue_rows']<len(records)
            or type(value['conservation_pass']) is not bool or value['conservation_pass']!=(value['queue_rows']==len(records))
            or value['stage_counts']!=dict(Counter(r['stage'] for r in records))
            or value['blocker_counts']!=dict(Counter(c for r in records for c in r['reason_codes']))
            or value['fit_counts']!=dict(Counter(r['fit_bucket'] for r in records))
            or type(value['retained_dispatches']) is not int
            or value['retained_dispatches']!=len({ref for r in records for ref in r['dispatch_refs']})):
        raise ValueError('measurement counters do not reconcile')
    return value


def compare(before,after):
    validate(before);validate(after)
    if any(before[k]!=after[k] for k in ('key_scope','workspace_ref','code_sha256','policy_ref')):
        raise ValueError('measurement workspace, key, code or policy changed')
    if aware_time(after['observed_at'])<=aware_time(before['observed_at']):raise ValueError('measurement interval must advance')
    if not before['conservation_pass'] or not after['conservation_pass']:raise ValueError('duplicate queue homes prevent comparison')
    old={r['role_ref']:r for r in before['roles']};new={r['role_ref']:r for r in after['roles']};common=set(old)&set(new)
    retargeted={rid for rid in common if old[rid]['target_ref']!=new[rid]['target_ref']}
    gains=sum(not old[r]['prepared_artifact'] and new[r]['prepared_artifact'] for r in common-retargeted)
    previous={ref for r in old.values() for ref in r['dispatch_refs']};current={ref for r in new.values() for ref in r['dispatch_refs']}
    dispatches=len(current-previous)
    return {'schema':'keel.supply-measurement-window.v1','before_sha256':before['integrity_sha256'],
        'after_sha256':after['integrity_sha256'],'transitions':dict(Counter(old[r]['stage']+'->'+new[r]['stage'] for r in common-retargeted)),
        'added_roles':len(set(new)-set(old)),'removed_roles':len(set(old)-set(new)),
        'retargeted_roles':len(retargeted),'observed_prepared_gains':gains,'new_retained_dispatches':dispatches,
        'lost_dispatch_references':len(previous-current),
        'gains_per_new_retained_dispatch':gains/dispatches if dispatches else None,
        'dispatch_coverage_complete':False,'model_tokens':None,'credits_used':None,
        'causal_attribution':False,'submission_authorized':False}


def failure_cases(value):
    validate(value)
    cases=[]
    for r in value['roles']:
        reasons=set(r['reason_codes']);scenario=None
        if r['verification_signal']=='NONE' and r['transport_class']=='HTTP_429':scenario='http_429'
        elif any(c.startswith('unanswered_questions:') for c in reasons):scenario='questions'
        elif 'ledger_hold' in reasons:scenario='ledger_hold'
        elif reasons & {'explicit_hold:d1_office_exclusion','explicit_gate:d1_office_exclusion'}:scenario='office_hold'
        elif r['local_ownership_marker']:scenario='ownership_change'
        if scenario:
            cases.append({'case_id':digest([value['integrity_sha256'],r['role_ref'],scenario]),'scenario':scenario})
    return {'schema':CASE_SCHEMA,'source_snapshot_sha256':value['integrity_sha256'],
        'scope':'Representative synthetic regression recipes; not incident reconstruction or authority.',
        'cases':cases[:25],'omitted_cases':max(0,len(cases)-25),'submission_authorized':False}


def report(workspace,*,key_file,before_file=None,cases=False):
    current=snapshot(workspace,key_file=key_file)
    if cases:return failure_cases(current)
    if before_file:
        root=bridge.bound_workspace(workspace);path=Path(before_file)
        if not path.is_absolute():path=root/path
        try:relative=path.relative_to(root).as_posix()
        except ValueError as exc:raise ValueError('prior snapshot must be inside workspace') from exc
        prior=bridge.loads(bridge.secure_bytes(root,relative))
        return {'snapshot':current,'window':compare(prior,current)}
    return current
