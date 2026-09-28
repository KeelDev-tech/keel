"""Read canonical trial evidence and propose bounded follow-up observations.

This is an offline diagnostic, not an optimizer with execution authority. It
does not accept uploaded success summaries, create plans, release holds, or
reserve resources. Repeated reads detect some changes, not a coherent snapshot
or privileged tampering. Declared wall-time ceilings are cooperative limits.
"""
from __future__ import annotations

import math
import time

import host_readiness
import productivity_service as service
import productivity_trials as trials
from keel_efficiency.ledger import ResourceLedger, RESOURCES
from safe_io import digest

SCHEMA = 'keel.productivity.advice.v1'
FLAGS = {'read_only': True, 'offline': True, 'network_calls': 0,
         'application_data_mutated': False, 'budget_reserved': False,
         'execution_authorized': False, 'submission_authorized': False,
         'paid_services_required': False, 'coherent_cross_store_snapshot': False,
         'admission_recheck_required': True, 'production_savings_proven': False,
         'causal_improvement_established': False, 'external_credit_micros': None,
         'credits_per_verified_completion': None,
         'application_completions_verified': None,
         'sqlite_coordination_files_possible': True}

_IDLE_ACTIONS = {
    'verified_posting_target_met': 'review_existing_verified_postings',
    'observation_outbox_pending': 'inspect_pending_observation_delivery',
    'verification_cooldown': 'wait_for_verification_eligibility',
    'queue_backlog_limit': 'review_existing_backlog_without_raising_limits',
    'human_decisions_pending': 'request_only_the_existing_required_human_decisions',
    'existing_backlog_requires_repair': 'repair_exact_identity_or_cooldown_evidence',
    'no_registered_sources': 'configure_explicit_public_sources',
    'discover_low_yield_cooldown': 'inspect_discovery_evidence_and_wait_for_eligibility',
    'verify_low_yield_cooldown': 'inspect_verification_evidence_and_wait_for_eligibility',
    'unfinished_or_uncertain_run': 'reconcile_existing_run_without_redispatch',
}


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _observation(workspace, plan, ledger, scope_id, now):
    options = plan['options']
    return host_readiness.inspect(workspace, budget_ledger=ledger.path, budget_scope=scope_id,
        target_verified=options['target_verified'], backlog_limit=options['backlog_limit'],
        max_requests=options['max_requests'], timeout=options['timeout'], clock=lambda: now)


def _diagnosis(code, action, *, facts=None, run_ids=()):
    return {'code': code, 'next_action': action, 'facts': facts or {}, 'run_ids': list(run_ids),
            'automatic_action_authorized': False}


def _evidence(rows):
    return [{'run_id': row['run_id'], 'sequence': row['sequence'],
             'receipt_sha256': row['receipt_sha256'], 'record_sha256': digest(row)} for row in rows]


def _analyze(report, rows, observed):
    findings = []
    if report['measurement_status'] == 'INCONCLUSIVE':
        findings.append(_diagnosis('trial_measurements_incomplete', 'inspect_canonical_missing_or_uncertain_runs',
            facts={'missing': len(report['missing_run_ids']), 'incomplete': len(report['incomplete_run_ids']),
                   'unknown': len(report['unknown_outcome_run_ids']),
                   'planned_not_dispatched': len(report['planned_not_dispatched'])},
            run_ids=sorted(set(report['missing_run_ids'] + report['incomplete_run_ids'] +
                               report['unknown_outcome_run_ids']))))
    if report['stop'] is not None:
        findings.append(_diagnosis('trial_stopped', 'inspect_stop_before_scheduling_more_work',
            facts={'canonical_stop_evidence': report['stop_evidence'] == 'canonical_run_receipt',
                   'undispatched_cycles': len(report['planned_not_dispatched'])}))
    for stage, item in sorted(report['stages'].items()):
        if stage not in ('discover', 'verify'):
            continue
        facts = {key: item[key] for key in ('runs', 'calls', 'compute_ms', 'stage_results_known',
                                          'unknown_progress_runs', 'result_unit')}
        # A partial or unknown cohort cannot become a success denominator.
        if report['measurement_status'] != 'COMPLETE' or item['unknown_progress_runs']:
            continue
        if item['calls'] and item['stage_results_known'] == 0:
            findings.append(_diagnosis(stage + '_zero_measured_yield',
                'inspect_' + stage + '_evidence_before_changing_configuration', facts=facts,
                run_ids=[row['run_id'] for row in rows if row['result'] and row['result']['stage'] == stage]))
        elif item['stage_results_known']:
            findings.append(_diagnosis(stage + '_measured_progress', 'retain_separate_stage_units', facts=facts))
    stage = observed.get('suggested_stage')
    if stage is not None and stage['stage'] == 'idle':
        reason = stage['reason']
        # Only fixed controller codes leave the trust boundary; no source text.
        code = reason if reason in _IDLE_ACTIONS else 'current_stage_idle'
        findings.append(_diagnosis(code, _IDLE_ACTIONS.get(reason, 'inspect_current_idle_state'),
            facts={'public_requests_needed_now': 0}))
    if stage is not None and stage['stage'] == 'discover':
        if stage.get('scheduler_global_cooldown_active'):
            findings.append(_diagnosis('source_global_cooldown_active', 'wait_for_existing_source_cooldown'))
        elif stage.get('scheduler_eligible_sources') == 0:
            findings.append(_diagnosis('no_eligible_source_now', 'wait_for_existing_source_eligibility'))
    budget = observed.get('budget')
    if budget is not None and budget['locked']:
        findings.append(_diagnosis('shared_budget_locked', 'reconcile_existing_resource_accounting'))
    blocked = [item['id'] for item in observed['checks'] if item['status'] != 'PASS']
    if blocked:
        findings.append(_diagnosis('host_observation_blocked', 'resolve_observed_preflight_failures',
                                  facts={'check_ids': blocked}))
    return findings


