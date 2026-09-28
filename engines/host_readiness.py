"""Offline observations before a bounded public productivity trial.

This does not acquire writer locks, initialize stores, migrate schemas, reserve
resources, or authorize execution. Reads of separate stores are not a coherent
snapshot. SQLite read-only connections may use WAL coordination sidecars.
Reports contain fixed diagnostic codes and counts, never source documents.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import time

import pipeline_service as pipeline
import productivity_service as productivity
import source_scheduler
from keel_efficiency.ledger import ResourceLedger, RESOURCES, _vector
from safe_io import fresh, loads, rows

SCHEMA = 'keel.host-readiness.v1'
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ROWS = 100000
DEFAULT_ESTIMATE = {'calls': 8, 'compute_ms': 30000, 'input_tokens': 0,
                    'output_tokens': 0, 'external_credit_micros': 0}
POLICY = {'target_verified': 20, 'backlog_limit': 200}


def _require(condition):
    if not condition:
        raise ValueError('host_preflight_input_invalid')


def _root(workspace):
    raw = os.fspath(workspace)
    _require(type(raw) is str and raw and '\x00' not in raw and '..' not in Path(raw).parts)
    root = Path(raw).absolute()
    _require(root.is_dir() and root.resolve(strict=True) == root)
    return root


def _path(root, relative, *, directory=False, optional=False):
    """Check every component without following links outside the workspace."""
    path = root
    for part in Path(relative).parts:
        _require(part not in ('..', '/', ''))
        path = path / part
        try:
            info = path.lstat()
        except FileNotFoundError:
            if optional:
                return root / relative
            raise
        _require(not stat.S_ISLNK(info.st_mode))
        if path != root / relative:
            _require(stat.S_ISDIR(info.st_mode))
    if directory:
        _require(stat.S_ISDIR(info.st_mode))
    else:
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    return path


def _read(root, relative, budget, *, limit=MAX_DOCUMENT_BYTES):
    path = _path(root, relative)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1)
        _require(before.st_size <= limit and before.st_size <= budget[0])
        raw = stream.read(min(limit, budget[0]) + 1)
        after = os.fstat(stream.fileno())
    _require(len(raw) <= limit and len(raw) <= budget[0])
    _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
             (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns))
    budget[0] -= len(raw)
    return loads(raw)


def _documents(root):
    budget = [MAX_TOTAL_BYTES]
    documents = {name: rows(_read(root, 'data/queues/' + name + '-queue.json', budget))
                 for name in pipeline.QUEUES}
    ledger = rows(_read(root, 'data/application-ledger.json', budget))
    _require(sum(map(len, documents.values())) + len(ledger) <= MAX_ROWS)
    for row in [entry for entries in documents.values() for entry in entries] + ledger:
        # These identifiers are used as hash keys by the real controller.
        _require(row.get('role_id') is None or
                 type(row['role_id']) is str and 0 < len(row['role_id']) <= 4096)
    config = _read(root, 'data/sources.json', budget)
    _require(type(config) is dict and type(config.get('sources')) is list
             and len(config['sources']) <= pipeline.MAX_BOARDS)
    found = set()
    enabled = []
    for source in config['sources']:
        _require(type(source) is dict)
        pipeline.parse_source(source.get('ref'))
        _require(source['ref'] not in found)
        found.add(source['ref'])
        _require('enabled' not in source or type(source['enabled']) is bool)
        _require('company_label' not in source or
                 type(source['company_label']) is str and len(source['company_label']) <= 200)
        if source.get('enabled') is True:
            enabled.append(source['ref'])
    return documents, ledger, enabled


def _supply(documents, ledger, sources, now):
    entries = [(name, row) for name, document in documents.items() for row in document]
    indexed, identities, posting_counts = pipeline._identity_index(entries)
    terminal_ids, terminal_keys = pipeline._terminal_ledger_index(ledger)
    counts, questions, roles = Counter(), set(), set()
    stamp = datetime.fromtimestamp(now, timezone.utc)
    for _, row, key in indexed:
        rid = row.get('role_id')
        if not rid or identities[rid] != 1:
            state = 'identity_conflict'
        elif pipeline._held(row) or rid in terminal_ids or key is not None and key in terminal_keys:
            state = 'held_active_or_terminal'
        elif row.get('verification_event_pending'):
            state = 'telemetry_pending'
        elif key is None:
            state = 'missing_exact_posting_identity'
        elif posting_counts[key] != 1:
            state = 'identity_conflict'
        elif (pipeline._observation_matches(row, key)
              and row['posting_verification'].get('verdict') == 'live'
              and fresh(row['posting_verification'].get('observed_at'), 3600, now=stamp)):
            state = 'posting_verified_form_and_approval_separate'
        elif (next_time := pipeline._next_time(row, key)) == 'invalid':
            state = 'invalid_cooldown'
        elif next_time and next_time > stamp:
            state = 'verification_cooldown'
        else:
            state = 'actionable_verification'
        counts[state] += 1
        unresolved = row.get('unresolved') or []
        if isinstance(unresolved, list) and rid:
            for question in unresolved:
                if type(question) is str and question.strip():
                    questions.add(question)
                    roles.add(rid)
    supply = {'queue_rows': len(entries), 'ledger_rows': len(ledger),
              'registered_sources': len(sources), 'states': dict(counts),
              'exact_question_groups': len(questions), 'roles_with_questions': len(roles)}
    integrity = {'missing_role_ids': sum(not row.get('role_id') for _, row in entries),
                 'duplicate_role_ids': sum(count > 1 for rid, count in identities.items() if rid),
                 'duplicate_posting_identities': sum(count > 1 for count in posting_counts.values())}
    return supply, integrity


def _history(root, now):
    _path(root, 'data/productivity', directory=True, optional=True)
    for name in ('history.json', 'history.sqlite3', 'journal.json', 'journal.v1.json'):
        _path(root, 'data/productivity/' + name, optional=True)
    state = productivity._load(root / 'data/productivity/journal.json', now, write=False)
    _require(state['namespace'] is None or state['namespace']['workspace'] == str(root))
    if '_history' in state:
        history = state['_history']
        _, pending = history.pending()
        counts = history.counts()
        detail = {'storage': 'indexed_sqlite', 'retained_runs': counts['runs'],
                  'pending_runs': pending, 'unmeasured_runs': counts['unknown'],
                  'read_coverage': 'metadata_counts_and_at_most_100_pending_rows'}
    else:
        pending = len(productivity._pending(state))
        detail = {'storage': 'legacy_requires_migration' if (root / 'data/productivity/journal.json').exists()
                  else 'not_initialized', 'retained_runs': len(state['runs']), 'pending_runs': pending,
                  'unmeasured_runs': sum(row['result'] is None and row['phase'] != 'CANCELLED'
                                         for row in state['runs'].values()),
                  'read_coverage': 'bounded_legacy_document'}
    return state, detail


def _budget(root, ledger_path, scope_id):
    _require(ledger_path is not None and scope_id is not None)
    raw = os.fspath(ledger_path.path if isinstance(ledger_path, ResourceLedger) else ledger_path)
    _require(type(raw) is str and raw and '\x00' not in raw and '..' not in Path(raw).parts)
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    _require(path.resolve(strict=True) == path and stat.S_ISREG(path.lstat().st_mode)
             and path.lstat().st_nlink == 1)
    ledger = ResourceLedger.open_readonly(path)
    seen, chain = set(), []
    current = scope_id
    while current is not None:
        _require(type(current) is str and current not in seen and len(chain) < 64)
        seen.add(current)
        item = ledger.snapshot(current)
        for name in ('limits', 'used', 'reserved', 'available'):
            _require(type(item[name]) is dict and set(item[name]) == set(RESOURCES))
            _vector(item[name])
        _require(type(item['locked']) is bool)
        _require(item['available'] == {name: max(0, item['limits'][name] - item['used'][name] -
                                                   item['reserved'][name]) for name in RESOURCES})
        chain.append(item)
        current = item['parent_id']
    available = {name: min(item['available'][name] for item in chain) for name in RESOURCES}
    locked = any(item['locked'] for item in chain)
    enough = not locked and all(available[name] >= value for name, value in DEFAULT_ESTIMATE.items())
    return ledger, {'ancestor_count': len(chain) - 1, 'locked': locked, 'available': available,
                    'default_cycle_estimate': dict(DEFAULT_ESTIMATE),
                    'default_cycle_fits_observed_budget': enough,
                    'reservation_created': False, 'atomic_ancestor_snapshot': False}


def inspect(workspace, *, budget_ledger=None, budget_scope=None, clock=time.time):
    """Return sanitized observations; PASS is local evidence, never permission.

    All budgets are re-opened without initialization, even if a caller supplied
    an existing ResourceLedger. No arbitrary exception text enters the report.
    """
    report = {'schema': SCHEMA, 'status': 'BLOCKED', 'local_checks_passed': False,
              'read_only': True, 'offline': True, 'network_calls': 0,
              'application_data_mutated': False, 'schema_migration_performed': False,
              'sqlite_coordination_files_possible': True,
              'coherent_cross_store_snapshot': False, 'production_readiness_established': False,
              'execution_authorized': False, 'submission_authorized': False,
              'paid_services_required': False, 'checks': [], 'supply': None,
              'history': None, 'budget': None, 'suggested_stage': None,
              'host_qualifications': [
                  {'id': name, 'status': 'UNKNOWN', 'reason': 'not_verified_by_offline_preflight'}
                  for name in ('private_executor', 'provider_authenticator', 'coherent_backup_restore',
                               'stopped_old_workers', 'cooperating_private_writers',
                               'receipt_telemetry_sink', 'filesystem_permissions_and_capacity',
                               'network_and_host_cooldowns',
                               'external_board_content', 'actual_credit_usage')],
              'scope': 'observations_before_public_trial_with_default_cycle_limits'}

    def check(name, callback, reason):
        try:
            result = callback()
        except Exception:
            report['checks'].append({'id': name, 'status': 'BLOCKED', 'reason': reason})
            return None
        report['checks'].append({'id': name, 'status': 'PASS', 'reason': 'observed_valid'})
        return result

    root = check('workspace', lambda: _root(workspace), 'workspace_missing_or_not_real')
    if root is None:
        return report
    now = check('clock', lambda: productivity._stamp(clock), 'clock_invalid')
    if now is None:
        return report
    check('runtime_binding', lambda: productivity._bound_workspace(root), 'runtime_workspace_binding_mismatch')
    check('data_directory', lambda: _path(root, 'data', directory=True), 'data_directory_missing_or_unsafe')
    check('demo_boundary', lambda: _require(not (root / 'DEMO_ONLY.json').exists()
          and not (root / 'DEMO_ONLY.json').is_symlink()), 'synthetic_workspace_forbids_public_live_reads')
    documents = check('input_documents', lambda: _documents(root), 'required_document_missing_unsafe_or_invalid')
    if documents is not None:
        found = check('supply_shape', lambda: _supply(*documents, now), 'supply_cannot_be_classified')
        if found is not None:
            report['supply'], integrity = found
            check('queue_identities', lambda: _require(not any(integrity.values())), 'queue_identity_conflicts')
            report['checks'][-1]['counts'] = integrity
    history = check('productivity_history', lambda: _history(root, now), 'history_missing_component_unsafe_or_invalid')
    if history is not None:
        state, report['history'] = history
        check('productivity_recovery', lambda: _require(report['history']['pending_runs'] == 0),
              'unfinished_or_uncertain_productivity_run')
    def scheduler():
        _path(root, 'data/source-scheduler.json', optional=True)
        value = source_scheduler._load(root / 'data/source-scheduler.json', now)
        _require(value['inflight'] is None)
        return value
    schedule = check('source_scheduler', scheduler, 'scheduler_invalid_or_recovery_required')
    budget = check('resource_ledger', lambda: _budget(root, budget_ledger, budget_scope),
                   'existing_budget_and_scope_missing_unsafe_or_invalid')
    if budget is not None:
        ledger, report['budget'] = budget
        check('default_cycle_budget', lambda: _require(report['budget']['default_cycle_fits_observed_budget']),
              'default_cycle_exceeds_available_budget_or_scope_locked')
        if history is not None:
            check('history_accounting', lambda: productivity._validate_accounting(state, ledger, readonly=True),
                  'selected_ledger_history_anchor_or_receipt_invalid')
            report['history']['accounting_coverage'] = 'selected_ledger_only; other_ledger_namespaces_unverified'
    if report['supply'] is not None and history is not None and schedule is not None:
        stage, reason = productivity._choice(report['supply'], POLICY, state, now)
        report['suggested_stage'] = {'stage': stage, 'reason': reason, 'policy': dict(POLICY),
                                     'dispatch_authorized': False, 'admission_recheck_required': True}
        if stage == 'discover':
            sources = documents[2]
            report['suggested_stage']['scheduler_eligible_sources'] = sum(
                schedule['sources'].get(ref, {}).get('next_at', 0) <= now for ref in sources)
            report['suggested_stage']['scheduler_global_cooldown_active'] = schedule['cooldown_until'] > now
    report['local_checks_passed'] = all(row['status'] == 'PASS' for row in report['checks'])
    report['status'] = 'PASS' if report['local_checks_passed'] else 'BLOCKED'
    return report
