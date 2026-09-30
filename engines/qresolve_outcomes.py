"""Local QRESOLVE supply diagnosis and durable READY observations.

Cleared questions, distinct affected leads, current queue readiness and proven
24-hour observations are deliberately different measures. Observation proves
that a matching committed queue row was READY at the recorded inspection time;
it proves neither an exact transition time nor that resolution caused revival.
Only explicit live observation appends private metadata. Queues, banks and
network are untouched. console_report exposes counts and fixed reason codes.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

import queue_io
import ready_gate
import answer_resolver
import safe_io
from genuine_pat import is_verify_only
from fit_policy import main_floor
from qresolve_corpus import _open_parent, _read
from qresolve_policy import fit_admission, identity_digest
from qresolve_recovery import COMPLETED_ACTIONS, journal_state
from qresolve_semantics import classify

SCHEMA = 'keel.qresolve.supply.v2'
OBSERVATION_SCHEMA = 'keel.qresolve.ready-observation.v1'
OBSERVATIONS = 'hidden_files/qresolve-ready-observations.jsonl'
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100_000
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
QUEUES = ('strategic', 'needs_input', 'standard', 'rejected')
READY = frozenset({'READY', 'READY-FOR-BROWSER'})
CLOSED = frozenset({'DEAD', 'CLOSED', 'REJECTED', 'SUBMITTED', 'APPLIED', 'WITHDRAWN'})
HASH = re.compile(r'[0-9a-f]{64}\Z')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _time(value):
    # Do not infer offsets for historical naive clocks or date-only strings.
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None


class _Snapshot:
    def __init__(self, root):
        self.root, self.hashes, self.total_bytes = root, {}, 0

    def read(self, relative, *, lines=False, missing=None):
        try:
            raw, digest, _, size = _read(self.root / relative, MAX_BYTES)
        except FileNotFoundError:
            self.hashes[relative] = None
            return missing
        self.hashes[relative] = digest
        self.total_bytes += size
        if self.total_bytes > MAX_TOTAL_BYTES:
            raise ValueError('snapshot_byte_limit')
        if lines:
            if raw and not raw.endswith('\n'):
                raise ValueError('incomplete_journal_tail')
            lines_raw = raw.splitlines()
            if any(not line.strip() or len(line.encode('utf-8')) > MAX_LINE_BYTES for line in lines_raw):
                raise ValueError('invalid_journal_line')
            rows = [queue_io.strict_loads(line) for line in lines_raw]
            if len(rows) > MAX_ROWS or any(type(row) is not dict for row in rows):
                raise ValueError('invalid_journal_shape')
            return rows
        return queue_io.strict_loads(raw)

    def stable(self):
        for relative, expected in self.hashes.items():
            try:
                actual = _read(self.root / relative, MAX_BYTES)[1]
            except FileNotFoundError:
                actual = None
            if actual != expected:
                return False
        return True


def _queue_rows(value):
    if type(value) is dict:
        fields = [key for key in ('entries', 'items', 'leads') if key in value]
        if len(fields) != 1:
            raise ValueError('ambiguous_queue_shape')
        value = value[fields[0]]
    if type(value) is not list or len(value) > MAX_ROWS:
        raise ValueError('invalid_queue_shape')
    if any(type(row) is not dict or not isinstance(row.get('role_id'), str)
           or not row['role_id'].strip() for row in value):
        raise ValueError('invalid_queue_identity')
    return value


def _completed(records):
    # Match the resolver's canonical state machine before doing accounting:
    # replayed intents/closures are invalid, never silently deduplicated into a
    # healthier state than the actuator itself can consume.
    journal_state(records)
    unique, seen = [], set()
    for row in records:
        key = _digest(row)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    pending, _ = journal_state(unique)
    intents, completions = {}, []
    for row in unique:
        did = row['decision_id']
        if row.get('action') == 'INTENT':
            intents[did] = row
        elif row.get('action') in COMPLETED_ACTIONS:
            intent = intents[did]
            ids = row.get('role_ids')
            if (type(ids) is not list or not ids or any(type(rid) is not str or not rid for rid in ids)
                    or ids != sorted(set(ids)) or row.get('target_role_ids') != ids
                    or intent.get('target_role_ids') != ids):
                raise ValueError('invalid_completion_targets')
            for key in ('changed_leads', 'removed_blockers', 'fully_unblocked'):
                if type(row.get(key)) is not int or row[key] < 0:
                    raise ValueError('invalid_completion_counts')
            if (row['changed_leads'] != len(ids) or row['removed_blockers'] < len(ids)
                    or row['fully_unblocked'] > len(ids)
                    or any(intent.get('planned_counts', {}).get(key) != row[key]
                           for key in ('changed_leads', 'removed_blockers', 'fully_unblocked', 'role_ids'))):
                raise ValueError('completion_count_mismatch')
            if (not row.get('evidence') or type(row['evidence']) is not list
                    or any(type(hit) is not dict or not hit.get('source') or not hit.get('pointer')
                           for hit in row['evidence'])
                    or row['evidence'] != intent.get('evidence')):
                raise ValueError('completion_evidence_mismatch')
            for key in ('context_sha256', 'evidence_sha256', 'config_sha256'):
                if (not isinstance(row.get(key), str) or not HASH.fullmatch(row[key])
                        or row[key] != intent.get(key)):
                    raise ValueError('completion_binding_mismatch')
            if row['evidence_sha256'] != _digest(row['evidence']):
                raise ValueError('completion_evidence_hash_mismatch')
            hashes = row.get('target_post_sha256')
            if (type(hashes) is not dict or set(hashes) != set(ids)
                    or any(not isinstance(v, str) or not HASH.fullmatch(v) for v in hashes.values())):
                raise ValueError('completion_poststate_missing')
            if 'target_resolution_state' in intent or 'target_resolution_state' in row:
                state = row.get('target_resolution_state')
                if (type(state) is not dict or state != intent.get('target_resolution_state')
                        or set(state) != set(ids)):
                    raise ValueError('completion_resolution_state_mismatch')
                for value in state.values():
                    _validate_resolution_state(value)
                if sum(value['fully_unblocked'] for value in state.values()) != row['fully_unblocked']:
                    raise ValueError('completion_resolution_state_counts_mismatch')
            at, started = _time(row.get('ts')), _time(intent.get('ts'))
            if at is None or started is None or at < started:
                raise ValueError('invalid_completion_time')
            completions.append(row)
    return completions, pending


def _validate_resolution_state(value):
    if (type(value) is not dict or not HASH.fullmatch(str(value.get('identity_sha256', '')))
            or type(value.get('remaining_blockers')) is not int or value['remaining_blockers'] < 0
            or type(value.get('fit_eligible')) is not bool
            or type(value.get('fully_unblocked')) is not bool
            or value.get('fully_unblocked') and value['remaining_blockers'] != 0
            or value.get('status') is not None and type(value['status']) is not str
            or value.get('status_updated') is not None and type(value['status_updated']) is not str):
        raise ValueError('invalid_resolution_state')
    score = value.get('fit_score')
    if score is not None and (type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100):
        raise ValueError('invalid_resolution_fit')
    floor = value.get('fit_floor')
    if type(floor) not in (int, float) or not math.isfinite(floor) or not 0 <= floor <= 100:
        raise ValueError('invalid_resolution_floor')
    if value['fit_eligible'] != (score is not None and score >= floor):
        raise ValueError('resolution_fit_mismatch')


def _blocked_names(root, snapshot):
    # Reuse the canonical parser on an already pinned descriptor. Its /proc
    # alias points only to our own regular file, not an untrusted source path.
    path = root / 'data/employer-blocklist.md'
    raw, digest, _, size = _read(path, 65536)
    snapshot.hashes['data/employer-blocklist.md'] = digest
    snapshot.total_bytes += size
    if snapshot.total_bytes > MAX_TOTAL_BYTES:
        raise ValueError('snapshot_byte_limit')
    parent, name = _open_parent(path)
    descriptor = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError('unsafe_blocklist')
        pinned = os.read(descriptor, 65537)
        if hashlib.sha256(pinned).hexdigest() != digest:
            raise ValueError('blocklist_changed')
        fd_root = '/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'
        names = ready_gate.blocked_employers(f'{fd_root}/{descriptor}')
        after = os.fstat(descriptor)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('blocklist_changed')
        return names
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _diagnose(row, queue, *, floor, now, ledger, blocked):
    admission = fit_admission(row, floor=floor)
    status = row.get('status')
    blockers = row.get('unresolved', [])
    reasons = []
    admission_codes = ready_gate.entry_admission(row, queue, now=now)['reason_codes']
    reasons.extend('admission_' + code for code in admission_codes)
    if ledger is None:
        reasons.append('ledger_unconfirmed')
    else:
        try:
            if ready_gate.ledger_holds(row, ledger):
                reasons.append('ledger_identity_hold')
        except (ValueError, TypeError, KeyError):
            reasons.append('ledger_unconfirmed')
    if blocked is None:
        reasons.append('employer_blocklist_unconfirmed')
    elif not isinstance(row.get('company'), str) or not row['company'].strip():
        reasons.append('employer_identity_unconfirmed')
    elif answer_resolver.normalize_employer(row['company']) in blocked:
        reasons.append('blocklisted_employer')
    if queue == 'rejected' or status in CLOSED:
        reasons.append('closed_or_submitted')
    if not admission['eligible']:
        reasons.append(admission['reason'])
    if type(blockers) is not list or any(type(value) is not str for value in blockers):
        reasons.append('invalid_blockers')
        blockers = None
    else:
        # Inspect current blocker fields; historical queue_notes do not themselves
        # prove a currently open structural blocker.
        context = {key: row.get(key) for key in ('unresolved', 'status_reason', 'gate_note', 'company')}
        label = classify({'question': '\n'.join(blockers)}, [context])
        if label['class'] == 'STRUCTURAL':
            reasons.append('structural_blocker')
        if blockers:
            reasons.append('partial_blockers')
    if (row.get('never_auto_submit_attestation') or row.get('holds')
            or row.get('hold_required') is True or status in {'HELD', 'HOLD', 'PARKED-AWAITING-MATERIALS', 'PARKED-LOW-FIT'}):
        reasons.append('held')
    if status in READY:
        band = row.get('action_band')
        band_ok = (band == 'APPLY' or queue == 'strategic' and
                   (band is None or isinstance(band, str) and band.startswith('STRATEGIC')))
        if not band_ok:
            reasons.append('action_band_not_apply')
        if not any(isinstance(row.get(key), str) and row[key].strip()
                   for key in ('ats_url', 'application_url', 'posting_url', 'job_url', 'apply_url', 'url')):
            reasons.append('missing_application_url')
    elif queue != 'rejected' and status not in CLOSED:
        reasons.append('verify_waiting' if is_verify_only(row) or status == 'PARKED-PENDING-VERIFICATION'
                       else 'not_ready')
    return {'fit_score': admission['score'], 'fit_eligible': admission['eligible'],
            'status': status if isinstance(status, str) else None,
            'remaining_blockers': len(blockers) if blockers is not None else None,
            'ready_eligible_by_queue_gates': status in READY and not reasons,
            'stall_reasons': sorted(set(reasons))}


def _verify_attempts(records, now):
    """Only current canonical retry records, never telemetry/company guessing."""
    attempts, transitions = {}, []
    for run in records:
        at = _time(run.get('ts'))
        if run.get('live') is not True or at is None or at > now:
            continue
        for attempt in run.get('attempts', []) if type(run.get('attempts', [])) is list else []:
            if (type(attempt) is not dict or not isinstance(attempt.get('role_id'), str)
                    or not isinstance(attempt.get('attempt_id'), str)
                    or attempt.get('wave_id') != run.get('wave_id')
                    or attempt['attempt_id'] != str(run.get('wave_id')) + ':' + attempt['role_id']
                    or attempt.get('verdict') not in {'live', 'dead', 'ambiguous', 'no_url', 'skipped'}):
                continue
            item = {**attempt, 'observed_at': run['ts']}
            if attempt['role_id'] not in attempts or at > _time(attempts[attempt['role_id']]['observed_at']):
                attempts[attempt['role_id']] = item
        # Forward-compatible exact transition receipts; current legacy retry
        # logs omit these and therefore cannot prove conversion timing.
        if run.get('status') == 'applied' and type(run.get('ready_transitions')) is list:
            transitions.extend((run, item) for item in run['ready_transitions'] if type(item) is dict)
    return attempts, transitions


def _observation_records(records, completions, now):
    by_id = {row['decision_id']: row for row in completions}
    result = {}
    for row in records:
        if type(row) is not dict or row.get('schema') != OBSERVATION_SCHEMA:
            raise ValueError('invalid_observation')
        did, rid = row.get('decision_id'), row.get('role_id')
        completion = by_id.get(did)
        state = (completion or {}).get('target_resolution_state', {}).get(rid)
        if (completion is None or type(rid) is not str or state is None
                or completion.get('action') == 'recovered_applied'
                or row.get('resolution_sha256') != _digest(completion)
                or row.get('target_state_sha256') != _digest(state)
                or row.get('identity_sha256') != state['identity_sha256']
                or row.get('resolution_at') != completion['ts']
                or state['remaining_blockers'] != 0 or state['fit_eligible'] is not True
                or state['fully_unblocked'] is not True
                or row.get('remaining_blockers') != 0 or type(row.get('remaining_blockers')) is not int
                or row.get('status') not in READY or row.get('queue') not in {'strategic', 'standard'}):
            raise ValueError('observation_receipt_mismatch')
        observed, resolved = _time(row.get('observed_at')), _time(completion['ts'])
        score, floor = row.get('fit_score'), row.get('fit_floor')
        if (observed is None or not resolved <= observed <= min(now, resolved + timedelta(hours=24))
                or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100
                or type(floor) not in (int, float) or not math.isfinite(floor) or not 0 <= floor <= 100
                or score < floor):
            raise ValueError('invalid_observation_time_or_fit')
        key = (did, rid)
        if key in result and result[key] != row:
            raise ValueError('conflicting_observation')
        result[key] = row
    return result


def _candidates(completions, rows, observations, now, floor, ledger, blocked):
    result = []
    for completion in completions:
        # Recovery closure timestamps describe reconciliation, not the original
        # queue change. No original application clock is bound today.
        if completion.get('action') == 'recovered_applied':
            continue
        resolved = _time(completion['ts'])
        if not resolved <= now <= resolved + timedelta(hours=24):
            continue
        for rid, state in completion.get('target_resolution_state', {}).items():
            if ((completion['decision_id'], rid) in observations or state['remaining_blockers'] != 0
                    or state['fit_eligible'] is not True or state['fully_unblocked'] is not True):
                continue
            matches = rows.get(rid, [])
            if len(matches) != 1:
                continue
            queue, current = matches[0]
            diagnosis = _diagnose(current, queue, floor=floor, now=now, ledger=ledger, blocked=blocked)
            if (queue not in {'strategic', 'standard'} or not diagnosis['ready_eligible_by_queue_gates']
                    or identity_digest(current) != state['identity_sha256']):
                continue
            result.append({'schema': OBSERVATION_SCHEMA, 'decision_id': completion['decision_id'],
                           'role_id': rid, 'resolution_sha256': _digest(completion),
                           'target_state_sha256': _digest(state), 'identity_sha256': state['identity_sha256'],
                           'resolution_at': completion['ts'], 'observed_at': now.isoformat(),
                           'queue': queue, 'status': current['status'], 'remaining_blockers': 0,
                           'fit_score': diagnosis['fit_score'], 'fit_floor': floor})
    return result


def _inspect(root, now, *, committed=False):
    floor = main_floor()
    snapshot, errors, rows, found_queues = _Snapshot(root), [], {}, []
    try:
        ledger = snapshot.read('data/application-ledger.json')
        if ledger is None:
            raise ValueError('ledger_missing')
        if len(safe_io.rows(ledger)) > MAX_ROWS:
            raise ValueError('ledger_row_limit')
    except (OSError, ValueError, TypeError, RecursionError):
        ledger = None
        errors.append('ledger_unconfirmed')
    try:
        blocked = _blocked_names(root, snapshot)
    except (OSError, ValueError, TypeError, RecursionError):
        blocked = None
        errors.append('employer_blocklist_unconfirmed')
    for queue in QUEUES:
        relative = f'data/queues/{queue}-queue.json'
        try:
            raw = snapshot.read(relative)
            if raw is None:
                continue
            found_queues.append(queue)
            for row in _queue_rows(raw):
                rows.setdefault(row['role_id'], []).append((queue, row))
        except (OSError, ValueError, TypeError, RecursionError):
            errors.append('invalid_' + queue + '_queue')
    if not found_queues:
        errors.append('canonical_queues_missing')
    elif set(QUEUES) != set(found_queues):
        errors.append('required_canonical_queue_missing')
    journal_present = False
    try:
        journal = snapshot.read('hidden_files/qresolve-resolutions.jsonl', lines=True)
        journal_present = journal is not None
        completions, pending = _completed(journal or [])
        if any(_time(item['ts']) > now for item in completions):
            raise ValueError('future_completion')
        if pending:
            errors.append('pending_resolution_intent')
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        errors.append('invalid_resolution_journal')
        completions, pending = [], {}
    try:
        verify = snapshot.read('data/hidden_files/verify_cron_runs.jsonl', lines=True, missing=[])
        attempts, transitions = _verify_attempts(verify, now)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        errors.append('invalid_verification_journal')
        attempts, transitions = {}, []
    try:
        observation_rows = snapshot.read(OBSERVATIONS, lines=True, missing=[])
        observations = _observation_records(observation_rows, completions, now)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        errors.append('invalid_observation_journal')
        observations = {}
    by_role = {}
    for completion in completions:
        for rid in completion['role_ids']:
            by_role.setdefault(rid, []).append(completion)
    leads = []
    cohort = Counter()
    for rid in sorted(set(rows) | set(by_role)):
        matches, receipts = rows.get(rid, []), by_role.get(rid, [])
        item = {'role_id': rid, 'resolution_tracking': 'tracked' if receipts else 'untracked',
                'decision_ids': sorted({value['decision_id'] for value in receipts})}
        current = matches[0][1] if len(matches) == 1 else None
        if current is None:
            item.update(queue=None, fit_score=None, fit_eligible=False, status=None,
                        remaining_blockers=None, ready_eligible_by_queue_gates=False,
                        stall_reasons=['missing_row' if not matches else 'duplicate_queue_identity'])
        else:
            item.update(queue=matches[0][0], **_diagnose(current, matches[0][0], floor=floor, now=now, ledger=ledger, blocked=blocked))
        if rid in attempts:
            attempt = attempts[rid]
            item['last_verification'] = {key: attempt.get(key) for key in
                                        ('observed_at', 'verdict', 'skip_reason', 'next_eligible_at', 'attempt_id')}
        if receipts:
            full = [value for value in receipts
                    if value.get('target_resolution_state', {}).get(rid, {}).get('remaining_blockers') == 0
                    and value.get('target_resolution_state', {}).get(rid, {}).get('fit_eligible') is True
                    and value.get('target_resolution_state', {}).get(rid, {}).get('fully_unblocked') is True]
            first = min(full or receipts, key=lambda value: _time(value['ts']))
            resolved_at = _time(first['ts'])
            item['first_tracked_resolution_at'] = first['ts']
            item['conversion_cohort'] = bool(full)
            item['conversion_resolution_decision_id'] = first['decision_id'] if full else None
            state = first.get('target_resolution_state', {}).get(rid)
            proof = observations.get((first['decision_id'], rid))
            item['fit_eligible_at_resolution'] = state['fit_eligible'] if state else None
            if proof is not None:
                outcome = 'observed_within_24h'
            elif first.get('action') == 'recovered_applied':
                outcome = 'unknown_recovered_resolution_time'
            elif state is None:
                outcome = 'unknown_legacy_resolution_snapshot'
            elif state['remaining_blockers'] != 0:
                outcome = 'unknown_partial_at_resolution'
            elif state['fit_eligible'] is not True:
                outcome = 'unknown_ineligible_at_resolution'
            elif state['fully_unblocked'] is not True:
                outcome = 'unknown_nonrevivable_at_resolution'
            elif now < resolved_at + timedelta(hours=24):
                outcome = 'right_censored'
            else:
                # Current state cannot disprove an earlier READY transition.
                outcome = 'unknown_not_observed_within_24h'
            item['resolved_to_ready_24h'] = outcome
            item['observed_ready_at'] = proof['observed_at'] if proof else None
            cohort[outcome] += 1
            if item['fit_eligible'] and full:
                cohort['currently_fit_eligible'] += 1
        leads.append(item)
    try:
        if not snapshot.stable():
            errors.append('snapshot_changed_during_read')
    except (OSError, ValueError, TypeError):
        errors.append('snapshot_revalidation_failed')
    # No claim of success survives incomplete/mutating inputs.
    if errors:
        for item in leads:
            item['ready_eligible_by_queue_gates'] = False
            item['stall_reasons'] = sorted(set(item['stall_reasons'] + ['incomplete_snapshot']))
            if item['resolution_tracking'] == 'tracked':
                item['resolved_to_ready_24h'] = 'unknown_incomplete_snapshot'
                item['observed_ready_at'] = None
    tracked = [item for item in leads if item['resolution_tracking'] == 'tracked']
    conversion_cohort = [item for item in tracked if item.get('conversion_cohort')]
    valid = not errors
    counts = {
        'queue_distinct_leads': len(rows),
        'current_ready_status_leads': sum(value.get('status') in READY for value in leads),
        'ready_eligible_by_queue_gates': sum(value['ready_eligible_by_queue_gates'] for value in leads) if valid else None,
        'completed_decisions': len(completions) if valid else None,
        'cleared_blockers': sum(value['removed_blockers'] for value in completions) if valid else None,
        'resolved_distinct_leads': len(by_role) if valid else None,
        'fully_unblocked_fit_eligible_distinct_leads': len(conversion_cohort) if valid else None,
        'resolution_role_pairs': sum(len(value['role_ids']) for value in completions) if valid else None,
        'pending_intents': len(pending) if valid else None,
        'tracked_ready_eligible_by_queue_gates': sum(value['ready_eligible_by_queue_gates'] for value in tracked) if valid else None,
        'untracked_queue_leads': len(set(rows) - set(by_role)),
    }
    metric = {'cohort_basis': 'earliest_fit_eligible_fully_unblocked_snapshot_per_distinct_role',
              'cohort_leads': len(conversion_cohort) if valid else None,
              'currently_fit_eligible_cohort': cohort['currently_fit_eligible'] if valid else None,
              'fit_at_resolution': 'recorded_for_v2_receipts_unknown_for_legacy',
              'observed_ready_within_24h': cohort['observed_within_24h'] if valid else None,
              'right_censored': cohort['right_censored'] if valid else None,
              'unknown': (cohort['unknown_not_observed_within_24h'] + cohort['unknown_recovered_resolution_time']) if valid else len(conversion_cohort),
              'legacy_snapshot_unknown_leads': cohort['unknown_legacy_resolution_snapshot'] if valid else None,
              'partial_resolution_leads': cohort['unknown_partial_at_resolution'] if valid else None,
              'ineligible_at_resolution_leads': cohort['unknown_ineligible_at_resolution'] if valid else None,
              'nonrevivable_resolution_leads': cohort['unknown_nonrevivable_at_resolution'] if valid else None,
              'confirmed_not_ready_within_24h': None,
              'rate': None,
              'rate_reason': 'observations_prove_presence_not_complete_transition_history',
              'causal_attribution': False}
    value = {'schema': SCHEMA, 'status': ('OK' if committed else 'UNCONFIRMED') if valid else 'HOLD', 'ts': now.isoformat(),
            'fit_floor': floor, 'execution_authorized': False, 'counts': counts,
            'resolved_to_ready_24h': metric,
            'stall_reasons': dict(sorted(Counter(reason for item in tracked for reason in item['stall_reasons']).items())),
            'inventory_stall_reasons': dict(sorted(Counter(reason for item in leads for reason in item['stall_reasons']).items())),
            'coverage': {'queues_found': found_queues, 'resolution_journal_present': journal_present,
                         'legacy_bank_sweep_distinct_leads': None,
                         'legacy_bank_sweep_reason': 'untracked_sweep_cannot_be_reconstructed_from_blocker_count',
                         'readiness_basis': 'current_queue_gates_only_full_lane_revalidation_required',
                         'snapshot_basis': 'queue_lock_without_recovery' if committed else 'revalidated_unlocked_inventory_only',
                         'committed_queue_observation': committed,
                         'executable_ready': None,
                         'execution_reason': 'live_lane_policy_packet_approval_provider_and_history_revalidation_required'},
            'errors': sorted(set(errors)), 'leads': leads}
    candidates = _candidates(completions, rows, observations, now, floor, ledger, blocked) if valid and committed else []
    return value, candidates, snapshot.hashes.get(OBSERVATIONS)


def _append_observations(root, records, expected):
    if not records:
        return 0
    lines = [json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
             for row in records]
    if any(len(line) > MAX_LINE_BYTES for line in lines):
        raise ValueError('observation_line_limit')
    payload = b''.join(lines)
    parent, name = _open_parent(root / OBSERVATIONS)
    descriptor = None
    try:
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        if expected is None:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(name, flags, 0o600, dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size + len(payload) > MAX_BYTES:
            raise ValueError('unsafe_or_full_observation_journal')
        prior = bytearray()
        while len(prior) <= MAX_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(prior)))
            if not chunk:
                break
            prior.extend(chunk)
        actual = hashlib.sha256(prior).hexdigest() if expected is not None else None
        if actual != expected or len(prior) != info.st_size:
            raise ValueError('observation_journal_changed')
        if len(prior.splitlines()) + len(records) > MAX_ROWS:
            raise ValueError('observation_row_limit')
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(payload)
        while remaining:
            count = os.write(descriptor, remaining)
            if count <= 0:
                raise OSError('observation_append_failed')
            remaining = remaining[count:]
        os.fsync(descriptor)
        os.fsync(parent)
        return len(records)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _clock(now):
    value = now or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('aware_now_required')
    return value.astimezone(timezone.utc)


def _held(now, reason, *, live, write_unconfirmed=False):
    return {'schema': SCHEMA, 'status': 'HOLD', 'ts': now.isoformat(),
            'fit_floor': None, 'execution_authorized': False,
            'counts': {}, 'resolved_to_ready_24h': {'observed_ready_within_24h': None, 'rate': None},
            'stall_reasons': {}, 'inventory_stall_reasons': {},
            'coverage': {'committed_queue_observation': False, 'executable_ready': None},
            'errors': [reason], 'leads': [], 'mode': 'live' if live else 'dry_run',
            'observation_writes': None if write_unconfirmed else 0,
            'observation_write_state': 'unconfirmed' if write_unconfirmed else 'none',
            'canonical_writes': 0, 'network_calls': 0, 'model_calls': 0}


def observe(workspace=None, *, live=False, now=None):
    """Inspect by default; live records matching READY presence, without revival.

    The existing queue lock is reused without recovery. An absent lock leaves a
    dry inventory explicitly UNCONFIRMED and creates nothing. Only live mode may
    create coordination metadata, and pending queue/resolution recovery holds
    the whole observation. No observation means history is unknown, not failure.
    """
    now = _clock(now)
    write_attempted = False
    try:
        from qresolve_recovery import _root, preflight_locks
        root = _root(workspace)
        preflight_locks(root)
        try:
            _read(root / 'hidden_files/queue.lock', MAX_BYTES)
            has_lock = True
        except FileNotFoundError:
            has_lock = False
        if not live and not has_lock:
            queue_io._refuse_pending_transactions()
            value, _, _ = _inspect(root, now)
            queue_io._refuse_pending_transactions()
        else:
            with queue_io.queue_lock(timeout=1, owner='qresolve:supply-observe', recover=False):
                value, candidates, expected = _inspect(root, now, committed=True)
                if live and value['status'] == 'OK' and candidates:
                    write_attempted = True
                    writes = _append_observations(root, candidates, expected)
                    value, _, _ = _inspect(root, now, committed=True)
                    value['observation_writes'] = writes
        value.update(mode='live' if live else 'dry_run', canonical_writes=0, network_calls=0, model_calls=0)
        value.setdefault('observation_writes', 0)
        value['observation_write_state'] = 'durable' if value['observation_writes'] else 'none'
        return value
    except queue_io.QueueRecoveryRequired:
        return _held(now, 'pending_queue_transaction', live=live)
    except queue_io.QueueLockTimeout:
        return _held(now, 'queue_lock_unavailable', live=live)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
        return _held(now, 'observation_state_unconfirmed', live=live, write_unconfirmed=write_attempted)


def report(workspace=None, *, now=None):
    return observe(workspace, now=now)


def console_report(value):
    """Default terminal output contains no role IDs, answers, URLs or source paths."""
    return {key: value[key] for key in ('schema', 'status', 'ts', 'fit_floor', 'execution_authorized',
                                       'counts', 'resolved_to_ready_24h', 'stall_reasons',
                                       'inventory_stall_reasons', 'coverage', 'errors', 'mode',
                                       'observation_writes', 'observation_write_state', 'canonical_writes', 'network_calls', 'model_calls')}
