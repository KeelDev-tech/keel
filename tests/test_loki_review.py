"""Adversarial regression cases discovered during independent 0.12 review."""
import json
import math
import os
from pathlib import Path

import pytest

from keel_loki import common, lab, retrieval


def test_output_publication_binds_written_inode_and_preserves_concurrent_temp(tmp_path, monkeypatch):
    destination = tmp_path/'result.json'
    original_link = os.link
    replacements = []

    def swap_temporary_before_link(source, target, **kwargs):
        if Path(target).name == destination.name:
            temporary = next(tmp_path.glob('.keel-loki-*'))
            temporary.rename(tmp_path/'original-inode-retained-by-host')
            temporary.write_text('{"concurrent_host_file":true}')
            replacements.append(temporary)
        return original_link(source, target, **kwargs)

    monkeypatch.setattr(os, 'link', swap_temporary_before_link)
    try:
        common.atomic_json(destination, {'pinned_value': 'original'})
    except (OSError, ValueError):
        assert not destination.exists()
    else:
        assert json.loads(destination.read_text()) == {'pinned_value': 'original'}
    assert replacements
    assert json.loads(replacements[0].read_text()) == {'concurrent_host_file': True}


def test_maximum_sized_json_is_readable_or_rejected_before_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(common, 'MAX_BYTES', 64)
    value = {'x': 'a'*56}
    assert len(common.canonical(value)) == common.MAX_BYTES
    destination = tmp_path/'maximum.json'
    try:
        common.atomic_json(destination, value)
    except common.LokiError:
        assert not destination.exists()
    else:
        assert common.load_json(destination) == value


def test_small_finite_vectors_do_not_crash_or_create_nonfinite_scores():
    text = 'Synthetic financial reconciliation.'
    query = 'reconciliation'
    corpus = {'schema': 'keel.loki.corpus.v1', 'passages': [{
        'passage_id': 'p', 'source_id': 's', 'source_sha256': 'a'*64,
        'text': text, 'scope': 'workspace', 'permitted_uses': ['review'],
        'valid_from': 0, 'valid_until': None, 'verification': 'verified_observation',
        'embedding': {'model_sha256': 'b'*64, 'content_sha256': common.digest(text),
                      'values': [1e-300, 0.0]},
    }]}
    query_embedding = {'model_sha256': 'b'*64, 'content_sha256': common.digest(query),
                       'values': [1e-300, 0.0]}
    try:
        result = retrieval.search(corpus, query, expected_corpus_sha256=common.digest(corpus),
                                  scope='workspace', purpose='review', now=1,
                                  query_embedding=query_embedding)
    except common.LokiError:
        return  # Explicit validation rejection is also a safe numeric boundary.
    assert result['matches']
    assert all(math.isfinite(match['rrf_score']) for match in result['matches'])


def test_lab_record_deletion_cannot_keep_the_original_dataset_and_plan_pins():
    report = lab.demo()
    original_pins = (report['dataset_sha256'], report['plan_sha256'])
    del report['records'][0]
    report['calls'] = sum(row['stages'][stage]['calls']
                          for row in report['records'] for stage in lab.STAGES)
    report['metrics'] = lab.summarize(report['records'])
    assert (report['dataset_sha256'], report['plan_sha256']) == original_pins
    with pytest.raises(ValueError):
        lab.validate_report(report)


def test_lab_relabelling_cannot_keep_the_original_dataset_and_plan_pins():
    report = lab.demo()
    original_pins = (report['dataset_sha256'], report['plan_sha256'])
    assert report['records'][0]['expected_verdict'] == 'PASS'
    report['records'][0]['expected_verdict'] = 'ABSTAIN'
    report['metrics'] = lab.summarize(report['records'])
    assert (report['dataset_sha256'], report['plan_sha256']) == original_pins
    with pytest.raises(ValueError):
        lab.validate_report(report)


@pytest.mark.parametrize('value', [-1, True, '0'])
def test_lab_measured_stage_latency_must_be_nonnegative_number(value):
    report = lab.demo()
    report['records'][0]['stages']['drafter_blind']['latency_ms'] = value
    with pytest.raises(ValueError):
        lab.validate_report(report)


def test_lab_subject_hash_cannot_change_under_an_unchanged_plan():
    report = lab.demo()
    report['records'][0]['subject_sha256'] = 'c'*64
    with pytest.raises(ValueError):
        lab.validate_report(report)


@pytest.mark.parametrize(('field', 'forged'), [('synthetic', False), ('split', 'held_out'),
                                            ('mode', 'LOCAL_LOOPBACK')])
def test_lab_provenance_cannot_change_under_external_plan_pin(field, forged):
    report = lab.demo()
    original_plan = report['plan_sha256']
    assert report[field] != forged
    report[field] = forged
    with pytest.raises(ValueError):
        lab.validate_report(report, expected_plan_sha256=original_plan, dataset=lab.mutation_dataset())


def test_contradiction_fixture_changes_value_without_mutating_subject_or_target():
    dataset = lab.mutation_dataset({'subject_id': 'team-A', 'predicate': 'certification',
                                    'value': 'A', 'contradicting_value': 'B', 'target_id': 'role-A'})
    case = next(case for case in dataset['cases'] if case['case_id'] == 'contradiction')
    assert case['expected_verdict'] == 'FAIL'
    assert case['subject']['evidence'][0]['text'] == (
        'Current valid source states: For team-A and target role-A, certification is B.')


def test_skill_section_control_cannot_be_filled(tmp_path):
    from keel_loki.skills import SkillWorkshop
    trace = {
        'trace_id': 'fixture-trace',
        'scope': {'workspace_id': 'w', 'origin': 'https://fixture.invalid',
                  'account_id': 'a', 'role_id': 'r'},
        'form_revision': 'a'*64, 'observed_at': 10,
        'observations': [{'action': 'fill_approved', 'field_id': 'heading',
                          'control': 'section', 'value_ref': 'b'*64, 'readback_ref': 'b'*64}],
    }
    workshop = SkillWorkshop(tmp_path/'workshop')
    with pytest.raises(ValueError):
        workshop.quarantine(trace, skill_id='bad-section-recipe', expires_at=100)
