"""In-process CLI checks under the unchanged network/process/source guard."""
import itertools
import json
from pathlib import Path
import stat

import pytest

from keel_loki.common import atomic_json, digest, load_json
from keel_muse import __main__ as cli
from keel_muse.browser import InjectedFixture
from keel_muse.capabilities import fixture_manifest


SOURCE = digest('cli-reviewed-source')
NOW = 1800000000
SERIAL = itertools.count()


@pytest.fixture(autouse=True)
def controlled_host(tmp_path, monkeypatch):
    # The predecessor source connector uses descriptor-relative attachment
    # mkdir; retain the existing tests' runtime cwd under its unchanged guard.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, '_source_inventory', lambda: {'sha256': SOURCE})
    monkeypatch.setattr(cli, '_now', lambda: NOW)


def document(tmp_path, value):
    path = tmp_path / ('input-%d.json' % next(SERIAL))
    atomic_json(path, value)
    return str(path)


def invoke(tmp_path, capsys, command, operation=None, arguments=None, *, flags=(), output=None):
    output = output or tmp_path / ('output-%d.json' % next(SERIAL))
    argv = [command, '--out', str(output), *map(str, flags)]
    if operation is not None:
        argv += ['--input', document(tmp_path, {'operation': operation, 'arguments': arguments or {}})]
    code = cli.main(argv)
    stdout = capsys.readouterr().out
    return code, load_json(output) if output.is_file() and not output.is_symlink() else None, stdout


def create_session(tmp_path, capsys, *, actual=False, native_gate=True):
    from keel_loki.forms import bind_fixture, demo_fixture
    fixture = InjectedFixture(bind_fixture(demo_fixture(), now=NOW))
    home = tmp_path / ('session-%d' % next(SERIAL))
    flags = ['--home', home, '--workspace-id', 'cli-workspace',
             '--manifest', document(tmp_path, fixture_manifest()),
             '--approvals', document(tmp_path, fixture.fixture['approvals']),
             '--host-snapshot', document(tmp_path, fixture.host_snapshot())]
    if actual:
        if native_gate:
            flags += ['--native-gate', cli.NATIVE_GATE]
    else:
        flags += ['--transport-mode', 'INJECTED']
    arguments = {key: fixture.fixture[key] for key in ('contract', 'values', 'attachments')}
    result = invoke(tmp_path, capsys, 'session', 'create', arguments, flags=flags)
    return home, fixture, result


def session_call(tmp_path, capsys, home, fixture, operation, arguments=None, *, dispatch=False):
    flags = ['--home', home, '--workspace-id', 'cli-workspace']
    if operation in {'next', 'observe'}:
        flags += ['--host-snapshot', document(tmp_path, fixture.host_snapshot())]
    if dispatch:
        flags += ['--dispatch']
    return invoke(tmp_path, capsys, 'session', operation, arguments, flags=flags)


def test_inspection_reports_mapping_gap_and_private_summary(tmp_path, capsys):
    code, report, stdout = invoke(tmp_path, capsys, 'inspect-host')
    assert code == 3 and report['status'] == 'HOST_MAPPING_REQUIRED'
    assert report['source_sha256'] == SOURCE and report['source_unchanged']
    assert json.loads(stdout) == {'command': 'inspect-host', 'exit_code': 3, 'status': 'HOST_MAPPING_REQUIRED'}
    assert not report['execution_authorized'] and not report['task_success_inferred']
    output = next(tmp_path.glob('output-*'))
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_configured_mapping_is_declared_without_tool_probe(tmp_path, capsys):
    code, report, _ = invoke(tmp_path, capsys, 'inspect-host', flags=[
        '--manifest', document(tmp_path, fixture_manifest())])
    assert code == 0
    assert report['result']['native_adapter']['mapping_status'] == 'HOST_MAPPING_CONFIGURED'
    assert report['result']['native_adapter']['tool_calls_probed'] is False
    assert report['actual_native_browser'] == 'NOT_RUN'


