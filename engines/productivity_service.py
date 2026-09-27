"""Bounded, owner-operated productivity cycles for the real public pipeline.

One public-read stage per cycle, no model calls or application execution. The
journal binds run IDs to inputs and retains uncertain effects across restarts.
ResourceLedger remains the sole resource accountant; credits are unobserved.
"""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path
import re
import stat
import time
from collections import Counter

import pipeline_service as pipeline
import source_scheduler
from safe_io import atomic_json, canonical, digest, file_lock, loads
from keel_efficiency.ledger import BudgetExceeded, ResourceLedger

SCHEMA = 'keel.productivity.v1'
MAX_RUNS = 256
MAX_STATE_BYTES = 2 * 1024 * 1024
BACKOFF_AFTER = 3
BACKOFF_SECONDS = 300
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
FLAGS = {'execution_authorized': False, 'submission_authorized': False,
         'paid_services_required': False, 'application_completions_verified': None,
         'credits_per_verified_completion': None}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**12


def _stamp(clock):
    value = clock()
    _require(_number(value), 'productivity_clock_invalid')
    return float(value)


def _policy(target_verified, backlog_limit):
    pipeline._bounded(target_verified, 'target_verified', 10000)
    pipeline._bounded(backlog_limit, 'backlog_limit', 100000)
    return {'target_verified': target_verified, 'backlog_limit': backlog_limit}


def _paths(workspace, *, create=False):
    root = Path(os.path.abspath(workspace))
    _require(root.is_dir() and root.resolve(strict=True) == root, 'productivity_workspace_must_be_real')
    data = root / 'data'
    _require(data.is_dir() and data.resolve(strict=True) == data, 'productivity_data_must_be_real')
    folder = data / 'productivity'
    _require(not folder.is_symlink(), 'productivity_directory_symlink')
    if create:
        folder.mkdir(mode=0o700, exist_ok=True)
    if folder.exists():
        _require(folder.is_dir() and folder.resolve(strict=True) == folder, 'productivity_directory_invalid')
    return root, folder, folder / 'journal.json'


def _bound_workspace(root):
    import queue_io
    import log_event
    import keel_paths
    import host_cooldowns
    _require(Path(queue_io._LOCK_PATH).absolute() == root / 'hidden_files/queue.lock'
             and Path(log_event.EVENTS).absolute() == root / 'data/telemetry/events.jsonl'
             and Path(keel_paths.HOME).absolute() == root
             and Path(host_cooldowns.cooldown_dir()).absolute() == root / 'hidden_files/http-cooldowns'
             and Path(os.environ.get('KEEL_HOME', '')).absolute() == root,
             'productivity_workspace_runtime_binding_mismatch')


def _blank(now):
    return {'schema': SCHEMA, 'last_now': now, 'namespace': None, 'runs': {}, 'stages': {}}


