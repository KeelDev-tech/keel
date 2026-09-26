"""Private JSON command surface for trusted local Keel host integration.

JSON selects only fixed operations. Host identity, clock, source inventory,
paths, approval observations and native gate configuration come from the
embedding operator's flags/files. They are observations, never authentication.
Ordinary session/status CLI opens do not fence or restart a live controller.
"""
import argparse
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import time

from .common import (FLAGS, MuseError, atomic_json, digest, load_json,
                     outside_source, require_dict, require_hash, require_id)


ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATE = 'NATIVE_TOOL_ENFORCES_PERMISSION'
TRUSTED_ARGUMENTS = {
    'now', 'source_sha256', 'workspace_id', 'home', 'runtime_home', 'backup_home',
    'destination', 'root', 'source_root', 'source_provider', 'state_provider',
    'source_head_provider', 'memory', 'transports', 'handler', 'handlers',
    'operations', 'clock', 'principal', 'host_snapshot', 'host_approvals',
    'native_gate', 'transport_mode', 'manifest', 'mandatory_policy', 'store',
    'scope', 'producer_components', 'approval_producer_id', 'dispatch',
}


def _now():
    return int(time.time())


def _source_inventory():
    from tools.muse_inventory import inventory
    return inventory(ROOT)


def _required(value, name):
    if value is None:
        raise MuseError('required_host_flag_' + name)
    return value


def _new_output(value):
    path = outside_source(value, ROOT)
    if path.exists() or path.is_symlink():
        raise MuseError('output_already_exists')
    return path


def _runtime(value):
    return outside_source(_required(value, 'home'), ROOT)


def _new_home_output(value, options):
    path = _new_output(value)
    if path == Path(options.out).absolute():
        raise MuseError('output_paths_must_differ')
    return path


def _file(value, name):
    return load_json(_required(value, name))


def _request(value, source):
    require_dict(value, {'operation', 'arguments'})
    require_id(value['operation'])
    arguments = value['arguments']
    if type(arguments) is not dict:
        raise MuseError('arguments_object_required')
    arguments = dict(arguments)
    # Optional caller pin is a comparison only, never a source provider.
    if 'expected_running_source_sha256' in arguments:
        if require_hash(arguments.pop('expected_running_source_sha256')) != source:
            raise MuseError('running_source_changed')
    if TRUSTED_ARGUMENTS & arguments.keys():
        raise MuseError('trusted_host_argument_in_json')
    return value['operation'], arguments


def _invoke(function, arguments, **trusted):
    if type(arguments) is not dict or arguments.keys() & trusted.keys():
        raise MuseError('trusted_argument_override')
    signature = inspect.signature(function)
    signature.bind(**arguments, **trusted)
    return function(**arguments, **trusted)


def _choose(operation, registry):
    if operation not in registry:
        raise MuseError('operation_not_allowed')
    return registry[operation]


def _session(options, operation, arguments, source):
    from .session import NativeSession
    home = _runtime(options.home)
    if options.dispatch and operation != 'next':
        raise MuseError('dispatch_only_for_next')
    if operation == 'create':
        home = _new_home_output(home, options)
        allowed = {'contract', 'values', 'attachments', 'lease_seconds', 'expires_at'}
        if arguments.keys() - allowed:
            raise MuseError('session_create_arguments_invalid')
        session = _invoke(NativeSession.create, arguments, home=home,
            workspace_id=options.workspace_id, source_sha256=source,
            manifest=_file(options.manifest, 'manifest'),
            host_approvals=_file(options.approvals, 'approvals'),
            host_snapshot=_file(options.host_snapshot, 'host_snapshot'), now=_now(),
            native_gate=options.native_gate, transport_mode=options.transport_mode, clock=_now)
        return session.status()
    session = NativeSession(home, options.workspace_id, clock=_now)
    if operation == 'status':
        return _invoke(session.status, arguments)
    if operation == 'recover':
        return _invoke(session.recover, arguments, now=_now())
    if operation == 'next':
        if arguments.keys() - {'native_permission'}:
            raise MuseError('session_next_arguments_invalid')
        return _invoke(session.next_request, arguments, source_sha256=source,
            host_snapshot=_file(options.host_snapshot, 'host_snapshot'),
            now=_now(), dispatch=options.dispatch)
    if operation == 'observe':
        if set(arguments) != {'response', 'request_sha256'}:
            raise MuseError('session_observe_arguments_invalid')
        return _invoke(session.observe, arguments, source_sha256=source,
            host_snapshot=_file(options.host_snapshot, 'host_snapshot'), now=_now())
    raise MuseError('operation_not_allowed')


