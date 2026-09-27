"""Incremental graphs of explicitly registered pure local computations.

The host owns function registration, source/dependency pins and live admission.
This module cannot prove that arbitrary Python is pure or terminate a stuck
function. Execute untrusted code only in the existing isolated worker boundary.
No task JSON can import code, choose a shell command, or obtain execution rights.
"""
from dataclasses import dataclass, field
import hashlib
import inspect
import marshal
import math
from pathlib import Path
import secrets
import time
import types

from .common import MachineError, canonical, clone, digest, ident, require, sha
from .contracts import check_contract, validate_contract

FLAGS={'execution_authorized':False,'external_actions':0,'paid_services_required':False}
SAFE_REASONS=frozenset({'operation_changed','graph_clock_invalid','graph_clock_regressed',
    'graph_guard_decision_invalid','graph_guard_binding_invalid','graph_guard_expired',
    'graph_host_hold','graph_deadline_exceeded','graph_input_contract_failed',
    'graph_cache_held','graph_cache_result_invalid','graph_compute_budget_exhausted',
    'graph_output_contract_failed','graph_cache_write_held'})


def _time(value):
    require(type(value) in (int,float) and math.isfinite(value) and 0<=value<=10**12,
            'graph_clock_invalid')
    return float(value)


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    request_sha256: str
    expires_at: float
    reason: str = 'host_policy'


@dataclass(frozen=True)
class Operation:
    name: str
    function: object
    input_contract: dict
    output_contract: dict
    source_files: tuple = ()
    _pin: str = field(init=False,repr=False)
    _files: tuple = field(init=False,repr=False)

    def __post_init__(self):
        ident(self.name)
        require(type(self.function) is types.FunctionType and not self.function.__closure__,
                'plain_host_function_without_closure_required')
        require(type(self.source_files) is tuple and len(self.source_files)<=32,'source_files_invalid')
        primary=inspect.getsourcefile(self.function)
        require(primary is not None,'operation_source_required')
        files=tuple(sorted(set(str(Path(p).resolve(strict=True)) for p in
                              (primary,__file__,str(Path(__file__).with_name('common.py')),
                               str(Path(__file__).with_name('contracts.py')),*self.source_files))))
        object.__setattr__(self,'_files',files)
        object.__setattr__(self,'input_contract',validate_contract(self.input_contract))
        object.__setattr__(self,'output_contract',validate_contract(self.output_contract))
        object.__setattr__(self,'_pin',self.fingerprint())

    def fingerprint(self):
        sources={}
        for name in self._files:
            path=Path(name)
            require(path.is_file() and path.stat().st_size<=8*1024*1024,'operation_source_unavailable')
            sources[name]=hashlib.sha256(path.read_bytes()).hexdigest()
        return digest({'name':self.name,'sources':sources,
                       'code':hashlib.sha256(marshal.dumps(self.function.__code__)).hexdigest(),
                       'defaults':list(self.function.__defaults__ or ()),
                       'keyword_defaults':self.function.__kwdefaults__ or {},
                       'input_contract':self.input_contract,'output_contract':self.output_contract})

    @property
    def pin(self):
        return self._pin

    def check(self):
        require(self.fingerprint()==self._pin,'operation_changed')


