"""Direct-fact review signals never override applicant assertions."""
import copy
import json

import pytest

import keel


def test_doctor_surfaces_difference_without_changing_readiness_or_files(tmp_path):
    keel.initialize(tmp_path)
    profile_path = tmp_path / 'data/applicant_profile.json'
    bank_path = tmp_path / 'data/answer_bank.json'
    profile = json.loads(profile_path.read_text())
    profile['contact']['email'] = 'profile@fixture.invalid'
    profile_path.write_text(json.dumps(profile))
    bank = json.loads(bank_path.read_text())
    bank['answers']['email'] = 'answer@fixture.invalid'
    bank_path.write_text(json.dumps(bank))
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = keel.doctor(tmp_path)
    assert result['profile_answer_review']['findings'] == [
        {'field': 'email', 'code': 'profile_answer_difference', 'status': 'REVIEW'}]
    assert result['ready_for_local_preparation'] == all(c['status'] == 'PASS' for c in result['checks'])
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert 'profile@fixture.invalid' not in json.dumps(result['profile_answer_review'])


@pytest.mark.parametrize('field,left,right', [
    ('phone', '+1 (202) 555-0198', '1-202-555-0198'),
    ('phone', '+1 202 555 0198 ext. 42', '+1.202.555.0198 x42'),
    ('linkedin', 'https://www.linkedin.com/in/synthetic/', 'http://linkedin.com/in/synthetic'),
    ('email', ' synthetic@EXAMPLE.INVALID ', 'synthetic@example.invalid'),
])
def test_equivalent_formatting_is_not_a_difference(field, left, right):
    from engines.profile_answer_review import compare
    profile = {'contact': {field: left}}
    bank = {'answers': {field: {'value': right, 'source': 'synthetic applicant assertion'}}}
    before = copy.deepcopy((profile, bank))
    report = compare(profile, bank)
    assert report['findings'] == []
    assert field in report['compared_fields']
    assert (profile, bank) == before


@pytest.mark.parametrize('field,left,right', [
    ('phone', '+1 202 555 0198', '+44 202 555 0198'),
    ('phone', '+1 202 555 0198 x42', '+1 202 555 0198 x43'),
    ('linkedin', 'https://linkedin.com/in/synthetic-a', 'https://linkedin.com/in/synthetic-b'),
    ('linkedin', 'https://linkedin.com/in/synthetic?q=a', 'https://linkedin.com/in/synthetic?q=b'),
])
def test_meaningful_differences_remain_advisory(field, left, right):
    from engines.profile_answer_review import compare
    report = compare({'contact': {field: left}}, {'answers': {field: right}})
    assert report['findings'] == [{'field': field, 'code': 'profile_answer_difference', 'status': 'REVIEW'}]


@pytest.mark.parametrize('profile,bank', [(None, {}), ({}, None), ({'contact': []}, {'answers': {}})])
def test_malformed_sources_are_unavailable(profile, bank):
    from engines.profile_answer_review import compare
    assert compare(profile, bank)['status'] == 'UNAVAILABLE'


def test_incomplete_year_only_history_cannot_limit_asserted_experience():
    from engines.profile_answer_review import compare
    profile = {'contact': {}, 'experience': [{'dates': '2023 to Present'}, {'dates': '2021 to 2022'}]}
    report = compare(profile, {'answers': {'total_professional_years': '20'}})
    assert report['findings'] == []
    assert report['experience_comparison'] == 'NOT_COMPARABLE'
    assert 'total_professional_years' not in report['compared_fields']


def test_advisory_difference_does_not_block_valid_preparation(tmp_path):
    keel.initialize(tmp_path)
    for key, value in [('first_name', 'Synthetic'), ('last_name', 'Fixture'),
                       ('email', 'answer@fixture.invalid')]:
        keel.confirm_answer(tmp_path, key, value, 'synthetic fixture')
    before = keel.doctor(tmp_path)
    assert before['ready_for_local_preparation'] is True
    profile_path = tmp_path / 'data/applicant_profile.json'
    profile = json.loads(profile_path.read_text())
    profile['contact']['email'] = 'different@fixture.invalid'
    profile_path.write_text(json.dumps(profile))
    after = keel.doctor(tmp_path)
    assert after['ready_for_local_preparation'] is True
    assert after['checks'] == before['checks']
    assert after['profile_answer_review']['status'] == 'REVIEW'


@pytest.mark.parametrize('content', ['null', '[]', '{bad'])
def test_doctor_reports_unavailable_profile_without_echoing_contents(tmp_path, content):
    keel.initialize(tmp_path)
    (tmp_path / 'data/applicant_profile.json').write_text(content)
    assert keel.doctor(tmp_path)['profile_answer_review']['status'] == 'UNAVAILABLE'
