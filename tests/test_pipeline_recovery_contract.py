"""Isolated regressions for portable verification and supply diagnosis."""
import copy
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engines'))
import packet_contract
import queue_io
from safe_io import atomic_json, utc_now


def initialize_workspace(path):
    # Exercise the installed CLI rather than whichever package named `keel`
    # an unrelated subsystem has already imported in the same test process.
    result = subprocess.run([sys.executable, str(ROOT / 'keel.py'),
                             '--home', str(path), 'init'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_receipt_does_not_widen_employer_scope():
    value = {'value': 'A prior employer', 'scope': 'employer:Fixture A',
             'source': 'applicant assertion'}
    bank = {'answers': {'current_company_name': value},
            '_provenance': {'current_company_name': packet_contract.answer_receipt(
                value, 'applicant assertion')}}
    approved, problems = packet_contract.confirmed_answers(
        bank, role_id='fixture-role', employer='Fixture B',
        role_context={'role_id': 'fixture-role', 'company': 'Fixture B'})
    assert not approved
    assert problems
    approved, problems = packet_contract.confirmed_answers(
        bank, role_id='fixture-role', employer='Fixture A',
        role_context={'role_id': 'fixture-role', 'company': 'Fixture A'})
    assert approved == {'current_company_name': 'A prior employer'}
    assert not problems


def test_receipt_cannot_turn_refusal_into_answer():
    value = 'DO NOT CERTIFY; route to PERSONAL TAKEOVER'
    bank = {'answers': {'no_ai_attestation': value}, '_provenance': {
        'no_ai_attestation': packet_contract.answer_receipt(value, 'applicant instruction')}}
    approved, problems = packet_contract.confirmed_answers(bank, role_id='fixture-role')
    assert not approved and problems


def test_receipt_and_embedded_expiry_both_apply():
    value = {'value': 'Yes', 'scope': 'global', 'expires_at': '2000-01-01T00:00:00Z'}
    bank = {'answers': {'fixture_fact': value}, '_provenance': {
        'fixture_fact': packet_contract.answer_receipt(value, 'applicant assertion')}}
    assert packet_contract.confirmed_answers(bank, role_id='fixture-role')[0] == {}


def test_pipeline_doctor_measures_losses_without_mutating_data(tmp_path, monkeypatch):
    import pipeline_doctor
    initialize_workspace(tmp_path)
    role = {'role_id': 'fixture-office-held', 'company': 'Fixture', 'title': 'Role',
            'status': 'READY', 'action_band': 'APPLY', 'fit_score': 75,
            'd1_office_exclusion': True, 'unresolved': []}
    atomic_json(tmp_path / 'data/queues/standard-queue.json', [role])
    prior_lock = queue_io.get_lock_path()
    queue_io.set_lock_path(str(tmp_path / 'queue.lock'))
    try:
        before = {p: p.read_bytes() for p in (tmp_path / 'data').rglob('*') if p.is_file()}
        result = pipeline_doctor.report(tmp_path)
        after = {p: p.read_bytes() for p in (tmp_path / 'data').rglob('*') if p.is_file()}
    finally:
        queue_io.set_lock_path(prior_lock)
    assert before == after
    assert result['nominal_ready'] == 1
    assert result['static_admissible_ready'] == 0
    assert result['packet_backed_ready'] == 0
    assert result['launchable_ready'] is None
    assert result['network_reads'] == 0


def test_doctor_duplicate_homes_never_count_as_ready(tmp_path):
    import pipeline_doctor
    initialize_workspace(tmp_path)
    role = {'role_id': 'fixture-duplicate', 'company': 'Fixture', 'title': 'Role',
            'status': 'READY', 'action_band': 'APPLY', 'fit_score': 80}
    for name in ('standard', 'strategic'):
        atomic_json(tmp_path / 'data/queues' / (name + '-queue.json'), [role])
    previous = queue_io.get_lock_path()
    queue_io.set_lock_path(str(tmp_path / 'queue.lock'))
    try:
        result = pipeline_doctor.report(tmp_path)
    finally:
        queue_io.set_lock_path(previous)
    assert not result['one_home_per_role']
    assert result['static_admissible_ready'] == 0
    assert result['duplicate_queue_homes']['fixture-duplicate'] == ['standard', 'strategic']


def test_doctor_counts_prepared_artifact_without_execution_authority(tmp_path):
    import pipeline_doctor
    import ready_gate
    initialize_workspace(tmp_path)
    role = {'role_id': 'fixture-prepared', 'company': 'Fixture', 'title': 'Role',
            'status': 'READY', 'action_band': 'APPLY', 'fit_score': 80,
            'ats_url': 'https://boards.greenhouse.io/fixture/jobs/123'}
    atomic_json(tmp_path / 'data/queues/standard-queue.json', [role])
    material = tmp_path / 'data/resumes/fixture.pdf'
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_bytes(b'%PDF-1.4\n%%EOF\n')
    bank = json.loads((tmp_path / 'data/answer_bank.json').read_text())
    packet = {key: role[key] for key in ('role_id', 'company', 'title', 'ats_url')}
    packet.update(brief='Fixture preparation', upload_files=[str(material)],
                  scope='preparation_only', execution_authorized=False)
    ready_gate.seal_packet(packet, role, bank, workspace=tmp_path)
    atomic_json(tmp_path / 'data/launch-packets/buffer/fixture-prepared.json', packet)
    previous = queue_io.get_lock_path()
    queue_io.set_lock_path(str(tmp_path / 'queue.lock'))
    try:
        result = pipeline_doctor.report(tmp_path)
    finally:
        queue_io.set_lock_path(previous)
    assert result['packet_backed_ready'] == 1
    assert result['launchable_ready'] is None
    assert result['submission_authorized'] is False
    assert not ready_gate.packet_admission(packet, role, bank, workspace=tmp_path)['allowed']
    (tmp_path / 'data/employer-blocklist.md').write_text('Fixture\n')
    queue_io.set_lock_path(str(tmp_path / 'queue.lock'))
    try:
        blocked = pipeline_doctor.report(tmp_path)
    finally:
        queue_io.set_lock_path(previous)
    assert blocked['static_admissible_ready'] == 0
    assert blocked['packet_backed_ready'] == 0
    assert 'blocklisted_employer' in blocked['ready_rows'][0]['reason_codes']


def test_diagnostic_missing_state_is_unknown(tmp_path):
    import pipeline_doctor
    previous = queue_io.get_lock_path()
    queue_io.set_lock_path(str(tmp_path / 'queue.lock'))
    try:
        result = pipeline_doctor.report(tmp_path)
    finally:
        queue_io.set_lock_path(previous)
    assert result['data_complete'] is False
    assert result['queue_rows'] is None and result['nominal_ready'] is None
    assert result['data_problems']
