"""In-process CLI boundary checks under the unchanged release test guard."""
import itertools
import json
from pathlib import Path
import stat

import pytest

from keel_loki.common import atomic_json, clone, digest, load_json
from keel_operational import __main__ as cli
from keel_operational.control import StopLedger


NOW = 1800000000
SOURCE = digest('reviewed-operational-cli-source')
WORKSPACE = 'fixture-workspace'
SERIAL = itertools.count()


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, '_source_inventory', lambda: {'sha256': SOURCE})
    monkeypatch.setattr(cli, '_now', lambda: NOW)


def doc(tmp_path, value):
    target = tmp_path / ('document-%d.json' % next(SERIAL))
    atomic_json(target, value)
    return str(target)


def configuration(command, **fields):
    return {'schema': 'keel.operational.' + command + '-cli.v1',
            'workspace_id': WORKSPACE, **fields}


def invoke(tmp_path, capsys, command, operation, config, arguments=None, *, dispatch=False, output=None):
    output = output or tmp_path / ('output-%d.json' % next(SERIAL))
    flags = [command, '--host-config', config if type(config) is str else doc(tmp_path, config),
             '--input', doc(tmp_path, {'operation': operation, 'arguments': arguments or {}}),
             '--out', str(output)]
    if dispatch:
        flags.append('--dispatch')
    code = cli.main(flags)
    stdout = capsys.readouterr().out
    report = load_json(output) if output.is_file() and not output.is_symlink() else None
    return code, report, stdout


def test_create_reopen_and_private_readonly_status(tmp_path, capsys):
    home = tmp_path / 'control'
    config = configuration('control', control_home=str(home))
    code, report, stdout = invoke(tmp_path, capsys, 'control', 'create', config)
    assert code == 0 and report['source_sha256'] == SOURCE
    assert report['source_unchanged'] and not report['execution_authorized']
    assert json.loads(stdout) == {'command': 'control', 'status': 'OBSERVED', 'exit_code': 0}
    database = home / 'control.sqlite3'
    before = database.read_bytes(), database.stat().st_mtime_ns
    for _ in range(2):
        code, report, _ = invoke(tmp_path, capsys, 'control', 'status', config)
        assert code == 0 and report['result']['canonical_delivery'] == 'NOT_ATTEMPTED'
    assert before == (database.read_bytes(), database.stat().st_mtime_ns)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in tmp_path.glob('output-*.json'))


@pytest.mark.parametrize('argument', ['now', 'principal', 'source_provider', 'host_snapshot',
    'control_gate_provider', 'canonical_sink', 'dispatch', 'expected_report_sha256',
    'authoritative_checkpoint', 'transport_mode', 'home', 'allow_injected'])
def test_request_cannot_override_host_authority(tmp_path, capsys, argument):
    home = tmp_path / 'control'
    code, report, stdout = invoke(tmp_path, capsys, 'control', 'create',
        configuration('control', control_home=str(home)), {argument: 'PRIVATE-INVENTED-AUTHORITY'})
    assert code == 2 and not home.exists()
    assert report['error_code'] == 'command_failed'
    assert 'PRIVATE-INVENTED-AUTHORITY' not in stdout


@pytest.mark.parametrize('kind', ['existing', 'symlink', 'alias'])
def test_output_rejected_before_state_creation(tmp_path, capsys, kind):
    home = tmp_path / 'control'
    output = tmp_path / 'result.json'
    if kind == 'existing':
        atomic_json(output, {'keep': True})
    elif kind == 'symlink':
        output.symlink_to(tmp_path / 'missing')
    else:
        output = home
    code, _, _ = invoke(tmp_path, capsys, 'control', 'create',
        configuration('control', control_home=str(home)), output=output)
    assert code == 2 and not home.is_dir()
    if kind == 'existing':
        assert load_json(output) == {'keep': True}
    elif kind == 'symlink':
        assert output.is_symlink() and not (tmp_path / 'missing').exists()


def test_source_paths_are_not_runtime_or_output(tmp_path, capsys, monkeypatch):
    root = tmp_path / 'code'
    root.mkdir()
    monkeypatch.setattr(cli, 'ROOT', root)
    code, report, _ = invoke(tmp_path, capsys, 'control', 'create',
        configuration('control', control_home=str(root / 'runtime')))
    assert code == 2 and not (root / 'runtime').exists()
    code, report, _ = invoke(tmp_path, capsys, 'control', 'create',
        configuration('control', control_home=str(tmp_path / 'runtime')), output=root / 'report.json')
    assert code == 2 and report is None and not (tmp_path / 'runtime').exists()


