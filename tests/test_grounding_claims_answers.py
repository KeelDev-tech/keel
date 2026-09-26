from copy import deepcopy
from datetime import timedelta
import hashlib
import pytest
from keel_grounding.demo import make_fixture
from keel_grounding.claims import verify_grounding
from keel_grounding.answers import resolve_grounded_answer
from keel_grounding.evidence import GroundingError
from keel_trust.common import canonical, digest


def verify(f, root):
    return verify_grounding(f['document'], f['bindings'], root=root/'evidence', now=f['now'])


def answer(f, root):
    return resolve_grounded_answer(f['records'], f['context'], f['answer_bindings'],
        f['document'], f['bindings'], root=root/'evidence', now=f['now'])


def rebind(f):
    f['bindings']['trust_snapshot_sha256'] = digest(f['document'])


def refile(f, root):
    raw = canonical({'value': f['records'][0]['value'], 'answer': f['records'][0]})
    (root/'evidence/profile.json').write_bytes(raw)
    f['document']['sources'][0]['content_hash'] = hashlib.sha256(raw).hexdigest()
    for c in f['document']['claims']:
        c['evidence'][0]['content_hash'] = hashlib.sha256(raw).hexdigest()
    f['answer_bindings'][0]['record_hash'] = digest(f['records'][0])
    rebind(f)


def test_grounding_and_answer_exact_value_but_no_truth_or_authority(tmp_path):
    f=make_fixture(tmp_path); old=deepcopy(f)
    r=verify(f,tmp_path); a=answer(f,tmp_path)
    assert r['status']=='VERIFIED' and a['status']=='RESOLVED'
    assert a['record_content_verified'] and a['scope']==f['records'][0]['scope']
    assert not r['truth_independently_verified'] and not a['execution_authorized']
    assert not r['claims'][0]['wording_entailment_verified'] and f==old
    assert 'selectors' not in r['sources'][0]


@pytest.mark.parametrize('what',['file','selection','value','claimrevision','sourcecoverage','claimcoverage','snapshot'])
def test_changed_or_missing_evidence_fails_closed(tmp_path,what):
    f=make_fixture(tmp_path)
    if what=='file': (tmp_path/'evidence/profile.json').write_text('{}')
    if what=='selection': f['bindings']['claims'][0]['selections'][0]['selector_id']='record'
    if what=='value': f['bindings']['claims'][0]['value']='unsupported'
    if what=='claimrevision': f['bindings']['claims'][0]['revision']='v2'
    if what=='sourcecoverage': f['bindings']['sources']=[]
    if what=='claimcoverage': f['bindings']['claims']=[]
    if what=='snapshot': f['document']['source_revision']='changed'
    try: assert verify(f,tmp_path)['status']=='BLOCKED'
    except GroundingError: pass
    assert answer(f,tmp_path)['status']=='NEEDS_USER'


@pytest.mark.parametrize('field,value',[('review_state','PENDING'),('review_state','REVOKED'),('approval_ref',None),
    ('allowed_wording',['different']),('allowed_scopes',['different'])])
def test_existing_review_and_scope_restrictions_remain(tmp_path,field,value):
    f=make_fixture(tmp_path); f['document']['claims'][0][field]=value; rebind(f)
    assert verify(f,tmp_path)['status']=='BLOCKED'


def test_file_failure_invalidates_transitive_artifacts(tmp_path):
    f=make_fixture(tmp_path)
    a=deepcopy(f['document']['artifacts'][0]); a.update(artifact_id='child',statements=[],depends_on=['artifact-fixture'])
    f['document']['artifacts'].append(a); rebind(f)
    (tmp_path/'evidence/profile.json').unlink()
    r=verify(f,tmp_path)
    assert all(a['status']=='BLOCKED' for a in r['artifacts'])
    assert any('grounding_dependency_blocked' in x for x in r['artifacts'][1]['reasons'])


def test_stale_snapshot_is_not_refreshed_by_reading_file(tmp_path):
    f=make_fixture(tmp_path); f['now']+=timedelta(seconds=91)
    assert verify(f,tmp_path)['status']=='BLOCKED'


@pytest.mark.parametrize('kind',['no_ai','unaided','original_unassisted'])
def test_unaided_always_needs_user(tmp_path,kind):
    f=make_fixture(tmp_path); f['context']['kind']=kind
    assert answer(f,tmp_path)['reason']=='unaided_work_boundary'


@pytest.mark.parametrize('field,value',[('employer_id','other'),('posting_id','other'),
    ('candidate_id','other'),('question_hash','other'),('field_type','checkbox')])
def test_answer_cannot_cross_context(tmp_path,field,value):
    f=make_fixture(tmp_path); f['context'][field]=value
    assert answer(f,tmp_path)['status']=='NEEDS_USER'


def test_matching_value_cannot_replace_whole_verified_record(tmp_path):
    f=make_fixture(tmp_path); f['records'][0]['verification_ref']='new-unauthenticated-review'
    f['answer_bindings'][0]['record_hash']=digest(f['records'][0])
    assert answer(f,tmp_path)['reason']=='answer_record_content_mismatch'


def test_conflicting_answer_records_keep_old_conflict_rule(tmp_path):
    f=make_fixture(tmp_path); other=deepcopy(f['records'][0]);other['value']='different'
    f['records'].append(other)
    assert answer(f,tmp_path)['reason']=='conflicting_answers'


def test_false_consent_record_bound_without_substituting_fact_approval(tmp_path):
    f=make_fixture(tmp_path); f['context'].update(kind='consent',field_type='checkbox')
    f['records'][0].update(kind='consent',field_type='checkbox',value=False,authorization_ref='fixture://human-consent')
    f['document']['claims']=[];f['document']['artifacts']=[]; f['bindings']['claims']=[]
    f['answer_bindings'][0].update(claim_id=None,claim_revision=None); refile(f,tmp_path)
    r=answer(f,tmp_path)
    assert r['status']=='RESOLVED' and r['value'] is False and not r['execution_authorized']
    f['records'][0].pop('authorization_ref');refile(f,tmp_path)
    assert answer(f,tmp_path)['status']=='NEEDS_USER'


def test_other_candidate_claim_with_same_value_not_usable(tmp_path):
    f=make_fixture(tmp_path); f['document']['claims'][0]['subject_id']='other'; rebind(f)
    assert verify(f,tmp_path)['status']=='VERIFIED'
    assert answer(f,tmp_path)['reason']=='answer_claim_binding_mismatch'


def test_wrong_json_type_is_not_equal(tmp_path):
    f=make_fixture(tmp_path); f['bindings']['claims'][0]['value']=True
    f['document']['claims'][0]['value_hash']=digest(True); rebind(f)
    assert verify(f,tmp_path)['claims'][0]['status']=='BLOCKED'


def test_newest_record_selected_by_old_resolver_needs_its_own_binding(tmp_path):
    f=make_fixture(tmp_path); newer=deepcopy(f['records'][0]);newer['issued_at']=(f['now']+timedelta(seconds=1)).isoformat()
    f['records'].append(newer);f['now']+=timedelta(seconds=2)
    assert answer(f,tmp_path)['reason']=='answer_record_binding_missing'
