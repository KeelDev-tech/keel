#!/usr/bin/env python3
"""Exercise the public productivity controller offline in a new private home.

The controller, trials, queues and accounting are real local implementations;
the board response and all applicant data are synthetic. This is not a live
host qualification, submission exercise, model benchmark or savings estimate.
Engine imports and environment bindings occur only in an isolated child.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def _destination(value):
    output = Path(os.path.abspath(value))
    if output.exists() or output.is_symlink():
        raise ValueError('output must be a new directory')
    if not output.parent.is_dir() or output.parent.resolve(strict=True) != output.parent:
        raise ValueError('output parent must be an existing real directory')
    return output


def _write(path, document):
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _worker(output):
    """Private child entry point; it independently refuses existing output."""
    output = _destination(output)
    output.mkdir(mode=0o700, exist_ok=False)
    os.umask(0o077)
    sys.dont_write_bytecode = True
    home = output / 'workspace'
    home.mkdir(mode=0o700)
    temporary = output / 'temporary'
    temporary.mkdir(mode=0o700)
    os.environ['KEEL_HOME'] = str(home)
    os.environ['JOB_PIPELINE_HTTP_COOLDOWN_DIR'] = str(home / 'hidden_files/http-cooldowns')
    os.environ['TMPDIR'] = str(temporary)

    socket_attempts = []

    def deny_network(event, args):
        if event.startswith('socket.'):
            socket_attempts.append(event)
            raise RuntimeError('offline_rehearsal_network_denied')

    sys.addaudithook(deny_network)
    report = {
        'schema': 'keel.productivity.rehearsal.v1', 'status': 'FAIL',
        'synthetic': True, 'production_performance_claim': False,
        'live_host_qualified': False, 'actual_credit_savings': None,
        'execution_authorized': False, 'submission_authorized': False,
        'paid_services_required': False,
        'boundary': 'Real local controller, queues, history and budget ledger; injected synthetic board response.',
        'checks': {}, 'observations': {},
        'effects': {'network_calls': 0, 'model_calls': 0, 'submissions': 0,
                    'synthetic_fetch_calls': 0, 'socket_attempts_denied': 0},
    }
    calls = []
    try:
        # Do not move these imports to module scope. Existing callers may have
        # a different KEEL_HOME and import-cached engine globals.
        root = Path(__file__).resolve().parents[1]
        sys.path[:0] = [str(root / 'engines'), str(root)]
        import tempfile
        tempfile.tempdir = str(temporary)
        import pipeline_service as pipeline
        import productivity_service as controller
        import productivity_trials as trials
        from safe_io import atomic_json, digest, read_json
        from keel_efficiency.ledger import ResourceLedger

        def check(name, condition):
            report['checks'][name] = bool(condition)

        protected = [
            {'role_id': 'synthetic-unknown', 'status': 'UNKNOWN_OUTCOME',
             'holds': ['unknown_application'], 'attempt_id': 'synthetic-attempt',
             'application_url': 'https://job-boards.greenhouse.io/fixture/jobs/99',
             'execution_authorized': False},
            {'role_id': 'synthetic-human-hold', 'status': 'PARKED',
             'holds': ['human_approval_required'], 'human_hold': True,
             'approval': {'approved': False},
             'application_url': 'https://job-boards.greenhouse.io/fixture/jobs/98',
             'execution_authorized': False},
        ]
        queue = home / 'data/queues/standard-queue.json'
        for name in pipeline.QUEUES:
            atomic_json(home / 'data/queues' / (name + '-queue.json'),
                        protected if name == 'standard' else [])
        atomic_json(home / 'data/application-ledger.json', [])
        atomic_json(home / 'data/sources.json', {'schema_version': 1, 'sources': []})
        atomic_json(home / 'DEMO_ONLY.json', {'synthetic': True, 'live_use_authorized': False})
        pipeline.add_source(home, 'greenhouse:fixture')
        application_ledger_before = (home / 'data/application-ledger.json').read_bytes()
        ledger = ResourceLedger(home / 'data/resources.sqlite3')
        ledger.create_scope('rehearsal', {'calls': 12, 'compute_ms': 120000})

        def fetch(url, timeout):
            calls.append(url)
            if url != 'https://boards-api.greenhouse.io/v1/boards/fixture/jobs?content=true':
                raise ValueError('unexpected_synthetic_board_url')
            return {'jobs': [{'id': 1, 'title': 'Synthetic Operations Manager',
                              'location': {'name': 'Synthetic'}}]}

        options = dict(trial_id='synthetic-discover-verify-stop', max_cycles=4,
                       ledger=ledger, scope_id='rehearsal', target_verified=1,
                       max_requests=2, timeout=5, fetcher=fetch, live=True)
        first = trials.run_trial(home, **options)
        _write(output / 'trial.json', first)
        before_replay = {
            'ledger': ledger.snapshot(), 'synthetic_fetch_calls': len(calls),
            'receipts': controller.receipts(home, ledger=ledger, scope_id='rehearsal'),
            'queue_sha256': digest(read_json(queue)),
        }
        replay = trials.run_trial(home, **options)
        _write(output / 'replay.json', replay)
        after_replay = {
            'ledger': ledger.snapshot(), 'synthetic_fetch_calls': len(calls),
            'receipts': controller.receipts(home, ledger=ledger, scope_id='rehearsal'),
            'queue_sha256': digest(read_json(queue)),
        }
        _write(output / 'replay-evidence.json', {'before': before_replay, 'after': after_replay})
        stages = [row['stage'] for row in before_replay['receipts']['receipts']]
        check('discovery_then_verification_then_idle', stages == ['discover', 'verify', 'idle'])
        check('one_new_posting_discovered', first['stages'].get('discover', {}).get('stage_results_known') == 1)
        check('one_posting_presence_verified', first['stages'].get('verify', {}).get('stage_results_known') == 1)
        check('idle_stops_predeclared_tail', first['measurement_status'] == 'STOPPED'
              and first['stop_reason'] == 'IDLE' and len(first['planned_not_dispatched']) == 1)
        check('two_synthetic_fetches_accounted', len(calls) == 2
              and ledger.snapshot('rehearsal')['used']['calls'] == 2)
        check('replay_ledger_unchanged', before_replay['ledger'] == after_replay['ledger'])
        check('replay_fetch_count_unchanged', before_replay['synthetic_fetch_calls'] == after_replay['synthetic_fetch_calls'])
        check('replay_retained_receipts_unchanged', before_replay['receipts'] == after_replay['receipts'])
        check('replay_queue_unchanged', before_replay['queue_sha256'] == after_replay['queue_sha256'])
        check('trial_report_uses_retained_receipts', first['receipts'] == replay['receipts'])

        ledger.create_scope('insufficient', {'calls': 1, 'compute_ms': 120000})
        before_refusal = {'ledger': ledger.snapshot(), 'synthetic_fetch_calls': len(calls),
                          'receipts': controller.receipts(home), 'queue_sha256': digest(read_json(queue))}
        refusal = trials.run_trial(home, trial_id='synthetic-budget-refusal', max_cycles=2,
                                   ledger=ledger, scope_id='insufficient', target_verified=2,
                                   max_requests=2, timeout=5, fetcher=fetch, live=True)
        _write(output / 'budget-refusal.json', refusal)
        after_refusal = {'ledger': ledger.snapshot(), 'synthetic_fetch_calls': len(calls),
                         'receipts': controller.receipts(home), 'queue_sha256': digest(read_json(queue))}
        _write(output / 'budget-evidence.json', {'before': before_refusal, 'after': after_refusal})
        check('insufficient_budget_refused_before_dispatch', refusal['stop_reason'] == 'HELD_BUDGET'
              and refusal['receipt_runs'] == 0 and before_refusal == after_refusal)
        check('refusal_does_not_invent_results', refusal['measurement_status'] == 'INCONCLUSIVE'
              and refusal['stages'] == {} and refusal['stop_evidence'] == 'local_stop_record_only')

        rows = read_json(queue)
        by_id = {row['role_id']: row for row in rows}
        check('unknown_and_human_holds_preserved', all(by_id.get(row['role_id']) == row for row in protected))
        added = [row for row in rows if row['role_id'] not in {item['role_id'] for item in protected}]
        check('verification_does_not_authorize_execution', len(added) == 1
              and added[0]['status'] == 'PARKED-PENDING-VERIFICATION'
              and added[0].get('execution_authorized') is False
              and added[0]['posting_verification'].get('execution_authorized') is False
              and added[0]['posting_verification'].get('form_verified') is False)
        check('application_ledger_unchanged', application_ledger_before == (home / 'data/application-ledger.json').read_bytes())
        check('credits_and_completions_not_invented', first['credits_per_verified_completion'] is None
              and first['external_credit_micros'] is None and not first['production_savings_proven'])
        report['observations'] = {
            'stages': stages, 'retained_run_receipts': after_replay['receipts']['total_runs'],
            'trial_measurement_status': first['measurement_status'], 'trial_stop_reason': first['stop_reason'],
            'planned_not_dispatched': len(first['planned_not_dispatched']),
            'budget_stop_reason': refusal['stop_reason'], 'budget_receipt_runs': refusal['receipt_runs'],
            'final_queue_rows': len(rows), 'protected_queue_rows': len(protected),
            'accounted_synthetic_calls': ledger.snapshot('rehearsal')['used']['calls'],
        }
    except Exception as exc:
        report['error_class'] = type(exc).__name__
        report['checks']['rehearsal_completed'] = False
    report['effects']['synthetic_fetch_calls'] = len(calls)
    report['effects']['socket_attempts_denied'] = len(socket_attempts)
    report['checks']['no_socket_attempts'] = not socket_attempts
    report['status'] = 'PASS' if report['checks'] and all(report['checks'].values()) else 'FAIL'
    _write(output / 'report.json', report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report['status'] == 'PASS' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, help='new private directory under an existing real parent')
    parser.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        output = _destination(args.out)
        if args._worker:
            return _worker(output)
        # A fresh interpreter prevents an imported caller's engine globals,
        # PYTHONPATH, plugins or home settings from affecting this fixture.
        environment = {key: value for key, value in os.environ.items()
                       if key in {'PATH', 'LANG', 'LC_ALL'}}
        result = subprocess.run([sys.executable, '-I', '-B', '-S', str(Path(__file__).resolve()),
                                 '--_worker', '--out', str(output)], env=environment, timeout=120,
                                check=False)
        return result.returncode
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'status': 'FAIL', 'error_class': type(exc).__name__,
                          'reason': 'rehearsal requires a new output directory and an isolated child'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
