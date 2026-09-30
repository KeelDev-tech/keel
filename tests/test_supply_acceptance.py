"""Grade complete supply paths and prove that broken evidence fails release."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import pytest
from tools.run_supply_acceptance import ROOT, collect_checks, run_acceptance

@pytest.fixture
def measured(tmp_path):
    return run_acceptance(tmp_path/'rehearsal',prohibited_events=[])


def test_complete_fixture_checks_actual_postconditions(measured):
    assert measured['status']=='PASS' and measured['checks_total']==23
    assert measured['checks_passed']==23 and measured['prohibited_events']==[]
    assert measured['synthetic'] and not measured['live_muse_verified']
    assert not measured['production_supply_recovery_verified']
    assert len({c['name'] for c in measured['checks']})==23
    assert measured['evidence']['preparation']['verification']['requests']==1
    assert measured['evidence']['preparation']['window']['observed_prepared_gains']==1


@pytest.mark.parametrize('mutation,expected', [
    ('promotion','posting_check_does_not_promote_ready'),
    ('hold','protected_row_byte_equivalent'),
    ('cost','actual_dispatch_cost_counted'),
    ('replay','interrupted_run_cannot_replay'),
    ('authority','launch_authority_remains_unknown'),
])
def test_grader_rejects_incorrect_final_evidence(measured,mutation,expected):
    e=copy.deepcopy(measured['evidence']);p=e['preparation']
    if mutation=='promotion':p['row']['status']='READY'
    elif mutation=='hold':p['held_after']['d1_office_exclusion']=False
    elif mutation=='cost':p['window']['reader_dispatches']=0
    elif mutation=='replay':e['interrupted']['replay_blocked']=False
    elif mutation=='authority':p['after']['launchable_ready']=1
    checks=collect_checks(e,source_unchanged=True,prohibited_events=[])
    assert next(c for c in checks if c['name']==expected)['status']=='FAIL'


def test_source_or_attempted_network_cannot_pass(measured):
    checks=collect_checks(measured['evidence'],source_unchanged=False,prohibited_events=['socket.connect'])
    failed={c['name'] for c in checks if c['status']=='FAIL'}
    assert {'release_source_unchanged','no_network_or_child_process_attempts'}<=failed


def test_rejects_source_and_existing_output(tmp_path):
    with pytest.raises(ValueError):run_acceptance(ROOT/'forbidden-runtime',prohibited_events=[])
    with pytest.raises(FileExistsError):run_acceptance(tmp_path,prohibited_events=[])


def test_cli_runs_offline_without_installed_packages(tmp_path):
    result=subprocess.run([sys.executable,'-S',str(ROOT/'tools/run_supply_acceptance.py'),
        '--out',str(tmp_path/'output')],cwd=tmp_path,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    report=json.loads((tmp_path/'output/acceptance.json').read_text())
    assert report['status']=='PASS' and not report['prohibited_events']
    assert json.loads(result.stdout)['release_source_sha256']==report['release_source_sha256']


def test_caught_network_attempt_still_fails_cli_release(tmp_path):
    program = '''
import sys, socket
sys.path.insert(0, sys.argv[1])
from tools import run_supply_acceptance as runner
original = runner.reader
def probing_reader(fetch):
    def probe(*args):
        try: socket.socket()
        except RuntimeError: pass
        return fetch(*args)
    return original(probe)
runner.reader = probing_reader
raise SystemExit(runner.main(['--out',sys.argv[2]]))
'''
    result=subprocess.run([sys.executable,'-S','-c',program,str(ROOT),str(tmp_path/'guarded')],
        cwd=tmp_path,capture_output=True,text=True,timeout=20)
    assert result.returncode==1,result.stdout+result.stderr
    report=json.loads((tmp_path/'guarded/acceptance.json').read_text())
    assert report['status']=='FAIL' and 'socket.__new__' in report['prohibited_events']
    assert next(c for c in report['checks'] if c['name']=='no_network_or_child_process_attempts')['status']=='FAIL'