def test_source_change_quarantines_result(tmp_path, capsys, monkeypatch):
    pins = iter([{'sha256': SOURCE}, {'sha256': 'b' * 64}])
    monkeypatch.setattr(cli, '_source_inventory', lambda: next(pins))
    code, report, _ = invoke(tmp_path, capsys, 'control', 'create',
        configuration('control', control_home=str(tmp_path / 'control')))
    assert code == 4 and report['status'] == 'QUARANTINED'
    assert not report['source_unchanged'] and not report['result_valid_for_source']


def test_host_configuration_change_quarantines_result(tmp_path, capsys, monkeypatch):
    config = configuration('control', control_home=str(tmp_path / 'control'))
    path = doc(tmp_path, config)
    original = cli._control
    def change(*args):
        result = original(*args)
        Path(path).write_text(json.dumps({**config, 'workspace_id': 'changed'}))
        return result
    monkeypatch.setattr(cli, '_control', change)
    code, report, _ = invoke(tmp_path, capsys, 'control', 'create', path)
    assert code == 4 and report['status'] == 'QUARANTINED'
    assert not report['result_valid_for_source']


def test_wrong_caller_pin_and_duplicate_keys_cannot_mutate(tmp_path, capsys):
    home = tmp_path / 'control'
    config = configuration('control', control_home=str(home))
    code, _, _ = invoke(tmp_path, capsys, 'control', 'create', config,
        {'expected_running_source_sha256': 'b' * 64})
    assert code == 2 and not home.exists()
    request = tmp_path / 'duplicate.json'
    request.write_text('{"operation":"status","operation":"create","arguments":{}}')
    output = tmp_path / 'duplicate-result.json'
    code = cli.main(['control', '--host-config', doc(tmp_path, config),
                     '--input', str(request), '--out', str(output)])
    assert code == 2 and not home.exists()
    capsys.readouterr()


def test_pending_stop_is_visible_and_json_ack_not_exposed(tmp_path, capsys):
    home = tmp_path / 'control'
    ledger = StopLedger.create(home, WORKSPACE, clock=lambda: NOW)
    ledger.record_stop({'schema': 'keel.operational.stop.v1', 'event_id': 'rate-fixture',
        'workspace_id': WORKSPACE, 'kind': 'RATE_429', 'attempt_id': None,
        'account_id': 'account', 'role_id': 'role', 'resource_id': 'form',
        'observation_sha256': digest('fixture-429')})
    config = configuration('control', control_home=str(home))
    code, report, _ = invoke(tmp_path, capsys, 'control', 'pending', config)
    assert code == 3 and report['result']['status'] == 'PARTIAL'
    assert report['result']['canonical_delivery'] == 'NOT_ATTEMPTED'
    assert len(report['result']['pending']) == 1
    code, report, _ = invoke(tmp_path, capsys, 'control', 'status', config)
    assert code == 3 and report['result']['state']['rate_limited']
    before = ledger.snapshot()['checkpoint_sha256']
    code, _, _ = invoke(tmp_path, capsys, 'control', 'ack', config, {'event_id': 'rate-fixture'})
    assert code == 2 and ledger.snapshot()['checkpoint_sha256'] == before


def qualification_config(tmp_path):
    from keel_operational.qualification import fixture_inputs
    inputs = fixture_inputs()
    return inputs, configuration('qualification', **{
        key: doc(tmp_path, inputs[key]) for key in ('manifest', 'tool_schemas', 'host_config')})


def freeze_cli(tmp_path, capsys):
    inputs, shared = qualification_config(tmp_path)
    config = {**shared, 'fixture': doc(tmp_path, inputs['fixture']), 'ttl': 3600,
              'required_operations': inputs['manifest']['operations']}
    code, report, _ = invoke(tmp_path, capsys, 'qualification', 'freeze', config)
    assert code == 0
    plan = report['result']
    assert plan['created_at'] == NOW and plan['fixture']['now'] == NOW
    assert plan['source_sha256'] == SOURCE and not plan['execution_authorized']
    return inputs, shared, plan