def _load(path, now):
    if not path.exists():
        _require(not path.is_symlink(), 'productivity_journal_symlink')
        return _blank(now)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'productivity_journal_must_be_regular')
        raw = stream.read(MAX_STATE_BYTES + 1)
    _require(len(raw) <= MAX_STATE_BYTES, 'productivity_journal_too_large')
    state = loads(raw)
    _require(type(state) is dict and set(state) == {'schema', 'last_now', 'namespace', 'runs', 'stages'}
             and state['schema'] == SCHEMA and _number(state['last_now']) and now >= state['last_now'],
             'productivity_journal_or_clock_invalid')
    namespace = state['namespace']
    _require(namespace is None or type(namespace) is dict and set(namespace) == {'workspace'}
             and all(type(value) is str and 0 < len(value) <= 4096 for value in namespace.values()),
             'productivity_namespace_invalid')
    _require(type(state['runs']) is dict and len(state['runs']) <= MAX_RUNS
             and (not state['runs'] or namespace is not None), 'productivity_runs_invalid')
    for run_id, row in state['runs'].items():
        _require(type(run_id) is str and IDENT.fullmatch(run_id) is not None and type(row) is dict
                 and set(row) == {'binding', 'request_id', 'stage', 'phase', 'result', 'receipt_sha256', 'recovery', 'accounting_namespace'}
                 and row['phase'] in ('INTENT', 'RECORDED', 'COMPLETE', 'CANCELLED')
                 and type(row['binding']) is str and re.fullmatch('[0-9a-f]{64}', row['binding'])
                 and type(row['request_id']) is str
                 and row['request_id'] == 'productivity:' + digest({'workspace': namespace['workspace'], 'run_id': run_id})
                 and row['stage'] in ('idle', 'discover', 'verify'), 'productivity_run_invalid')
        accounting = row['accounting_namespace']
        _require(type(accounting) is dict and set(accounting) == {'ledger_path_sha256', 'scope_id'}
                 and type(accounting['ledger_path_sha256']) is str and re.fullmatch('[0-9a-f]{64}', accounting['ledger_path_sha256'])
                 and type(accounting['scope_id']) is str and 0 < len(accounting['scope_id']) <= 200, 'productivity_accounting_namespace_invalid')
        _require(row['recovery'] is None or type(row['recovery']) is dict and set(row['recovery']) == {'at', 'queue_sha256'}
                 and _number(row['recovery']['at']) and type(row['recovery']['queue_sha256']) is str
                 and re.fullmatch('[0-9a-f]{64}', row['recovery']['queue_sha256']), 'productivity_recovery_invalid')
        if row['phase'] in ('INTENT', 'CANCELLED'):
            _require(row['result'] is None and row['receipt_sha256'] is None, 'productivity_intent_invalid')
        else:
            _require(type(row['result']) is dict and digest(row['result']) == row['receipt_sha256']
                     and row['result'].get('run_id') == run_id, 'productivity_result_receipt_invalid')
            _validate_result(row['result'], run_id, row)
    _require(type(state['stages']) is dict and not set(state['stages']) - {'discover', 'verify'},
             'productivity_stages_invalid')
    for row in state['stages'].values():
        _require(type(row) is dict and set(row) == {'zero_progress_runs', 'next_at'}
                 and type(row['zero_progress_runs']) is int and 0 <= row['zero_progress_runs'] <= BACKOFF_AFTER
                 and _number(row['next_at']), 'productivity_backoff_invalid')
    return state


def _count(value, maximum=10**9):
    return type(value) is int and 0 <= value <= maximum


def _validate_supply(value):
    _require(type(value) is dict and set(value) == {'queue_rows', 'ledger_rows', 'registered_sources',
             'states', 'exact_question_groups', 'roles_with_questions'}, 'productivity_supply_invalid')
    _require(all(_count(value[key]) for key in value if key != 'states')
             and type(value['states']) is dict and len(value['states']) <= 16
             and all(type(key) is str and len(key) <= 100 and _count(count) for key, count in value['states'].items())
             and sum(value['states'].values()) == value['queue_rows'], 'productivity_supply_counts_invalid')


def _validate_result(value, run_id, row):
    expected = {'schema', 'run_id', 'status', 'stage', 'reason', 'before', 'after', 'progress', 'usage',
                'error_class', 'manual_recovery_required', 'outbox', 'request_id', 'dry_run',
                'credit_measurement', 'request_count_scope', 'compute_ms_semantics'} | set(FLAGS)
    _require(set(value) == expected and value['schema'] == SCHEMA and value['run_id'] == run_id
             and value['request_id'] == row['request_id'] and value['stage'] == row['stage']
             and type(value['status']) is str and 0 < len(value['status']) <= 80
             and type(value['reason']) is str and 0 < len(value['reason']) <= 100
             and type(value['manual_recovery_required']) is bool and value['dry_run'] is False,
             'productivity_result_invalid')
    _require(all(type(value[key]) is type(expected_value) and value[key] == expected_value
                 for key, expected_value in FLAGS.items()), 'productivity_result_authority_invalid')
    _validate_supply(value['before']); _validate_supply(value['after'])
    usage = value['usage']
    _require(type(usage) is dict and set(usage) == {'calls', 'compute_ms', 'input_tokens', 'output_tokens', 'external_credit_micros'}
             and _count(usage['calls'], 1000) and _count(usage['compute_ms'], 2**53-1)
             and type(usage['input_tokens']) is int and usage['input_tokens'] == 0
             and type(usage['output_tokens']) is int and usage['output_tokens'] == 0
             and usage['external_credit_micros'] is None, 'productivity_result_usage_invalid')
    progress = value['progress']
    _require(type(progress) is dict and set(progress) == {'added_leads', 'committed_posting_presence'}
             and all(_count(amount, 2000) or value['status'] == 'UNKNOWN' and amount is None for amount in progress.values()),
             'productivity_result_progress_invalid')
    _require(value['credit_measurement'] == 'unobserved'
             and value['request_count_scope'] == 'bounded_reader_dispatches; not sockets or redirect hops'
             and value['compute_ms_semantics'] == 'cooperative_cycle_wall_time; not CPU or GPU metering'
             and (value['error_class'] is None or type(value['error_class']) is str and 0 < len(value['error_class']) <= 100),
             'productivity_measurement_semantics_invalid')
    outbox = value['outbox']
    _require(outbox is None or type(outbox) is dict and set(outbox) == {'emitted', 'pending', 'error_count'}
             and all(_count(amount) for amount in outbox.values()), 'productivity_outbox_invalid')