def validate_plan(plan):
    plan=clone(plan)
    require(type(plan) is dict and set(plan)=={'schema','run_id','account_id','scope','purpose',
            'nodes','outputs','max_compute_nodes','max_seconds'},'graph_plan_schema_invalid')
    require(plan['schema']=='keel.machine.graph.v1','graph_plan_version_invalid')
    for key in ('run_id','account_id','scope'): ident(plan[key])
    require(plan['purpose'] in ('analysis','preparation','test'),'graph_purpose_invalid')
    require(type(plan['max_compute_nodes']) is int and 1<=plan['max_compute_nodes']<=64,
            'graph_compute_budget_invalid')
    require(type(plan['max_seconds']) in (int,float) and math.isfinite(plan['max_seconds'])
            and 0<plan['max_seconds']<=300,'graph_deadline_invalid')
    require(type(plan['nodes']) is list and 1<=len(plan['nodes'])<=64,'graph_nodes_invalid')
    nodes={}
    for node in plan['nodes']:
        require(type(node) is dict and set(node)=={'id','operation','arguments','config','needs','revisions'},
                'graph_node_schema_invalid')
        ident(node['id']); ident(node['operation'])
        require(len(node['id'])<=110,'graph_node_id_too_long')
        require(node['id'] not in nodes,'graph_duplicate_node')
        require(type(node['arguments']) is dict and type(node['config']) is dict,'graph_arguments_invalid')
        require(type(node['needs']) is list and len(node['needs'])<=16
                and all(type(n) is str for n in node['needs'])
                and len(set(node['needs']))==len(node['needs']),'graph_dependencies_invalid')
        for dependency in node['needs']: ident(dependency)
        require(type(node['revisions']) is dict and len(node['revisions'])<=32,'graph_revisions_invalid')
        for name,revision in node['revisions'].items():
            ident(name);sha(revision);require(len(name)<=110,'graph_revision_id_too_long')
        nodes[node['id']]=node
    require(all(set(n['needs'])<=set(nodes) for n in nodes.values()),'graph_missing_dependency')
    require(type(plan['outputs']) is list and 1<=len(plan['outputs'])<=16
            and all(type(n) is str and n in nodes for n in plan['outputs'])
            and len(set(plan['outputs']))==len(plan['outputs']),'graph_outputs_invalid')
    order=[]; pending=set(nodes)
    while pending:
        ready=sorted(n for n in pending if set(nodes[n]['needs'])<=set(order))
        require(ready,'graph_cycle')
        order.extend(ready); pending.difference_update(ready)
    return plan,nodes,order