def _forms(arguments, options):
    from keel_loki.forms import build_plan
    if set(arguments) != {'contract', 'values'}:
        raise MuseError('forms_arguments_invalid')
    return build_plan(**arguments, approvals=_file(options.approvals, 'approvals'), now=_now())


def _bridge(options, request):
    from .bridge import CommandBridge, ReadOperation
    from .capabilities import probe_host
    from .review import project_review

    def probe(arguments):
        require_dict(arguments, set())
        return probe_host(load_json(options.manifest) if options.manifest else None)

    def forms(arguments):
        return _forms(arguments, options)

    def review(arguments):
        # The generic request has its own exact scope/source/deadline schema.
        if type(arguments) is not dict or TRUSTED_ARGUMENTS & arguments.keys():
            raise MuseError('trusted_host_argument_in_json')
        return _invoke(project_review, arguments, now=_now())

    bridge = CommandBridge(options.workspace_id, {
        'capabilities.probe': ReadOperation(probe),
        'forms.build_plan': ReadOperation(forms),
        'review.project': ReadOperation(review),
    }, source_provider=lambda: _source_inventory()['sha256'], clock=_now)
    return bridge.invoke(request)


def _source_config(options, *, context=False):
    from keel_sources.store import SourceStore
    config = _file(options.host_config, 'host_config')
    keys = {'store_home', 'source_root', 'scope',
            'mandatory_policy' if context else 'producer_components'}
    require_dict(config, keys)
    require_dict(config['scope'], {'workspace_id', 'role_id', 'application_id', 'action'})
    if config['scope']['workspace_id'] != options.workspace_id:
        raise MuseError('source_workspace_mismatch')
    home = _runtime(config['store_home'])
    # CLI does not provision identities/scopes or synthesize empty authority.
    for name in ('sources.sqlite3', 'store.identity', 'attachments'):
        path = home / name
        if not path.exists() or path.is_symlink():
            raise MuseError('preexisting_source_store_required')
    store = SourceStore(home, options.workspace_id,
        clock=lambda: datetime.fromtimestamp(_now(), timezone.utc))
    return config, store


def _sources(options, operation, arguments):
    from .sources import FileSourceProducer, source_head
    if operation not in {'head', 'capture'}:
        raise MuseError('operation_not_allowed')
    config, store = _source_config(options)
    if operation == 'head':
        require_dict(arguments, set())
        head = source_head(store, config['scope'])
        return {'schema': 'keel.muse.source-head-read.v1', 'status': 'OBSERVED',
                'head': head, 'head_sha256': digest(head), **FLAGS}
    producer = FileSourceProducer(store, root=config['source_root'], scope=config['scope'],
                                  producer_components=config['producer_components'])
    return _invoke(producer.capture, arguments)