def _save(path, state, now):
    _require(now >= state['last_now'], 'productivity_clock_regressed')
    state['last_now'] = now
    _require(len(canonical(state)) <= MAX_STATE_BYTES, 'productivity_journal_capacity')
    atomic_json(path, state)


def _supply(workspace):
    report = pipeline.supply_report(workspace)
    _require(report['conservation_ok'] is True, 'productivity_supply_conservation_failed')
    return {'queue_rows': report['queue_rows'], 'ledger_rows': report['ledger_rows'],
            'registered_sources': report['registered_sources'],
            'states': report['mutually_exclusive_supply_states'],
            'exact_question_groups': len(report['exact_question_groups']),
            'roles_with_questions': len({role for group in report['exact_question_groups'] for role in group['roles']})}


def _choice(supply, policy, state, now):
    counts = supply['states']
    if counts.get('posting_verified_form_and_approval_separate', 0) >= policy['target_verified']:
        return 'idle', 'verified_posting_target_met'
    if counts.get('telemetry_pending', 0):
        return 'idle', 'observation_outbox_pending'
    if counts.get('actionable_verification', 0):
        stage = 'verify'
    elif counts.get('verification_cooldown', 0):
        return 'idle', 'verification_cooldown'
    elif supply['queue_rows'] >= policy['backlog_limit']:
        return 'idle', 'queue_backlog_limit'
    elif supply['roles_with_questions']:
        return 'idle', 'human_decisions_pending'
    elif any(counts.get(name, 0) for name in ('identity_conflict', 'missing_exact_posting_identity', 'invalid_cooldown')):
        return 'idle', 'existing_backlog_requires_repair'
    elif not supply['registered_sources']:
        return 'idle', 'no_registered_sources'
    else:
        stage = 'discover'
    if now < state['stages'].get(stage, {}).get('next_at', 0):
        return 'idle', stage + '_low_yield_cooldown'
    return stage, 'actionable_verification' if stage == 'verify' else 'verified_supply_below_target'


def _pending(state):
    return [run_id for run_id, row in state['runs'].items() if row['phase'] not in ('COMPLETE', 'CANCELLED')
            or row['result'] and row['result'].get('manual_recovery_required') and row['recovery'] is None]


def _ledger(ledger, scope_id, *, required=False):
    _require((ledger is None) == (scope_id is None), 'productivity_ledger_scope_pair_required')
    if ledger is None:
        _require(not required, 'live_productivity_requires_shared_ledger_and_scope')
        return None
    _require(isinstance(ledger, ResourceLedger), 'productivity_resource_ledger_required')
    return ledger.snapshot(scope_id)


def _accounting_namespace(ledger, scope_id):
    return {'ledger_path_sha256': digest(str(Path(ledger.path).absolute())), 'scope_id': scope_id}


