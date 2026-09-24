"""Freeze local experiments, run explicitly selected models, and inspect results."""
import argparse
import json
import os
from pathlib import Path
import sys

from keel_agent.io import read_json
from keel_eval.evaluation import _config
from tools.bench_inventory import inventory
from .experiment import make_plan,plan_digest,run_experiment
from .comparison import analyze_run
from .datasets import audit_partition
from .demo import run_demo
from .host import inspect_host,run_host_trial

ROOT=Path(__file__).resolve().parents[1]


def _write(path,value):
    path=Path(path).absolute()
    if path.parent.resolve(strict=True)!=path.parent:raise ValueError('noncanonical_output_parent')
    raw=(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n').encode()
    if len(raw)>8*1024*1024:raise ValueError('output_byte_limit')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(raw)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('plan');p.add_argument('--dataset',required=True);p.add_argument('--systems',required=True)
    p.add_argument('--experiment-id',required=True);p.add_argument('--trials',type=int,default=3)
    p.add_argument('--seed',type=int,default=0);p.add_argument('--max-model-calls',type=int,default=4096)
    p.add_argument('--max-wall-seconds',type=float,default=3600);p.add_argument('--out',required=True)
    for name in ('run','compare'):
        p=sub.add_parser(name)
        p.add_argument('--dataset',required=True);p.add_argument('--plan',required=True)
        p.add_argument('--expected-plan-sha256',required=True);p.add_argument('--out',required=True)
        if name=='run':p.add_argument('--allow-model-calls',action='store_true')
        else:p.add_argument('--run',required=True)
    p=sub.add_parser('audit-partition');p.add_argument('--development',required=True)
    p.add_argument('--heldout',required=True);p.add_argument('--out',required=True)
    p=sub.add_parser('inspect-host');p.add_argument('--out',required=True)
    p=sub.add_parser('host-trial');p.add_argument('--home',required=True);p.add_argument('--out',required=True)
    p.add_argument('--render-browser',action='store_true');p.add_argument('--model-config')
    p.add_argument('--allow-model-calls',action='store_true')
    p=sub.add_parser('demo');p.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    try:
        # Refuse an existing result destination before any model/browser work.
        if os.path.lexists(args.out):raise ValueError('new_output_required')
        if args.command=='plan':
            before=inventory()
            result=make_plan(read_json(args.dataset),read_json(args.systems),experiment_id=args.experiment_id,
                source_sha256=before['sha256'],trials=args.trials,seed=args.seed,
                max_model_calls=args.max_model_calls,max_wall_seconds=args.max_wall_seconds)
            if inventory()!=before:raise ValueError('source_changed')
            _write(args.out,result)
            print(json.dumps({'status':'PLANNED','plan_sha256':plan_digest(result),'model_calls':0,'execution_authorized':False}))
            return 0
        if args.command=='run':
            before=inventory()
            result=run_experiment(read_json(args.dataset),read_json(args.plan),
                expected_plan_sha256=args.expected_plan_sha256,source_sha256=before['sha256'],
                allow_model_calls=args.allow_model_calls)
            if inventory()!=before:
                _write(args.out,{'schema':'keel.bench.invalid-run.v1','status':'INVALID','reason':'source_changed',
                    'quarantined_run':result,'execution_authorized':False})
                return 4
        elif args.command=='compare':
            result=analyze_run(read_json(args.dataset),read_json(args.plan),read_json(args.run),
                               expected_plan_sha256=args.expected_plan_sha256)
        elif args.command=='audit-partition':
            result=audit_partition(read_json(args.development),read_json(args.heldout))
        elif args.command=='inspect-host':result=inspect_host()
        elif args.command=='host-trial':
            config=_config(read_json(args.model_config)) if args.model_config else None
            if args.allow_model_calls and config is None:raise ValueError('model_config_required')
            before=inventory()
            result=run_host_trial(args.home,render_browser=args.render_browser,reviewer_config=config,
                                   allow_model_calls=args.allow_model_calls)
            if inventory()!=before:
                _write(args.out,{'schema':'keel.bench.invalid-host-trial.v1','status':'INVALID','reason':'source_changed',
                                'quarantined_trial':result,'execution_authorized':False})
                return 4
            result['release_source_sha256']=before['sha256']
        else:
            before=inventory()
            result=run_demo(read_json(ROOT/'fixtures/grounding_eval/dataset.json'),source_sha256=before['sha256'])
            if inventory()!=before:raise ValueError('source_changed')
        _write(args.out,result)
        print(json.dumps({'status':result.get('status','RECORDED'),'output':str(Path(args.out).absolute()),'execution_authorized':False}))
        if args.command=='run' and any(r['observed_verdict']=='ERROR' for r in result['records']):return 3
        if args.command=='host-trial' and result['status']!='PASS':return 3
        if args.command=='audit-partition' and result['status']=='EXACT_OVERLAP_DETECTED':return 3
        return 0
    except (ValueError,TypeError,KeyError,OSError,RecursionError):
        print(json.dumps({'status':'BLOCKED','reason':'invalid_input_configuration_or_output','execution_authorized':False}),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
