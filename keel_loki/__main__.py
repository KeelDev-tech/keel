"""Local capability tools. Every result is an observation, never submission authority."""
import argparse
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

from .common import atomic_json,load_json,require_dict,LokiError

ROOT=Path(__file__).resolve().parents[1]


def _outside(path):
    path=Path(path).absolute()
    resolved=path.resolve(strict=False)
    if resolved==ROOT or ROOT in resolved.parents:
        raise LokiError('runtime state and outputs must be outside release source')
    if path.parent.resolve(strict=True)!=path.parent:
        raise LokiError('canonical existing parent required')
    return path


def inspect_host():
    from keel_bench.host import inspect_host as inspect
    result=inspect()
    jar=os.environ.get('TLA2TOOLS_JAR')
    result={'schema':'keel.loki.host-inspection.v1','base':result,
        'base_scope':'0.11 Node/JavaScript browser worker inspection',
        'python_playwright':{'status':'DISCOVERABLE' if importlib.util.find_spec('playwright') else 'NOT_FOUND',
                             'runnability_verified':False,'chromium_launch_verified':False},
        'java_executable_found':shutil.which('java') is not None,
        'tlc_jar_configured':bool(jar),'tlc_jar_file_found':bool(jar and Path(jar).is_file()),
        'external_tlc_model_check':'NOT_RUN','real_local_model_inference':'NOT_RUN',
        'rendered_browser':'NOT_RUN','os_containment_verified':False,
        'network_calls':0,'execution_authorized':False}
    return result


def _request(args):
    value=load_json(args.input)
    require_dict(value,{'operation','arguments'})
    if type(value['operation']) is not str or type(value['arguments']) is not dict:
        raise LokiError('operation and argument object required')
    return value['operation'],value['arguments']


def _api(module,allowed,args):
    operation,kwargs=_request(args)
    if operation not in allowed:
        raise LokiError('unsupported operation')
    return getattr(importlib.import_module('keel_loki.'+module),operation)(**kwargs)