def _aggregate(state, namespace=None):
    stages = {}
    unknown = 0
    cancelled = 0
    for row in state['runs'].values():
        if namespace is not None and row['accounting_namespace'] != namespace:
            continue
        report = row['result']
        if row['phase'] == 'CANCELLED':
            cancelled += 1
            continue
        if report is None:
            unknown += 1
            continue
        stage = report['stage']
        item = stages.setdefault(stage, {'runs': 0, 'requests': 0, 'elapsed_ms': 0,
            'added_leads': 0, 'committed_posting_presence': 0, 'statuses': Counter(),
            'external_credit_micros': None, 'progress_unknown_runs': 0})
        item['runs'] += 1
        item['requests'] += report['usage']['calls']
        item['elapsed_ms'] += report['usage']['compute_ms']
        item['added_leads'] += report['progress']['added_leads'] or 0
        item['committed_posting_presence'] += report['progress']['committed_posting_presence'] or 0
        item['progress_unknown_runs'] += int(report['progress']['added_leads'] is None or report['progress']['committed_posting_presence'] is None)
        item['statuses'][report['status']] += 1
    for stage, item in stages.items():
        item['statuses'] = dict(item['statuses'])
        amount = item['added_leads'] if stage == 'discover' else item['committed_posting_presence']
        item['requests_per_stage_result'] = item['requests'] / amount if amount and not unknown and not item['progress_unknown_runs'] else None
        item['coverage'] = 'incomplete' if unknown or item['progress_unknown_runs'] else 'retained_controller_runs_only'
        item['result_unit'] = 'deduplicated_lead_added' if stage == 'discover' else 'committed_live_posting_presence' if stage == 'verify' else None
    return {'stages': stages, 'runs_without_measured_result': unknown, 'cancelled_before_dispatch': cancelled,
            'retained_runs': len(state['runs']), 'run_capacity': MAX_RUNS,
            'accounting_namespace': namespace,
            'scope_coverage': list({digest(row['accounting_namespace']): row['accounting_namespace'] for row in state['runs'].values()
                                    if namespace is None or row['accounting_namespace'] == namespace}.values()),
            'credit_measurement': 'unobserved',
            'compute_ms_semantics': 'cooperative_cycle_wall_time; not CPU or GPU metering',
            'request_count_scope': 'bounded_reader_dispatches; not sockets or redirect hops'}


def status(workspace, *, ledger=None, scope_id=None, target_verified=20, backlog_limit=200, clock=time.time):
    policy = _policy(target_verified, backlog_limit)
    root, _, path = _paths(workspace)
    _bound_workspace(root)
    now = _stamp(clock)
    state = _load(path, now)
    _require(state['namespace'] is None or state['namespace']['workspace'] == str(root), 'productivity_namespace_mismatch')
    budget = _ledger(ledger, scope_id)
    namespace = _accounting_namespace(ledger, scope_id) if ledger is not None else None
    supply = _supply(root)
    stage, reason = _choice(supply, policy, state, now)
    pending = _pending(state)
    return {'schema': SCHEMA, 'status': 'HELD_RECOVERY' if pending else 'OBSERVED',
            'next_stage': 'idle' if pending else stage, 'reason': 'unfinished_or_uncertain_run' if pending else reason,
            'supply': supply, 'policy': policy, 'budget': budget, 'pending_run_ids': pending,
            'stage_backoff': copy.deepcopy(state['stages']), 'accounting_namespace': state['namespace'], 'metrics': _aggregate(state, namespace), **FLAGS}


def _validate_accounting(state, ledger):
    """Detect missing or rolled-back ledger requests before further dispatch."""
    ledger_hash = digest(str(Path(ledger.path).absolute()))
    for row in state['runs'].values():
        if row['accounting_namespace']['ledger_path_sha256'] != ledger_hash:
            continue
        request = ledger.request(row['request_id'])
        _require(request['scope_id'] == row['accounting_namespace']['scope_id']
                 and request['metadata'] == {'purpose': 'public_pipeline_productivity', 'binding_sha256': row['binding']},
                 'productivity_ledger_binding_mismatch')
        if row['phase'] == 'CANCELLED':
            _require(request['state'] == 'CANCELLED', 'productivity_ledger_cancellation_missing')
        elif row['phase'] == 'COMPLETE':
            _require(request['state'] in ('UNKNOWN', 'SETTLED') and all(value is None or request['usage'][key] == value
                     for key, value in row['result']['usage'].items()), 'productivity_ledger_receipt_missing')
        else:
            _require(request['state'] in ('RESERVED', 'DISPATCHED', 'UNKNOWN', 'SETTLED', 'CANCELLED'),
                     'productivity_ledger_intent_invalid')


def _settle_recorded(ledger, row):
    return ledger.settle(row['request_id'], row['result']['usage'])


def _response(row, *, replay=False, accounting=None):
    return {**copy.deepcopy(row['result']), 'replayed': replay, 'receipt_sha256': row['receipt_sha256'],
            'accounting_state': accounting, 'journal_phase': row['phase'],
            'recovery_acknowledgment': copy.deepcopy(row['recovery'])}