def _context(options, operation, arguments):
    from .context import compile_context, validate_cache
    from .sources import source_head
    function = _choose(operation, {'compile': compile_context, 'validate_cache': validate_cache})
    config, store = _source_config(options, context=True)
    trusted = {'root': config['source_root'], 'source_head_provider': lambda: source_head(store, config['scope']),
               'now': _now()}
    if operation == 'compile':
        trusted['mandatory_policy'] = config['mandatory_policy']
    else:
        # Cache validation receives the active host policy pin, not a JSON choice.
        if 'expected_policy_sha256' in arguments:
            if require_hash(arguments['expected_policy_sha256']) != digest(config['mandatory_policy']):
                raise MuseError('mandatory_policy_pin_changed')
            arguments = {key: value for key, value in arguments.items() if key != 'expected_policy_sha256'}
        trusted['expected_policy_sha256'] = digest(config['mandatory_policy'])
    return _invoke(function, arguments, **trusted)


def _backup(options, operation, arguments):
    from . import backup
    if operation == 'checkpoint':
        return _invoke(backup.checkpoint, arguments, home=_runtime(options.home),
                       workspace_id=options.workspace_id, profile=options.profile)
    if operation == 'snapshot':
        destination = _new_home_output(_required(options.backup_home, 'backup_home'), options)
        checkpoint = arguments.get('expected_checkpoint')
        _backup_scope(checkpoint, options)
        return _invoke(backup.snapshot, arguments, runtime_home=_runtime(options.home),
                       backup_home=destination, workspace_id=options.workspace_id, profile=options.profile)
    if operation == 'restore':
        destination = _new_home_output(_required(options.destination, 'destination'), options)
        _backup_scope(arguments.get('authoritative_checkpoint'), options)
        return _invoke(backup.restore, arguments, backup_home=_runtime(options.backup_home),
                       destination=destination)
    raise MuseError('operation_not_allowed')


def _backup_scope(checkpoint, options):
    if (type(checkpoint) is not dict or checkpoint.get('workspace_id') != options.workspace_id
            or checkpoint.get('profile') != options.profile):
        raise MuseError('backup_host_scope_mismatch')


def _run(options, operation, arguments, source):
    command = options.command
    if command == 'inspect-host':
        from .capabilities import probe_host
        return probe_host(load_json(options.manifest) if options.manifest else None)
    if command == 'demo':
        from .demo import run_demo
        return run_demo(_new_home_output(_runtime(options.home), options))
    if command == 'session':
        return _session(options, operation, arguments, source)
    if command == 'forms-plan':
        _choose(operation, {'build_plan': True})
        return _forms(arguments, options)
    if command == 'sources':
        return _sources(options, operation, arguments)
    if command == 'context':
        return _context(options, operation, arguments)
    if command == 'coordinator':
        from .coordinator import Coordinator
        _choose(operation, {'snapshot': True})
        reader = Coordinator.open_readonly(_runtime(options.home), options.workspace_id)
        return _invoke(reader.snapshot, arguments)
    if command == 'review':
        from . import review
        functions = {'project_review': review.project_review, 'validate_projection': review.validate_projection,
                     'make_review_request': review.make_review_request,
                     'validate_review_request': review.validate_review_request,
                     'record_session_effort': review.record_session_effort}
        function = _choose(operation, functions)
        return _invoke(function, arguments, **({} if operation == 'validate_projection' else {'now': _now()}))
    if command == 'dashboard':
        from .dashboard import write_dashboard
        _choose(operation, {'render': True})
        require_dict(arguments, {'report'})
        return write_dashboard(options.html, arguments['report'])
    if command == 'evaluation':
        from . import evaluation
        function = _choose(operation, {'freeze_plan': evaluation.freeze_plan,
                                      'run': evaluation.run, 'compare': evaluation.compare})
        return _invoke(function, arguments, source_sha256=source)
    if command == 'repair':
        from . import repair
        function = _choose(operation, {'freeze_fixtures': repair.freeze_fixtures, 'propose': repair.propose,
                                      'evaluate': repair.evaluate, 'recommend': repair.recommend})
        return _invoke(function, arguments, **({} if operation == 'freeze_fixtures' else {'source_sha256': source}))
    if command == 'backup':
        return _backup(options, operation, arguments)
    if command == 'reconciliation':
        from keel_loki.recovery import RecoveryJournal
        from .reconciliation import propose_receipt
        _choose(operation, {'propose_receipt': True})
        config = _file(options.host_config, 'host_config')
        require_dict(config, {'source_root', 'expected_target'})
        reader = RecoveryJournal.open_readonly(_runtime(options.home), options.workspace_id)
        return _invoke(propose_receipt, arguments, journal=reader, root=config['source_root'],
                       expected_target=config['expected_target'], now=_now())
    raise MuseError('command_not_allowed')


