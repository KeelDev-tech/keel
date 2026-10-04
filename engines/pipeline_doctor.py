"""Offline supply and READY diagnosis, without queue, bank or event writes.

This inspects a coherent local snapshot. Static admission and packet integrity
are measured here; runtime task liveness and submission approval are not.
"""
from collections import Counter, defaultdict
from pathlib import Path

from genuine_pat import is_verify_only
from queue_io import queue_lock
from safe_io import read_json, rows, contained_path, utc_now
from fit_policy import main_floor

QUEUES = ('standard', 'strategic', 'needs_input', 'rejected')


def report(workspace):
    workspace = Path(workspace).absolute()
    problems, entries, ledger = [], [], []
    # A pending interrupted transaction must be recovered by an operational
    # writer, never silently by this diagnostic command.
    with queue_lock(timeout=10, owner='pipeline-doctor:read', recover=False):
        for name in QUEUES:
            path = workspace / 'data/queues' / (name + '-queue.json')
            try:
                if not path.is_file():
                    raise ValueError('missing queue')
                entries.extend((name, entry) for entry in rows(read_json(path)))
            except (OSError, ValueError, TypeError) as exc:
                problems.append({'source': path.name, 'reason': str(exc)})
        try:
            ledger_path = workspace / 'data/application-ledger.json'
            if not ledger_path.is_file():
                raise ValueError('missing ledger')
            ledger = rows(read_json(ledger_path))
        except (OSError, ValueError, TypeError) as exc:
            problems.append({'source': 'application-ledger.json', 'reason': str(exc)})
    from ready_gate import entry_admission, packet_admission, ledger_holds
    bank = read_json(workspace / 'data/answer_bank.json', missing={})
    homes = defaultdict(list)
    states, reasons = Counter(), Counter()
    for name, entry in entries:
        rid = entry.get('role_id')
        if isinstance(rid, str) and rid:
            homes[rid].append(name)
    duplicates = {rid: names for rid, names in homes.items() if len(names) != 1}
    ready_rows, zero_question_input, verify_only_input = [], 0, 0
    for name, entry in entries:
        rid = entry.get('role_id')
        states[(name, str(entry.get('status', 'UNKNOWN')))] += 1
        if name == 'needs_input':
            unresolved = entry.get('unresolved')
            if isinstance(unresolved, list) and not unresolved:
                zero_question_input += 1
            if is_verify_only(entry):
                verify_only_input += 1
        if entry.get('status') not in {'READY', 'READY-FOR-BROWSER'}:
            continue
        assessment = entry_admission(entry, origin=name, workspace=workspace)
        codes = list(assessment['reason_codes'])
        if not isinstance(rid, str) or not rid:
            codes.append('invalid_role_id')
        elif rid in duplicates:
            codes.append('duplicate_queue_home')
        try:
            if ledger_holds(entry, ledger):
                codes.append('ledger_hold')
        except (TypeError, ValueError):
            codes.append('ledger_unconfirmed')
        if name not in {'standard', 'strategic'}:
            codes.append('non_admission_queue')
        packet_valid, packet_reason = None, 'packet_not_evaluated'
        if not codes:
            packet_valid, packet_reason = False, 'packet_missing'
            for folder in ('data/launch-packets/buffer', 'data/launch-packets'):
                try:
                    path = contained_path(workspace / folder, str(rid) + '.json')
                    packet = read_json(path)
                    result = packet_admission(packet, entry, bank, workspace=workspace,
                                              for_execution=False)
                    packet_valid = result['allowed']
                    packet_reason = '' if packet_valid else ','.join(result['reason_codes'])
                    if packet_valid:
                        break
                except (OSError, ValueError, TypeError, KeyError):
                    continue
        reasons.update(codes)
        if packet_valid is False:
            reasons[packet_reason or 'packet_invalid'] += 1
        ready_rows.append({'role_id': rid, 'queue': name,
                           'static_admission_pass': not codes,
                           'packet_integrity_pass': packet_valid,
                           'reason_codes': codes,
                           'packet_reason': packet_reason})
    return {'schema_version': 1, 'mode': 'OFFLINE_DIAGNOSTIC',
            'observed_at': utc_now().isoformat(), 'main_fit_floor': main_floor(),
            'data_complete': not problems, 'data_problems': problems,
            'queue_rows': len(entries) if not problems else None,
            'observed_queue_rows': len(entries), 'distinct_role_ids': len(homes),
            'one_home_per_role': (not duplicates and all(
                isinstance(e.get('role_id'), str) and bool(e['role_id'].strip())
                for _, e in entries)) if not problems else None,
            'duplicate_queue_homes': duplicates,
            'queue_states': [{'queue': q, 'status': s, 'rows': count}
                             for (q, s), count in sorted(states.items())],
            'nominal_ready': len(ready_rows) if not problems else None,
            'static_admissible_ready': sum(r['static_admission_pass'] for r in ready_rows) if not problems else None,
            'packet_backed_ready': sum(r['packet_integrity_pass'] is True and r['static_admission_pass'] for r in ready_rows) if not problems else None,
            'launchable_ready': None,
            'launchable_scope': 'Runtime ownership, current form and explicit execution approval require the host; not observed here.',
            'packet_scope': 'Packet integrity measures prepared artifacts; it does not establish execution authority.',
            'ready_loss_reasons': dict(reasons), 'ready_rows': ready_rows,
            'needs_input_empty_structured_unresolved': zero_question_input,
            'needs_input_verification_only': verify_only_input,
            'input_scope': 'Empty unresolved is an inspection candidate, never authority to clear other holds or promote READY.',
            'submission_authorized': False, 'network_reads': 0}
