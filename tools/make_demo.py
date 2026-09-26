#!/usr/bin/env python3
"""Create synthetic offline review data in a NEW directory. Never contacts an ATS."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'engines'))
import keel
from safe_io import atomic_json,atomic_bytes
from build_dashboard import build


def make_demo(destination):
    destination=Path(destination)
    if destination.exists():raise ValueError('demo destination must be new')
    keel.initialize(destination)
    roles=[{'role_id':'DEMO-1','company':'Northstar · Demo','title':'Operations Manager','status':'READY','action_band':'APPLY'},
           {'role_id':'DEMO-2','company':'Cedar · Demo','title':'Experience Lead','status':'PARKED-NEEDS-INPUT','status_reason':'Confirm the requested travel commitment.'},
           {'role_id':'DEMO-3','company':'Pine · Demo','title':'Program Manager','status':'PARKED-PENDING-VERIFICATION','status_reason':'Inspect the current rendered form before preparation.'}]
    atomic_json(destination/'data/queues/standard-queue.json',{'entries':roles,'demo':True})
    atomic_json(destination/'data/application-ledger.json',{'rows':[{'role_id':'DEMO-4','company':'Harbor · Demo','title':'Coordinator','status':'SUBMITTED','date_submitted':'2026-09-17T10:00:00+00:00'}],'demo':True})
    normal=build(destination)
    atomic_json(destination/'data/queues/strategic-queue.json',[{'role_id':'ATTACK','company':'<img src=x onerror="window.injected=true">','status':'<script>alert(1)</script>'}])
    build(destination,destination/'dashboard/adversarial.html')
    atomic_json(destination/'data/queues/strategic-queue.json',[])
    atomic_bytes(destination/'DEMO_ONLY.txt',b'Synthetic demonstration only. Not an applicant profile or submission record.\n')
    return normal

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination');args=parser.parse_args()
    print(make_demo(args.destination))