@pytest.mark.parametrize('kind', ['existing', 'symlink', 'alias'])
def test_occupied_or_aliased_output_rejected_before_new_home(tmp_path, capsys, kind):
    home = tmp_path / 'new-home'
    output = tmp_path / 'result.json'
    if kind == 'existing':
        atomic_json(output, {'keep': True})
    elif kind == 'symlink':
        output.symlink_to(tmp_path / 'absent')
    else:
        output = home
    code, _, stdout = invoke(tmp_path, capsys, 'demo', flags=['--home', home], output=output)
    assert code == 2 and not home.is_dir()
    assert json.loads(stdout)['status'] == 'ERROR'
    if kind == 'existing':
        assert load_json(output) == {'keep': True}
    elif kind == 'symlink':
        assert output.is_symlink() and not (tmp_path / 'absent').exists()


def test_output_under_source_rejected_before_dispatch(tmp_path, capsys, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    monkeypatch.setattr(cli, 'ROOT', source)
    output = source / 'report.json'
    code, report, _ = invoke(tmp_path, capsys, 'inspect-host', output=output)
    assert code == 2 and report is None and not output.exists()


@pytest.mark.parametrize('name,value', [
    ('now', 1), ('source_sha256', '0' * 64), ('workspace_id', 'elsewhere'),
    ('home', '/tmp/elsewhere'), ('host_approvals', []), ('host_snapshot', {}),
    ('native_gate', cli.NATIVE_GATE), ('transports', {}), ('handler', 'os.system'),
    ('memory', {}), ('root', '/'), ('principal', {'actor_id': 'invented'}),
])
def test_request_cannot_supply_trusted_parameters(tmp_path, capsys, name, value):
    home = tmp_path / 'session'
    code, report, stdout = invoke(tmp_path, capsys, 'session', 'create', {name: value},
        flags=['--home', home, '--workspace-id', 'cli-workspace'])
    assert code == 2 and report['error_code'] == 'command_failed'
    assert not home.exists()
    assert 'invented' not in stdout and '/tmp/elsewhere' not in stdout


def test_source_pin_mismatch_rejected_before_session_creation(tmp_path, capsys):
    home = tmp_path / 'session'
    code, report, _ = invoke(tmp_path, capsys, 'session', 'create',
        {'expected_running_source_sha256': '0' * 64},
        flags=['--home', home, '--workspace-id', 'cli-workspace'])
    assert code == 2 and not home.exists()
    assert report['source_sha256'] == SOURCE


def test_actual_mode_requires_native_gate_without_manufacturing_authority(tmp_path, capsys):
    home, _, (code, report, _) = create_session(tmp_path, capsys, actual=True, native_gate=False)
    assert code == 3 and report['status'] == 'HOST_MAPPING_REQUIRED'
    assert report['error_code'] == 'HOST_MAPPING_REQUIRED_native_permission_gate'
    assert not home.exists()
    home, _, (code, report, _) = create_session(tmp_path, capsys, actual=True)
    assert code == 0 and report['result']['status'] == 'READY'
    assert report['result']['native_permission_authenticated'] is False
    assert report['result']['human_approval_authenticated'] is False


def test_json_session_full_form_reopens_each_step_and_never_reissues(tmp_path, capsys):
    home, fixture, (code, report, stdout) = create_session(tmp_path, capsys)
    assert code == 0 and report['result']['status'] == 'READY'
    assert fixture.fixture['plain_values']['full_name'] not in stdout
    issued_count = 0
    while report['result']['status'] != 'SIMULATED':
        code, proposal, _ = session_call(tmp_path, capsys, home, fixture, 'next')
        assert code == 0 and proposal['result']['status'] == 'PROPOSED'
        code, issued, _ = session_call(tmp_path, capsys, home, fixture, 'next',
            {'native_permission': fixture.permission(proposal['result']['request'])}, dispatch=True)
        assert code == 0 and issued['result']['status'] == 'ISSUED'
        code, waiting, _ = session_call(tmp_path, capsys, home, fixture, 'next', dispatch=True)
        assert code == 3 and waiting['result']['status'] == 'WAITING_OBSERVATION'
        assert waiting['result']['request'] is None
        response = fixture(issued['result']['request'])
        code, report, stdout = session_call(tmp_path, capsys, home, fixture, 'observe', {
            'response': response, 'request_sha256': issued['result']['request_sha256']})
        assert code == 0 and fixture.fixture['plain_values']['full_name'] not in stdout
        issued_count += 1
        assert issued_count < 30
    assert issued_count == 23
    assert report['result']['actions_reported_applied'] == 11
    assert report['result']['execution_authorized'] is False
    assert report['result']['actual_native_browser'] == 'NOT_RUN'
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_session_status_does_not_mutate_database_or_epoch(tmp_path, capsys):
    from keel_muse.session import DB_NAME
    home, fixture, _ = create_session(tmp_path, capsys)
    db = home / DB_NAME
    before = db.read_bytes(), db.stat().st_mtime_ns
    for _ in range(2):
        code, report, _ = session_call(tmp_path, capsys, home, fixture, 'status')
        assert code == 0 and report['result']['status'] == 'READY'
    assert before == (db.read_bytes(), db.stat().st_mtime_ns)


def test_recover_issued_effect_preserves_unknown_and_no_blind_retry(tmp_path, capsys):
    home, fixture, _ = create_session(tmp_path, capsys)
    # First observation establishes an accessibility snapshot.
    _, proposal, _ = session_call(tmp_path, capsys, home, fixture, 'next')
    _, issued, _ = session_call(tmp_path, capsys, home, fixture, 'next',
        {'native_permission': fixture.permission(proposal['result']['request'])}, dispatch=True)
    session_call(tmp_path, capsys, home, fixture, 'observe', {
        'response': fixture(issued['result']['request']), 'request_sha256': issued['result']['request_sha256']})
    _, proposal, _ = session_call(tmp_path, capsys, home, fixture, 'next')
    _, issued, _ = session_call(tmp_path, capsys, home, fixture, 'next',
        {'native_permission': fixture.permission(proposal['result']['request'])}, dispatch=True)
    assert issued['result']['request']['operation'] != 'accessibility_snapshot'
    code, report, _ = session_call(tmp_path, capsys, home, fixture, 'recover')
    assert code == 3 and report['result']['status'] == 'UNKNOWN'
    code, report, _ = session_call(tmp_path, capsys, home, fixture, 'next', dispatch=True)
    assert code == 3 and report['result']['request'] is None


def test_changed_running_source_blocks_existing_session(tmp_path, capsys, monkeypatch):
    home, fixture, _ = create_session(tmp_path, capsys)
    monkeypatch.setattr(cli, '_source_inventory', lambda: {'sha256': 'b' * 64})
    code, report, _ = session_call(tmp_path, capsys, home, fixture, 'next')
    assert code == 3 and report['result']['status'] == 'BLOCKED'
    assert report['result']['request'] is None


def test_inventory_change_quarantines_completed_result(tmp_path, capsys, monkeypatch):
    values = iter([{'sha256': SOURCE}, {'sha256': 'b' * 64}])
    monkeypatch.setattr(cli, '_source_inventory', lambda: next(values))
    code, report, stdout = invoke(tmp_path, capsys, 'inspect-host', flags=[
        '--manifest', document(tmp_path, fixture_manifest())])
    assert code == 4 and report['status'] == 'QUARANTINED'
    assert report['source_unchanged'] is False and report['result_valid_for_source'] is False
    assert json.loads(stdout)['exit_code'] == 4


def test_inventory_disappears_after_operation_fails_closed(tmp_path, capsys, monkeypatch):
    count = 0
    def inventory():
        nonlocal count
        count += 1
        if count > 1:
            raise ValueError('private-source-path')
        return {'sha256': SOURCE}
    monkeypatch.setattr(cli, '_source_inventory', inventory)
    code, report, stdout = invoke(tmp_path, capsys, 'inspect-host')
    assert code == 4 and report['status'] == 'QUARANTINED'
    assert 'private-source-path' not in stdout


def bridge_request(operation, arguments=None):
    return {'schema': 'keel.muse.bridge-request.v1', 'request_id': 'request',
            'workspace_id': 'cli-workspace', 'operation': operation,
            'expected_source_sha256': SOURCE, 'expected_state_sha256': None,
            'expires_at': NOW + 20, 'arguments': arguments or {}}


def test_bridge_recorded_mapping_blocker_returns_nonzero(tmp_path, capsys):
    request = bridge_request('capabilities.probe')
    code, report, _ = invoke(tmp_path, capsys, 'bridge', flags=[
        '--workspace-id', 'cli-workspace', '--input', document(tmp_path, request)])
    assert code == 3 and report['result']['status'] == 'RECORDED'
    assert report['status'] == 'HOST_MAPPING_REQUIRED'
    assert report['task_success_inferred'] is False


def test_bridge_registry_cannot_load_handler(tmp_path, capsys):
    request = bridge_request('os.system', {'command': 'secret-command'})
    code, report, stdout = invoke(tmp_path, capsys, 'bridge', flags=[
        '--workspace-id', 'cli-workspace', '--input', document(tmp_path, request)])
    assert code == 2 and report['error_type'] == 'MuseError'
    assert 'secret-command' not in stdout


def test_real_forms_plan_uses_host_approvals_and_keeps_values_private(tmp_path, capsys):
    from keel_loki.forms import bind_fixture, demo_fixture
    fixture = bind_fixture(demo_fixture(), now=NOW)
    code, report, stdout = invoke(tmp_path, capsys, 'forms-plan', 'build_plan',
        {key: fixture[key] for key in ('contract', 'values')},
        flags=['--approvals', document(tmp_path, fixture['approvals'])])
    assert code == 0 and sum(row['state'] == 'prepared' for row in report['result']['fields']) == 11
    assert fixture['plain_values']['full_name'] not in stdout


def test_coordinator_cli_is_readonly_and_disallows_worker_operations(tmp_path, capsys):
    from keel_muse.coordinator import Coordinator
    home = tmp_path / 'coordinator'
    coordinator = Coordinator(home, 'cli-workspace', {'review': lambda _: {}}, lambda _: {}, clock=lambda: NOW)
    before = coordinator.snapshot()
    flags = ['--home', home, '--workspace-id', 'cli-workspace']
    code, report, _ = invoke(tmp_path, capsys, 'coordinator', 'snapshot', flags=flags)
    assert code == 0 and report['result'] == before
    assert coordinator.snapshot() == before
    code, _, _ = invoke(tmp_path, capsys, 'coordinator', 'worker_once', {'worker_id': 'worker'}, flags=flags)
    assert code == 2 and coordinator.snapshot() == before


def test_sources_capture_reads_actual_files_and_keeps_approval_absent(tmp_path, capsys, monkeypatch):
    from keel_muse.sources import make_demo_inputs
    from keel_agent.revisions import PREAPPROVAL_COMPONENTS
    host = make_demo_inputs(tmp_path / 'producer')
    monkeypatch.setattr(cli, '_now', lambda: int(host['clock']().timestamp()))
    config = {'store_home': str(host['store'].home), 'source_root': str(host['root']),
              'scope': host['scope'], 'producer_components': {'synthetic-file-producer': list(PREAPPROVAL_COMPONENTS)}}
    flags = ['--host-config', document(tmp_path, config), '--workspace-id', host['scope']['workspace_id']]
    code, report, _ = invoke(tmp_path, capsys, 'sources', 'head', flags=flags)
    assert code == 0 and report['result']['head_sha256'] == host['expected_head_sha256']
    arguments = {'bindings': host['bindings'], 'expected_bindings_sha256': digest(host['bindings']),
                 'flow': host['flow'], 'expected_flow_sha256': digest(host['flow']),
                 'expected_head_sha256': host['expected_head_sha256']}
    code, report, _ = invoke(tmp_path, capsys, 'sources', 'capture', arguments, flags=flags)
    assert code == 3 and report['status'] == 'PARTIAL'
    assert report['result']['missing_families'] == ['approval']
    assert report['result']['approval_decisions_recorded'] == 0
    _, head, _ = invoke(tmp_path, capsys, 'sources', 'head', flags=flags)
    assert head['result']['head']['record_presence']['answers']
    assert not head['result']['head']['record_presence']['approval']


def test_sources_cli_cannot_provision_store_or_scope(tmp_path, capsys):
    config = {'store_home': str(tmp_path / 'absent'), 'source_root': str(tmp_path),
              'scope': {'workspace_id': 'cli-workspace', 'role_id': 'role', 'application_id': 'application', 'action': 'PREPARE'},
              'producer_components': {}}
    code, _, _ = invoke(tmp_path, capsys, 'sources', 'head', flags=[
        '--host-config', document(tmp_path, config), '--workspace-id', 'cli-workspace'])
    assert code == 2 and not (tmp_path / 'absent').exists()


def test_context_uses_live_store_head_instead_of_json_authority(tmp_path, capsys, monkeypatch):
    from keel_muse.context import make_demo_inputs
    from keel_muse.sources import make_demo_inputs as make_sources
    inputs = make_demo_inputs(tmp_path / 'context')
    host = make_sources(tmp_path / 'sources')
    monkeypatch.setattr(cli, '_now', lambda: int(host['clock']().timestamp()))
    config = {'store_home': str(host['store'].home), 'source_root': str(inputs['kwargs']['root']),
              'scope': host['scope'], 'mandatory_policy': inputs['kwargs']['mandatory_policy']}
    arguments = {'contract': inputs['contract'], 'support': inputs['support'],
                 **{key: value for key, value in inputs['kwargs'].items() if key.startswith('expected_')}}
    code, report, stdout = invoke(tmp_path, capsys, 'context', 'compile', arguments, flags=[
        '--host-config', document(tmp_path, config), '--workspace-id', host['scope']['workspace_id']])
    assert code == 2 and report['result_valid_for_source'] is False
    assert inputs['fixture']['plain_values']['full_name'] not in stdout


def evaluation_data():
    from keel_loki.forms import demo_fixture
    return {'schema': 'keel.muse.e2e-dataset.v1', 'dataset_id': 'cli-dataset',
            'synthetic': True, 'split': 'held_out', 'cases': [
                {'case_id': 'complete', 'fixture': demo_fixture(), 'expected_outcome': 'PREPARED'}]}


@pytest.mark.parametrize('adapter,expected', [('fixture', 0), ('external', 3)])
def test_evaluation_pins_actual_source_and_unavailable_stays_incomplete(tmp_path, capsys, adapter, expected):
    dataset = evaluation_data()
    systems = [{'system_id': 'system', 'adapter': adapter, 'config_sha256': digest(adapter)}]
    code, frozen, _ = invoke(tmp_path, capsys, 'evaluation', 'freeze_plan',
        {'dataset': dataset, 'systems': systems, 'trials': 1})
    assert code == 0 and frozen['result']['source_sha256'] == SOURCE
    plan = frozen['result']
    args = {'plan': plan, 'dataset': dataset, 'expected_plan_sha256': digest(plan)}
    code, run, _ = invoke(tmp_path, capsys, 'evaluation', 'run', args)
    assert code == expected and run['result']['model_calls'] == 0
    code, compared, _ = invoke(tmp_path, capsys, 'evaluation', 'compare',
        {'report': run['result'], 'dataset': dataset, 'expected_plan_sha256': digest(plan)})
    assert code == expected and compared['result']['competitive_superiority_established'] is False


def test_dashboard_preflights_html_before_any_write(tmp_path, capsys):
    from keel_muse.review import example_snapshot, project_review
    snapshot = example_snapshot()
    report = project_review(snapshot, now=snapshot['captured_at'])
    html = tmp_path / 'review.html'
    html.write_text('keep')
    code, _, _ = invoke(tmp_path, capsys, 'dashboard', 'render', {'report': report}, flags=['--html', html])
    assert code == 2 and html.read_text() == 'keep'
    output = tmp_path / 'new.html'
    code, result, stdout = invoke(tmp_path, capsys, 'dashboard', 'render', {'report': report}, flags=['--html', output])
    assert code == 0 and result['result']['network_requests'] == 0
    assert output.exists() and '<html' in output.read_text().lower()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert '<html' not in stdout


def test_backup_roundtrip_retains_429_and_refuses_json_path_override(tmp_path, capsys):
    from keel_loki.recovery import RecoveryJournal
    home = tmp_path / 'recovery'
    journal = RecoveryJournal(home, 'cli-workspace', now=NOW)
    journal.record_429(now=NOW)
    before = journal.snapshot()
    flags = ['--home', home, '--workspace-id', 'cli-workspace']
    code, checkpoint, _ = invoke(tmp_path, capsys, 'backup', 'checkpoint', flags=flags)
    assert code == 0 and journal.snapshot() == before
    backup = tmp_path / 'backup'
    code, captured, _ = invoke(tmp_path, capsys, 'backup', 'snapshot',
        {'expected_checkpoint': checkpoint['result']}, flags=[*flags, '--backup-home', backup])
    assert code == 0
    destination = tmp_path / 'restored'
    code, restored, _ = invoke(tmp_path, capsys, 'backup', 'restore',
        {'authoritative_checkpoint': checkpoint['result'], 'expected_manifest_sha256': captured['result']['manifest_sha256']},
        flags=['--workspace-id', 'cli-workspace', '--backup-home', backup, '--destination', destination])
    assert code == 0 and RecoveryJournal.open_readonly(destination, 'cli-workspace').snapshot() == before
    code, _, _ = invoke(tmp_path, capsys, 'backup', 'restore', {'destination': str(tmp_path / 'injected')},
        flags=['--workspace-id', 'cli-workspace', '--backup-home', backup, '--destination', tmp_path / 'unused'])
    assert code == 2 and not (tmp_path / 'injected').exists()


def test_reconciliation_reads_bound_bytes_without_clearing_unknown(tmp_path, capsys, monkeypatch):
    from keel_muse.reconciliation import make_demo_inputs
    host = make_demo_inputs(tmp_path / 'receipt-fixture')
    monkeypatch.setattr(cli, '_now', lambda: 107)
    config = {'source_root': str(host['kwargs']['root']), 'expected_target': host['kwargs']['expected_target']}
    before = host['journal'].snapshot()
    code, report, _ = invoke(tmp_path, capsys, 'reconciliation', 'propose_receipt',
        {'binding': host['binding'], 'expected_binding_sha256': host['kwargs']['expected_binding_sha256'],
         'expected_checkpoint_sha256': host['kwargs']['expected_checkpoint_sha256']},
        flags=['--home', host['home'] / 'recovery', '--workspace-id', host['journal'].workspace_id,
               '--host-config', document(tmp_path, config)])
    assert code == 0 and report['result']['status'] == 'REVIEW_ONLY'
    assert report['result']['journal_unchanged'] and not report['result']['unknown_cleared']
    assert host['journal'].snapshot() == before


def test_help_documents_json_session_and_fixed_command_surface(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(['--help'])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    assert all(name in text for name in ('session', 'sources', 'context', 'coordinator', 'reconciliation'))
    with pytest.raises(SystemExit):
        cli.main(['session', '--help'])
    text = capsys.readouterr().out
    assert '--input' in text and '--dispatch' in text and '--host-snapshot' in text