def _outcome(result):
    """Exit codes concern requested work, not successes of nested demo cases."""
    if type(result) is not dict:
        return 'RECORDED', 0
    status = result.get('status', 'RECORDED')
    if type(status) is not str:
        return 'ERROR', 2
    failures = {'BLOCKED', 'QUARANTINED', 'PARTIAL', 'UNKNOWN', 'ERROR', 'FAIL',
                'FAILED', 'HOST_MAPPING_REQUIRED', 'UNAVAILABLE', 'NO_FILES_SELECTED'}
    if status in failures or status.startswith('WAIT'):
        return status, 3
    if result.get('schema') == 'keel.muse.bridge-result.v1':
        return _outcome(result['result'])
    adapter = result.get('native_adapter')
    if type(adapter) is dict and adapter.get('mapping_status') == 'HOST_MAPPING_REQUIRED':
        return 'HOST_MAPPING_REQUIRED', 3
    metrics = result.get('metrics')
    if type(metrics) is dict and result.get('schema') in {'keel.muse.e2e-run.v1', 'keel.muse.e2e-comparison.v1'}:
        if any(any(row.get(key, 0) for key in ('errors', 'false_ready', 'false_block')) for row in metrics.values()):
            return 'PARTIAL', 3
    if result.get('missing_families'):
        return 'PARTIAL', 3
    return status, 0


