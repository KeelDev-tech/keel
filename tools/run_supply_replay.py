#!/usr/bin/env python3
"""Replay metadata-only failure categories against synthetic supply fixtures."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'engines'))
import muse_bridge as bridge
import supply_measurements as measurements
from safe_io import atomic_json
from tools.run_supply_acceptance import run_acceptance

CHECKS={'http_429':('429_stops_without_retry','transport_none_preserves_prior_live','failed_latest_attempt_not_current'),
    'questions':('real_questions_and_ledger_holds_excluded',),
    'ledger_hold':('real_questions_and_ledger_holds_excluded',),
    'office_hold':('protected_row_byte_equivalent','posting_instructions_cannot_clear_hold'),
    'ownership_change':('ownership_change_stops_commit',)}


def validate_cases(value):
    if (not isinstance(value,dict) or set(value)!={'schema','source_snapshot_sha256','scope','cases','omitted_cases','submission_authorized'}
            or value['schema']!=measurements.CASE_SCHEMA or value['submission_authorized'] is not False
            or not isinstance(value['source_snapshot_sha256'],str) or not measurements.HEX.fullmatch(value['source_snapshot_sha256'])
            or type(value['omitted_cases']) is not int or value['omitted_cases']<0
            or not isinstance(value['cases'],list) or not 1<=len(value['cases'])<=25):
        raise ValueError('nonempty bounded replay contract required')
    seen=set()
    for case in value['cases']:
        if (not isinstance(case,dict) or set(case)!={'case_id','scenario'}
                or not isinstance(case['case_id'],str) or not measurements.HEX.fullmatch(case['case_id'])
                or case['case_id'] in seen or not isinstance(case['scenario'],str) or case['scenario'] not in CHECKS):
            raise ValueError('replay accepts only fixed synthetic recipes; no targets or payloads')
        seen.add(case['case_id'])
    return value


def replay(cases,out,*,prohibited_events):
    validate_cases(cases)
    out=Path(out).resolve()
    if out==ROOT or ROOT in out.parents:raise ValueError('replay output must be outside source')
    out.mkdir(parents=True,exist_ok=False)
    acceptance=run_acceptance(out/'synthetic',prohibited_events=prohibited_events)
    checks={c['name']:c['status'] for c in acceptance['checks']}
    results=[{'case_id':case['case_id'],'scenario':case['scenario'],
        'status':'PASS' if acceptance['status']=='PASS' and all(checks.get(c)=='PASS' for c in CHECKS[case['scenario']]) else 'FAIL',
        'checked_postconditions':list(CHECKS[case['scenario']])} for case in cases['cases']]
    report={'schema':'keel.supply-replay.v1','status':'PASS' if all(c['status']=='PASS' for c in results) else 'FAIL',
        'source_snapshot_sha256':cases['source_snapshot_sha256'],'release_source_sha256':acceptance['release_source_sha256'],
        'synthetic':True,'incident_reconstructed':False,'input_authenticated':False,'live_muse_verified':False,
        'submission_authorized':False,'cases':results,'cases_total':len(results),
        'cases_passed':sum(c['status']=='PASS' for c in results),'prohibited_events':list(prohibited_events)}
    atomic_json(out/'replay.json',report)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--cases',required=True);parser.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    path=Path(args.cases).absolute();value=bridge.loads(bridge.secure_bytes(path.parent.resolve(),path.name,limit=65536))
    events=[]
    def guard(event,args):
        if event in {'socket.__new__','socket.connect','subprocess.Popen','os.system','os.exec','os.posix_spawn'}:
            events.append(event);raise RuntimeError('offline replay prohibits network/process execution')
    sys.addaudithook(guard)
    report=replay(value,args.out,prohibited_events=events)
    print(json.dumps({k:report[k] for k in ('status','cases_total','cases_passed','release_source_sha256')}))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
