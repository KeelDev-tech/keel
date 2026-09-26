"""Private, fixed-operation CLI for a trusted Keel host installation.

The operator supplies paths, scope, observations and pins through --host-config.
The request document selects an operation and carries untrusted observations.
This distinction is an installation contract, not protection from the same OS
account rewriting either file. No native tool, model or canonical sink is called
here. A returned issuance still requires the host's real native permission gate.
"""
import argparse
import json
from pathlib import Path
import time

from .common import (FLAGS, OperationalError, atomic_json, clone, digest, load_json,
                     outside_source, require_dict, require_hash, require_id,
                     require_int)


ROOT = Path(__file__).resolve().parents[1]
TRUSTED_ARGUMENTS = {
    'now', 'clock', 'captured_at', 'source_sha256', 'source_provider', 'workspace_id',
    'home', 'native_home', 'runtime_home', 'control_home', 'backup_home', 'destination',
    'root', 'source_root', 'host_config', 'qualification_provider', 'manifest',
    'tool_schemas', 'host_snapshot', 'host_snapshot_provider', 'control_gate_provider',
    'host_approvals', 'principal', 'native_gate', 'transport_mode', 'dispatch',
    'canonical_sink', 'sink', 'handler', 'handlers', 'transport', 'transports',
    'capture', 'ledger', 'control', 'expected_plan_sha256', 'expected_report_sha256',
    'expected_manifest_sha256', 'authoritative_checkpoint', 'expected_checkpoint',
    'required_operations', 'allow_injected', 'fixture', 'session_id', 'scope_id',
}
QUALIFICATION_REFS = {'manifest', 'tool_schemas', 'host_config', 'plan',
                      'expected_plan_sha256', 'report', 'expected_report_sha256'}


def _now():
    return int(time.time())


def _source_inventory():
    from tools.operational_inventory import inventory
    return inventory(ROOT)


def _path(value):
    if type(value) is not str or not Path(value).is_absolute():
        raise OperationalError('absolute_operator_path_required')
    return outside_source(value, ROOT)


def _new_output(value, *, report_path=None):
    path = _path(str(value))
    if path.exists() or path.is_symlink():
        raise OperationalError('output_already_exists')
    if report_path is not None and path == report_path:
        raise OperationalError('distinct_output_paths_required')
    return path


def _read(value):
    return load_json(_path(value))


class HostConfig:
    """Pin the selected operator configuration for this one invocation."""
    def __init__(self, path, command):
        self.path = _path(path)
        self.value = load_json(self.path)
        if type(self.value) is not dict:
            raise OperationalError('host_configuration_object_required')
        if self.value.get('schema') != 'keel.operational.' + command + '-cli.v1':
            raise OperationalError('host_configuration_schema_invalid')
        require_id(self.value.get('workspace_id'))
        self.pin = digest(self.value)

    def check(self):
        if digest(load_json(self.path)) != self.pin:
            raise OperationalError('operator_configuration_changed')

    def exact(self, fields):
        require_dict(self.value, {'schema', 'workspace_id'} | set(fields))
        self.check()
        return self.value

    def read(self, key):
        self.check()
        return _read(self.value[key])


def _request(value, source):
    require_dict(value, {'operation', 'arguments'})
    require_id(value['operation'])
    arguments = clone(value['arguments'])
    if type(arguments) is not dict:
        raise OperationalError('request_arguments_object_required')
    if 'expected_running_source_sha256' in arguments:
        expected = require_hash(arguments.pop('expected_running_source_sha256'))
        if expected != source:
            raise OperationalError('running_source_changed')
    if set(arguments) & TRUSTED_ARGUMENTS:
        raise OperationalError('trusted_parameter_in_request')
    return value['operation'], arguments


def _ledger(config):
    from .control import StopLedger
    return StopLedger(_path(config['control_home']), config['workspace_id'], clock=_now)