def test_qualification_freeze_uses_clock_and_missing_observations_stay_unavailable(tmp_path, capsys):
    inputs, shared, plan = freeze_cli(tmp_path, capsys)
    config = {**shared, 'plan': doc(tmp_path, plan), 'expected_plan_sha256': digest(plan)}
    code, report, _ = invoke(tmp_path, capsys, 'qualification', 'grade', config, {'records': []})
    assert code == 3 and report['result']['status'] == 'UNAVAILABLE'
    assert report['result']['observations'] == 0 and not report['result']['native_browser_authenticated']


def test_qualification_never_rebinds_real_approval_template(tmp_path, capsys):
    inputs, config = qualification_config(tmp_path)
    fixture = {**inputs['fixture'], 'synthetic': False}
    config.update(fixture=doc(tmp_path, fixture), ttl=3600, required_operations=inputs['manifest']['operations'])
    code, report, _ = invoke(tmp_path, capsys, 'qualification', 'freeze', config)
    assert code == 2 and report['error_code'] == 'command_failed'


def test_export_fixture_uses_pinned_plan_and_private_new_home(tmp_path, capsys):
    _inputs, shared, plan = freeze_cli(tmp_path, capsys)
    home = tmp_path / 'rendered-fixture'
    config = {**shared, 'plan': doc(tmp_path, plan), 'expected_plan_sha256': digest(plan),
              'fixture_home': str(home)}
    code, report, _ = invoke(tmp_path, capsys, 'qualification', 'export_fixture', config)
    assert code == 0 and report['result']['synthetic'] is True
    assert not report['result']['served'] and not report['result']['browser_opened']
    assert (home / 'fixture.html').is_file()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in home.iterdir())
    wrong = {**config, 'fixture_home': str(tmp_path / 'bad-fixture'), 'expected_plan_sha256': 'b' * 64}
    code, _, _ = invoke(tmp_path, capsys, 'qualification', 'export_fixture', wrong)
    assert code == 2 and not (tmp_path / 'bad-fixture').exists()


def test_original_tool_bytes_kept_separate_from_normalized_output(tmp_path, capsys):
    import base64
    from keel_muse.browser import InjectedFixture, _request
    inputs, shared, plan = freeze_cli(tmp_path, capsys)
    fixture = InjectedFixture(plan['fixture'])
    request = _request('accessibility_snapshot', fixture.contract)
    normalized = fixture(request)
    raw = tmp_path / 'native-original.txt'
    raw.write_bytes(b'native original syntax, distinct from JSON')
    config = {**shared, 'native_request': doc(tmp_path, request), 'raw_observation': str(raw),
              'normalized_observation': doc(tmp_path, normalized), 'captured_at': NOW}
    code, report, _ = invoke(tmp_path, capsys, 'qualification', 'record', config)
    assert code == 0
    record = report['result']
    assert base64.b64decode(record['raw_base64']) == raw.read_bytes()
    assert record['normalized'] == normalized and record['normalized_sha256'] == digest(normalized)
    assert record['native_tool_name'] == inputs['manifest']['native_tool_names']['accessibility_snapshot']


def qualified_inputs(tmp_path, capsys):
    from keel_muse.browser import InjectedFixture
    from keel_operational import qualification
    inputs, shared, plan = freeze_cli(tmp_path, capsys)
    fixture = InjectedFixture(plan['fixture'])
    kwargs = {key: inputs[key] for key in ('manifest', 'tool_schemas', 'host_config')}
    def capture(request):
        observed = fixture(request)
        return {'raw': b'original fixture observation:' + json.dumps(observed).encode(),
                'normalized': observed,
                'native_tool_name': inputs['manifest']['native_tool_names'][request['operation']]}
    report = qualification.run(tmp_path / ('qualification-%d' % next(SERIAL)), plan,
        expected_plan_sha256=digest(plan), source_sha256=SOURCE, now=NOW, clock=lambda: NOW,
        capture=capture, **kwargs)
    refs = {key: shared[key] for key in ('manifest', 'tool_schemas', 'host_config')}
    refs.update(plan=doc(tmp_path, plan), expected_plan_sha256=digest(plan),
                report=doc(tmp_path, report), expected_report_sha256=digest(report))
    return inputs, refs, plan, report


def test_fixture_qualification_is_not_real_host_authority(tmp_path, capsys):
    inputs, refs, plan, report = qualified_inputs(tmp_path, capsys)
    config = configuration('qualification', **refs,
        required_operations=inputs['manifest']['operations'], allow_injected=False)
    code, check, _ = invoke(tmp_path, capsys, 'qualification', 'verify', config)
    assert code == 3 and check['result']['status'] == 'NOT_QUALIFIED'
    assert not check['result']['admissible'] and not check['result']['native_browser_authenticated']
    code, check, _ = invoke(tmp_path, capsys, 'qualification', 'verify', {**config, 'allow_injected': True})
    assert code == 0 and check['result']['admissible']
    assert not check['result']['execution_authorized']


