from pathlib import Path
import pytest
from keel_muse.demo import run_demo
from tools.run_muse_acceptance import collect_checks


def test_aggregate_runs_every_required_component_and_checks(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    home=tmp_path/'rehearsal'
    demo=run_demo(home)
    expected={'capabilities','sources','context','bridge','browser','coordinator','review','dashboard','evaluation','repair','backup','integration','session','reconciliation'}
    assert set(demo['components'])==expected
    checks=collect_checks(demo,home,source_unchanged=True,network_events=[])
    assert len(checks)>=50 and all(row['status']=='PASS' for row in checks)
    assert len({row['name'] for row in checks})==len(checks)
    assert demo['actual_native_browser']=='NOT_RUN'
    assert demo['account_muse_integration']=='NOT_VERIFIED'
    assert demo['components']['session']['preparation']['issued_requests']==23


def test_adverse_measured_conditions_do_not_become_green(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    home=tmp_path/'rehearsal';demo=run_demo(home)
    demo['components']['repair']['recommendation']['promoted']=True
    demo['components']['session']['recovered_effect_status']='READY'
    checks=collect_checks(demo,home,source_unchanged=False,network_events=['socket.connect'])
    failed={row['name'] for row in checks if row['status']=='FAIL'}
    assert {'repair_recommendation_not_self_promotion','native_session_restart_preserves_uncertain_effect','source_unchanged','no_network_or_process_attempts'}<=failed


def test_demo_rejects_existing_or_source_directory(tmp_path):
    occupied=tmp_path/'occupied';occupied.mkdir()
    with pytest.raises(FileExistsError):run_demo(occupied)
    source=Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError):run_demo(source/'not-a-runtime-directory')