def run_once(workspace, *, run_id=None, ledger=None, scope_id=None, live=False,
             max_requests=8, timeout=30, max_boards=2, max_new=50, verify_limit=100,
             target_verified=20, backlog_limit=200, titles=(), locations=(),
             fetcher=None, clock=time.time, monotonic=time.monotonic):
    """Inspect by default; explicitly live calls execute one bounded public stage.

    A trusted fixture fetcher has the same reader admission limits. It is never
    accepted from JSON or a model. An incomplete intent is never retried. The
    operator must investigate its stores and ledger; no automatic refund exists.
    """
    policy = _policy(target_verified, backlog_limit)
    for value, name, maximum in ((max_requests, 'max_requests', 1000), (max_boards, 'max_boards', 16),
                                  (max_new, 'max_new', 2000), (verify_limit, 'verify_limit', 1000)):
        pipeline._bounded(value, name, maximum)
    pipeline._budget(timeout)
    _require(type(live) is bool and (fetcher is None or callable(fetcher)), 'productivity_invocation_invalid')
    for terms in (titles, locations):
        _require(type(terms) in (tuple, list) and len(terms) <= 32 and all(type(term) is str and 0 < len(term) <= 200 for term in terms),
                 'productivity_filters_invalid')
    if not live:
        report = status(workspace, ledger=ledger, scope_id=scope_id, clock=clock, **policy)
        return {**report, 'dry_run': True, 'network_calls': 0}
    _require(type(run_id) is str and IDENT.fullmatch(run_id) is not None, 'live_productivity_run_id_required')
    _ledger(ledger, scope_id, required=True)
    root, folder, path = _paths(workspace)
    _bound_workspace(root)
    root, folder, path = _paths(workspace, create=True)
    _require(not (root / 'DEMO_ONLY.json').exists() or fetcher is not None,
             'synthetic_workspace_cannot_perform_live_productivity_reads')
    ledger_path = Path(ledger.path)
    _require(ledger_path.resolve(strict=True) == ledger_path, 'productivity_ledger_path_invalid')
    binding = digest({'workspace': str(root), 'ledger': str(ledger_path), 'scope_id': scope_id,
        'policy': policy, 'max_requests': max_requests, 'timeout': timeout, 'max_boards': max_boards,
        'max_new': max_new, 'verify_limit': verify_limit, 'titles': list(titles), 'locations': list(locations),
        'transport': 'injected' if fetcher is not None else 'public_https'})
    namespace = {'workspace': str(root)}
    accounting_namespace = _accounting_namespace(ledger, scope_id)
    request_id = 'productivity:' + digest({'workspace': str(root), 'run_id': run_id})
    with file_lock(folder / 'controller.lock', timeout=0):
        now = _stamp(clock)
        state = _load(path, now)
        _validate_accounting(state, ledger)
        prior = state['runs'].get(run_id)
        if prior is not None:
            _require(prior['binding'] == binding, 'productivity_run_id_binding_conflict')
            if prior['phase'] == 'CANCELLED':
                return {'schema': SCHEMA, 'status': 'CANCELLED', 'run_id': run_id,
                        'reason': 'reconciled_before_dispatch', 'replayed': True, 'effects_dispatched': False, **FLAGS}
            if prior['phase'] == 'COMPLETE':
                return _response(prior, replay=True)
            if prior['phase'] == 'RECORDED':
                accounting = _settle_recorded(ledger, prior)
                prior['phase'] = 'COMPLETE'
                _save(path, state, _stamp(clock))
                return _response(prior, replay=True, accounting=accounting['state'])
            return {'schema': SCHEMA, 'status': 'HELD_RECOVERY', 'reason': 'interrupted_intent',
                    'run_id': run_id, 'manual_recovery_required': True, 'replayed': True, **FLAGS}
        _require(state['namespace'] is None or state['namespace'] == namespace, 'productivity_namespace_mismatch')
        pending = _pending(state)
        if pending:
            return {'schema': SCHEMA, 'status': 'HELD_RECOVERY', 'reason': 'unfinished_or_uncertain_run',
                    'pending_run_ids': pending, 'manual_recovery_required': True, **FLAGS}
        _require(len(state['runs']) < MAX_RUNS, 'productivity_run_capacity_exhausted')
        started = _stamp(monotonic)
        before = _supply(root)
        estimate = {'calls': max_requests, 'compute_ms': math.ceil(timeout * 1000),
                    'input_tokens': 0, 'output_tokens': 0, 'external_credit_micros': 0}
        try:
            reserved = ledger.reserve(request_id, scope_id, estimate,
                {'purpose': 'public_pipeline_productivity', 'binding_sha256': binding})
        except BudgetExceeded:
            return {'schema': SCHEMA, 'status': 'HELD_BUDGET', 'run_id': run_id,
                    'reason': 'shared_resource_budget_unavailable', 'supply': before, **FLAGS}
        _require(reserved['state'] == 'RESERVED', 'productivity_existing_dispatch_requires_recovery')
        initial_stage, _ = _choice(before, policy, state, now)
        row = {'binding': binding, 'request_id': request_id, 'stage': initial_stage, 'phase': 'INTENT', 'result': None, 'receipt_sha256': None, 'recovery': None, 'accounting_namespace': accounting_namespace}
        state['namespace'] = namespace
        state['runs'][run_id] = row
        try:
            _save(path, state, now)
        except Exception:
            # This controller has not received dispatch permission. Cancellation
            # is safe and avoids leaking an orphan reservation on disk failure.
            ledger.cancel(request_id)
            raise
        ledger.mark_dispatched(request_id)
        stage, reason, worker, after, error = 'idle', 'not_started', None, before, None
        progress = {'added_leads': 0, 'committed_posting_presence': 0}
        reader = None
        flush = None
        outcome_status = 'IDLE'
        try:
            flush = pipeline.flush_outbox(root)
            after = _supply(root)
            stage, reason = _choice(after, policy, state, _stamp(clock))
            remaining = timeout - (_stamp(monotonic) - started)
            if stage != 'idle' and remaining <= 0:
                stage, reason = 'idle', 'cycle_deadline_before_dispatch'
            if stage != 'idle':
                row['stage'] = stage
                _save(path, state, _stamp(clock))
                reader = pipeline.PublicBoardReader(timeout=remaining, max_requests=max_requests, fetcher=fetcher)
                if stage == 'verify':
                    worker = pipeline.verify(root, limit=verify_limit, timeout=remaining, live=True, reader=reader)
                    progress['committed_posting_presence'] = worker['committed_verdicts'].get('live', 0)
                    outcome_status = ('HELD_HTTP_429' if worker['rate_limit_hold'] else 'PARTIAL' if
                                      worker['write_errors'] or worker['concurrent_conflicts'] or
                                      worker['deferred_without_attempt'] else 'COMPLETE')
                else:
                    capacity = max(0, backlog_limit - after['queue_rows'])
                    worker = source_scheduler.scheduled_discover(root, max_boards=max_boards,
                        max_new=min(max_new, capacity), timeout=remaining, max_requests=max_requests,
                        titles=titles, locations=locations, reader=reader, clock=clock)
                    progress['added_leads'] = worker['added']
                    outcome_status = worker['status']
                after = _supply(root)
        except Exception as exc:
            # A queue or source writer can have committed before raising. No
            # automatic retry or success denominator is manufactured here.
            error = type(exc).__name__
            outcome_status = 'UNKNOWN'
            progress = {'added_leads': None, 'committed_posting_presence': None}
        elapsed = _stamp(monotonic) - started
        _require(elapsed >= 0, 'productivity_monotonic_clock_regressed')
        usage = {'calls': reader.requests if reader is not None else 0,
                 'compute_ms': math.ceil(elapsed * 1000), 'input_tokens': 0, 'output_tokens': 0,
                 'external_credit_micros': None}
        if stage in ('discover', 'verify') and outcome_status != 'UNKNOWN' and reader is not None and reader.requests:
            previous = state['stages'].get(stage, {'zero_progress_runs': 0, 'next_at': 0})
            amount = sum(progress.values())
            zero = 0 if amount else min(BACKOFF_AFTER, previous['zero_progress_runs'] + 1)
            state['stages'][stage] = {'zero_progress_runs': zero,
                'next_at': _stamp(clock) + BACKOFF_SECONDS if zero >= BACKOFF_AFTER else 0}
        result = {'schema': SCHEMA, 'run_id': run_id, 'status': outcome_status, 'stage': stage,
                  'reason': reason, 'before': before, 'after': after, 'progress': progress,
                  'usage': usage, 'error_class': error, 'manual_recovery_required': outcome_status in ('UNKNOWN', 'HELD_RECOVERY'),
                  'outbox': None if flush is None else {'emitted': flush['emitted'], 'pending': flush['pending'], 'error_count': len(flush['errors'])}, 'request_id': request_id, 'dry_run': False,
                  'credit_measurement': 'unobserved',
                  'request_count_scope': 'bounded_reader_dispatches; not sockets or redirect hops',
                  'compute_ms_semantics': 'cooperative_cycle_wall_time; not CPU or GPU metering', **FLAGS}
        row.update(stage=stage, phase='RECORDED', result=result, receipt_sha256=digest(result))
        _save(path, state, _stamp(clock))
        accounting = _settle_recorded(ledger, row)
        row['phase'] = 'COMPLETE'
        _save(path, state, _stamp(clock))
        return _response(row, accounting=accounting['state'])