def parser():
    result = argparse.ArgumentParser(prog='python -m keel_muse',
        description='Private JSON host integration; no model, shell or native tool dispatch in this CLI.',
        epilog='Exit codes: 0 recorded/ready; 2 invalid input/error; 3 blocked/incomplete/waiting; 4 source quarantine. '
               'JSON is {operation, arguments}, except bridge. Output must be a new file outside source.')
    commands = result.add_subparsers(dest='command', required=True)
    descriptions = {
        'inspect-host': 'Inspect host mapping; no tool calls.', 'demo': 'Run synthetic integration in a new home.',
        'bridge': 'Fixed read registry: capabilities.probe, forms.build_plan, review.project.',
        'forms-plan': 'build_plan with trusted --approvals.',
        'session': 'create/status/next/observe/recover durable native JSON handshake.',
        'sources': 'head/capture existing registered source store; no approval ingestion.',
        'context': 'compile/validate_cache with a live source head and host policy.',
        'coordinator': 'snapshot only; readonly open, no controller restart.',
        'review': 'project_review/validate_projection/make_review_request/validate_review_request/record_session_effort.',
        'dashboard': 'render a validated review projection to new private --html.',
        'evaluation': 'freeze_plan/run/compare; fixture or unavailable transports only.',
        'repair': 'freeze_fixtures/propose/evaluate/recommend; quarantine, no promotion.',
        'backup': 'checkpoint/snapshot/restore named runtime stores with external pins.',
        'reconciliation': 'propose_receipt from bound evidence; readonly, no hold clearance.',
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument('--out', required=True, help='New private JSON report outside the code tree.')
        if name not in {'inspect-host', 'demo'}:
            command.add_argument('--input', required=True, help='Strict bounded JSON request file.')
        if name in {'session', 'coordinator', 'backup', 'reconciliation', 'demo'}:
            command.add_argument('--home', required=name != 'backup', help='Operator-selected runtime home.')
        if name in {'bridge', 'session', 'sources', 'context', 'coordinator', 'backup', 'reconciliation'}:
            command.add_argument('--workspace-id', required=True)
        if name in {'inspect-host', 'bridge', 'session'}:
            command.add_argument('--manifest', help='Host-declared native mapping JSON; no SDK discovery claim.')
        if name in {'bridge', 'forms-plan', 'session'}:
            command.add_argument('--approvals', help='Trusted host approval observations JSON; not human authentication.')
        if name in {'sources', 'context', 'reconciliation'}:
            command.add_argument('--host-config', required=True, help='Operator-owned paths, scope and policy JSON.')
        if name == 'session':
            command.add_argument('--host-snapshot', help='Fresh host state JSON for create/next/observe.')
            command.add_argument('--native-gate', choices=[NATIVE_GATE],
                                 help='Declare delegation to the real native permission gate, never a permission grant.')
            command.add_argument('--transport-mode', choices=['HOST_NATIVE_REPORTED', 'INJECTED'], default='HOST_NATIVE_REPORTED')
            command.add_argument('--dispatch', action='store_true',
                                 help='For next: durably issue once; host invokes returned native tool separately.')
        if name == 'dashboard':
            command.add_argument('--html', required=True, help='New private HTML output outside source.')
        if name == 'backup':
            command.add_argument('--backup-home')
            command.add_argument('--destination')
            command.add_argument('--profile', choices=['recovery', 'coordinator'], default='recovery')
    return result


def main(argv=None):
    options = parser().parse_args(argv)
    output = None
    before = None
    operation = options.command
    report = None
    status, code = 'ERROR', 2
    try:
        # Reject occupied output slots before any runtime creation or mutation.
        output = _new_output(options.out)
        if options.command == 'dashboard':
            options.html = _new_output(options.html)
            if output == options.html:
                raise MuseError('output_paths_must_differ')
        before = _source_inventory()
        source = require_hash(before['sha256'])
        if options.command == 'bridge':
            request = load_json(options.input)
            operation = request.get('operation', 'bridge') if type(request) is dict else 'bridge'
            result = _bridge(options, request)
        else:
            arguments = {}
            if hasattr(options, 'input'):
                operation, arguments = _request(load_json(options.input), source)
            result = _run(options, operation, arguments, source)
        after = _source_inventory()
        unchanged = source == require_hash(after['sha256'])
        status, code = _outcome(result) if unchanged else ('QUARANTINED', 4)
        report = {'schema': 'keel.muse.command.v1', 'command': options.command,
                  'operation': operation, 'status': status, 'source_sha256': source,
                  'source_unchanged': unchanged, 'result': result,
                  'result_valid_for_source': unchanged, 'task_success_inferred': False,
                  **FLAGS}
    except Exception as exc:
        # Exception messages may contain paths or human answers. Only the class
        # name and a fixed failure code enter the public summary/private envelope.
        changed = False
        if before is not None:
            try:
                changed = before['sha256'] != _source_inventory()['sha256']
            except Exception:
                changed = True
        mapping_codes = {'HOST_MAPPING_REQUIRED', 'HOST_MAPPING_REQUIRED_native_permission_gate',
                         'HOST_MAPPING_REQUIRED_unsupported_operation'}
        from .session import SessionError
        mapping = isinstance(exc, SessionError) and str(exc) in mapping_codes
        status, code = ('QUARANTINED', 4) if changed else ('HOST_MAPPING_REQUIRED', 3) if mapping else ('ERROR', 2)
        report = {'schema': 'keel.muse.command-error.v1', 'command': options.command,
                  'status': status, 'error_code': str(exc) if mapping else 'command_failed', 'error_type': type(exc).__name__,
                  'source_sha256': before['sha256'] if before else None,
                  'source_unchanged': False if changed else None, 'result_valid_for_source': False,
                  'task_success_inferred': False, **FLAGS}
    if output is not None:
        try:
            atomic_json(output, report)
        except Exception:
            status, code = 'ERROR', 2
    print(json.dumps({'command': options.command, 'status': status, 'exit_code': code}, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
