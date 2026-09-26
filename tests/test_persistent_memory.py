"""Real SQLite observation/index integration on explicitly synthetic evidence."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import sqlite3

import pytest

from keel_memory import EvidenceIndex, IndexError, document_digest, text_digest
from keel_memory.evaluation import evaluate
from keel_loki.common import digest
from keel_observability import ObservationStore, Verification
from keel_observability.store import event_digest


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


@pytest.fixture
def system(tmp_path):
    now = [1000]
    home = tmp_path / 'observations'
    home.mkdir(mode=0o700)
    def verify(raw, context, verified_now):
        event = json.loads(raw)
        if context != 'synthetic-trusted-host':
            return None
        return Verification(account_id=event['account_id'], producer_id=event['producer_id'],
            subject='synthetic-operator', event_sha256=event_digest(event), allowed_kinds=(event['kind'],),
            verified_at=now[0], expires_at=now[0]+1000, assurance='human_reported')
    store = ObservationStore.create(home/'events.sqlite3',store_id='synthetic-store',verifier=verify,clock=lambda:now[0])
    index = EvidenceIndex(tmp_path/'index',store,clock=lambda:now[0])
    return index, store, now


def document(identity='one', **changes):
    value = {'schema':'keel.memory.document.v1','document_id':identity,'observation_id':'event-'+identity,
        'account_id':'account','scope':'role','source_id':'source-'+identity,'claim_key':'experience',
        'text':'Synthetic spreadsheet reconciliation experience.','permitted_uses':['review','application_fact'],
        'valid_from':900,'valid_until':None,'supersedes':[]}
    value.update(changes)
    return value


def publish(store, doc, *, authenticate=True):
    event = {'schema':'keel.observation.v1','event_id':doc['observation_id'],'account_id':doc['account_id'],
        'producer_id':'producer','kind':'source','occurred_at':stamp(999),
        'lineage':{'source_id':doc['source_id'],'opportunity_id':None,'application_id':None,'attempt_id':None},
        'revisions':{'evidence':text_digest(doc['text']),'memory_document':document_digest(doc)},
        'payload':{'name':'synthetic source','evidence_ref':'synthetic:local-observation'}}
    store.ingest(event,authenticate=authenticate,context='synthetic-trusted-host')
    return event


def search(index, **changes):
    args={'account_id':'account','scope':'role','purpose':'review'}
    args.update(changes)
    return index.search('spreadsheet reconciliation',**args)


def artifact():
    return {'artifact_id':'packet','account_id':'account','scope':'role','purpose':'review',
            'sha256':'b'*64,'documents':['one']}


def test_persistent_real_store_index_and_reopen(system):
    index,store,now=system
    doc=document(); publish(store,doc)
    assert index.ingest(doc)['created']
    assert not index.ingest(doc)['created']
    reopened=EvidenceIndex(index.home,store,clock=lambda:now[0])
    result=search(reopened)
    assert [m['document_id'] for m in result['matches']]==['one']
    assert result['matches'][0]['producer_binding_verified']
    assert result['matches'][0]['assurance']=='human_reported'
    assert not result['truth_verified'] and not result['execution_authorized']
    assert result['untrusted_text'] and result['model_calls']==0


@pytest.mark.parametrize('field,value',[
    ('text','invented spreadsheet fact'),('scope','other-role'),
    ('permitted_uses',['planning']),('valid_until',999999),
    ('claim_key','unrelated'),('supersedes',['another-document']),
])
def test_whole_document_binding_prevents_permission_or_text_tampering(system,field,value):
    index,store,_=system
    doc=document(); publish(store,doc)
    doc[field]=value
    with pytest.raises(IndexError,match='document_authentication_failed'):
        index.ingest(doc)
    assert search(index)['matches']==[]


def test_import_does_not_self_certify(system):
    index,store,_=system
    doc=document(); publish(store,doc,authenticate=False)
    with pytest.raises(IndexError,match='authentication_failed'):
        index.ingest(doc)


@pytest.mark.parametrize('changes',[{'account_id':'other'},{'scope':'other'},{'purpose':'planning'}])
def test_account_scope_and_purpose_filter_before_ranking(system,changes):
    index,store,_=system; doc=document(); publish(store,doc); index.ingest(doc)
    assert search(index,**changes)['matches']==[]


def test_expiry_and_clock_regression_do_not_revive_old_evidence(system):
    index,store,now=system; doc=document(); publish(store,doc); index.ingest(doc)
    index.register_artifact(artifact())
    now[0]=2000
    assert search(index)['matches']==[]
    assert index.artifact_status('account','packet')['status']=='STALE'
    now[0]=1000
    with pytest.raises(IndexError,match='clock_regressed'):
        search(index)


def test_revocation_propagates_without_manual_index_refresh(system):
    index,store,now=system; doc=document(); event=publish(store,doc); index.ingest(doc)
    assert index.register_artifact(artifact())['status']=='DEPENDENCIES_CURRENT'
    revoke={'schema':'keel.observation.v1','event_id':'revoke','account_id':'account','producer_id':'producer',
        'kind':'revocation','occurred_at':stamp(1000),
        'lineage':dict.fromkeys(('source_id','opportunity_id','application_id','attempt_id')),
        'revisions':{},'payload':{'target_event_id':event['event_id'],'target_sha256':event_digest(event),'reason':'synthetic correction'}}
    store.ingest(revoke,authenticate=True,context='synthetic-trusted-host')
    assert search(index)['matches']==[]
    result=index.artifact_status('account','packet')
    assert result['status']=='STALE' and result['reapproval_required']
    assert not result['execution_authorized']


def test_temporal_conflict_then_explicit_correction(system):
    index,store,_=system
    a=document(); b=document('two',text='Synthetic conflicting spreadsheet account.')
    for doc in (a,b): publish(store,doc); index.ingest(doc)
    assert search(index)['matches']==[]
    c=document('three',supersedes=['one','two'])
    publish(store,c); index.ingest(c)
    assert [m['document_id'] for m in search(index)['matches']]==['three']


def test_revision_change_invalidates_existing_packet(system):
    index,store,_=system; a=document(); publish(store,a); index.ingest(a)
    index.register_artifact(artifact())
    b=document('two',supersedes=['one']); publish(store,b); index.ingest(b)
    assert index.artifact_status('account','packet')['status']=='STALE'
    projection=index._temporal.invalidation_projection(1000)
    assert projection['stale_artifact_count']==1


def test_evidence_revision_corrects_same_source_without_redeclaring_identity(system):
    index,store,_=system
    first=document(); publish(store,first); index.ingest(first)
    index.register_artifact(artifact())
    corrected=document('two',source_id=first['source_id'],supersedes=['one'],
                       text='Corrected synthetic spreadsheet reconciliation experience.')
    event={'schema':'keel.observation.v1','event_id':corrected['observation_id'],
        'account_id':'account','producer_id':'producer','kind':'evidence','occurred_at':stamp(1000),
        'lineage':{'source_id':first['source_id'],'opportunity_id':None,'application_id':None,'attempt_id':None},
        'revisions':{'evidence':text_digest(corrected['text']),
                     'memory_document':document_digest(corrected)},
        'payload':{'evidence_ref':'synthetic:corrected-evidence'}}
    store.ingest(event,authenticate=True,context='synthetic-trusted-host')
    index.ingest(corrected)
    assert [m['document_id'] for m in search(index)['matches']]==['two']
    assert store.inspect('account',first['observation_id'])['status']=='CURRENT'
    assert index.artifact_status('account','packet')['status']=='STALE'


def test_writable_ancestor_rejected_before_creation_and_after_reopen(system,tmp_path):
    _,store,_=system
    unsafe=tmp_path/'unsafe'; unsafe.mkdir(mode=0o777); unsafe.chmod(0o777)
    with pytest.raises(IndexError,match='writable_ancestor'):
        EvidenceIndex(unsafe/'index',store,clock=lambda:1000)
    assert not (unsafe/'index').exists()
    unsafe.chmod(0o700)
    index=EvidenceIndex(unsafe/'index',store,clock=lambda:1000)
    unsafe.chmod(0o777)
    try:
        with pytest.raises(IndexError,match='writable_ancestor'):
            search(index)
    finally:
        unsafe.chmod(0o700)


def test_future_document_does_not_hide_current_revision_until_valid(system):
    index,store,now=system
    a=document(); publish(store,a); index.ingest(a)
    b=document('two',valid_from=1100,supersedes=['one']); publish(store,b); index.ingest(b)
    assert [m['document_id'] for m in search(index)['matches']]==['one']
    now[0]=1100
    assert [m['document_id'] for m in search(index)['matches']]==['two']


def test_cross_scope_supersession_rejected(system):
    index,store,_=system
    a=document(); publish(store,a); index.ingest(a)
    b=document('two',scope='elsewhere',supersedes=['one']); publish(store,b)
    with pytest.raises(IndexError,match='cross_scope_supersession'):
        index.ingest(b)


def test_missing_store_is_fail_closed_at_retrieval(system,monkeypatch):
    index,store,_=system; a=document(); publish(store,a); index.ingest(a)
    def unavailable(*args): raise OSError('synthetic storage fault')
    monkeypatch.setattr(store,'inspect',unavailable)
    assert search(index)['matches']==[]


def test_pending_projection_recovers_after_process_boundary(system,monkeypatch):
    index,store,now=system; a=document(); publish(store,a)
    original=index._temporal.record_observation
    def fail(*args,**kwargs): raise RuntimeError('synthetic interruption')
    monkeypatch.setattr(index._temporal,'record_observation',fail)
    with pytest.raises(RuntimeError): index.ingest(a)
    assert search(index)['matches']==[]
    monkeypatch.setattr(index._temporal,'record_observation',original)
    reopened=EvidenceIndex(index.home,store,clock=lambda:now[0])
    assert reopened.refresh('account')['projections'][0]['status']=='INDEXED'
    assert len(search(reopened)['matches'])==1


def test_identical_document_ids_are_account_scoped(system):
    index,store,_=system
    for account in ('account','second'):
        d=document(account_id=account); publish(store,d); index.ingest(d)
    assert len(search(index)['matches'])==1
    assert len(search(index,account_id='second')['matches'])==1


def test_fts_operators_are_only_literal_query_terms(system):
    index,store,_=system; d=document(); publish(store,d); index.ingest(d)
    result=index.search('spreadsheet OR "NOT" near*',account_id='account',scope='role',purpose='review')
    assert result['status']=='RETRIEVED'
    with pytest.raises(IndexError,match='query_terms_invalid'):
        index.search('"*',account_id='account',scope='role',purpose='review')


def test_payload_corruption_does_not_return_text(system):
    index,store,_=system; d=document(); publish(store,d); index.ingest(d)
    with sqlite3.connect(index.path) as db:
        tampered=deepcopy(d); tampered['text']='synthetic modified private text'
        db.execute('UPDATE documents SET payload=?',(json.dumps(tampered),))
    with pytest.raises(IndexError,match='indexed_document_corrupt'):
        search(index)


def test_symlink_hardlink_and_store_rebinding_rejected(system,tmp_path):
    index,store,now=system
    other=tmp_path/'linked'; other.symlink_to(index.home,target_is_directory=True)
    with pytest.raises(IndexError,match='symlink'):
        EvidenceIndex(other,store,clock=lambda:now[0])
    os.link(index.path,tmp_path/'linked-db')
    with pytest.raises(IndexError,match='hardlink'):
        search(index)


def test_retrieval_benchmark_records_denominators(system):
    index,store,_=system; d=document(); publish(store,d); index.ingest(d)
    data={'schema':'keel.memory.eval.v1','split':'held_out','synthetic':True,'cases':[
        {'case_id':'hit','account_id':'account','scope':'role','purpose':'review',
         'query':'spreadsheet','relevant_documents':['one']},
        {'case_id':'empty','account_id':'account','scope':'other','purpose':'review',
         'query':'spreadsheet','relevant_documents':[]}]}
    result=evaluate(index,data,expected_sha256=digest(data))
    assert result['recall_at_k']==result['precision_at_k']==1
    assert len(result['cases'])==2 and result['cases'][1]['returned_count']==0
    assert not result['labels_independently_authenticated']


def test_artifact_content_changes_require_new_identity(system):
    index,store,_=system; d=document(); publish(store,d); index.ingest(d)
    assert not index.register_artifact(artifact())['artifact_content_verified']
    changed=artifact(); changed['sha256']='c'*64
    with pytest.raises(IndexError,match='artifact_identity_conflict'):
        index.register_artifact(changed)


def test_concurrent_authenticated_conflict_cannot_escape_final_search_check(system,monkeypatch):
    import keel_memory.index as module
    index,store,_=system; a=document(); publish(store,a); index.ingest(a)
    original=module.rank_passages
    def concurrent(*args,**kwargs):
        ranked=original(*args,**kwargs)
        conflict=document(observation_id='event-two',source_id='source-two',text='Different synthetic spreadsheet claim.')
        publish(store,conflict)
        assert index.ingest(conflict)['status']=='CONFLICT'
        return ranked
    monkeypatch.setattr(module,'rank_passages',concurrent)
    result=search(index)
    assert result['matches']==[] and result['held_matching_documents']==1


def test_concurrent_revocation_during_ranking_is_not_returned(system,monkeypatch):
    import keel_memory.index as module
    index,store,_=system; d=document(); publish(store,d); index.ingest(d)
    original=module.rank_passages
    def revoke(*args,**kwargs):
        ranked=original(*args,**kwargs)
        monkeypatch.setattr(store,'inspect',lambda *args:None)
        return ranked
    monkeypatch.setattr(module,'rank_passages',revoke)
    assert search(index)['matches']==[]
