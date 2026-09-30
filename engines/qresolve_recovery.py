"""Conservative, offline reconciliation of interrupted QRESOLVE applications.

Recovery never replays an answer or changes a queue, bank, or resolved map. It
only appends a closure when the original durable backup and current canonical
state prove that the queues still match the pre-write snapshot, or that the
entire recorded application and its resolved receipt are present. Intermediate
states remain held. Inspection is read-only, including lock diagnostics.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

import queue_io
import safe_io
from qresolve_corpus import _open_parent, _read

MAX_BYTES = 16 * 1024 * 1024
COMPLETED_ACTIONS = frozenset({'auto_applied', 'human_applied', 'recovered_applied'})
CLOSED_ACTIONS = COMPLETED_ACTIONS | {'cancelled_unwritten'}
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_BACKUP = re.compile(r'_backup-tray-answer-[A-Za-z0-9_-]+\Z')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def journal_state(records):
    """Return (pending intents, completed IDs), respecting cancelled retries.

    Set subtraction loses a later INTENT after a cancelled attempt with the
    same decision ID. Process events in journal order instead. New recovery
    closures bind to one exact intent; a completed decision cannot be reopened.
    """
    pending, completed = {}, set()
    for record in records:
        if (type(record) is not dict or not isinstance(record.get('decision_id'), str)
                or not _HASH.fullmatch(record['decision_id'])):
            raise ValueError('invalid_journal_event')
        decision_id, action = record['decision_id'], record.get('action')
        if action == 'INTENT':
            if decision_id in pending or decision_id in completed:
                raise ValueError('duplicate_or_completed_intent')
            pending[decision_id] = record
        elif action in CLOSED_ACTIONS:
            if decision_id not in pending:
                raise ValueError('orphan_journal_closure')
            if action in {'recovered_applied', 'cancelled_unwritten'}:
                if record.get('intent_sha256') != _digest(pending[decision_id]):
                    raise ValueError('recovery_intent_mismatch')
            del pending[decision_id]
            if action in COMPLETED_ACTIONS:
                completed.add(decision_id)
        elif action != 'held':
            raise ValueError('unknown_journal_action')
    return pending, completed


def _root(workspace):
    from keel_paths import HOME
    root = Path(workspace or HOME).absolute()
    if root != Path(HOME).absolute():
        raise ValueError('workspace_mismatch')
    # Pinned no-follow traversal checks every ancestor, even for an empty run.
    parent, _ = _open_parent(root / 'unused')
    os.close(parent)
    if Path(queue_io.get_lock_path()).absolute() != root / 'hidden_files/queue.lock':
        raise ValueError('lock_workspace_mismatch')
    return root


def _json(path, *, missing=None):
    try:
        return queue_io.strict_loads(_read(path, MAX_BYTES)[0])
    except FileNotFoundError:
        if missing is not None:
            return missing
        raise


def _journal(path):
    try:
        text = _read(path, MAX_BYTES)[0]
    except FileNotFoundError:
        return []
    if text and not text.endswith('\n'):
        raise ValueError('incomplete_journal_tail')
    return [queue_io.strict_loads(line) for line in text.splitlines()]


def _queues(root, backup=None):
    values, identities = {}, set()
    for name in ('needs_input-queue.json', 'standard-queue.json'):
        canonical = root / 'data/queues' / name
        rows = _json((backup / name) if backup else canonical)
        if type(rows) is not list:
            raise ValueError('invalid_queue')
        for row in rows:
            if (type(row) is not dict or not isinstance(row.get('role_id'), str)
                    or not row['role_id'].strip() or row['role_id'] in identities):
                raise ValueError('invalid_queue_identity')
            identities.add(row['role_id'])
        values[str(canonical)] = rows
    return values


def _counts(intent):
    counts = intent.get('planned_counts')
    if type(counts) is not dict:
        raise ValueError('missing_plan')
    ids = counts.get('role_ids')
    if (type(ids) is not list or not ids or any(not isinstance(r, str) or not r for r in ids)
            or ids != sorted(set(ids)) or intent.get('target_role_ids') != ids):
        raise ValueError('invalid_plan_targets')
    for key in ('removed_blockers', 'changed_leads', 'fully_unblocked'):
        if type(counts.get(key)) is not int or counts[key] < 0:
            raise ValueError('invalid_plan_counts')
    if (counts['changed_leads'] != len(ids) or counts['removed_blockers'] < len(ids)
            or counts['fully_unblocked'] > len(ids)):
        raise ValueError('invalid_plan_counts')
    return counts


def _classify(root, intent, queues, resolved, bank):
    """Return an internal closure or a fixed reason. No private data is output."""
    counts = _counts(intent)
    for key in ('queue_before_sha256', 'queue_after_sha256'):
        if not isinstance(intent.get(key), str) or not _HASH.fullmatch(intent[key]):
            raise ValueError('missing_queue_hash')
    if intent['queue_before_sha256'] == intent['queue_after_sha256']:
        raise ValueError('empty_application')
    name = intent.get('backup')
    if not isinstance(name, str) or not _BACKUP.fullmatch(name):
        raise ValueError('invalid_backup_reference')
    backup = root / 'data/queues' / name
    before = _queues(root, backup)
    if _digest(before) != intent['queue_before_sha256']:
        return None, 'backup_mismatch'
    # These application modes are bank-read-only. A changed bank is an
    # ambiguous authority revision and is deliberately left for manual review.
    if _json(backup / 'answer_bank.json') != bank:
        return None, 'bank_changed'
    decision_id = intent['decision_id']
    current_hash = _digest(queues)
    if current_hash == intent['queue_before_sha256']:
        if decision_id in resolved:
            return None, 'receipt_conflicts_with_unwritten_state'
        return {'action': 'cancelled_unwritten'}, 'queues_match_prewrite_backup'
    if current_hash != intent['queue_after_sha256']:
        return None, 'partial_or_changed_queues'
    receipt = resolved.get(decision_id)
    if type(receipt) is not dict:
        return None, 'completed_receipt_missing'
    for key in ('decision_id', 'fingerprint', 'card_key', 'answer', 'bank_key', 'evidence',
                'context_sha256', 'evidence_sha256', 'config_sha256', 'target_role_ids'):
        if key not in intent or receipt.get(key) != intent[key]:
            return None, 'completed_receipt_mismatch'
    # Older receipts have no outcome snapshot. New receipts bind it to the
    # prewrite intent; recovery must not accept a rewritten attribution record.
    if (('target_resolution_state' in intent or 'target_resolution_state' in receipt)
            and ('target_resolution_state' not in intent
                 or receipt.get('target_resolution_state') != intent['target_resolution_state'])):
        return None, 'completed_resolution_state_mismatch'
    if (not intent.get('evidence') or intent['evidence_sha256'] != _digest(intent['evidence'])
            or receipt.get('approval') != intent.get('approval')):
        return None, 'completed_evidence_mismatch'
    before_rows = {row['role_id']: row for rows in before.values() for row in rows}
    after_rows = {row['role_id']: row for rows in queues.values() for row in rows}
    if set(before_rows) != set(after_rows):
        return None, 'queue_identity_changed'
    changed = sorted(rid for rid in before_rows if before_rows[rid] != after_rows[rid])
    if changed != counts['role_ids']:
        return None, 'changed_target_mismatch'
    removed = 0
    for rid in counts['role_ids']:
        old, new = before_rows[rid].get('unresolved'), after_rows[rid].get('unresolved')
        if (type(old) is not list or type(new) is not list
                or not all(isinstance(item, str) for item in old + new)):
            return None, 'invalid_target_blockers'
        remaining = list(old)
        for blocker in new:
            if blocker not in remaining:
                return None, 'target_blockers_changed'
            remaining.remove(blocker)
        if not remaining:
            return None, 'target_blockers_not_removed'
        removed += len(remaining)
    if removed != counts['removed_blockers']:
        return None, 'completed_count_mismatch'
    snapshots = {rid: _digest(after_rows[rid]) for rid in counts['role_ids']}
    if receipt.get('target_post_sha256') != snapshots:
        return None, 'completed_target_hash_mismatch'
    return {**receipt, **counts, 'action': 'recovered_applied',
            'original_action': 'human_applied' if intent.get('approval') else 'auto_applied',
            'class': intent.get('class'), 'confidence': intent.get('confidence'),
            'ready_verified': False}, 'queues_and_receipt_match_completed_application'


def _report(*, live, status='NO_CHANGE', reason=None, decisions=None, audit_writes=0):
    result = {'schema': 'keel.qresolve.recovery.v1', 'status': status,
              'mode': 'live' if live else 'dry_run', 'decisions': decisions or [],
              'canonical_writes': 0, 'audit_writes': audit_writes,
              'network_calls': 0, 'model_calls': 0, 'ready_verified': False}
    if reason:
        result['reason'] = reason
    return result


def preflight_locks(root):
    """Refuse unsafe existing lock/diagnostic files before lock-side writes.

    All participants must still use the canonical queue then bank locks. This
    check protects against pre-existing aliases, not a privileged concurrent
    process replacing directories between system calls.
    """
    root = Path(root).absolute()
    if Path(queue_io.get_lock_path()).absolute() != root / 'hidden_files/queue.lock':
        raise ValueError('lock_workspace_mismatch')
    for path in (root / 'hidden_files/queue.lock',
                 root / 'hidden_files/queue.lock.meta',
                 root / 'hidden_files/queue_lock_waits.jsonl',
                 root / 'data/answer_bank.json.lock'):
        try:
            _read(path, MAX_BYTES)
        except FileNotFoundError:
            # _read still pins existing parents, rejecting an aliased ancestor.
            pass


def _inspect(root, decision_id, *, live):
    queue_io._refuse_pending_transactions()
    folder = root / 'hidden_files'
    records = _journal(folder / 'qresolve-resolutions.jsonl')
    pending, completed = journal_state(records)
    if decision_id and decision_id not in pending:
        reason = 'already_completed' if decision_id in completed else 'no_pending_intent'
        return _report(live=live, reason=reason), None
    if not pending:
        return _report(live=live, reason='no_pending_intent'), None
    queues = _queues(root)
    bank = _json(root / 'data/answer_bank.json')
    resolved = _json(folder / 'qresolve-resolved.json', missing={})
    if type(bank) is not dict or type(resolved) is not dict:
        raise ValueError('invalid_canonical_state')
    decisions, closure = [], None
    for key, intent in pending.items():
        if decision_id and key != decision_id:
            continue
        try:
            candidate, reason = _classify(root, intent, queues, resolved, bank)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            candidate, reason = None, 'invalid_or_missing_recovery_evidence'
        item = {'decision_id': key, 'status': 'RECOVERABLE' if candidate else 'HOLD',
                'reason': reason}
        if candidate:
            item['proposed_action'] = candidate['action']
            closure = {**candidate, 'decision_id': key,
                       'ts': datetime.now(timezone.utc).isoformat(),
                       'intent_sha256': _digest(intent),
                       'observed_queue_sha256': _digest(queues),
                       'recovery_policy': 'qresolve.recovery.v1', 'reason': reason}
        decisions.append(item)
    status = 'HOLD' if any(row['status'] == 'HOLD' for row in decisions) else 'RECOVERABLE'
    return _report(live=live, status=status, decisions=decisions), closure


def recover(workspace=None, *, decision_id=None, live=False):
    """Inspect all pending intents, or durably close one explicitly selected ID."""
    if decision_id is not None and (not isinstance(decision_id, str) or not _HASH.fullmatch(decision_id)):
        return _report(live=live, status='HOLD', reason='invalid_decision_id')
    if live and decision_id is None:
        return _report(live=True, status='HOLD', reason='decision_id_required')
    try:
        root = _root(workspace)
        if not live:
            return _inspect(root, decision_id, live=False)[0]
        preflight_locks(root)
        with queue_io.queue_lock(owner='qresolve:recovery', recover=False), safe_io.file_lock(
                str(root / 'data/answer_bank.json') + '.lock'):
            report, closure = _inspect(root, decision_id, live=True)
            if report['status'] != 'RECOVERABLE' or closure is None:
                return report
            from tray_answer import _qresolve_append
            _qresolve_append(str(root / 'hidden_files/qresolve-resolutions.jsonl'), closure)
            report['status'], report['audit_writes'] = 'RECOVERED', 1
            report['decisions'][0]['status'] = 'RECOVERED'
            return report
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
        # Never expose an exception's file paths, answers, or source excerpts.
        return _report(live=live, status='HOLD', reason='recovery_state_unavailable_or_invalid')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect interrupted answer applications; never replay or roll back.')
    parser.add_argument('--decision-id', help='exact pending decision ID required for a live closure')
    parser.add_argument('--live', action='store_true', help='append a verified closure (default: inspect only)')
    args = parser.parse_args(argv)
    report = recover(decision_id=args.decision_id, live=args.live)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 3 if report['status'] == 'HOLD' else 0


if __name__ == '__main__':
    raise SystemExit(main())