def dispatch(args,source_sha256):
    command=args.command
    if command=='inspect-host':return inspect_host()
    if command=='demo':
        from .demo import run_demo
        return run_demo(args.home)
    if command=='browser-trial':
        from .browser import run_fixture
        return run_fixture(args.home,render=args.render_browser)
    if command=='forms-plan':
        from .forms import build_plan
        return build_plan(**load_json(args.input))
    if command=='forms-readback':
        from .forms import validate_readback
        return validate_readback(**load_json(args.input))
    if command=='forms-inventory':
        from .forms import inventory_blockers
        return inventory_blockers(**load_json(args.input))
    if command=='questions':
        return _api('questions',{'plan_questions','check_fact_reuse','evaluate_outcomes','evaluate_policy'},args)
    if command=='retrieval':return _api('retrieval',{'search'},args)
    if command=='calibration':
        operation,kwargs=_request(args)
        if operation not in {'make_plan','fit','evaluate','invalidate'}:
            raise LokiError('unsupported calibration operation')
        if kwargs.get('source_sha256',source_sha256)!=source_sha256:
            raise LokiError('calibration source pin differs from running source')
        kwargs['source_sha256']=source_sha256
        return getattr(importlib.import_module('keel_loki.calibration'),operation)(**kwargs)
    if command=='route':
        from . import routing
        operation,kwargs=_request(args)
        if operation not in {'make_policy','run_route'} or any(k in kwargs for k in ('transport','allow_model_calls','state_path')):
            raise LokiError('unsupported routing request')
        if kwargs.get('source_sha256',source_sha256)!=source_sha256:
            raise LokiError('route source pin differs from running source')
        kwargs['source_sha256']=source_sha256
        if operation=='make_policy':return routing.make_policy(**kwargs)
        if args.state is None:raise LokiError('state path required for route execution')
        return routing.run_route(**kwargs,state_path=args.state,allow_model_calls=args.allow_model_calls)
    if command=='lab':
        from . import lab
        operation,kwargs=_request(args)
        if operation not in {'mutation_dataset','run_lab','optimize','validate_report'} or any(k in kwargs for k in ('transport','allow_model_calls')):
            raise LokiError('unsupported laboratory request')
        if operation in {'run_lab','optimize'}:kwargs['allow_model_calls']=args.allow_model_calls
        return getattr(lab,operation)(**kwargs)
    if command=='modelcheck':
        from .modelcheck import check_model
        return check_model(max_states=args.max_states)
    if command=='trace-check':return _api('modelcheck',{'replay_trace'},args)
    if command=='research':return _api('research',{'propose','record_result'},args)
    operation,kwargs=_request(args)
    if command=='memory':
        from .temporal import TemporalMemory
        allowed={'record_claim','record_observation','register_artifact','register_approval','resolve','invalidation_projection','verify'}
        if operation not in allowed:raise LokiError('unsupported memory operation')
        with TemporalMemory(args.home) as memory:return getattr(memory,operation)(**kwargs)
    if command=='skills':
        from .skills import SkillWorkshop
        allowed={'quarantine','freeze_fixtures','replay','promote','active','rollback'}
        if operation not in allowed:raise LokiError('unsupported skill operation')
        return getattr(SkillWorkshop(args.home),operation)(**kwargs)
    if command=='recovery':
        from .recovery import RecoveryJournal,demo
        if operation=='demo':
            if kwargs:raise LokiError('recovery demo takes no request arguments')
            return demo(args.home)
        if operation not in {'snapshot','reconciliation_proposal'}:
            raise LokiError('state transitions require the long-lived trusted controller API')
        journal=RecoveryJournal.open_readonly(args.home,workspace_id=args.workspace_id)
        return getattr(journal,operation)(**kwargs)
    raise LokiError('unsupported command')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    inputs={'forms-plan','forms-readback','forms-inventory','questions','retrieval','calibration','route','lab','trace-check','research','memory','skills','recovery'}
    homes={'demo','browser-trial','memory','skills','recovery'}
    for name in sorted(inputs|homes|{'inspect-host','modelcheck'}):
        p=sub.add_parser(name);p.add_argument('--out',required=True)
        if name in inputs:p.add_argument('--input',required=True)
        if name in homes:p.add_argument('--home',required=True)
        if name in {'lab','route'}:p.add_argument('--allow-model-calls',action='store_true')
        if name=='route':p.add_argument('--state')
        if name=='browser-trial':p.add_argument('--render-browser',action='store_true')
        if name=='modelcheck':p.add_argument('--max-states',type=int,default=50000)
        if name=='recovery':p.add_argument('--workspace-id',required=True)
    args=parser.parse_args(argv)
    try:
        out=_outside(args.out)
        if os.path.lexists(out):raise LokiError('new output required')
        for key in ('home','state'):
            value=getattr(args,key,None)
            if value:_outside(value)
        from tools.loki_inventory import inventory
        before=inventory()
        result=dispatch(args,before['sha256'])
        after=inventory()
        same=before==after
        envelope={'schema':'keel.loki.command.v1' if same else 'keel.loki.invalid-command.v1',
            'command':args.command,'status':'RECORDED' if same else 'INVALID_SOURCE_CHANGED',
            'release_source_sha256':before['sha256'],'source_unchanged':same,'result':result,
            'execution_authorized':False,'production_deployed':False}
        atomic_json(out,envelope)
        print(json.dumps({'status':envelope['status'],'output':str(out),'execution_authorized':False}))
        if not same:return 4
        status=result.get('status') if type(result) is dict else None
        if status in {'FAIL','BLOCKED','PARTIAL','UNAVAILABLE','MISMATCH','INCOMPLETE',
                      'NONCONFORMING','COUNTEREXAMPLE','ERROR','INVALIDATED','INSUFFICIENT_EVIDENCE'}:return 3
        if args.command=='browser-trial' and not result.get('whole_fixture_prepared',False):return 3
        return 0
    except (ValueError,TypeError,KeyError,OSError,RecursionError):
        print(json.dumps({'status':'BLOCKED','reason':'invalid_input_configuration_or_output','execution_authorized':False}),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