def _control(host, operation, arguments, output):
    from .control import StopLedger
    config = host.exact({'control_home'})
    require_dict(arguments, set())
    if operation == 'create':
        home = _new_output(config['control_home'], report_path=output)
        return StopLedger.create(home, config['workspace_id'], clock=_now).snapshot()
    if operation not in {'status', 'recover', 'pending'}:
        raise OperationalError('operation_not_allowed')
    ledger = _ledger(config)
    if operation == 'recover':
        recovered = ledger.recover()
        snapshot = ledger.snapshot()
        state = snapshot['state']
        blocked = state['rate_limited'] or bool(state['holds'])
        return {'schema': 'keel.operational.control-recovery.v1',
                'status': 'BLOCKED' if blocked else recovered['status'],
                'recovery': recovered, 'snapshot': snapshot,
                'canonical_delivery': 'NOT_ATTEMPTED', 'execution_authorized': False}
    snapshot = ledger.snapshot()
    if operation == 'pending':
        pending = [row for row in snapshot['state']['outbox'].values()
                   if row['status'] != 'ACKNOWLEDGED']
        return {'schema': 'keel.operational.pending-outbox.v1',
                'status': 'PARTIAL' if pending else 'OBSERVED', 'pending': pending,
                'checkpoint_sha256': snapshot['checkpoint_sha256'],
                'canonical_delivery': 'NOT_ATTEMPTED', 'execution_authorized': False}
    state = snapshot['state']
    return {**snapshot, 'status': 'BLOCKED' if state['rate_limited'] or state['holds'] else 'OBSERVED',
            'canonical_delivery': 'NOT_ATTEMPTED'}


def _qualification_bundle(host, refs):
    require_dict(refs, QUALIFICATION_REFS)
    host.check()
    return {key: require_hash(refs[key]) if key.startswith('expected_') else _read(refs[key])
            for key in QUALIFICATION_REFS}


def _gate_provider(host, config):
    """Rebind an exact scoped host observation, preserving its original times."""
    def provide(binding):
        from .control import attempt_document
        binding = attempt_document(binding)
        host.check()
        gate = _read(config['control_gate'])
        require_dict(gate, {'schema', 'workspace_id', 'account_id', 'role_id', 'resource_id',
                           'source_sha256', 'consent', 'no_ai', 'approval_current', 'holds',
                           'unknown_attempt', 'rate_limited', 'issued_at', 'expires_at'})
        if gate['schema'] != 'keel.operational.cli-gate.v1':
            raise OperationalError('host_gate_schema_invalid')
        for key in ('workspace_id', 'account_id', 'role_id', 'resource_id', 'source_sha256'):
            if gate[key] != binding[key]:
                raise OperationalError('host_gate_scope_or_source_mismatch')
        if gate['workspace_id'] != config['workspace_id'] or gate['role_id'] != config['scope_id']:
            raise OperationalError('host_gate_configuration_mismatch')
        return {'schema': 'keel.operational.host-snapshot.v1',
                'workspace_id': gate['workspace_id'], 'attempt_sha256': digest(binding),
                **{key: gate[key] for key in ('source_sha256', 'consent', 'no_ai',
                    'approval_current', 'holds', 'unknown_attempt', 'rate_limited',
                    'issued_at', 'expires_at')}}
    return provide


def _runtime(host, operation, arguments, source, output, dispatch):
    from .runtime import OperationalSession
    config = host.exact({'runtime_home', 'native_home', 'control_home', 'session_id', 'scope_id',
                         'transport_mode', 'native_gate', 'lease_seconds', 'expires_at',
                         'application', 'host_approvals', 'host_snapshot', 'control_gate',
                         'qualification'})
    if operation not in {'create', 'status', 'next', 'observe', 'recover'}:
        raise OperationalError('operation_not_allowed')
    if dispatch and operation != 'next':
        raise OperationalError('dispatch_only_for_next')
    expected = {'response', 'request_sha256'} if operation == 'observe' else set()
    if operation == 'next' and 'native_permission' in arguments:
        expected = {'native_permission'}
    require_dict(arguments, expected)
    if operation == 'next' and arguments and config['transport_mode'] != 'INJECTED':
        raise OperationalError('real_native_permission_not_accepted_from_json')
    home, native_home = _path(config['runtime_home']), _path(config['native_home'])
    control_home = _path(config['control_home'])
    paths = [home, native_home, control_home]
    if any(a == b or a in b.parents or b in a.parents
           for index, a in enumerate(paths) for b in paths[index + 1:]):
        raise OperationalError('separate_runtime_store_roots_required')
    if operation == 'create':
        _new_output(home, report_path=output)
        _new_output(native_home, report_path=output)
    ledger = _ledger(config)
    callbacks = {
        'qualification_provider': lambda: _qualification_bundle(host, config['qualification']),
        'source_provider': lambda: require_hash(_source_inventory()['sha256']),
        'host_snapshot_provider': lambda: host.read('host_snapshot'),
        'control_gate_provider': _gate_provider(host, config),
        'clock': _now,
    }
    if operation == 'create':
        application = host.read('application')
        require_dict(application, {'contract', 'values', 'attachments'})
        session = OperationalSession.create(home, native_home=native_home, ledger=ledger,
            workspace_id=config['workspace_id'], session_id=config['session_id'], scope_id=config['scope_id'],
            host_approvals=host.read('host_approvals'), **application,
            transport_mode=config['transport_mode'], native_gate=config['native_gate'],
            lease_seconds=config['lease_seconds'], expires_at=config['expires_at'], **callbacks)
        return session.status()
    session = OperationalSession(home, config['workspace_id'], native_home=native_home, ledger=ledger, **callbacks)
    if operation == 'status':
        return session.status()
    if operation == 'recover':
        return session.recover()
    if operation == 'next':
        return session.next_request(dispatch=dispatch, **arguments)
    return session.observe(**arguments)


