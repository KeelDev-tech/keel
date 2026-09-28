#!/usr/bin/env python3
"""Kill and restart the real public controller at five synthetic commit cuts.

Only freshly created private fixture homes are used. All workers are isolated
stdlib Python processes with denied sockets. This measures process-crash
recovery, not power-loss safety, provider acceptance, or production readiness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import stat
import subprocess
import sys


SCHEMA = 'keel.productivity.faultlab.v1'
CASES = ('after_reservation', 'before_dispatch', 'after_queue_commit',
         'after_recorded', 'after_settlement')
MODES = ('crash', 'restart-one', 'restart-two')
RUN_ID = 'synthetic-crash-cycle'
SCOPE = 'synthetic-faultlab'
WORKER_TIMEOUT = 30
BUILD_FILES = ('tools/run_productivity_faultlab.py', 'engines/productivity_service.py',
               'engines/productivity_history.py', 'engines/pipeline_service.py',
               'engines/source_scheduler.py', 'engines/safe_io.py', 'engines/safe_http.py',
               'engines/queue_io.py', 'engines/log_event.py', 'engines/host_cooldowns.py',
               'keel_efficiency/ledger.py')
PROTECTED = [
    {'role_id': 'synthetic-unknown', 'status': 'UNKNOWN_OUTCOME',
     'holds': ['unknown_application'], 'attempt_id': 'synthetic-existing-attempt',
     'application_url': 'https://job-boards.greenhouse.io/fixture/jobs/99',
     'execution_authorized': False},
    {'role_id': 'synthetic-human-hold', 'status': 'PARKED', 'human_hold': True,
     'holds': ['human_approval_required'], 'approval': {'approved': False},
     'application_url': 'https://job-boards.greenhouse.io/fixture/jobs/98',
     'execution_authorized': False},
]
DISCOVERED = {'identity': ['greenhouse', 'fixture', '1'],
              'posting_identity': ['greenhouse', 'fixture', '1'],
              'execution_authorized': False, 'status': 'PARKED-PENDING-VERIFICATION'}


def _require(condition):
    if not condition:
        raise ValueError('invalid_faultlab_fixture_or_worker')


def _destination(value):
    output = Path(os.path.abspath(value))
    _require(not output.exists() and not output.is_symlink())
    _require(output.parent.is_dir() and output.parent.resolve(strict=True) == output.parent)
    return output


def _write_new(path, document):
    raw = (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _read_private(path):
    _require(path.resolve(strict=True) == path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                 and info.st_uid == os.getuid() and not info.st_mode & 0o077)
        raw = stream.read(1024 * 1024 + 1)
    _require(len(raw) <= 1024 * 1024)
    return json.loads(raw)


def _build():
    root = Path(__file__).resolve().parents[1]
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in BUILD_FILES}


def _worker(output, case, mode):
    """Fixed private worker; no paths, callbacks, or executable code from JSON."""
    _require(case in CASES and mode in MODES)
    root = Path(os.path.abspath(output))
    _require(root.is_dir() and root.resolve(strict=True) == root)
    info = root.stat()
    _require(info.st_uid == os.getuid() and not info.st_mode & 0o077)
    session = _read_private(root / 'session.json')
    _require(session == {'schema': SCHEMA, 'root': str(root),
                         'nonce': os.environ.get('KEEL_FAULTLAB_NONCE')}
             and type(session['nonce']) is str and len(session['nonce']) == 64)
    folder = root / case
    if mode == 'crash':
        folder.mkdir(mode=0o700, exist_ok=False)
    else:
        _require(folder.is_dir() and folder.resolve(strict=True) == folder)
        _require(_read_private(folder / 'fixture.json') ==
                 {'schema': SCHEMA, 'case': case, 'nonce': session['nonce']})
        _require((folder / 'cutpoint.json').is_file())
        if mode == 'restart-two':
            _require(_read_private(folder / 'restart-one.json')['status'] == 'PASS')
    # An existing worker invocation is never resumed or reused implicitly.
    _write_new(folder / (mode + '.started.json'), {'mode': mode, 'pid': os.getpid()})
    os.umask(0o077)
    sys.dont_write_bytecode = True
    home = folder / 'workspace'
    if mode == 'crash':
        home.mkdir(mode=0o700)
        _write_new(folder / 'fixture.json', {'schema': SCHEMA, 'case': case, 'nonce': session['nonce']})
    _require(home.is_dir() and home.resolve(strict=True) == home)
    os.environ['KEEL_HOME'] = str(home)
    os.environ['JOB_PIPELINE_HTTP_COOLDOWN_DIR'] = str(home / 'hidden_files/http-cooldowns')
    temporary = folder / 'temporary'
    temporary.mkdir(mode=0o700, exist_ok=True)
    _require(temporary.resolve(strict=True) == temporary)
    os.environ['TMPDIR'] = str(temporary)

    def deny_network(event, args):
        if event.startswith('socket.'):
            with (folder / 'network-denied.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'event': event}) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            raise RuntimeError('offline_faultlab_network_denied')

    sys.addaudithook(deny_network)
    source_root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(source_root / 'engines'), str(source_root)]
    import tempfile
    tempfile.tempdir = str(temporary)
    import pipeline_service as pipeline
    import productivity_service as controller
    from safe_io import atomic_json, digest, read_json
    from keel_efficiency.ledger import ResourceLedger

    queue = home / 'data/queues/standard-queue.json'
    if mode == 'crash':
        for name in pipeline.QUEUES:
            atomic_json(home / 'data/queues' / (name + '-queue.json'),
                        PROTECTED if name == 'standard' else [])
        atomic_json(home / 'data/application-ledger.json', [])
        atomic_json(home / 'data/sources.json', {'schema_version': 1, 'sources': []})
        atomic_json(home / 'DEMO_ONLY.json', {'synthetic': True, 'live_use_authorized': False})
        pipeline.add_source(home, 'greenhouse:fixture')
        ledger = ResourceLedger(home / 'data/resources.sqlite3')
        ledger.create_scope(SCOPE, {'calls': 6, 'compute_ms': 120000})
    else:
        _require(_read_private(home / 'DEMO_ONLY.json') ==
                 {'synthetic': True, 'live_use_authorized': False})
        ledger = ResourceLedger(home / 'data/resources.sqlite3')
    request_id = 'productivity:' + digest({'workspace': str(home), 'run_id': RUN_ID})

    def fetch(url, timeout):
        _require(url == 'https://boards-api.greenhouse.io/v1/boards/fixture/jobs?content=true')
        # Persist dispatch observations before returning any synthetic data.
        with (folder / 'fetches.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'synthetic': True, 'reader_dispatch': True}) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        return {'jobs': [{'id': 1, 'title': 'Synthetic Operations Manager',
                          'location': {'name': 'Synthetic'}}]}

    def snapshot():
        try:
            retained = controller.get_run(home, RUN_ID, ledger=ledger, scope_id=SCOPE)
        except ValueError as exc:
            if str(exc) != 'productivity_run_missing':
                raise
            retained = None
        entries = read_json(queue)
        fetches = folder / 'fetches.jsonl'
        records = [json.loads(line) for line in fetches.read_text().splitlines()] if fetches.exists() else []
        _require(all(record == {'synthetic': True, 'reader_dispatch': True} for record in records))
        return {'retained_run': retained, 'request': ledger.request(request_id),
                'scope': ledger.snapshot(SCOPE), 'ledger': ledger.snapshot(),
                'synthetic_fetches': len(records), 'queue_sha256': digest(entries),
                'queue_rows': len(entries), 'protected_rows_intact': entries[:2] == PROTECTED,
                'unique_queue_roles': len({row.get('role_id') for row in entries}) == len(entries),
                'new_postings': [{'identity': row.get('source_identity'),
                                  'posting_identity': list(pipeline.identity(row.get('application_url')) or ()),
                                  'execution_authorized': row.get('execution_authorized'),
                                  'status': row.get('status')} for row in entries[2:]],
                'application_ledger_empty': read_json(home / 'data/application-ledger.json') == [],
                'network_attempts_denied': len((folder / 'network-denied.jsonl').read_text().splitlines())
                    if (folder / 'network-denied.jsonl').exists() else 0}

    def cycle():
        return controller.run_once(home, run_id=RUN_ID, ledger=ledger, scope_id=SCOPE,
                                   live=True, max_requests=2, timeout=5, target_verified=1, fetcher=fetch)

    def terminate_at_cut():
        _write_new(folder / 'cutpoint.json', {'case': case, 'before': snapshot(), 'pid': os.getpid()})
        os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError('SIGKILL did not terminate child')

    if mode == 'crash':
        if case == 'after_reservation':
            reserve = ledger.reserve
            def after_reservation(*args, **kwargs):
                reserve(*args, **kwargs)
                terminate_at_cut()
            ledger.reserve = after_reservation
        elif case == 'before_dispatch':
            ledger.mark_dispatched = lambda *args, **kwargs: terminate_at_cut()
        elif case == 'after_recorded':
            ledger.settle = lambda *args, **kwargs: terminate_at_cut()
        else:
            save = controller._save
            def before_save(path, state, now):
                row = state['runs'].get(RUN_ID)
                target = 'RECORDED' if case == 'after_queue_commit' else 'COMPLETE'
                if row is not None and row['phase'] == target:
                    terminate_at_cut()
                return save(path, state, now)
            controller._save = before_save
        cycle()
        raise AssertionError('fixed crash cutpoint was not reached')

    before = snapshot()
    recovery = None
    if case == 'before_dispatch':
        recovery = controller.recover(home, run_id=RUN_ID, ledger=ledger, scope_id=SCOPE)
    response = cycle()
    if case == 'after_queue_commit':
        recovery = controller.recover(home, run_id=RUN_ID, ledger=ledger, scope_id=SCOPE)
    after = snapshot()
    expected_phase = ('CANCELLED' if case == 'before_dispatch' else
                      'INTENT' if case == 'after_queue_commit' else 'COMPLETE')
    expected_status = ('CANCELLED' if case == 'before_dispatch' else
                       'HELD_RECOVERY' if case == 'after_queue_commit' else 'COMPLETE')
    expected_used = 0 if case in ('before_dispatch', 'after_queue_commit') else 1
    expected_reserved = 2 if case == 'after_queue_commit' else 0
    expected_fetches = 0 if case == 'before_dispatch' else 1
    # UNKNOWN ledger state here means only external credit usage is unobserved;
    # known reader calls remain immutably charged, never invented as free.
    expected_request = ('CANCELLED' if case == 'before_dispatch' else
                        'DISPATCHED' if case == 'after_queue_commit' else 'UNKNOWN')
    expected_extra = 1 if case == 'after_reservation' and mode == 'restart-one' else 0
    expected_postings = [] if case == 'before_dispatch' else [DISCOVERED]
    checks = {
        'expected_controller_response': response['status'] == expected_status,
        'expected_retained_phase': after['retained_run']['phase'] == expected_phase,
        'expected_request_state': after['request']['state'] == expected_request,
        'only_preintent_restart_may_dispatch': after['synthetic_fetches'] - before['synthetic_fetches'] == expected_extra,
        'exact_fixture_dispatch_count': after['synthetic_fetches'] == expected_fetches,
        'known_calls_charged_once': after['scope']['used']['calls'] == expected_used,
        'uncertain_reservation_preserved': after['scope']['reserved']['calls'] == expected_reserved
            and after['request']['remaining']['calls'] == expected_reserved,
        'available_calls_conserved': after['scope']['available']['calls'] == 6 - expected_used - expected_reserved,
        'single_request_retained': sum(after['ledger']['requests_by_state'].values()) == 1,
        'exact_queue_contents_retained': after['queue_rows'] == 2 + len(expected_postings)
            and after['new_postings'] == expected_postings and after['unique_queue_roles'],
        'all_reservation_dimensions_retained_or_released': after['request']['remaining'] ==
            (after['request']['estimate'] if case == 'after_queue_commit' else
             {key: 0 for key in after['request']['estimate']}),
        'all_available_dimensions_conserved': all(after['scope']['available'][key] ==
            after['scope']['limits'][key] - after['scope']['used'][key] - after['scope']['reserved'][key]
            for key in after['scope']['limits']),
        'protected_rows_preserved': before['protected_rows_intact'] and after['protected_rows_intact'],
        'application_ledger_unchanged': before['application_ledger_empty'] and after['application_ledger_empty'],
        'no_socket_attempt': after['network_attempts_denied'] == 0,
        'no_execution_authority': response['execution_authorized'] is False
            and response['submission_authorized'] is False,
        'credit_usage_not_invented': after['request']['usage']['external_credit_micros'] is None,
        'second_restart_is_stable': mode != 'restart-two' or before == after,
    }
    if case == 'before_dispatch':
        checks['predispatch_recovery_releases_only_undispatched_work'] = (
            recovery['status'] == 'CANCELLED' and recovery['effects_dispatched'] is False)
    if case == 'after_queue_commit':
        checks['postdispatch_recovery_refuses_refund'] = (recovery['status'] == 'HELD_RECOVERY'
                                                        and recovery['reservation_released'] is False)
    result = {'status': 'PASS' if all(checks.values()) else 'FAIL', 'case': case, 'mode': mode,
              'pid': os.getpid(), 'checks': checks, 'before': before, 'after': after}
    _write_new(folder / (mode + '.json'), result)
    print(json.dumps({'status': result['status'], 'case': case, 'mode': mode}))
    return 0 if result['status'] == 'PASS' else 1


def _child(output, case, mode, nonce):
    environment = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL'}}
    environment['KEEL_FAULTLAB_NONCE'] = nonce
    try:
        child = subprocess.run([sys.executable, '-I', '-B', '-S', str(Path(__file__).resolve()),
                                '--out', str(output), '--_case', case, '--_worker', mode],
                               env=environment, capture_output=True, timeout=WORKER_TIMEOUT, check=False)
        return {'returncode': child.returncode, 'timed_out': False,
                'diagnostic_sha256': hashlib.sha256(child.stderr).hexdigest()}
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for this exact owned child on timeout.
        return {'returncode': None, 'timed_out': True, 'diagnostic_sha256': None}


def run(output):
    """Run five fixed fixture scenarios; refuse every existing destination."""
    output = _destination(output)
    output.mkdir(mode=0o700, exist_ok=False)
    nonce = secrets.token_hex(32)
    _write_new(output / 'session.json', {'schema': SCHEMA, 'root': str(output), 'nonce': nonce})
    build = _build()
    report = {'schema': SCHEMA, 'status': 'FAIL', 'synthetic': True,
              'source_fingerprints': build, 'source_fingerprint_scope': 'selected local source files; not source authenticity',
              'python': platform.python_version(), 'platform': platform.system(), 'cases': {},
              'live_host_qualified': False, 'production_performance_claim': False,
              'execution_authorized': False, 'submission_authorized': False,
              'paid_services_required': False, 'actual_credit_savings': None,
              'network_calls': 0, 'model_calls': 0, 'submissions': 0,
              'scope': 'Synthetic process termination at five fixed controller boundaries; no power-loss or distributed-host claim.'}
    initial_phases = {'after_reservation': None, 'before_dispatch': 'INTENT',
                      'after_queue_commit': 'INTENT', 'after_recorded': 'RECORDED', 'after_settlement': 'RECORDED'}
    initial_requests = {'after_reservation': 'RESERVED', 'before_dispatch': 'RESERVED',
                        'after_queue_commit': 'DISPATCHED', 'after_recorded': 'DISPATCHED',
                        'after_settlement': 'UNKNOWN'}
    for case in CASES:
        crash = _child(output, case, 'crash', nonce)
        item = {'crash': crash, 'checks': {'actual_sigkill': crash['returncode'] == -signal.SIGKILL
                                         and not crash['timed_out']}, 'restarts': []}
        report['cases'][case] = item
        try:
            if not item['checks']['actual_sigkill']:
                continue
            evidence = _read_private(output / case / 'cutpoint.json')['before']
            phase = evidence['retained_run']['phase'] if evidence['retained_run'] else None
            item['checks']['expected_durable_cutpoint'] = (phase == initial_phases[case]
                and evidence['request']['state'] == initial_requests[case]
                and evidence['synthetic_fetches'] == int(case not in ('after_reservation', 'before_dispatch'))
                and evidence['queue_rows'] == 2 + evidence['synthetic_fetches']
                and evidence['protected_rows_intact'] and evidence['unique_queue_roles']
                and evidence['new_postings'] == ([DISCOVERED] if evidence['synthetic_fetches'] else []))
            previous = evidence
            for mode in MODES[1:]:
                child = _child(output, case, mode, nonce)
                item['restarts'].append(child)
                if child['returncode'] != 0 or child['timed_out']:
                    item['checks'][mode] = False
                    break
                result = _read_private(output / case / (mode + '.json'))
                item['checks'][mode] = result['status'] == 'PASS' and bool(result['checks']) and all(result['checks'].values())
                item['checks'][mode + '_continues_retained_state'] = result['before'] == previous
                previous = result['after']
            item['checks']['two_fresh_restarts'] = len(item['restarts']) == 2
        except Exception as exc:
            item['checks']['evidence_readable'] = False
            item['error_class'] = type(exc).__name__
    report['source_files_unchanged'] = build == _build()
    report['status'] = 'PASS' if report['source_files_unchanged'] and all(
        all(item['checks'].values()) for item in report['cases'].values()) else 'FAIL'
    _write_new(output / 'report.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, help='new private synthetic-only output directory')
    parser.add_argument('--_worker', choices=MODES, help=argparse.SUPPRESS)
    parser.add_argument('--_case', choices=CASES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args._worker:
            return _worker(args.out, args._case, args._worker)
        _require(args._case is None)
        report = run(args.out)
        print(json.dumps(report, sort_keys=True))
        return 0 if report['status'] == 'PASS' else 1
    except Exception as exc:
        print(json.dumps({'status': 'FAIL', 'error_class': type(exc).__name__,
                          'reason': 'faultlab_requires_new_private_output_and_fixed_isolated_workers'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