def runtime_config(tmp_path, capsys):
    from keel_muse.browser import InjectedFixture
    inputs, refs, plan, report = qualified_inputs(tmp_path, capsys)
    fixture = InjectedFixture(plan['fixture'])
    control_home = tmp_path / 'control'
    StopLedger.create(control_home, WORKSPACE, clock=lambda: NOW)
    config = configuration('runtime', runtime_home=str(tmp_path / 'runtime'),
        native_home=str(tmp_path / 'native'), control_home=str(control_home),
        session_id='fixture-session', scope_id='fixture-role', transport_mode='INJECTED', native_gate=None,
        lease_seconds=30, expires_at=None, qualification=refs,
        application=doc(tmp_path, {key: fixture.fixture[key] for key in ('contract', 'values', 'attachments')}),
        host_approvals=doc(tmp_path, fixture.fixture['approvals']),
        host_snapshot=doc(tmp_path, fixture.host_snapshot()))
    gate = {'schema': 'keel.operational.cli-gate.v1', 'workspace_id': WORKSPACE,
        'account_id': fixture.contract['account_id'], 'role_id': config['scope_id'],
        'resource_id': fixture.contract['form_id'], 'source_sha256': SOURCE,
        'consent': True, 'no_ai': False, 'approval_current': True, 'holds': [],
        'unknown_attempt': False, 'rate_limited': False, 'issued_at': NOW, 'expires_at': NOW + 60}
    config['control_gate'] = doc(tmp_path, gate)
    return fixture, config


def test_durable_runtime_reopens_each_step_without_replaying(tmp_path, capsys):
    fixture, config = runtime_config(tmp_path, capsys)
    code, report, stdout = invoke(tmp_path, capsys, 'runtime', 'create', config)
    assert code == 0 and report['result']['status'] == 'READY'
    assert fixture.fixture['plain_values']['full_name'] not in stdout
    calls = 0
    while report['result']['status'] != 'SIMULATED':
        code, proposal, _ = invoke(tmp_path, capsys, 'runtime', 'next', config)
        assert code == 0 and proposal['result']['status'] == 'PROPOSED'
        native = proposal['result']['native']
        code, issued, _ = invoke(tmp_path, capsys, 'runtime', 'next', config,
            {'native_permission': fixture.permission(native['request'])}, dispatch=True)
        assert code == 0 and issued['result']['status'] == 'ISSUED'
        native = issued['result']['native']
        assert native['dispatch_issued_once'] and native['request']
        code, waiting, _ = invoke(tmp_path, capsys, 'runtime', 'next', config, dispatch=True)
        assert code == 3 and waiting['result']['status'] == 'WAITING_OBSERVATION'
        assert waiting['result']['native']['request'] is None
        code, report, _ = invoke(tmp_path, capsys, 'runtime', 'observe', config,
            {'response': fixture(native['request']), 'request_sha256': native['request_sha256']})
        assert code == 0
        calls += 1
        assert calls < 30
    assert calls == 23 and report['result']['native']['actions_reported_applied'] == 11
    ledger = StopLedger(config['control_home'], WORKSPACE, clock=lambda: NOW)
    assert len(ledger.snapshot()['state']['attempts']) == calls
    assert not report['result']['native_permission_authenticated']


@pytest.mark.parametrize('change', ['scope', 'expired', 'hold'])
def test_gate_scope_expiry_and_hold_block_actual_dispatch(tmp_path, capsys, change):
    fixture, config = runtime_config(tmp_path, capsys)
    code, _, _ = invoke(tmp_path, capsys, 'runtime', 'create', config)
    assert code == 0
    code, proposal, _ = invoke(tmp_path, capsys, 'runtime', 'next', config)
    assert code == 0
    gate = load_json(config['control_gate'])
    gate.update({'role_id': 'other-role'} if change == 'scope' else
                {'expires_at': NOW} if change == 'expired' else {'holds': ['dedupe-hold']})
    config['control_gate'] = doc(tmp_path, gate)
    code, blocked, _ = invoke(tmp_path, capsys, 'runtime', 'next', config,
        {'native_permission': fixture.permission(proposal['result']['native']['request'])}, dispatch=True)
    assert code == 3 and blocked['result']['status'] in {'BLOCKED', 'UNKNOWN'}
    assert blocked['result']['native']['dispatch_issued_once'] is False
    assert blocked['result']['native']['request'] is None


