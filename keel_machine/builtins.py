"""Reviewed deterministic lexical operations and an explicitly synthetic demo."""
import inspect
import os
from pathlib import Path
import time

from keel_loki import retrieval
from keel_loki import common as loki_common
from .cache import ComputationCache
from .common import MachineError, clone, digest, require
from .contracts import check_contract
from .drift import FailureMonitor
from .graph import GraphRunner, GuardDecision, Operation


def object_contract(properties):
    return {'type':'object','properties':properties,'required':sorted(properties),'additionalProperties':False}


def _tokenize(payload):
    return retrieval._tokens(payload['arguments']['text'])


def _summarize(payload):
    left,right=payload['dependencies']['left'],payload['dependencies']['right']
    return {'left_words':len(left),'right_words':len(right),
            'shared_terms':sorted(set(left)&set(right))}


def registered_operations():
    """No model, network, filesystem-write or effect operations are registered."""
    empty=object_contract({})
    tokens={'type':'array','items':{'type':'string','maxLength':65536},'maxItems':10000}
    count={'type':'integer','minimum':0,'maximum':10000}
    return {
        'tokenize_text':Operation('tokenize_text',_tokenize,
            object_contract({'arguments':object_contract({'text':{'type':'string','minLength':1,'maxLength':32768}}),
                             'config':empty,'dependencies':empty}),tokens,
            (inspect.getsourcefile(retrieval),inspect.getsourcefile(loki_common))),
        'summarize_terms':Operation('summarize_terms',_summarize,
            object_contract({'arguments':empty,'config':empty,
                             'dependencies':object_contract({'left':tokens,'right':tokens})}),
            object_contract({'left_words':count,'right_words':count,'shared_terms':tokens}))}


def demo_plan(run_id='demo',left_text='Synthetic spreadsheet reconciliation experience.'):
    return {'schema':'keel.machine.graph.v1','run_id':run_id,'account_id':'synthetic-account',
        'scope':'synthetic-analysis','purpose':'test','max_compute_nodes':3,'max_seconds':30,
        'nodes':[
            {'id':'left','operation':'tokenize_text','arguments':{'text':left_text},'config':{},'needs':[],
             'revisions':{'document':digest(left_text)}},
            {'id':'right','operation':'tokenize_text','arguments':{'text':'Synthetic reconciliation documentation.'},
             'config':{},'needs':[],'revisions':{'document':digest('Synthetic reconciliation documentation.')}},
            {'id':'summary','operation':'summarize_terms','arguments':{},'config':{},'needs':['left','right'],
             'revisions':{}}], 'outputs':['summary']}


def demo(home):
    home=Path(os.path.abspath(home))
    require(home.parent.resolve(strict=True)==home.parent,'demo_parent_symlink')
    home.mkdir(mode=0o700,exist_ok=False)
    allowed=[True]
    def synthetic_guard(request,now):
        return GuardDecision(allowed[0] and request['account_id']=='synthetic-account'
                             and request['purpose']=='test',digest(request),now+30,'synthetic_host')
    cache=ComputationCache(home/'computation.sqlite3')
    runner=GraphRunner(cache,registered_operations(),synthetic_guard)
    first=runner.run(demo_plan())
    repeated=runner.run(demo_plan('repeat'))
    changed=runner.run(demo_plan('changed','Synthetic spreadsheet reconciliation experience plus analysis.'))
    allowed[0]=False
    denied=runner.run(demo_plan('revoked'))
    allowed[0]=True
    broken=demo_plan('contract-failure');broken['nodes'][0]['arguments']['text']=123
    contract_failure=runner.run(broken)
    # Controlled local fault injection, not real independent production data.
    baseline={'schema':'keel.machine.failure-baseline.v1','baseline_id':'synthetic-contract-baseline',
        'bindings':{name:digest(name) for name in ('source_sha256','config_sha256','model_sha256','calibration_sha256')},
        'sample_count':10000,'failures':0,'tolerance':0.05,'delta':0.05,
        'min_samples':30,'max_samples':1000,'evidence_sha256':digest('synthetic-baseline')}
    monitor=FailureMonitor(home/'failure-monitor.sqlite3',baseline=baseline)
    contract=object_contract({'value':{'type':'integer','minimum':0,'maximum':1000}})
    for sequence in range(64):
        value={'value':'synthetic-wrong-type-'+str(sequence)}
        observation=check_contract(contract,value)
        monitor.record('synthetic-'+str(sequence),observation['status']=='HOLD',
                       current_bindings=baseline['bindings'],provenance_sha256=digest(value))
    drift=monitor.report(current_bindings=baseline['bindings'])
    checks={
        'initial_graph_computed':first['status']=='COMPLETED' and first['computed_nodes']==3,
        'unchanged_graph_reused':repeated['status']=='COMPLETED' and repeated['computed_nodes']==0 and repeated['cache_hits']==3,
        'only_changed_branch_recomputed':changed['status']=='COMPLETED' and changed['computed_nodes']==2 and changed['cache_hits']==1,
        'live_hold_blocks_cache_hit':denied['status']=='HELD' and not denied['outputs'] and denied['computed_nodes']==0,
        'changed_contract_held':contract_failure['status']=='HELD' and not contract_failure['outputs'],
        'injected_failure_increase_detected':drift['status']=='HOLD'}
    return {'schema':'keel.machine.demo.v1','status':'DEMO_PASSED' if all(checks.values()) else 'DEMO_FAILED',
        'synthetic':True,'checks':checks,'runs':{'initial':first,'repeat':repeated,'changed':changed,
                                              'revoked':denied,'contract_failure':contract_failure},
        'drift_report':drift,'cache':cache.stats(),'model_calls':0,'network_calls':0,
        'external_actions':0,'execution_authorized':False,'production_deployed':False,
        'paid_services_required':False}
