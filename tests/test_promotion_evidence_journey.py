"""Offline regression journeys for corrected answers and source coverage."""
import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engines'))
import form_intel
import packet_contract
import prescreen
import verify_retry

URL = 'https://jobs.lever.co/fixture/synthetic'
TEXT = 'Synthetic remote posting evidence.'
ENTRY = {'role_id': 'fixture-role', 'company': 'Fixture', 'ats_url': URL}


def intel(questions=None):
    return {'ats': 'fixture', 'source_url': URL, 'extraction_complete': True,
            'questions': questions or [], 'rendered_option_fetch_needed': []}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv('KEEL_HOME', str(tmp_path))
    spec = importlib.util.spec_from_file_location('keel_journey_cli', ROOT / 'keel.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    cli.initialize(tmp_path)
    monkeypatch.setattr(form_intel, 'probe_url', lambda *args, **kwargs: intel([
        {'label': 'First Name', 'type': 'text', 'required': True, 'options': []}]))
    return cli, tmp_path


def test_supported_answer_correction_reaches_real_promotion_screen(workspace):
    cli, root = workspace
    before = verify_retry.screen_promotion_form(ENTRY, URL, posting_text=TEXT)
    assert any('First Name' in reason for reason in before)
    cli.confirm_answer(root, 'first_name', 'Riley', 'synthetic applicant correction', scope='fixture-role')
    assert verify_retry.screen_promotion_form(ENTRY, URL, posting_text=TEXT) == []


def test_required_dropdown_without_choices_is_unknown(monkeypatch):
    evidence = intel([{'label': 'Select your preferred workflow', 'type': 'dropdown',
                       'required': True, 'options': []}])
    monkeypatch.setattr(form_intel, 'probe_url', lambda *args, **kwargs: evidence)
    result = prescreen.screen_entry_prepromotion(ENTRY, answer_bank={'answers': {}}, posting_text=TEXT)
    assert result['verdict'] == 'UNKNOWN'


def test_wrong_posting_url_is_not_clean(monkeypatch):
    monkeypatch.setattr(form_intel, 'probe_url', lambda *args, **kwargs: intel())
    entry = {**ENTRY, 'posting_text': TEXT, 'posting_text_url': 'https://fixture.invalid/other'}
    result = prescreen.screen_entry_prepromotion(entry, answer_bank={'answers': {}})
    assert result['verdict'] == 'UNKNOWN'


@pytest.mark.parametrize('damage', ['unconfirmed', 'changed', 'expired', 'other-role', 'other-employer'])
def test_invalid_or_out_of_scope_answers_do_not_clear_promotion(workspace, damage):
    cli, root = workspace
    cli.confirm_answer(root, 'first_name', 'Riley', 'synthetic assertion', scope='fixture-role')
    path = root / 'data/answer_bank.json'
    bank = json.loads(path.read_text())
    if damage == 'unconfirmed':
        bank['_provenance'].pop('first_name')
    elif damage == 'changed':
        bank['answers']['first_name'] = 'Changed'
    elif damage == 'expired':
        bank['_provenance']['first_name']['expires_at'] = '2000-01-01T00:00:00+00:00'
    elif damage == 'other-role':
        bank['_provenance']['first_name']['scope'] = 'another-role'
    else:
        scoped = {'value': 'Riley', 'scope': {'employer': 'Different employer'},
                  'provenance': 'synthetic assertion'}
        bank['answers']['first_name'] = scoped
        bank['_provenance']['first_name'] = packet_contract.answer_receipt(scoped, 'synthetic assertion', role_id='fixture-role')
    path.write_text(json.dumps(bank))
    assert verify_retry.screen_promotion_form(ENTRY, URL, posting_text=TEXT)


def test_corrected_identity_does_not_clear_other_applicant_questions(workspace, monkeypatch):
    cli, root = workspace
    cli.confirm_answer(root, 'first_name', 'Riley', 'synthetic correction')
    questions = [{'label': 'First Name', 'type': 'text', 'required': True, 'options': []},
                 {'label': 'I certify this was personally completed without AI assistance',
                  'type': 'checkbox', 'required': True, 'options': []}]
    monkeypatch.setattr(form_intel, 'probe_url', lambda *args, **kwargs: intel(questions))
    reasons = verify_retry.screen_promotion_form(ENTRY, URL, posting_text=TEXT)
    assert not any('"First Name"' in reason for reason in reasons)
    assert any('attest' in reason for reason in reasons)


def test_malformed_workspace_bank_is_a_verification_hold(workspace):
    _, root = workspace
    (root / 'data/answer_bank.json').write_text('{invalid json')
    with pytest.raises(verify_retry.VerificationUnavailable):
        verify_retry.screen_promotion_form(ENTRY, URL, posting_text=TEXT)


@pytest.mark.parametrize('choices', [None, [], [' ', '']])
def test_empty_required_choices_fail_coverage(choices):
    question = {'label': 'Select a preference', 'type': 'dropdown', 'required': True}
    if choices is not None:
        question['options'] = choices
    assert not prescreen.form_intel_is_complete(intel([question]), URL)


def test_known_required_choices_can_be_screened(monkeypatch):
    evidence = intel([{'label': 'Select a preference', 'type': 'dropdown',
                       'required': True, 'options': ['One', 'Two']}])
    monkeypatch.setattr(form_intel, 'probe_url', lambda *args, **kwargs: evidence)
    assert prescreen.form_intel_is_complete(evidence, URL)
    assert prescreen.screen_entry_prepromotion(ENTRY, answer_bank={'answers': {}}, posting_text=TEXT)['verdict'] == 'CLEAN'


@pytest.mark.parametrize('source_url', [None, 'https://fixture.invalid/other', URL])
def test_stored_posting_requires_exact_url_binding(monkeypatch, source_url):
    monkeypatch.setattr(form_intel, 'probe_url', lambda *args, **kwargs: intel())
    entry = {**ENTRY, 'posting_text': TEXT, 'posting_text_url': source_url}
    result = prescreen.screen_entry_prepromotion(entry, answer_bank={'answers': {}})
    assert result['verdict'] == ('CLEAN' if source_url == URL else 'UNKNOWN')


def test_probe_source_mismatch_cannot_be_overwritten(monkeypatch):
    evidence = intel()
    evidence['source_url'] = 'https://fixture.invalid/other'
    monkeypatch.setattr(form_intel, 'probe_url', lambda *args, **kwargs: evidence)
    assert prescreen.screen_entry_prepromotion(ENTRY, answer_bank={'answers': {}}, posting_text=TEXT)['verdict'] == 'UNKNOWN'
