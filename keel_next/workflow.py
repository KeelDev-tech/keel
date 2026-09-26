"""Host capability inventory and an offline end-to-end synthetic workflow."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path


def doctor(home):
    from tools.release_profile import capability_report
    from security.execution.resource_limits import doctor as resource_doctor
    return {'schema':'keel.next.capabilities.v1', 'local':capability_report(home),
            'worker_enforcement':resource_doctor(),
            'python_playwright_installed':importlib.util.find_spec('playwright') is not None,
            'rendered_browser_qualified':False,'provider_authentication_connected':False,
            'operator_authentication_connected':False,'paid_services_required':False,
            'execution_authorized':False}


def demo(home):
    """Create only NEW private synthetic state; never use a real Keel workspace."""
    from engines.dedupe_gate import check_candidate
    from engines.source_feedback import propose
    from keel_eval.reliability import synthetic_demo
    from keel_learning import bounded_confidence_sequence
    from keel_memory import EvidenceIndex, document_digest, text_digest
    from keel_observability import ObservationStore, Verification, event_digest
    from keel_observability.adapters import source_feedback_snapshot

    home=Path(os.path.abspath(home))
    if home.parent.resolve(strict=True)!=home.parent:
        raise ValueError('demo_parent_must_not_be_symlink')
    home.mkdir(mode=0o700,exist_ok=False)
    now=datetime(2026,9,24,12,tzinfo=timezone.utc)
    epoch=int(now.timestamp())
    def iso(offset): return (now+timedelta(seconds=offset)).isoformat()
    def verifier(raw, context, observed_now):
        # Only this synthetic demonstration can possess this exact in-memory
        # marker. This callback does not establish any real person's identity.
        if context is not marker:
            return None
        event=json.loads(raw)
        return Verification(account_id=event['account_id'],producer_id=event['producer_id'],
            subject='synthetic-test-host',event_sha256=event_digest(event),allowed_kinds=(event['kind'],),
            verified_at=observed_now,expires_at=observed_now+3600,
            assurance='human_reported' if event['kind']=='effort' else 'host_measured')
    marker=object()
    store=ObservationStore.create(home/'observations.sqlite3',store_id='synthetic-next-demo',
                                   verifier=verifier,clock=lambda:epoch)
    source_lineage={'source_id':'synthetic-source','opportunity_id':None,'application_id':None,'attempt_id':None}
    role_lineage={**source_lineage,'opportunity_id':'synthetic-role'}
    def event(identity,kind,offset,lineage,payload,revisions=None):
        value={'schema':'keel.observation.v1','event_id':identity,'account_id':'synthetic-account',
            'producer_id':'synthetic-producer','kind':kind,'occurred_at':iso(offset),
            'lineage':lineage,'revisions':revisions or {},'payload':payload}
        store.ingest(value,authenticate=True,context=marker)
        return value
    event('source','source',-1000,source_lineage,{'name':'Synthetic source','evidence_ref':'synthetic:source'})
    document={'schema':'keel.memory.document.v1','document_id':'synthetic-document',
        'observation_id':'opportunity','account_id':'synthetic-account','scope':'synthetic-role',
        'source_id':'synthetic-source','claim_key':'role-description',
        'text':'Synthetic role needs spreadsheet reconciliation.','permitted_uses':['planning','review'],
        'valid_from':epoch-700,'valid_until':None,'supersedes':[]}
    opportunity=event('opportunity','opportunity',-600,role_lineage,
        {'qualified':True,'evidence_ref':'synthetic:role'},
        {'evidence':text_digest(document['text']),'memory_document':document_digest(document)})
    event('effort','effort',-550,role_lineage,{'human_minutes':5,'measurement':'human_reported',
        'task_ids':['synthetic-task'],'evidence_ref':'synthetic:effort'})
    event('window','window',-50,source_lineage,{'window_start':iso(-800),'window_end':iso(-100),
        'observed_through':iso(-100),'complete':True,'evidence_ref':'synthetic:window'})
    index=EvidenceIndex(home/'evidence-index',store,clock=lambda:epoch)
    index.ingest(document)
    retrieved=index.search('spreadsheet',account_id='synthetic-account',scope='synthetic-role',purpose='review')
    packet=index.register_artifact({'artifact_id':'synthetic-packet','account_id':'synthetic-account',
        'scope':'synthetic-role','purpose':'review','sha256':'b'*64,'documents':['synthetic-document']})
    policies=[{'source_id':'synthetic-source','permitted':True,'cap_minutes':10,'cooldown_until':None,
               'cooldown_verified':True,'evidence_ref':'synthetic:policy'}]
    def feedback():
        exported=source_feedback_snapshot(store,'synthetic-account',source_policies=policies,
            window_start=iso(-800),window_end=iso(-100),observed_at=iso(0),now=now)
        return propose(exported['document'],budget_minutes=10,now=now)
    before=feedback()
    event('revocation','revocation',0,dict.fromkeys(source_lineage),
          {'target_event_id':'opportunity','target_sha256':event_digest(opportunity),'reason':'Synthetic correction'})
    after=index.search('spreadsheet',account_id='synthetic-account',scope='synthetic-role',purpose='review')
    stale=index.artifact_status('synthetic-account','synthetic-packet')
    held=feedback()
    identity_result=check_candidate('Source','Sales Manager','https://source.example/jobs/a',
        ledger_rows=[{'company':'Sourcegraph','title':'Sales Manager','status':'SUBMITTED',
                      'posting_url':'https://sourcegraph.example/jobs/b','role_id':'synthetic-distinct'}],queue_entries=[])
    reliability=synthetic_demo()['report']
    checks={'authenticated_synthetic_evidence_retrieved':len(retrieved['matches'])==1,
            'initial_dependencies_current':packet['status']=='DEPENDENCIES_CURRENT',
            'measured_source_proposal_created':before['allocated_minutes']==10,
            'revoked_evidence_excluded':not after['matches'],
            'dependent_packet_requires_review':stale['status']=='STALE' and stale['reapproval_required'],
            'revoked_source_measurements_hold_budget':held['allocated_minutes']==0,
            'distinct_employers_not_hard_merged':identity_result[0]!='duplicate',
            'synthetic_evaluation_not_release_authority':reliability['status']!='QUALIFIED' and not reliability['execution_authorized']}
    return {'schema':'keel.next.demo.v1','status':'DEMO_PASSED' if all(checks.values()) else 'DEMO_FAILED',
            'synthetic':True,'checks':checks,'statistical_arithmetic':bounded_confidence_sequence(
                [0,1,1,0,1],lower=0,upper=1,delta=.05)[-1],
            'live_identity_or_provider_proof':False,'rendered_browser_exercised':False,
            'kernel_resource_limits_exercised':False,'model_calls':0,'network_calls':0,
            'execution_authorized':False,'schedule_writes':0,'production_deployed':False}
