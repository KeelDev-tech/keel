"""Audit must detect broken gates as well as preserve valid controls."""
import json
import subprocess
import sys

import pytest

from tools import run_tailoring_audit as audit


def test_matrix_counts_controls_and_rejections(tmp_path):
    report = audit.audit(tmp_path / 'audit')
    assert report['status'] == 'PASS'
    assert report['case_count'] == report['clean_control_count'] == 90
    assert report['negative_case_count'] == 75
    assert report['false_verified_packets'] == report['clean_control_failures'] == 0
    assert set(report['scenario_counts'].values()) == {15}
    assert len({(r['profile_id'], r['role_id']) for r in report['cases']}) == 90
    assert not any(r['execution_authorized'] for r in report['cases'])
    assert json.loads((tmp_path / 'audit/audit.json').read_text()) == report


@pytest.mark.parametrize('status,false_count,control_failures', [('VERIFIED', 75, 0), ('BLOCKED', 0, 90)])
def test_audit_fails_when_verifier_accepts_everything_or_blocks_everything(tmp_path, monkeypatch,
                                                                         status, false_count, control_failures):
    monkeypatch.setattr(audit, 'verify_packet', lambda *a, **k: {'status': status, 'execution_authorized': False})
    report = audit.audit(tmp_path / 'audit')
    assert report['status'] == 'FAIL'
    assert report['false_verified_packets'] == false_count
    assert report['clean_control_failures'] == control_failures


def test_authority_upgrade_fails_audit(tmp_path, monkeypatch):
    original = audit.verify_packet
    def upgraded(*args, **kwargs):
        return {**original(*args, **kwargs), 'execution_authorized': True}
    monkeypatch.setattr(audit, 'verify_packet', upgraded)
    assert audit.audit(tmp_path / 'audit')['status'] == 'FAIL'


def test_cli_runs_without_site_packages_and_never_overwrites_output(tmp_path):
    out = tmp_path / 'audit'
    command = [sys.executable, '-S', str(audit.ROOT / 'tools/run_tailoring_audit.py'), '--out', str(out)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads((out / 'audit.json').read_text())
    assert report['external_effect_attempts'] == 0
    before = (out / 'audit.json').read_bytes()
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert (out / 'audit.json').read_bytes() == before
