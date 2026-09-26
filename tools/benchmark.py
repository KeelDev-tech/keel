#!/usr/bin/env python3
"""Repeatable local stress probe; synthetic data, no network or quality claims."""
import argparse
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engines'))
from outcome_tracking.evidence_gate import coverage_report
from safe_io import canonical,utc_now


def run(size=10000):
    if type(size) is not int or not 1<=size<=100000:raise ValueError('size must be 1..100000')
    now=utc_now();rows=[{'role_id':'R-'+str(i),'status':'SUBMITTED','date_submitted':now.isoformat()} for i in range(size)]
    events=[{'event_id':'event-'+str(i),'event_type':'submission_claimed','role_id':'R-'+str(i),'ts':now.isoformat(),'details':{'confirmation':'"synthetic quote"'}} for i in range(size)]
    elapsed=[]
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'events.jsonl'
        path.write_bytes(b''.join(canonical(event)+b'\n' for event in events))
        for _ in range(3):
            start=time.perf_counter();report=coverage_report(rows+rows,str(path),now=now);elapsed.append(time.perf_counter()-start)
            assert report['submission_claims']==size and report['verified']==0 and report['pending']==size
        expired=coverage_report(rows,str(path),now=now+timedelta(hours=25))
        assert expired['pending']==0 and expired['unevidenced']==size
        return {'schema_version':1,'fixture':'synthetic claim rows duplicated once + unique claim events',
                'unique_roles':size,'input_rows':size*2,'events':size,'event_bytes':path.stat().st_size,
                'runs_seconds':elapsed,'median_seconds':statistics.median(elapsed),'max_seconds':max(elapsed),
                'checks':{'duplicates_not_double_counted':True,'quotes_never_verified':True,'pending_expires':True},
                'python':platform.python_version(),'platform':platform.system(),'network_requests':0,
                'limitation':'Local synthetic reducer performance only; not ATS throughput, hiring quality, or a production SLO.'}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--size',type=int,default=10000);parser.add_argument('--out',required=True);args=parser.parse_args()
    report=run(args.size);Path(args.out).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