def _qualification(host, operation, arguments, source, output):
    from . import qualification
    shared = {'manifest', 'tool_schemas', 'host_config'}
    fields = {
        'freeze': shared | {'fixture', 'ttl', 'required_operations'},
        'record': shared | {'native_request', 'raw_observation', 'normalized_observation', 'captured_at'},
        'grade': shared | {'plan', 'expected_plan_sha256'},
        'verify': shared | {'plan', 'expected_plan_sha256', 'report', 'expected_report_sha256',
                            'required_operations', 'allow_injected'},
        'export_fixture': shared | {'plan', 'expected_plan_sha256', 'fixture_home'},
    }
    if operation not in fields:
        raise OperationalError('operation_not_allowed')
    config = host.exact(fields[operation])
    require_dict(arguments, {'records'} if operation == 'grade' else set())
    trusted = {key: host.read(key) for key in shared}
    if operation == 'freeze':
        from keel_loki.forms import bind_fixture
        # Only the explicit synthetic template is rebound. This operation never
        # provisions approval records for an application or a real principal.
        fixture = host.read('fixture')
        if fixture.get('synthetic') is not True:
            raise OperationalError('synthetic_qualification_fixture_required')
        now = _now()
        fixture = bind_fixture(fixture, now=now)
        return qualification.freeze_plan(fixture, source_sha256=source, now=now,
            ttl=config['ttl'], required_operations=config['required_operations'], **trusted)
    if operation == 'record':
        from tools.operational_inventory import read_file
        from keel_muse.capabilities import validate_manifest
        request = host.read('native_request')
        manifest = validate_manifest(trusted['manifest'])
        tool_name = manifest['native_tool_names'].get(request.get('operation'))
        if tool_name is None:
            raise OperationalError('undeclared_native_operation')
        captured_at = require_int(config['captured_at'])
        if captured_at > _now():
            raise OperationalError('future_capture_time')
        raw_path = _path(config['raw_observation'])
        raw = read_file(raw_path.parent, raw_path.name)
        return qualification.record_observation(request, raw, host.read('normalized_observation'),
                    captured_at=captured_at, native_tool_name=tool_name)
    plan = host.read('plan')
    plan_pin = require_hash(config['expected_plan_sha256'])
    if operation == 'export_fixture':
        from .fixture import export_fixture
        # Validate current source/config/time before publishing a runnable fixture.
        qualification._pinned(plan, plan_pin, source_sha256=source, now=_now(), **trusted)
        return export_fixture(_new_output(config['fixture_home'], report_path=output),
                              plan, expected_plan_sha256=plan_pin)
    common = {'plan': plan, 'expected_plan_sha256': plan_pin,
              'source_sha256': source, 'now': _now(), **trusted}
    if operation == 'grade':
        return qualification.grade(records=arguments['records'], **common)
    return qualification.verify(host.read('report'),
        expected_report_sha256=require_hash(config['expected_report_sha256']),
        required_operations=config['required_operations'], allow_injected=config['allow_injected'], **common)


def _recovery(host, operation, arguments, source, output):
    from . import recovery
    require_dict(arguments, set())
    fields = {
        'checkpoint': {'inventory', 'control_home'},
        'snapshot': {'inventory', 'control_home', 'backup_home', 'expected_checkpoint'},
        'restore': {'backup_home', 'destination', 'authoritative_checkpoint', 'expected_manifest_sha256'},
    }
    if operation not in fields:
        raise OperationalError('operation_not_allowed')
    config = host.exact(fields[operation])
    if operation == 'restore':
        backup_home = _path(config['backup_home'])
        destination = _new_output(config['destination'], report_path=output)
        authority_path = _path(config['authoritative_checkpoint'])
        if backup_home == authority_path or backup_home in authority_path.parents:
            raise OperationalError('independently_retained_checkpoint_required')
        authority = load_json(authority_path)
        if authority.get('workspace_id') != config['workspace_id'] or authority.get('source_sha256') != source:
            raise OperationalError('restore_checkpoint_host_or_source_mismatch')
        return recovery.restore(backup_home, destination, authoritative_checkpoint=authority,
                                expected_manifest_sha256=require_hash(config['expected_manifest_sha256']))
    inventory = recovery.validate_inventory(host.read('inventory'))
    if inventory['workspace_id'] != config['workspace_id'] or inventory['source_sha256'] != source:
        raise OperationalError('recovery_inventory_host_or_source_mismatch')
    for spec in inventory['stores']:
        _path(spec['home'])
    ledger = _ledger(config)
    # The recovery module owns the EXCLUSIVE maintenance barrier. Wrapping it in
    # writer() would deadlock and undermine the checkpoint contract.
    if operation == 'checkpoint':
        return recovery.checkpoint(inventory, control=ledger)
    return recovery.snapshot(inventory, _new_output(config['backup_home'], report_path=output),
                             control=ledger, expected_checkpoint=host.read('expected_checkpoint'))


