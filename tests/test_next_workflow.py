"""Actual cross-component offline workflow; no fake live-provider claims."""
from pathlib import Path
import json

from keel_next.__main__ import main
from keel_next.workflow import demo, doctor


def test_integrated_demo_preserves_provenance_and_revocation(tmp_path):
    result=demo(tmp_path/'new-demo')
    assert result['status']=='DEMO_PASSED'
    assert all(result['checks'].values())
    assert result['synthetic'] and not result['live_identity_or_provider_proof']
    assert result['model_calls']==result['network_calls']==result['schedule_writes']==0
    assert not result['execution_authorized']


def test_demo_refuses_existing_real_or_synthetic_workspace(tmp_path,capsys):
    existing=tmp_path/'existing'; existing.mkdir()
    marker=existing/'keep'; marker.write_text('original')
    assert main(['demo','--home',str(existing)])==2
    assert marker.read_text()=='original' and list(existing.iterdir())==[marker]


def test_doctor_is_read_only_and_honest_about_missing_bindings(tmp_path):
    home=tmp_path/'not-created'
    report=doctor(home)
    assert not home.exists()
    assert not report['provider_authentication_connected']
    assert not report['operator_authentication_connected']
    assert not report['rendered_browser_qualified']
    assert not report['execution_authorized']
