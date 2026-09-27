"""Predeclared bounded trials of the real public pipeline; no background work.

Trial plans contain hashes and operator labels, not copied applicant documents.
Every reported result is re-read from canonical productivity history and checked
against ResourceLedger. Even matched starting snapshots support only descriptive
comparisons: live boards, concurrency and elapsed host time can change.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import re
import stat
import time

import productivity_service as service
from safe_io import atomic_json, canonical, digest, file_lock, loads

SCHEMA = 'keel.productivity.trial.v1'
MAX_CYCLES = 100
MAX_PLAN_BYTES = 65536
DEFAULT_OPTIONS = {'max_requests': 8, 'timeout': 30, 'max_boards': 2, 'max_new': 50,
                   'verify_limit': 100, 'target_verified': 20, 'backlog_limit': 200,
                   'titles': [], 'locations': []}
BUILD_FILES = ('engines/productivity_trials.py', 'engines/productivity_service.py',
               'engines/productivity_history.py', 'engines/pipeline_service.py',
               'engines/source_scheduler.py', 'engines/safe_http.py',
               'engines/safe_io.py', 'keel_efficiency/ledger.py')
FLAGS = {'execution_authorized': False, 'submission_authorized': False,
         'paid_services_required': False, 'production_savings_proven': False,
         'causal_improvement_established': False, 'credits_per_verified_completion': None,
         'external_credit_micros': None, 'application_completions_verified': None}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _sha(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _identity(trial_id, label, max_cycles):
    _require(type(trial_id) is str and service.IDENT.fullmatch(trial_id), 'trial_id_invalid')
    _require(type(label) is str and 0 < len(label) <= 100 and
             not any(ord(char) < 32 for char in label), 'trial_label_invalid')
    _require(type(max_cycles) is int and 1 <= max_cycles <= MAX_CYCLES, 'trial_cycle_limit_invalid')


def _options(options):
    _require(type(options) is dict and not set(options) - set(DEFAULT_OPTIONS), 'trial_options_invalid')
    result = copy.deepcopy(DEFAULT_OPTIONS)
    result.update(copy.deepcopy(options))
    for name, maximum in (('max_requests', 1000), ('max_boards', 16), ('max_new', 2000),
                          ('verify_limit', 1000), ('target_verified', 10000), ('backlog_limit', 100000)):
        service.pipeline._bounded(result[name], name, maximum)
    service.pipeline._budget(result['timeout'])
    for name in ('titles', 'locations'):
        terms = result[name]
        _require(type(terms) in (tuple, list) and len(terms) <= 32 and
                 all(type(term) is str and 0 < len(term) <= 200 for term in terms), 'trial_filters_invalid')
        result[name] = list(terms)
    return result


def _paths(workspace, trial_id, *, create=False):
    root, parent, _ = service._paths(workspace, create=create)
    service._bound_workspace(root)
    folder = parent / 'trials'
    _require(not folder.is_symlink(), 'trial_directory_symlink')
    if create:
        folder.mkdir(mode=0o700, exist_ok=True)
    if folder.exists():
        info = folder.stat()
        _require(folder.is_dir() and folder.resolve(strict=True) == folder and
                 info.st_uid == os.getuid() and not info.st_mode & 0o077, 'trial_directory_not_private')
    return root, folder, folder / (digest(trial_id) + '.json')


def _read(path, *, limit=MAX_PLAN_BYTES, missing=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return missing
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'trial_input_not_regular')
        raw = stream.read(limit + 1)
    _require(len(raw) <= limit, 'trial_input_too_large')
    return loads(raw)


def _run_ids(root, trial_id, max_cycles):
    prefix = 'trial:' + digest({'workspace': str(root), 'trial_id': trial_id})[:48]
    return [prefix + ':' + str(index).zfill(3) for index in range(max_cycles)]


def _build():
    root = Path(__file__).resolve().parents[1]
    files = {}
    for name in BUILD_FILES:
        with (root / name).open('rb') as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        _require(len(raw) <= 4 * 1024 * 1024, 'trial_build_file_too_large')
        files[name] = hashlib.sha256(raw).hexdigest()
    return files


def _workload(root):
    # Hold the existing writer locks while hashing the actual input documents.
    # Missing documents are distinct from explicit empty documents.
    names = ['data/queues/' + name + '-queue.json' for name in service.pipeline.QUEUES]
    names += ['data/application-ledger.json', 'data/sources.json']
    with service.pipeline.queue_lock(timeout=10, owner='productivity:trial-snapshot'):
        with file_lock(root / 'data/sources.json.lock'):
            files = {name: digest(_read(root / name, limit=16 * 1024 * 1024,
                                       missing={'trial_document_missing': True})) for name in names}
    return files


def _budget(ledger, scope_id):
    service._ledger(ledger, scope_id, required=True)
    chain, seen = [], set()
    current = scope_id
    while current is not None:
        _require(current not in seen and len(chain) < 64, 'trial_budget_ancestry_invalid')
        seen.add(current)
        row = ledger.snapshot(current)
        chain.append({key: copy.deepcopy(row[key]) for key in ('scope_id', 'parent_id', 'limits')})
        current = row['parent_id']
    return {'accounting_namespace': service._accounting_namespace(ledger, scope_id), 'budget_chain': chain}


def _load(path, root, trial_id):
    document = _read(path)
    _require(document is not None, 'productivity_trial_missing')
    _require(type(document) is dict and set(document) == {'schema', 'plan', 'plan_sha256', 'stop'}
             and document['schema'] == SCHEMA and _sha(document['plan_sha256'])
             and digest(document['plan']) == document['plan_sha256'], 'trial_plan_digest_invalid')
    plan = document['plan']
    fields = {'trial_id', 'label', 'max_cycles', 'run_ids', 'options', 'workspace',
              'accounting_namespace', 'budget_chain', 'transport', 'created_at',
              'initial_documents', 'initial_workload_sha256', 'build_files', 'build_sha256'}
    _require(type(plan) is dict and set(plan) == fields, 'trial_plan_fields_invalid')
    _identity(plan['trial_id'], plan['label'], plan['max_cycles'])
    _require(plan['trial_id'] == trial_id and plan['workspace'] == str(root)
             and plan['run_ids'] == _run_ids(root, trial_id, plan['max_cycles'])
             and plan['options'] == _options(plan['options']) and service._number(plan['created_at'])
             and plan['transport'] in ('injected', 'public_https'), 'trial_plan_binding_invalid')
    expected_documents = {'data/queues/' + name + '-queue.json' for name in service.pipeline.QUEUES}
    expected_documents |= {'data/application-ledger.json', 'data/sources.json'}
    for name, expected in (('initial_documents', expected_documents), ('build_files', set(BUILD_FILES))):
        _require(type(plan[name]) is dict and set(plan[name]) == expected and
                 all(_sha(value) for value in plan[name].values()), 'trial_plan_hashes_invalid')
    _require(plan['initial_workload_sha256'] == digest(plan['initial_documents']) and
             plan['build_sha256'] == digest(plan['build_files']), 'trial_plan_snapshot_invalid')
    stop = document['stop']
    if stop is not None:
        _require(type(stop) is dict and set(stop) == {'run_id', 'index', 'status', 'response_status',
                 'receipt_sha256', 'at', 'reason'} and type(stop['index']) is int and
                 0 <= stop['index'] < plan['max_cycles'] and stop['run_id'] == plan['run_ids'][stop['index']]
                 and all(type(stop[key]) is str and 0 < len(stop[key]) <= 100
                         for key in ('status', 'response_status', 'reason'))
                 and (stop['receipt_sha256'] is None or _sha(stop['receipt_sha256']))
                 and service._number(stop['at']), 'trial_stop_invalid')
    return document


def _stop(result):
    return (result.get('status') not in ('COMPLETE', 'PARTIAL', 'PARTIAL_SOURCES') or
            result.get('manual_recovery_required') is True or result.get('stage') == 'idle')


def _expected_binding(plan, ledger, scope_id):
    options = plan['options']
    return service._run_binding(plan['workspace'], ledger, scope_id,
        policy={'target_verified': options['target_verified'], 'backlog_limit': options['backlog_limit']},
        **{key: options[key] for key in ('max_requests', 'timeout', 'max_boards', 'max_new',
                                        'verify_limit', 'titles', 'locations')},
        transport=plan['transport'])


def run_trial(workspace, *, trial_id, label='candidate', max_cycles=3, ledger=None,
              scope_id=None, live=False, fetcher=None, clock=time.time,
              monotonic=time.monotonic, **cycle_options):
    """Execute at most the predeclared cycles; inspection is the default.

    Live use needs the existing shared ledger. Trial ID, options, scope ancestry,
    source build and transport are immutable. Repeating a trial replays completed
    run IDs without dispatch; missing/uncertain intents retain existing recovery
    gates. A stopped trial never invents results for unattempted future cycles.
    """
    _identity(trial_id, label, max_cycles)
    options = _options(cycle_options)
    _require(type(live) is bool and (fetcher is None or callable(fetcher)), 'trial_invocation_invalid')
    if not live:
        preview = service.run_once(workspace, ledger=ledger, scope_id=scope_id,
                                   clock=clock, live=False, **options)
        return {'schema': SCHEMA, 'trial_id': trial_id, 'label': label, 'max_cycles': max_cycles,
                'dry_run': True, 'network_calls': 0, 'preview': preview, **FLAGS}
    budget = _budget(ledger, scope_id)
    root, folder, path = _paths(workspace, trial_id, create=True)
    build = _build()
    requested = {'label': label, 'max_cycles': max_cycles, 'options': options, **budget,
                 'transport': 'injected' if fetcher is not None else 'public_https',
                 'build_files': build, 'build_sha256': digest(build)}
    with file_lock(folder / (digest(trial_id) + '.lock'), timeout=0):
        if path.exists() or path.is_symlink():
            document = _load(path, root, trial_id)
            _require(all(document['plan'][key] == value for key, value in requested.items()),
                     'trial_id_binding_conflict')
        else:
            # Use the controller's lock so a cooperating cycle cannot start
            # between checking predeclared IDs and capturing the starting state.
            with file_lock(folder.parent / 'controller.lock', timeout=0):
                ids = _run_ids(root, trial_id, max_cycles)
                for run_id in ids:
                    try:
                        service.get_run(root, run_id, ledger=ledger, scope_id=scope_id, clock=clock)
                    except ValueError as exc:
                        if str(exc) != 'productivity_run_missing':
                            raise
                    else:
                        raise ValueError('trial_run_id_already_exists')
                files = _workload(root)
                plan = {'trial_id': trial_id, 'workspace': str(root), 'run_ids': ids,
                        'created_at': service._stamp(clock), 'initial_documents': files,
                        'initial_workload_sha256': digest(files), **requested}
                document = {'schema': SCHEMA, 'plan': plan, 'plan_sha256': digest(plan), 'stop': None}
                _require(len(canonical(document)) <= MAX_PLAN_BYTES, 'trial_plan_too_large')
                atomic_json(path, document)
        stop = document['stop']['status'] if document['stop'] else 'CYCLE_LIMIT'
        for index, run_id in enumerate(document['plan']['run_ids'] if document['stop'] is None else []):
            result = service.run_once(root, run_id=run_id, ledger=ledger, scope_id=scope_id,
                                      live=True, fetcher=fetcher, clock=clock, monotonic=monotonic, **options)
            locked = ledger.snapshot(scope_id)['locked']
            if _stop(result) or locked:
                stop = 'HELD_BUDGET' if locked else result['status']
                document['stop'] = {'run_id': run_id, 'index': index, 'status': stop,
                    'response_status': result['status'], 'receipt_sha256': result.get('receipt_sha256'),
                    'at': service._stamp(clock), 'reason': 'scope_locked_after_usage' if locked else result.get('reason', stop)}
                atomic_json(path, document)
                break
        report = trial_report(root, trial_id=trial_id, ledger=ledger, scope_id=scope_id, clock=clock)
        return {**report, 'dry_run': False, 'stop_reason': stop}


def trial_report(workspace, *, trial_id, ledger, scope_id, clock=time.time):
    """Read every predeclared canonical receipt and its real ledger binding."""
    _identity(trial_id, 'report', 1)
    root, _, path = _paths(workspace, trial_id)
    document = _load(path, root, trial_id)
    plan = document['plan']
    _require(all(plan[key] == value for key, value in _budget(ledger, scope_id).items()),
             'trial_accounting_namespace_mismatch')
    missing, incomplete, unknown, rows, stages, undispatched = [], [], [], [], {}, []
    stop = document['stop']
    stop_receipt_verified = stop is None
    expected_binding = _expected_binding(plan, ledger, scope_id)
    for index, run_id in enumerate(plan['run_ids']):
        try:
            row = service.get_run(root, run_id, ledger=ledger, scope_id=scope_id, clock=clock)
        except ValueError as exc:
            if str(exc) != 'productivity_run_missing':
                raise
            if stop is not None and index >= stop['index']:
                undispatched.append({'run_id': run_id, 'reason': stop['reason']})
            else:
                missing.append(run_id)
            continue
        _require(stop is None or index <= stop['index'], 'trial_receipt_after_recorded_stop')
        _require(row['binding'] == expected_binding, 'trial_run_configuration_mismatch')
        if stop is not None and index == stop['index']:
            _require(row['receipt_sha256'] == stop['receipt_sha256'] and
                     (row['result'] is None or row['result']['status'] == stop['response_status']),
                     'trial_stop_receipt_mismatch')
            stop_receipt_verified = row['result'] is not None
        result = row['result']
        rows.append({'run_id': run_id, 'request_id': row['request_id'], 'phase': row['phase'],
                     'receipt_sha256': row['receipt_sha256'],
                     'stage': result['stage'] if result is not None else None,
                     'status': result['status'] if result is not None else None})
        if row['phase'] != 'COMPLETE' or result is None:
            incomplete.append(run_id)
        if result is None:
            continue
        stage = result['stage']
        unit = ('deduplicated_lead_added' if stage == 'discover' else
                'committed_live_posting_presence' if stage == 'verify' else None)
        item = stages.setdefault(stage, {'runs': 0, 'calls': 0, 'compute_ms': 0,
            'stage_results_known': 0, 'unknown_progress_runs': 0, 'result_unit': unit})
        item['runs'] += 1
        item['calls'] += result['usage']['calls']
        item['compute_ms'] += result['usage']['compute_ms']
        progress = result['progress']
        if result['status'] == 'UNKNOWN' or result['manual_recovery_required'] or any(value is None for value in progress.values()):
            unknown.append(run_id)
            item['unknown_progress_runs'] += 1
        else:
            item['stage_results_known'] += (progress['added_leads'] if stage == 'discover'
                                            else progress['committed_posting_presence'] if stage == 'verify' else 0)
    complete = not missing and not incomplete and not unknown and not undispatched and stop_receipt_verified
    for item in stages.values():
        count = item['stage_results_known']
        for name in ('calls', 'compute_ms'):
            item[name + '_per_stage_result'] = item[name] / count if complete and count else None
    return {'schema': SCHEMA + '.report', 'trial_id': trial_id, 'label': plan['label'],
            'plan_sha256': document['plan_sha256'], 'initial_workload_sha256': plan['initial_workload_sha256'],
            'build_sha256': plan['build_sha256'], 'transport': plan['transport'],
            'predeclared_runs': len(plan['run_ids']), 'receipt_runs': len(rows), 'receipts': rows,
            'missing_run_ids': missing, 'incomplete_run_ids': incomplete, 'unknown_outcome_run_ids': unknown,
            'planned_not_dispatched': undispatched, 'stop': copy.deepcopy(stop),
            'stop_evidence': ('canonical_run_receipt' if stop is not None and stop_receipt_verified else
                              'local_stop_record_only' if stop is not None else None),
            'measurement_status': ('COMPLETE' if complete else 'STOPPED' if undispatched and
                                   not missing and not incomplete and not unknown and stop_receipt_verified
                                   else 'INCONCLUSIVE'), 'stages': stages,
            'credit_measurement': 'unobserved', 'comparison_kind': 'descriptive_observational',
            'request_count_scope': 'bounded_reader_dispatches; not sockets or redirect hops',
            'compute_ms_semantics': 'cooperative_cycle_wall_time; not CPU or GPU metering', **FLAGS}


def compare_trials(workspace, *, baseline_trial_id, candidate_trial_id, ledger, scope_id, clock=time.time):
    """Compare actual complete cohorts; never accept uploaded success summaries."""
    first = trial_report(workspace, trial_id=baseline_trial_id, ledger=ledger, scope_id=scope_id, clock=clock)
    second = trial_report(workspace, trial_id=candidate_trial_id, ledger=ledger, scope_id=scope_id, clock=clock)
    root, _, path_a = _paths(workspace, baseline_trial_id)
    _, _, path_b = _paths(workspace, candidate_trial_id)
    a, b = _load(path_a, root, baseline_trial_id)['plan'], _load(path_b, root, candidate_trial_id)['plan']
    reasons = []
    if set(a['run_ids']) & set(b['run_ids']):
        reasons.append('run_cohorts_overlap')
    if a['initial_workload_sha256'] != b['initial_workload_sha256']:
        reasons.append('starting_workloads_differ')
    if a['max_cycles'] != b['max_cycles']:
        reasons.append('predeclared_cycle_counts_differ')
    if a['budget_chain'] != b['budget_chain'] or any(a['options'][key] != b['options'][key]
                                                  for key in ('max_requests', 'timeout')):
        reasons.append('resource_limits_differ')
    if a['options'] != b['options']:
        reasons.append('work_defining_options_differ')
    if a['transport'] != b['transport']:
        reasons.append('transport_provenance_differs')
    if first['measurement_status'] != 'COMPLETE' or second['measurement_status'] != 'COMPLETE':
        reasons.append('cohort_measurements_incomplete')
    if {key: item['result_unit'] for key, item in first['stages'].items()} != {
            key: item['result_unit'] for key, item in second['stages'].items()}:
        reasons.append('observed_stage_units_differ')
    comparisons = {}
    if not reasons:
        for stage, before in first['stages'].items():
            after = second['stages'][stage]
            comparisons[stage] = {'result_unit': before['result_unit'],
                'stage_results_delta': after['stage_results_known'] - before['stage_results_known'],
                'calls_delta': after['calls'] - before['calls'],
                'compute_ms_delta': after['compute_ms'] - before['compute_ms'],
                'calls_per_stage_result_delta': (after['calls_per_stage_result'] - before['calls_per_stage_result'])
                    if before['calls_per_stage_result'] is not None and after['calls_per_stage_result'] is not None else None}
    return {'schema': SCHEMA + '.comparison', 'baseline': first, 'candidate': second,
            'comparable': not reasons, 'reasons': reasons, 'stage_differences': comparisons,
            'comparison_kind': 'descriptive_observational',
            'live_external_inputs_controlled': False, 'statistical_significance_established': False, **FLAGS}
