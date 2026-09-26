from copy import deepcopy
import pytest
from keel_loki.common import digest,LokiError
from keel_loki.retrieval import search


def corpus():
    return {'schema':'keel.loki.corpus.v1','passages':[
        {'passage_id':pid,'source_id':'source','source_sha256':'a'*64,'text':text,'scope':scope,
         'permitted_uses':['review'],'valid_from':0,'valid_until':until,'verification':verified,'embedding':None}
        for pid,text,scope,until,verified in [('real','spreadsheets reconciliation','a',None,'verified_observation'),
         ('private','spreadsheets spreadsheets reconciliation','b',None,'verified_observation'),
         ('expired','spreadsheets spreadsheets','a',2,'verified_observation'),
         ('unknown','spreadsheets','a',None,'unverified')]]}


def run(c,**kwargs):
    return search(c,'spreadsheets',expected_corpus_sha256=digest(c),scope='a',purpose='review',now=3,**kwargs)


def test_scope_expiry_and_verification_before_ranking():
    r=run(corpus());assert [p['passage_id'] for p in r['matches']]==['real']
    assert not r['truth_verified'] and r['matches'][0]['untrusted_text']


def test_supplied_vectors_bound_to_content_and_model():
    c=corpus();p=c['passages'][0]
    p['embedding']={'model_sha256':'b'*64,'content_sha256':digest(p['text']),'values':[1,0]}
    q={'model_sha256':'b'*64,'content_sha256':digest('spreadsheets'),'values':[1,0]}
    assert run(c,query_embedding=q)['mode']=='LEXICAL_AND_SUPPLIED_VECTORS'
    changed=deepcopy(c);changed['passages'][0]['text']='changed'
    with pytest.raises(LokiError):run(changed,query_embedding=q)
    q['values']=[1,0,0]
    with pytest.raises(LokiError):run(c,query_embedding=q)


def test_corpus_pin_prevents_revision_reuse():
    c=corpus();pin=digest(c);c['passages'][0]['source_sha256']='c'*64
    with pytest.raises(LokiError):search(c,'spreadsheets',expected_corpus_sha256=pin,scope='a',purpose='review',now=3)