def test_runtime_recover_exports_uncertainty_without_canonical_claim(tmp_path, capsys):
    fixture, config = runtime_config(tmp_path, capsys)
    invoke(tmp_path, capsys, 'runtime', 'create', config)
    _, proposal, _ = invoke(tmp_path, capsys, 'runtime', 'next', config)
    _, issued, _ = invoke(tmp_path, capsys, 'runtime', 'next', config,
        {'native_permission': fixture.permission(proposal['result']['native']['request'])}, dispatch=True)
    assert issued['result']['status'] == 'ISSUED'
    code, recovered, _ = invoke(tmp_path, capsys, 'runtime', 'recover', config)
    assert code == 3 and recovered['result']['status'] == 'UNKNOWN'
    assert recovered['result']['canonical_stop_deliveries_pending'] >= 1
    code, pending, _ = invoke(tmp_path, capsys, 'control', 'pending',
        configuration('control', control_home=config['control_home']))
    assert code == 3 and pending['result']['canonical_delivery'] == 'NOT_ATTEMPTED'
    code, again, _ = invoke(tmp_path, capsys, 'runtime', 'next', config, dispatch=True)
    assert code == 3 and again['result']['native']['request'] is None


def recovery_config(tmp_path):
    home = tmp_path / 'control'
    StopLedger.create(home, WORKSPACE, clock=lambda: NOW)
    canonical = tmp_path / 'canonical'
    canonical.mkdir(mode=0o700)
    packet = canonical / 'packet.json'
    atomic_json(packet, {'status': 'needs-human-approval', 'approval': None})
    import hashlib
    inventory = {'schema': 'keel.operational.runtime-inventory.v1', 'workspace_id': WORKSPACE,
        'source_sha256': SOURCE, 'writer_coordination': 'control_barrier',
        'stores': [{'store_id': 'control', 'kind': 'control', 'home': str(home)},
                   {'store_id': 'canonical', 'kind': 'canonical_files', 'home': str(canonical),
                    'files': {'packet.json': hashlib.sha256(packet.read_bytes()).hexdigest()}}],
        'required_store_ids': ['control', 'canonical'], 'canonical_store_ids': ['canonical'],
        'external_state': ['muse-native-service']}
    return configuration('recovery', inventory=doc(tmp_path, inventory), control_home=str(home))


def test_cli_complete_inventory_backup_restores_offline_without_approval(tmp_path, capsys):
    config = recovery_config(tmp_path)
    code, checked, _ = invoke(tmp_path, capsys, 'recovery', 'checkpoint', config)
    assert code == 0
    checkpoint = checked['result']
    authority_path = doc(tmp_path, checkpoint)
    backup_home = tmp_path / 'backup'
    code, saved, _ = invoke(tmp_path, capsys, 'recovery', 'snapshot',
        {**config, 'backup_home': str(backup_home), 'expected_checkpoint': authority_path})
    assert code == 0 and saved['result']['manifest']['configured_state_complete']
    destination = tmp_path / 'restored'
    restore_config = configuration('recovery', backup_home=str(backup_home), destination=str(destination),
        authoritative_checkpoint=authority_path, expected_manifest_sha256=saved['result']['manifest_sha256'])
    code, restored, _ = invoke(tmp_path, capsys, 'recovery', 'restore', restore_config)
    assert code == 0 and not restored['result']['controller_started']
    assert (destination / 'OFFLINE_RESTORE.json').is_file()
    assert load_json(destination / 'canonical' / 'packet.json')['approval'] is None
    assert not restored['result']['external_service_state_restored']


def test_restore_rejects_checkpoint_taken_from_inside_backup_before_creation(tmp_path, capsys):
    backup_home = tmp_path / 'backup'
    backup_home.mkdir(mode=0o700)
    checkpoint = backup_home / 'checkpoint.json'
    atomic_json(checkpoint, {'workspace_id': WORKSPACE, 'source_sha256': SOURCE})
    destination = tmp_path / 'restore'
    config = configuration('recovery', backup_home=str(backup_home), destination=str(destination),
        authoritative_checkpoint=str(checkpoint), expected_manifest_sha256='a' * 64)
    code, _, _ = invoke(tmp_path, capsys, 'recovery', 'restore', config)
    assert code == 2 and not destination.exists()