def _outcome(value):
    if type(value) is not dict:
        return 'ERROR', 2
    status = value.get('status', 'OBSERVED')
    if type(status) is not str:
        return 'ERROR', 2
    failures = {'BLOCKED', 'UNKNOWN', 'PARTIAL', 'QUARANTINED', 'UNAVAILABLE',
                'NOT_QUALIFIED', 'HOST_MAPPING_REQUIRED', 'ERROR', 'FAIL', 'FAILED'}
    if status in failures or status.startswith('WAIT'):
        return status, 3
    return status, 0


def parser():
    result = argparse.ArgumentParser(prog='python -m keel_operational',
        description='Fixed local host integration; no native tool or canonical sink dispatch.',
        epilog='Input: {operation,arguments}. Private output must be new and outside source. '
               'Exit 0 recorded/ready, 2 invalid/error, 3 blocked/incomplete, 4 source/config quarantine.')
    commands = result.add_subparsers(dest='command', required=True)
    descriptions = {
        'control': 'create/status/recover/pending; no caller acknowledgements or hold clearing.',
        'runtime': 'create/status/next/observe/recover a qualified durable native session.',
        'qualification': 'freeze/record/grade/verify/export_fixture; original evidence, no browser calls.',
        'recovery': 'checkpoint/snapshot/restore explicitly inventoried private state.',
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument('--host-config', required=True, help='Trusted operator configuration outside source.')
        command.add_argument('--input', required=True, help='Bounded strict JSON operation/arguments.')
        command.add_argument('--out', required=True, help='New private JSON report outside source.')
        if name == 'runtime':
            command.add_argument('--dispatch', action='store_true',
                help='For next only: persist issuance once; host invokes the native tool separately.')
    return result


def main(argv=None):
    options = parser().parse_args(argv)
    output = before = host = None
    status, code = 'ERROR', 2
    operation = options.command
    try:
        output = _new_output(options.out)
        before = _source_inventory()
        source = require_hash(before['sha256'])
        host = HostConfig(options.host_config, options.command)
        operation, arguments = _request(load_json(options.input), source)
        if options.command == 'control':
            result = _control(host, operation, arguments, output)
        elif options.command == 'runtime':
            result = _runtime(host, operation, arguments, source, output, options.dispatch)
        elif options.command == 'qualification':
            result = _qualification(host, operation, arguments, source, output)
        else:
            result = _recovery(host, operation, arguments, source, output)
        host.check()
        unchanged = source == require_hash(_source_inventory()['sha256'])
        status, code = _outcome(result) if unchanged else ('QUARANTINED', 4)
        report = {'schema': 'keel.operational.command.v1', 'command': options.command,
                  'operation': operation, 'status': status, 'source_sha256': source,
                  'source_unchanged': unchanged, 'host_config_sha256': host.pin,
                  'result_valid_for_source': unchanged, 'result': result,
                  'task_success_inferred': False, **FLAGS}
    except Exception as exc:
        changed = False
        if before is not None:
            try:
                changed = before['sha256'] != _source_inventory()['sha256']
                if host is not None:
                    host.check()
            except Exception:
                changed = True
        status, code = ('QUARANTINED', 4) if changed else ('ERROR', 2)
        report = {'schema': 'keel.operational.command-error.v1', 'command': options.command,
                  'operation': operation, 'status': status, 'error_code': 'command_failed',
                  'error_type': type(exc).__name__, 'source_sha256': before['sha256'] if before else None,
                  'source_unchanged': False if changed else None,
                  'result_valid_for_source': False, 'task_success_inferred': False, **FLAGS}
    if output is not None:
        try:
            atomic_json(output, report)
        except Exception:
            status, code = 'ERROR', 2
    print(json.dumps({'command': options.command, 'status': status, 'exit_code': code}, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
