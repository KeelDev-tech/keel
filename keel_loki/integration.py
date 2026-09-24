"""Bind current temporal observations to complete preparation plans.

This adapter consumes host evidence and approvals; it cannot authenticate their
issuers, enumerate omitted applicant claims, or authorize a submission.
"""
import hashlib
from pathlib import Path
from .common import clone, digest, require_dict, require_id, require_hash, LokiError, atomic_json
from .forms import build_plan, demo_fixture, digest as form_digest
from .actions import ActionGateway
from .temporal import TemporalMemory


def compile_grounded_plan(memory, fixture, bindings, *, expected_memory_head, now):
    """Validate explicitly enumerated fact-field bindings before the gateway.

    Bindings cover only the named factual fields; caller must enumerate all
    material facts. Attestations cannot be supplied by reusable factual memory.
    """
    fixture=clone(fixture);bindings=clone(bindings)
    require_hash(expected_memory_head)
    integrity=memory.verify(expected_head_sha256=expected_memory_head)
    if type(bindings) is not dict or not bindings:
        raise LokiError('nonempty factual field bindings required')
    rows={f['id']:f for f in fixture['contract']['fields']}
    history=memory.history()
    claims={e['identity']:e['payload'] for e in history if e['kind']=='claim'}
    resolved={}
    for fid,binding in bindings.items():
        require_id(fid)
        require_dict(binding,{'subject_id','key','scope','revision_id'})
        if fid not in rows or rows[fid]['kind'] in ('attestation','checkbox','attachment'):
            raise LokiError('only explicit textual or choice fact bindings supported')
        if binding['scope'] != fixture['contract']['form_id']:
            raise LokiError('memory/form scope mismatch')
        result=memory.resolve(binding['subject_id'],binding['key'],binding['scope'],'application_fact',now)
        if result['status']!='VERIFIED_OBSERVATION' or result['revision_ids'] != [binding['revision_id']]:
            raise LokiError('fact absent, conflicted, unverified, expired or superseded')
        claim=claims.get(binding['revision_id'])
        if claim is None or fid not in fixture['values']:
            raise LokiError('missing fact value')
        value=fixture['values'][fid]
        if (digest(value['value'])!=digest(result['value'])
                or claim['source']['sha256'] not in value['evidence_sha256']):
            raise LokiError('prepared field does not bind current fact/source')
        resolved[fid]={'revision_id':binding['revision_id'],'value_sha256':digest(result['value']),
                       'source_sha256':claim['source']['sha256']}
    gateway=ActionGateway(fixture['contract'],fixture['host_snapshot'],fixture['approvals'])
    grant=gateway.issue(fixture['values'],now=now)
    memory.verify(expected_head_sha256=expected_memory_head)
    return {'schema':'keel.loki.integration.v1','status':'PREPARATION_PLAN_BOUND',
        'memory_head_sha256':expected_memory_head,'bound_fact_fields':resolved,
        'plan':grant['plan'],'plan_sha256':form_digest(grant['plan']),
        'all_material_claims_enumerated':False,'source_truth_authenticated':False,
        'human_approval_authenticated':False,'browser_actions':0,'execution_authorized':False,
        'submission_authorized':False}


def demo(home):
    """Actual local bytes -> temporal fact -> approved fixture field -> full plan.

    Then append a correction and verify the same packet cannot be reused.
    Every actor, source and approval here is explicitly synthetic.
    """
    home=Path(home).absolute()
    home.mkdir(mode=0o700,exist_ok=False)
    fixture=demo_fixture();now=fixture['now'];clock=[now]
    value=fixture['values']['motivation']['value']
    source=home/'synthetic-source.json'
    atomic_json(source,{'synthetic':True,'motivation':value})
    source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    fixture['values']['motivation']['evidence_sha256']=[source_hash]
    for a in fixture['approvals']:
        if a['field_id']=='motivation':a['evidence_sha256']=[source_hash]
    claim={'revision_id':'motivation-v1','subject_id':'synthetic-candidate','key':'motivation',
        'value':value,'source':{'source_id':'fixture-source','sha256':source_hash,'classification':'personal',
        'allowed_scopes':[fixture['contract']['form_id']],'permitted_uses':['application_fact']},
        'scope':fixture['contract']['form_id'],'permitted_uses':['application_fact'],
        'valid_from':now,'valid_until':None,'supersedes':[]}
    with TemporalMemory(home/'memory',clock=lambda:clock[0]) as memory:
        memory.record_claim(claim,'claim-v1')
        memory.record_observation({'observation_id':'observation-v1','revision_id':'motivation-v1',
            'reviewer_id':'synthetic-reviewer','verdict':'verified','source_sha256':source_hash,'observed_at':now},'review-v1')
        head=memory.verify()['head_sha256']
        bindings={'motivation':{'subject_id':'synthetic-candidate','key':'motivation','scope':fixture['contract']['form_id'],
                                'revision_id':'motivation-v1'}}
        prepared=compile_grounded_plan(memory,fixture,bindings,expected_memory_head=head,now=now)
        clock[0]+=1
        corrected=clone(claim);corrected.update(revision_id='motivation-v2',value='Corrected synthetic statement',supersedes=['motivation-v1'])
        corrected['source']['sha256']=digest('new-synthetic-source')
        memory.record_claim(corrected,'claim-v2')
        rejected=False
        try:compile_grounded_plan(memory,fixture,bindings,expected_memory_head=memory.verify()['head_sha256'],now=clock[0])
        except ValueError:rejected=True
    return {'schema':'keel.loki.integration-demo.v1','synthetic':True,'source_bytes_sha256':source_hash,
        'initial':prepared,'stale_fact_rejected':rejected,'browser_actions':0,'real_model_calls':0,
        'canonical_writes':0,'execution_authorized':False}
