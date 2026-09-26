"""Exercise the machine-only local workflow without network or model calls."""
import argparse
import json
import os
import stat
import sys


def _read_plan(path):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    def invalid_constant(value):
        raise ValueError('nonfinite JSON')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('regular plan file required')
        body=stream.read(8193)
    if len(body)>8192:
        raise ValueError('plan too large')
    return json.loads(body.decode('utf-8'),object_pairs_hook=pairs,
                      parse_constant=invalid_constant)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['capabilities','demo','init','submit','work','status','result','hold'])
    parser.add_argument('--home')
    parser.add_argument('--workspace-id')
    parser.add_argument('--account-id')
    parser.add_argument('--plan',help='bounded JSON plan using fixed local operations')
    parser.add_argument('--run-id')
    parser.add_argument('--max-tasks',type=int,default=16)
    parser.add_argument('--reason',default='owner_hold')
    args=parser.parse_args(argv)
    if args.command!='capabilities' and not args.home:
        parser.error('this command requires --home')
    if args.command=='init' and (not args.workspace_id or not args.account_id):
        parser.error('init requires --workspace-id and --account-id local namespaces')
    if args.command=='submit' and not args.plan:
        parser.error('submit requires --plan')
    if args.command in ('result','hold') and not args.run_id:
        parser.error('result and hold require --run-id')
    try:
        if args.command=='demo':
            from .builtins import demo
            report=demo(args.home)
        elif args.command=='capabilities':
            report={'schema':'keel.machine.capabilities.v1','pure_computation_graph':True,
                'persistent_computation_cache':True,'closed_contract_checks':True,'failure_rate_monitor':True,
                'bounded_http_cache':'engines.http_cache','paid_services_required':False,
                'execution_authorized':False,'production_integrated':False,
                'local_graph_runtime':True,'fixed_operations':['tokenize_text','summarize_terms'],
                'scope':'Owner-controlled local computation; external identity and effect authority remain disconnected.'}
        else:
            from .runtime import LocalGraphRuntime
            if args.command=='init':
                runtime=LocalGraphRuntime.create(args.home,args.workspace_id,args.account_id)
                report=runtime.status()
            else:
                runtime=LocalGraphRuntime(args.home)
                if args.command=='submit':report=runtime.submit(_read_plan(args.plan))
                elif args.command=='work':report=runtime.work(max_tasks=args.max_tasks)
                elif args.command=='status':report=runtime.status()
                elif args.command=='result':report=runtime.result(args.run_id)
                else:report=runtime.hold(args.run_id,reason=args.reason)
        print(json.dumps(report,sort_keys=True,indent=2,allow_nan=False))
        return 3 if report.get('status') in ('DEMO_FAILED','HELD','BLOCKED','UNKNOWN') else 0
    except (ValueError,OSError,RuntimeError,TypeError,KeyError,RecursionError):
        print('keel-machine: blocked input or storage; demo needs a new directory under a private or non-writable ancestor chain',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
