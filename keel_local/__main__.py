"""Local shadow analysis. Outputs proposals; never mutates the live pipeline."""
import argparse
import json
from pathlib import Path
from .contracts import strict_json,timestamp
from .readiness import inventory,refill_plan
from .operations import buffer_plan,scheduler_drift
from .supply_audit import audit_supply
from .tray_audit import audit_tray


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    for command in ('readiness','refill','buffer','scheduler-drift','supply-audit','tray-audit'):
        child=commands.add_parser(command);child.add_argument('input',type=Path)
        if command in ('readiness','supply-audit'):child.add_argument('--now',required=True)
    args=parser.parse_args()
    with args.input.open('rb') as stream:raw=stream.read(16*1024*1024+1)
    if len(raw)>16*1024*1024:raise ValueError('input exceeds 16 MiB')
    data=strict_json(raw)
    if args.command=='readiness':result=inventory(data,now=timestamp(args.now))
    elif args.command=='refill':result=refill_plan(**data)
    elif args.command=='buffer':result=buffer_plan(data['queues'],data['packets'])
    elif args.command=='supply-audit':result=audit_supply(data,now=timestamp(args.now))
    elif args.command=='tray-audit':result=audit_tray(data)
    else:result=scheduler_drift(data['expected'],data['observed'])
    print(json.dumps({'mode':'SHADOW_ONLY','execution_authorized':False,'result':result},indent=2,allow_nan=False))

if __name__=='__main__':main()
