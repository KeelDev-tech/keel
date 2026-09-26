import pytest
from keel_loki.common import digest
from keel_loki.forms import demo_fixture
from keel_muse import repair as r


def inputs():
    fixture=demo_fixture()
    frozen=r.freeze_fixtures([{'case_id':'one','fixture':fixture,'fault':'missing_locator','field_id':'full_name','expected_outcome':'PREPARED'},
        {'case_id':'two','fixture':fixture,'fault':'unknown_required','field_id':'full_name','expected_outcome':'BLOCKED'}])
    trace={'trace_id':'trace','failure_code':'missing_locator','field_id':'full_name','form_sha256':digest(fixture['contract']),'counterexample':{'missing':'full_name'}}
    candidate=r.propose(trace,frozen_fixtures_sha256=frozen['fixtures_sha256'],source_sha256='a'*64)
    return candidate,frozen


def test_actual_replay_recommends_without_promoting():
    result=r.demo()
    assert result['recommendation']['beneficial']==1
    assert result['recommendation']['false_ready']==0
    assert result['recommendation']['promoted'] is False


@pytest.mark.parametrize('action',['approve','set_fact','change_label','shell','change_policy'])
def test_poisoned_actions_fail(action):
    candidate,frozen=inputs();candidate['action']=action
    with pytest.raises(ValueError):r.evaluate(candidate,frozen,expected_candidate_sha256=digest(candidate),expected_fixtures_sha256=frozen['fixtures_sha256'],source_sha256='a'*64)


def test_false_claimed_replay_success_is_recomputed():
    candidate,frozen=inputs()
    report=r.evaluate(candidate,frozen,expected_candidate_sha256=digest(candidate),expected_fixtures_sha256=frozen['fixtures_sha256'],source_sha256='a'*64)
    report['rows'][1]['candidate']['outcome']='PREPARED';report['replay_sha256']=digest(report['rows'])
    with pytest.raises(ValueError):r.recommend(report,expected_report_sha256=digest(report),candidate=candidate,frozen=frozen,source_sha256='a'*64)


def test_changed_frozen_label_or_source_fails_pin():
    candidate,frozen=inputs();frozen['cases'][0]['expected_outcome']='BLOCKED'
    with pytest.raises(ValueError):r.evaluate(candidate,frozen,expected_candidate_sha256=digest(candidate),expected_fixtures_sha256=frozen['fixtures_sha256'],source_sha256='a'*64)


def test_missing_counterexample_and_unknown_repair_denied():
    candidate,frozen=inputs()
    with pytest.raises(ValueError):r.propose({'trace_id':'trace','failure_code':'approve','field_id':'x','form_sha256':'a'*64,'counterexample':{}},frozen_fixtures_sha256=frozen['fixtures_sha256'],source_sha256='a'*64)


@pytest.mark.parametrize('fault,field_id',[('missing_locator','full_name'),('bad_upload','resume'),('stale_form','full_name')])
def test_repair_executes_state_change_before_same_observer(fault,field_id):
    fixture=demo_fixture()
    frozen=r.freeze_fixtures([{'case_id':'fault','fixture':fixture,'fault':fault,'field_id':field_id,'expected_outcome':'PREPARED'},
        {'case_id':'unknown','fixture':fixture,'fault':'unknown_required','field_id':field_id,'expected_outcome':'BLOCKED'}])
    trace={'trace_id':'trace','failure_code':fault,'field_id':field_id,'form_sha256':digest(fixture['contract']),'counterexample':{'fault':fault}}
    candidate=r.propose(trace,frozen_fixtures_sha256=frozen['fixtures_sha256'],source_sha256='a'*64)
    report=r.evaluate(candidate,frozen,expected_candidate_sha256=digest(candidate),expected_fixtures_sha256=frozen['fixtures_sha256'],source_sha256='a'*64)
    before,after=report['rows'][0]['baseline'],report['rows'][0]['candidate']
    assert before['outcome']=='BLOCKED' and after['outcome']=='PREPARED'
    assert before['before_driver_sha256']==after['before_driver_sha256']
    assert after['before_driver_sha256']!=after['after_driver_sha256']
    assert len(after['repair_actions'])==1
    assert report['rows'][1]['candidate']['outcome']=='BLOCKED'