def recover(workspace, *, run_id, ledger, scope_id, clock=time.time):
    """Explicitly reconcile recorded public work; never infer missing usage.

    A durable result can finish its accounting without dispatch. An operator
    may acknowledge an UNKNOWN result after validating current local intake,
    allowing future idempotent public reads. Its unknown progress and original
    usage remain unchanged. A provably undispatched intent can be cancelled;
    a dispatched intent without a result retains its reservation and blocks
    new work until authoritative host reconciliation is available.
    """
    _require(type(run_id) is str and IDENT.fullmatch(run_id) is not None, 'productivity_run_id_required')
    _ledger(ledger, scope_id, required=True)
    root, folder, path = _paths(workspace)
    _bound_workspace(root)
    _require(path.exists(), 'productivity_run_missing')
    with file_lock(folder / 'controller.lock', timeout=0):
        state = _load(path, _stamp(clock))
        _validate_accounting(state, ledger)
        _require(state['namespace'] == {'workspace': str(root)}, 'productivity_namespace_mismatch')
        row = state['runs'].get(run_id)
        _require(row is not None and row['accounting_namespace'] == _accounting_namespace(ledger, scope_id),
                 'productivity_recovery_namespace_mismatch')
        if row['phase'] in ('INTENT', 'CANCELLED'):
            request = ledger.request(row['request_id'])
            if request['state'] in ('RESERVED', 'CANCELLED'):
                ledger.cancel(row['request_id'])
                row['phase'] = 'CANCELLED'
                _save(path, state, _stamp(clock))
                return {'schema': SCHEMA, 'status': 'CANCELLED', 'run_id': run_id,
                        'reason': 'reconciled_before_dispatch', 'effects_dispatched': False,
                        'reservation_released': True, 'manual_recovery_required': False, **FLAGS}
            return {'schema': SCHEMA, 'status': 'HELD_RECOVERY', 'run_id': run_id,
                    'reason': 'durable_result_missing_authoritative_host_reconciliation_required',
                    'manual_recovery_required': True, 'reservation_released': False, **FLAGS}
        accounting = _settle_recorded(ledger, row)
        row['phase'] = 'COMPLETE'
        if row['result']['manual_recovery_required'] and row['recovery'] is None:
            # These reads validate real current stores, not the untrusted
            # previous success claim. Application outcome states are untouched.
            with pipeline.queue_lock(timeout=10, owner='productivity:recover'):
                documents, application_ledger = pipeline._documents(root)
                snapshot_hash = digest({'queues': {str(path.relative_to(root)): document for path, document in documents.items()},
                                        'ledger': application_ledger})
            if row['stage'] == 'discover':
                source_scheduler.recover_interrupted(root, clock=clock)
            row['recovery'] = {'at': _stamp(clock), 'queue_sha256': snapshot_hash}
        _save(path, state, _stamp(clock))
        return {'schema': SCHEMA, 'status': 'RECOVERED' if row['recovery'] else 'RECORDED',
                'run_id': run_id, 'accounting_state': accounting['state'],
                'previous_outcome': row['result']['status'],
                'previous_progress': copy.deepcopy(row['result']['progress']),
                'receipt_sha256': row['receipt_sha256'], 'reservation_released': False,
                'recovery_acknowledgment': copy.deepcopy(row['recovery']), **FLAGS}
