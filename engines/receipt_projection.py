"""Project freshly authenticated exact-attempt receipts into canonical queues.

This host-facing adapter performs no submission or provider request itself.
Validators are trusted Python callbacks, never names or trust flags loaded from
JSON. The host must authenticate provider evidence, account/recipient, attempt
and application; a ProviderValidation instance alone is not authentication.

Local owner-controlled POSIX stores and cooperating writers are required. Lock
order is queue -> receipt store -> intent store; existing public receipt/intent
writers do not acquire the queue lock while retaining their store locks. Hosts
must qualify that order and retain projection/outbox fields in other writers.
The bridge never transitions intents, releases leases or modifies approvals.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from collections.abc import Mapping

import keel_paths
import log_event
import pipeline_service as pipeline
import queue_io
import submit_intent
from outcome_tracking.receipt_intake import ReceiptStore, ProviderValidation, grade_claim, normalize_receipt
from safe_io import atomic_json, aware_time, digest, file_lock, loads, rows, utc_now

SCHEMA = 'keel.receipt-projection.v1'
MARKER = 'receipt_projection'
OUTBOX = 'receipt_projection_event_pending'
MAX_BYTES = 16 * 1024 * 1024
ELIGIBLE = {'READY', 'PARKED', 'PARKED-NEEDS-INPUT', 'PARKED-PENDING-VERIFICATION',
            'SUBMISSION_CLAIMED', 'IN-FLIGHT', 'APPLYING', 'SUBMITTED'}
WRITABLE = {'status', 'status_reason', 'status_updated', MARKER, OUTBOX}
URL_FIELDS = ('ats_url', 'application_url', 'posting_url', 'job_url', 'apply_url', 'url')
FLAGS = {'execution_authorized': False, 'submission_authorized': False,
         'attempts_modified': False, 'holds_released': False}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _identifier(value):
    _require(type(value) is str and value and value == value.strip() and len(value) <= 512,
             'receipt_projection_identifier_invalid')
    return value


def _regular(root, value):
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = Path(os.path.abspath(path))
    _require(path.is_relative_to(root) and path != root and path.resolve(strict=True) == path,
             'receipt_projection_path_must_be_real_and_contained')
    _require(stat.S_ISREG(path.stat().st_mode), 'receipt_projection_path_must_be_regular')
    return path


def _read(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'receipt_projection_store_invalid')
        raw = stream.read(MAX_BYTES + 1)
    _require(len(raw) <= MAX_BYTES, 'receipt_projection_store_too_large')
    return loads(raw)


def _workspace(workspace, receipts_path):
    root = Path(os.path.abspath(workspace))
    _require(root.is_dir() and root.resolve(strict=True) == root, 'receipt_projection_workspace_invalid')
    _require(Path(keel_paths.HOME).absolute() == root
             and Path(os.environ.get('KEEL_HOME', '')).absolute() == root
             and Path(queue_io.get_lock_path()).absolute() == root / 'hidden_files/queue.lock'
             and Path(log_event.EVENTS).absolute() == root / 'data/telemetry/events.jsonl'
             and Path(submit_intent.STORE_PATH).absolute() == root / 'hidden_files/submit-intents.json'
             and Path(submit_intent.LOCK_PATH).absolute() == root / 'hidden_files/submit-intents.json.lock',
             'receipt_projection_workspace_runtime_binding_mismatch')
    receipt = _regular(root, receipts_path)
    intent = _regular(root, root / 'hidden_files/submit-intents.json')
    _require(receipt != intent and receipt != root / 'data/application-ledger.json'
             and receipt not in [pipeline._queue_path(root, name) for name in pipeline.QUEUES],
             'receipt_projection_store_alias')
    return root, receipt, intent


@contextmanager
def _locked(root, receipt, intent):
    with queue_io.queue_lock(timeout=10, owner='receipt-projection'):
        with file_lock(str(receipt) + '.lock', timeout=10):
            with file_lock(str(intent) + '.lock', timeout=10):
                yield


def _snapshot(root, receipt, intent, now):
    documents = {}
    for name in pipeline.QUEUES:
        path = _regular(root, pipeline._queue_path(root, name))
        document = _read(path)
        rows(document)
        documents[path] = document
    ledger = _read(_regular(root, root / 'data/application-ledger.json'))
    rows(ledger)
    intents = _read(_regular(root, intent))
    _require(type(intents) is dict, 'receipt_projection_intent_store_invalid')
    for aid, record in intents.items():
        _identifier(aid)
        _require(type(record) is dict and record.get('attempt_id') == aid
                 and record.get('state') in {'INTENT', 'UNKNOWN', 'SUBMITTED', 'FAILED'},
                 'receipt_projection_intent_record_invalid')
        _identifier(record.get('role_id'))
        _require(record.get('page_ref') is None or isinstance(record.get('page_ref'), dict),
                 'receipt_projection_intent_page_ref_invalid')
    _regular(root, receipt)
    receipt_data = ReceiptStore(receipt)._read()
    for record in receipt_data['receipts'].values():
        _require(normalize_receipt(record, now=now) == record, 'receipt_projection_receipt_invalid')
    snapshot = {'documents': documents, 'ledger': ledger, 'intents': intents,
                'receipt_data': receipt_data}
    snapshot['sha256'] = digest({'queues': {str(path.relative_to(root)): value for path, value in documents.items()},
                                 'ledger': ledger, 'intents': intents, 'receipts': receipt_data})
    return snapshot


def _held(reason, role_id, attempt_id, **extra):
    return {'schema': SCHEMA, 'status': 'HELD', 'reason': reason, 'role_id': role_id,
            'attempt_id': attempt_id, 'provider_verified': False, 'queue_committed': False,
            'event_pending': False, 'event_id': None, **extra, **FLAGS}


def _exact_key(entry):
    """Every declared URL must identify the same supported exact posting."""
    keys = set()
    for field in URL_FIELDS:
        value = entry.get(field)
        if value is None or value == '':
            continue
        _require(type(value) is str and value.strip(), 'invalid_declared_posting_url')
        key = pipeline.identity(value.strip())
        _require(key is not None, 'unsupported_declared_posting_url')
        keys.add(key)
    _require(len(keys) <= 1, 'conflicting_declared_posting_urls')
    return next(iter(keys), None)


def _possible_keys(entry):
    """Index all known targets in other records without guessing unknown URLs."""
    keys = set()
    for field in URL_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            try:
                keys.add(pipeline.identity(value.strip()))
            except (TypeError, ValueError):
                pass
    return keys


def _selection(snapshot, role_id, attempt_id):
    try:
        return _select(snapshot, role_id, attempt_id)
    except (TypeError, ValueError):
        return None, 'conflicting_or_invalid_posting_identity'


def _select(snapshot, role_id, attempt_id):
    ledger = rows(snapshot['ledger'])
    claims = [row for row in ledger if row.get('attempt_id') == attempt_id]
    if len(claims) != 1 or claims[0].get('role_id') != role_id:
        return None, 'missing_or_ambiguous_ledger_attempt'
    claim = claims[0]
    application = claim.get('application_id')
    if not (type(application) is str and application and application == application.strip()
            and len(application) <= 512):
        return None, 'exact_application_id_required'
    if sum(row.get('application_id') == application for row in ledger) != 1:
        return None, 'ambiguous_ledger_application'
    if (claim.get('attempt_id_source') not in {'writer', 'role_id'}
            or claim.get('status') not in {'SUBMITTED', 'SUBMISSION_CLAIMED'}):
        return None, 'ledger_claim_not_qualified'
    key = _exact_key(claim)
    if key is None:
        return None, 'ledger_posting_identity_required'
    queue_rows = pipeline._all_rows(snapshot['documents'])
    owners = [(path, row) for path, row in queue_rows if row.get('role_id') == role_id]
    if (len(owners) != 1 or sum(key in _possible_keys(row) for _, row in queue_rows) != 1
            or sum(row.get('attempt_id') == attempt_id for _, row in queue_rows) != 1
            or any(row is not owners[0][1] and row.get('application_id') == application
                   for _, row in queue_rows)):
        return None, 'missing_or_ambiguous_queue_owner'
    path, entry = owners[0]
    if (entry.get('attempt_id') != attempt_id or _exact_key(entry) != key
            or entry.get('application_id') not in (None, '', application)):
        return None, 'queue_identity_mismatch'
    if entry.get('status') not in ELIGIBLE:
        return None, 'queue_outcome_protected'
    if any(entry.get(name) for name in ('holds', 'human_hold', 'revoked', 'structurally_blocked')):
        return None, 'queue_hold_preserved'
    if entry.get('verification_event_pending') or entry.get('reconciliation_publication'):
        return None, 'other_publication_pending_or_retained'
    intent = snapshot['intents'].get(attempt_id)
    if (not intent or intent.get('role_id') != role_id or intent.get('state') != 'SUBMITTED'
            or intent.get('application_id') not in (None, '', application) or intent.get('inconsistent_states')):
        return None, 'terminal_exact_intent_required'
    intent_keys = {_exact_key(intent), _exact_key(intent.get('page_ref') or {})} - {None}
    if intent_keys - {key}:
        return None, 'intent_posting_mismatch'
    equivalent_roles = {row.get('role_id') for row in ledger if key in _possible_keys(row)}
    equivalent_roles.update(row.get('role_id') for _, row in queue_rows if key in _possible_keys(row))
    for aid, other in snapshot['intents'].items():
        if aid == attempt_id or other['state'] not in {'INTENT', 'UNKNOWN'}:
            continue
        other_keys = _possible_keys(other) | _possible_keys(other.get('page_ref') or {})
        if other['role_id'] in equivalent_roles or key in other_keys:
            return None, 'other_open_attempt_preserved'
    try:
        aware_time(claim.get('date_submitted') or claim.get('ts'))
    except (ValueError, TypeError):
        return None, 'claim_timestamp_invalid'
    return {'claim': claim, 'entry': entry, 'path': path, 'intent': intent, 'key': key}, None


def _authenticate(snapshot, selected, validators, now, *, live_clock=False):
    claim = selected['claim']
    receipt_data = snapshot['receipt_data']
    records = [(key, record, key in receipt_data['conflicts'])
               for key, record in receipt_data['receipts'].items()]
    # Refuse conflicting observations before invoking any host authenticator.
    preliminary = grade_claim(copy.deepcopy(claim), copy.deepcopy(records), now=now)
    if preliminary['held']:
        return None, 'receipt_conflict_requires_review', now
    decisions, validation_failed = {}, False
    for key, receipt, conflict in records:
        if (conflict or receipt['kind'] != 'provider_receipt' or receipt['outcome'] != 'AUTO_ACK'
                or receipt['role_id'] != claim['role_id']
                or any(receipt[field] != claim.get(field) for field in ('attempt_id', 'application_id'))
                or not aware_time(claim.get('date_submitted') or claim.get('ts')) <= aware_time(receipt['received_at']) <= now):
            continue
        callback = validators.get(receipt['source'])
        if callable(callback):
            try:
                decisions[key] = callback(copy.deepcopy(receipt), copy.deepcopy(claim))
            except Exception:
                # Unavailable authentication never becomes acceptance. Keep
                # its failure distinguishable without exposing provider errors.
                validation_failed = True
    # A real host stamps checked_at when authentication finishes. Grading with
    # the earlier invocation time would reject every such result as future.
    # Only this call's ephemeral decisions are reused; no stored trust is read.
    if live_clock:
        completed = utc_now()
        if completed < now:
            return None, 'validation_clock_regressed', now
        now = completed
    valid, wrapped = {}, {}
    for source in validators:
        def checked(receipt, current_claim):
            key = digest([receipt['source'], receipt['receipt_id']])
            decision = decisions.get(key)
            if (isinstance(decision, ProviderValidation) and decision.accepted is True
                    and decision.receipt_digest == digest(receipt)
                    and decision.claim_digest == digest(current_claim)
                    and type(decision.provider) is str and decision.provider
                    and decision.provider == decision.provider.strip() and len(decision.provider) <= 512
                    and aware_time(receipt['received_at']) <= aware_time(decision.checked_at) <= now):
                valid[key] = {'receipt': receipt, 'decision': decision}
            return decision
        wrapped[source] = checked
    grade = grade_claim(copy.deepcopy(claim), copy.deepcopy(records), now=now, validators=wrapped)
    if grade['held']:
        return None, 'receipt_conflict_requires_review', now
    if not grade['provider_verified'] or not valid:
        return None, ('provider_validation_unavailable' if validation_failed
                      else 'provider_validation_required'), now
    existing = selected['entry'].get(MARKER)
    chosen = existing.get('receipt_key') if isinstance(existing, dict) else min(valid)
    if chosen not in valid:
        return None, 'retained_projection_evidence_not_revalidated', now
    return (chosen, valid[chosen]), None, now


def _event(marker):
    return {'event_type': 'pipeline_recovery', 'event_id': marker['event_id'],
            'role_id': marker['role_id'], 'source': 'receipt-projection',
            'details': {'recovery_kind': 'provider_receipt_queue_projection',
                        'attempt_id': marker['attempt_id'], 'application_id': marker['application_id'],
                        'receipt_sha256': marker['receipt_sha256'], 'claim_sha256': marker['claim_sha256'],
                        'posting_identity': marker['posting_identity'], 'validated_at': marker['validated_at'],
                        'projected_at': marker['projected_at'], **FLAGS}}


def _marker_valid(marker, selected, receipt_key, evidence, now):
    required = {'schema', 'event_id', 'role_id', 'attempt_id', 'application_id', 'posting_identity',
                'receipt_key', 'receipt_sha256', 'claim_sha256', 'intent_sha256', 'provider',
                'validated_at', 'projected_at'}
    if not isinstance(marker, dict) or set(marker) != required:
        return False
    claim = selected['claim']
    expected = {'schema': SCHEMA, 'role_id': claim['role_id'], 'attempt_id': claim['attempt_id'],
                'application_id': claim['application_id'], 'posting_identity': list(selected['key']),
                'receipt_key': receipt_key, 'receipt_sha256': digest(evidence['receipt']),
                'claim_sha256': digest(claim), 'intent_sha256': digest(selected['intent']),
                'provider': evidence['decision'].provider}
    if any(marker[name] != value for name, value in expected.items()):
        return False
    try:
        if not aware_time(evidence['receipt']['received_at']) <= aware_time(marker['validated_at']) <= aware_time(marker['projected_at']) <= now:
            return False
    except (ValueError, TypeError):
        return False
    return marker['event_id'] == 'receipt-projection:' + digest(expected)


def _deliver(root, path, marker, reply):
    """At-least-once durable telemetry; stable IDs make append retries harmless."""
    event = _event(marker)
    try:
        receipt = log_event.log(**event)
        _require(isinstance(receipt, dict) and receipt.get('event_id') == event['event_id'],
                 'receipt_projection_telemetry_receipt_missing')
    except Exception as exc:
        return {**reply, 'event_pending': True, 'telemetry_error': type(exc).__name__}
    acknowledgment_write_attempted = False
    try:
        with queue_io.queue_lock(timeout=10, owner='receipt-projection:ack'):
            document = _read(_regular(root, path))
            matching = [entry for entry in rows(document) if entry.get('role_id') == marker['role_id']]
            try:
                current_key = _exact_key(matching[0]) if len(matching) == 1 else None
            except (ValueError, TypeError):
                current_key = None
            if (len(matching) != 1 or matching[0].get(MARKER) != marker
                    or matching[0].get(OUTBOX) != event or matching[0].get('attempt_id') != marker['attempt_id']
                    or list(current_key or ()) != marker['posting_identity']
                    or matching[0].get('application_id') not in (None, '', marker['application_id'])
                    or matching[0].get('status') != 'SUBMITTED'):
                return {**reply, 'event_pending': True, 'telemetry_error': 'acknowledgment_conflict'}
            matching[0].pop(OUTBOX)
            acknowledgment_write_attempted = True
            atomic_json(path, document)
    except Exception as exc:
        if acknowledgment_write_attempted:
            return {**reply, 'status': 'UNKNOWN', 'reason': 'acknowledgment_write_outcome_unknown',
                    'event_pending': None, 'error_class': type(exc).__name__}
        # The projection and event append already succeeded. Failure to reach
        # the acknowledgment writer must preserve that known outcome and leave
        # the durable outbox available for a freshly validated replay.
        return {**reply, 'reason': 'acknowledgment_unavailable', 'event_pending': True,
                'telemetry_error': type(exc).__name__}
    return {**reply, 'event_pending': False}


def reconcile(workspace, *, role_id, attempt_id, receipts_path, live=False, validators=None, now=None):
    """Review by default; a qualified Python host can explicitly project once.

    HELD/ELIGIBLE are not queue mutations. PROJECTED/REPLAYED mean a queue
    projection exists after fresh provider validation; event_pending separately
    describes its durable telemetry delivery. UNKNOWN forbids inferring whether
    a writer completed. Application submission is never attempted or retried.
    """
    role_id, attempt_id = _identifier(role_id), _identifier(attempt_id)
    _require(type(live) is bool, 'receipt_projection_live_must_be_boolean')
    validators = {} if validators is None else validators
    _require(isinstance(validators, Mapping) and all(type(source) is str and callable(callback)
             for source, callback in validators.items()), 'receipt_projection_host_validators_required')
    live_clock = now is None
    now = utc_now() if live_clock else now
    _require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None,
             'receipt_projection_clock_invalid')
    now = now.astimezone(timezone.utc)
    root, receipt_path, intent_path = _workspace(workspace, receipts_path)
    with _locked(root, receipt_path, intent_path):
        original = _snapshot(root, receipt_path, intent_path, now)
    selected, reason = _selection(original, role_id, attempt_id)
    if reason:
        return _held(reason, role_id, attempt_id)
    authenticated, reason, now = _authenticate(original, selected, validators, now, live_clock=live_clock)
    if reason:
        return _held(reason, role_id, attempt_id)
    receipt_key, evidence = authenticated
    entry = selected['entry']
    marker = entry.get(MARKER)
    if marker is not None:
        if (not _marker_valid(marker, selected, receipt_key, evidence, now) or entry.get('status') != 'SUBMITTED'
                or OUTBOX in entry and entry[OUTBOX] != _event(marker)):
            return _held('retained_projection_conflict', role_id, attempt_id)
    elif MARKER in entry or OUTBOX in entry:
        return _held('orphan_projection_outbox', role_id, attempt_id)
    if not live:
        return {**_held('fresh_provider_evidence_reviewed', role_id, attempt_id),
                'status': 'ELIGIBLE', 'provider_verified': True, 'dry_run': True,
                'event_pending': bool(entry.get(OUTBOX)), 'event_id': marker['event_id'] if marker else None}
    with _locked(root, receipt_path, intent_path):
        current = _snapshot(root, receipt_path, intent_path, now)
        if current['sha256'] != original['sha256']:
            return _held('inputs_changed_after_validation', role_id, attempt_id)
        fresh, reason = _selection(current, role_id, attempt_id)
        if reason:
            return _held(reason, role_id, attempt_id)
        replayed = marker is not None
        if not replayed:
            claim = fresh['claim']
            bindings = {'schema': SCHEMA, 'role_id': role_id, 'attempt_id': attempt_id,
                        'application_id': claim['application_id'], 'posting_identity': list(fresh['key']),
                        'receipt_key': receipt_key, 'receipt_sha256': digest(evidence['receipt']),
                        'claim_sha256': digest(claim), 'intent_sha256': digest(fresh['intent']),
                        'provider': evidence['decision'].provider}
            marker = {**bindings, 'event_id': 'receipt-projection:' + digest(bindings),
                      'validated_at': aware_time(evidence['decision'].checked_at).isoformat(),
                      'projected_at': now.isoformat()}
            before = copy.deepcopy(fresh['entry'])
            fresh['entry'].update(status='SUBMITTED', status_reason='Authenticated provider receipt for exact existing attempt',
                                  status_updated=now.isoformat(), **{MARKER: marker, OUTBOX: _event(marker)})
            _require({key: value for key, value in before.items() if key not in WRITABLE}
                     == {key: value for key, value in fresh['entry'].items() if key not in WRITABLE},
                     'receipt_projection_changed_protected_fields')
            try:
                atomic_json(fresh['path'], current['documents'][fresh['path']])
            except Exception as exc:
                return {**_held('queue_write_outcome_unknown', role_id, attempt_id),
                        'status': 'UNKNOWN', 'provider_verified': True, 'queue_committed': None,
                        'event_pending': None, 'event_id': marker['event_id'], 'error_class': type(exc).__name__}
        pending = OUTBOX in fresh['entry']
        reply = {**_held('exact_provider_receipt_projected', role_id, attempt_id),
                 'status': 'REPLAYED' if replayed else 'PROJECTED', 'provider_verified': True,
                 'queue_committed': True, 'event_pending': pending, 'event_id': marker['event_id']}
    return _deliver(root, fresh['path'], marker, reply) if pending else reply
