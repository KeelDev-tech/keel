"""Local lexical/vector retrieval with scope, time and content bindings.

Vectors are supplied by a trusted host; this module does not call an embedding
service or claim that a similarity score measures truth.
"""
import math
import re
from .common import clone, digest, require_dict, require_hash, require_id, require_int, LokiError


def _tokens(text):
    if type(text) is not str or not text or len(text) > 65536:
        raise LokiError('bounded nonempty text required')
    return re.findall(r'\w+', text.casefold(), flags=re.UNICODE)


def _vector(value):
    if type(value) is not list or not 1 <= len(value) <= 4096:
        raise LokiError('bounded embedding required')
    if any(type(x) not in (int, float) or abs(x) > 1e6 or not math.isfinite(x) for x in value):
        raise LokiError('finite embedding coordinates required')
    if not any(value):
        raise LokiError('nonzero embedding required')
    return value


def validate_corpus(corpus):
    corpus = clone(corpus)
    require_dict(corpus, {'schema','passages'})
    if corpus['schema'] != 'keel.loki.corpus.v1' or type(corpus['passages']) is not list or not 1 <= len(corpus['passages']) <= 1024:
        raise LokiError('bounded corpus required')
    seen = set()
    for p in corpus['passages']:
        require_dict(p, {'passage_id','source_id','source_sha256','text','scope','permitted_uses',
                         'valid_from','valid_until','verification','embedding'})
        for k in ('passage_id','source_id','scope'):
            require_id(p[k])
        if p['passage_id'] in seen:
            raise LokiError('duplicate passage')
        seen.add(p['passage_id'])
        require_hash(p['source_sha256'])
        _tokens(p['text'])
        require_int(p['valid_from'])
        if p['valid_until'] is not None:
            require_int(p['valid_until'],p['valid_from']+1)
        if p['verification'] not in ('verified_observation','unverified'):
            raise LokiError('explicit verification observation required')
        if (type(p['permitted_uses']) is not list or not p['permitted_uses']
                or any(x not in ('planning','review','application_fact') for x in p['permitted_uses'])
                or len(set(p['permitted_uses'])) != len(p['permitted_uses'])):
            raise LokiError('explicit permitted uses required')
        if p['embedding'] is not None:
            e=p['embedding']; require_dict(e, {'model_sha256','content_sha256','values'})
            require_hash(e['model_sha256'])
            if e['content_sha256'] != digest(p['text']):
                raise LokiError('embedding content changed')
            _vector(e['values'])
    return corpus


def search(corpus, query, *, expected_corpus_sha256, scope, purpose, now, limit=5, query_embedding=None):
    corpus=validate_corpus(corpus)
    if digest(corpus) != require_hash(expected_corpus_sha256):
        raise LokiError('corpus pin changed')
    require_id(scope); require_int(now); require_int(limit,1,50)
    if purpose not in ('planning','review','application_fact'):
        raise LokiError('unsupported purpose')
    terms=set(_tokens(query))
    if not terms:
        raise LokiError('query terms required')
    if query_embedding is not None:
        query_embedding=clone(query_embedding)
        require_dict(query_embedding, {'model_sha256','content_sha256','values'})
        require_hash(query_embedding['model_sha256'])
        if query_embedding['content_sha256'] != digest(query):
            raise LokiError('query embedding content changed')
        _vector(query_embedding['values'])
    # Privacy, freshness and declared verification filter BEFORE ranking.
    eligible=[p for p in corpus['passages'] if p['scope']==scope and purpose in p['permitted_uses']
        and p['valid_from']<=now and (p['valid_until'] is None or now<p['valid_until'])
        and p['verification']=='verified_observation']
    words={p['passage_id']:_tokens(p['text']) for p in eligible}
    avg=sum(len(x) for x in words.values())/len(words) if words else 1
    lex=[]; dense=[]
    for p in eligible:
        tokens=words[p['passage_id']]; score=0.0
        for term in terms:
            df=sum(term in x for x in words.values()); tf=tokens.count(term)
            if tf:
                idf=math.log(1+(len(words)-df+0.5)/(df+0.5))
                score+=idf*(tf*2.2)/(tf+1.2*(0.25+0.75*len(tokens)/max(avg,1)))
        if score>0:lex.append((p['passage_id'],score))
        e=p['embedding']
        if query_embedding is not None and e is not None and e['model_sha256']==query_embedding['model_sha256']:
            a,b=e['values'],query_embedding['values']
            if len(a)!=len(b):raise LokiError('embedding dimension mismatch')
            na,nb=math.hypot(*a),math.hypot(*b)
            cosine=max(-1.0,min(1.0,sum((x/na)*(y/nb) for x,y in zip(a,b))))
            dense.append((p['passage_id'],cosine))
    lex.sort(key=lambda x:(-x[1],x[0])); dense.sort(key=lambda x:(-x[1],x[0]))
    scores={}
    for ranking in (lex,dense):
        for rank,(pid,_) in enumerate(ranking,1): scores[pid]=scores.get(pid,0)+1/(60+rank)
    by_id={p['passage_id']:p for p in eligible}
    ordered=sorted(scores,key=lambda pid:(-scores[pid],pid))[:limit]
    matches=[{'passage_id':pid,'source_id':by_id[pid]['source_id'],'source_sha256':by_id[pid]['source_sha256'],
              'text':by_id[pid]['text'],'text_sha256':digest(by_id[pid]['text']),'rrf_score':scores[pid],
              'untrusted_text':True} for pid in ordered]
    return {'schema':'keel.loki.retrieval.v1','status':'RETRIEVED','mode':'LEXICAL_AND_SUPPLIED_VECTORS' if query_embedding is not None else 'LEXICAL',
        'corpus_sha256':digest(corpus),'query_sha256':digest(query),'eligible_passages':len(eligible),
        'matches':matches,'verification_observations_authenticated':False,'truth_verified':False,
        'model_calls':0,'execution_authorized':False}


def demo():
    corpus={'schema':'keel.loki.corpus.v1','passages':[
        {'passage_id':'p1','source_id':'profile','source_sha256':digest('fixture-source'),
         'text':'Synthetic applicant uses spreadsheet reconciliation.','scope':'fixture',
         'permitted_uses':['review'],'valid_from':0,'valid_until':None,
         'verification':'verified_observation','embedding':None}]}
    return search(corpus,'spreadsheet reconciliation',expected_corpus_sha256=digest(corpus),scope='fixture',purpose='review',now=1)