def _proposal(plan, report, rows, observed, max_cycles):
    blocked = []
    if report['measurement_status'] not in ('COMPLETE', 'STOPPED'):
        blocked.append('canonical_cohort_incomplete')
    if report['measurement_status'] == 'STOPPED' and report['stop_evidence'] != 'canonical_run_receipt':
        blocked.append('canonical_stop_evidence_missing')
    if any(row['result'] and row['result']['status'] == 'HELD_HTTP_429' for row in rows):
        blocked.append('rate_limit_requires_host_eligibility_check')
    if any(item['status'] != 'PASS' for item in observed['checks']):
        blocked.append('host_observation_blocked')
    stage = observed.get('suggested_stage')
    if stage is None or stage['stage'] not in ('discover', 'verify'):
        blocked.append('no_actionable_public_stage')
    elif stage['stage'] == 'discover' and (stage.get('scheduler_global_cooldown_active') or
                                         not stage.get('scheduler_eligible_sources')):
        blocked.append('source_eligibility_hold')
    if stage is not None:
        measured = report['stages'].get(stage['stage'])
        if (report['measurement_status'] == 'COMPLETE' and measured is not None and
                measured['calls'] and measured['stage_results_known'] == 0):
            blocked.append('zero_measured_yield_requires_evidence_review')
    budget = observed.get('budget')
    if budget is None or budget['locked']:
        blocked.append('shared_budget_unavailable')
    options = plan['options']
    estimate = {name: 0 for name in RESOURCES}
    estimate.update(calls=options['max_requests'], compute_ms=math.ceil(options['timeout'] * 1000))
    available = budget['available'] if budget is not None else None
    fit = min([max_cycles] + [available[name] // amount for name, amount in estimate.items() if amount]) if available else 0
    if not fit:
        blocked.append('no_full_cycle_fits_observed_budget')
    envelope = {'available_across_ancestors': available, 'per_cycle_estimate': estimate,
                'max_cycles_requested': max_cycles, 'cycles_fitting_observed_budget': fit,
                'total_candidate_estimate': {name: amount * fit for name, amount in estimate.items()},
                'atomic_ancestor_snapshot': False, 'reservation_created': False,
                'estimate_semantics': 'declared_reservation_ceiling; cooperative_wall_time_can_overrun'}
    if blocked:
        return None, envelope, blocked
    return {'kind': 'bounded_followup_observation', 'max_cycles': fit,
            'numeric_options': {key: value for key, value in options.items() if key not in ('titles', 'locations')},
            'options_sha256': digest(options), 'titles_sha256': digest(options['titles']),
            'locations_sha256': digest(options['locations']), 'reuse_prior_configuration': True,
            'source_plan_sha256': report['plan_sha256'], 'transport': plan['transport'],
            'trusted_in_process_adapter_required': plan['transport'] == 'injected',
            'new_trial_id_required': True, 'budget_reserved': False,
            'admission_recheck_required': True, 'comparison_with_prior_trial_established': False,
            'execution_authorized': False, 'submission_authorized': False}, envelope, []


def diagnose(workspace, *, trial_id, ledger, scope_id, max_cycles=3, clock=time.time):
    """Observe at most 100 predeclared runs; return no runnable authority.

    Filters and labels are never copied into advice. A host can recover exact
    options from the source plan and verify ``options_sha256`` before creating
    a separately authorized trial. Both transport provenance and work options
    must remain unchanged. The real controller rechecks all admission gates.
    """
    _require(type(max_cycles) is int and 1 <= max_cycles <= trials.MAX_CYCLES,
             'advice_cycle_limit_invalid')
    _require(isinstance(ledger, ResourceLedger), 'advice_existing_resource_ledger_required')
    trials._identity(trial_id, 'advice', 1)
    now = service._stamp(clock)
    result = {'schema': SCHEMA, 'status': 'HOLD', 'observed_at': now,
              'trial_id_sha256': digest(trial_id), 'diagnosis': [], 'evidence': None,
              'resource_envelope': None, 'next_trial_proposal': None,
              'proposal_blockers': [], **FLAGS}
    try:
        # Never initialize or migrate a caller-supplied writable ledger.
        readonly = ResourceLedger.open_readonly(ledger.path)
        checkpoint = readonly.checkpoint()
        root, _, path = trials._paths(workspace, trial_id)
        document = trials._load(path, root, trial_id)
        plan = document['plan']
        report = trials.trial_report(root, trial_id=trial_id, ledger=readonly,
                                     scope_id=scope_id, clock=lambda: now)
        _require(report['plan_sha256'] == document['plan_sha256'], 'advice_evidence_changed')
        rows = [service.get_run(root, item['run_id'], ledger=readonly, scope_id=scope_id,
                                clock=lambda: now) for item in report['receipts']]
        _require(all(row['receipt_sha256'] == item['receipt_sha256'] and row['phase'] == item['phase']
                     for row, item in zip(rows, report['receipts'])), 'advice_evidence_changed')
        observed = _observation(root, plan, readonly, scope_id, now)
        findings = _analyze(report, rows, observed)
        proposal, envelope, blockers = _proposal(plan, report, rows, observed, max_cycles)
        current_build = digest(trials._build())
        if current_build != plan['build_sha256']:
            blockers.append('runtime_build_changed')
            findings.append(_diagnosis('runtime_build_changed', 'qualify_current_build_before_followup'))
            proposal = None
        # Bounded repeated observations catch changes encountered during advice.
        # These are not locks and do not turn separate stores into a snapshot.
        again = trials.trial_report(root, trial_id=trial_id, ledger=readonly,
                                    scope_id=scope_id, clock=lambda: now)
        _require(digest(report) == digest(again) and digest(document) == digest(trials._load(path, root, trial_id))
                 and all(digest(row) == digest(service.get_run(root, row['run_id'], ledger=readonly,
                             scope_id=scope_id, clock=lambda: now)) for row in rows)
                 and digest(observed) == digest(_observation(root, plan, readonly, scope_id, now))
                 and checkpoint == readonly.checkpoint() and current_build == digest(trials._build()),
                 'advice_evidence_changed')
        result.update(status='PROPOSAL' if proposal is not None else 'HOLD' if
                      report['measurement_status'] == 'INCONCLUSIVE' or not observed['local_checks_passed']
                      else 'NO_SPEND', diagnosis=findings, resource_envelope=envelope,
                      next_trial_proposal=proposal, proposal_blockers=blockers,
                      evidence={'plan_sha256': report['plan_sha256'], 'trial_report_sha256': digest(report),
                                'initial_workload_sha256': plan['initial_workload_sha256'],
                                'trial_build_sha256': plan['build_sha256'], 'current_build_sha256': current_build,
                                'host_observation_sha256': digest(observed), 'ledger_checkpoint': checkpoint,
                                'measurement_status': report['measurement_status'], 'receipts': _evidence(rows),
                                'stage_measurements': report['stages'],
                                'source_attribution_available': False,
                                'repeated_observations_unchanged': True})
    except Exception:
        # File contents, SQLite errors, user filters and provider text never
        # become diagnostics. No partial proposal survives an evidence failure.
        result.update(status='HOLD', diagnosis=[_diagnosis('canonical_evidence_unavailable_or_changed',
                      'inspect_local_canonical_stores_and_retry_read_only_advice')], evidence=None,
                      resource_envelope=None, next_trial_proposal=None,
                      proposal_blockers=['canonical_evidence_unavailable_or_changed'])
    return result