class GraphRunner:
    def __init__(self,cache,operations,guard,*,clock=time.time,monotonic=time.monotonic):
        require(type(operations) is dict and 1<=len(operations)<=32,'graph_registry_invalid')
        require(all(type(op) is Operation and name==op.name for name,op in operations.items()),
                'graph_registry_invalid')
        require(callable(guard) and callable(clock) and callable(monotonic),'graph_host_callbacks_required')
        self.cache,self.operations,self.guard=cache,dict(operations),guard
        self.clock,self.monotonic=clock,monotonic
        self._last_now=0.0

    def _now(self):
        now=_time(self.clock())
        require(now>=self._last_now,'graph_clock_regressed')
        self._last_now=now
        return now

    def _admit(self,plan,node,key,phase):
        now=self._now()
        request={'schema':'keel.machine.admission.v1','account_id':plan['account_id'],
                 'scope':plan['scope'],'purpose':plan['purpose'],'run_id':plan['run_id'],
                 'plan_sha256':digest(plan),'node_id':node['id'],'operation':node['operation'],
                 'revisions':clone(node['revisions']),'cache_key_sha256':digest(key),
                 'phase':phase,'now':now,'nonce':secrets.token_hex(16)}
        self._decision(request,now)

    def _decision(self,request,now):
        decision=self.guard(clone(request),now)
        require(type(decision) is GuardDecision and type(decision.allowed) is bool,
                'graph_guard_decision_invalid')
        require(decision.request_sha256==digest(request),'graph_guard_binding_invalid')
        expiry=_time(decision.expires_at)
        require(now<expiry<=now+60 and self._now()<expiry,'graph_guard_expired')
        ident(decision.reason)
        require(decision.allowed,'graph_host_hold')

    def _admit_graph(self,plan,nodes,keys,results):
        now=self._now()
        request={'schema':'keel.machine.graph-admission.v1','account_id':plan['account_id'],
            'scope':plan['scope'],'purpose':plan['purpose'],'run_id':plan['run_id'],
            'plan_sha256':digest(plan),'phase':'graph-return','now':now,'nonce':secrets.token_hex(16),
            'nodes':{name:{'operation':nodes[name]['operation'],'revisions':clone(nodes[name]['revisions']),
                           'cache_key_sha256':digest(keys[name]),'output_sha256':results[name]['output_sha256']}
                     for name in sorted(nodes)}}
        # The host must inspect these bindings in ONE coherent current snapshot.
        # Per-node checks alone cannot establish this graph-wide assertion.
        self._decision(request,now)

    def run(self,plan):
        plan,nodes,order=validate_plan(plan)
        require(all(n['operation'] in self.operations for n in nodes.values()),'graph_operation_unregistered')
        start=self.monotonic(); require(type(start) in (int,float) and math.isfinite(start),'monotonic_invalid')
        deadline=start+plan['max_seconds']
        results={}; values={}; keys={}; computed=0; hits=0; cache_writes=0
        def check_deadline():
            current=self.monotonic()
            require(type(current) in (int,float) and math.isfinite(current)
                    and start<=current<deadline,'graph_deadline_exceeded')
        for node_id in order:
            node=nodes[node_id]; op=self.operations[node['operation']]
            if any(results[n]['status'] not in ('COMPUTED','CACHED') for n in node['needs']):
                results[node_id]={'status':'BLOCKED','reason':'upstream_blocked','operation':op.name}
                continue
            try:
                check_deadline(); op.check()
                payload={'arguments':clone(node['arguments']),'config':clone(node['config']),
                         'dependencies':{n:clone(values[n]) for n in node['needs']}}
                checked=check_contract(op.input_contract,payload)
                require(checked['status']=='PASS','graph_input_contract_failed')
                key={'schema':'keel.machine.cache-key.v1','account_id':plan['account_id'],
                     'scope':plan['scope'],'purpose':plan['purpose'],'operation':op.name,
                     'implementation_sha256':op.pin,'config_sha256':digest(node['config']),
                     'inputs_sha256':digest(payload),
                     'dependencies':{n:digest([keys[n],results[n]['output_sha256']]) for n in node['needs']},
                     'contract_sha256':digest([op.input_contract,op.output_contract])}
                # Named source revisions are bound independently from input text.
                # Prefixing prevents collision with graph dependency node IDs.
                key['dependencies']={**{'node:'+n:d for n,d in key['dependencies'].items()},
                                     **{'revision:'+n:d for n,d in node['revisions'].items()}}
                self._admit(plan,node,key,'before')
                check_deadline()
                cached=self.cache.get(key)
                require(cached['status']!='HELD','graph_cache_held')
                if cached['status']=='HIT':
                    value=clone(cached['value']); status='CACHED'; hits+=1
                else:
                    require(cached['status']=='MISS','graph_cache_result_invalid')
                    require(computed<plan['max_compute_nodes'],'graph_compute_budget_exhausted')
                    computed+=1
                    value=clone(op.function(clone(payload)))
                    status='COMPUTED'
                require(check_contract(op.output_contract,value)['status']=='PASS','graph_output_contract_failed')
                op.check(); check_deadline(); self._admit(plan,node,key,'after')
                if status=='COMPUTED':
                    saved=self.cache.put(key,value)
                    require(saved['status'] in ('STORED','REFRESHED'),'graph_cache_write_held')
                    cache_writes+=1
                values[node_id]=value; keys[node_id]=key
                results[node_id]={'status':status,'operation':op.name,'output_sha256':digest(value),
                                  'cache_key_sha256':digest(key)}
            except Exception as exc:
                # Retain neither arbitrary callback exception prose nor payloads.
                reason=str(exc) if type(exc) is MachineError and str(exc) in SAFE_REASONS else 'operation_or_host_callback_failed'
                results[node_id]={'status':'HELD','operation':op.name,'reason':reason}
        # Recheck every contributing node: later callbacks may revoke evidence.
        for node_id in order:
            if results[node_id]['status'] not in ('COMPUTED','CACHED'):
                continue
            try:
                check_deadline(); self.operations[nodes[node_id]['operation']].check()
                self._admit(plan,nodes[node_id],keys[node_id],'return')
            except Exception:
                results[node_id]={'status':'HELD','operation':nodes[node_id]['operation'],
                                  'reason':'final_admission_failed'}
        complete=all(row['status'] in ('COMPUTED','CACHED') for row in results.values())
        graph_hold=None
        if complete:
            try:
                self._admit_graph(plan,nodes,keys,results); check_deadline()
            except Exception:
                complete=False; graph_hold='final_graph_admission_failed'
        report={'schema':'keel.machine.graph-result.v1','run_id':plan['run_id'],
                'plan_sha256':digest(plan),'status':'COMPLETED' if complete else 'HELD',
                'nodes':results,'outputs':{n:values[n] for n in plan['outputs']} if complete else {},
                'computed_nodes':computed,'cache_hits':hits,'cache_writes':cache_writes,'graph_hold':graph_hold,
                'cooperative_deadline':True,'handler_purity_required':True,**FLAGS}
        try:
            canonical(report)
        except MachineError:
            report.update(status='HELD',outputs={},graph_hold='graph_result_too_large')
        return report
