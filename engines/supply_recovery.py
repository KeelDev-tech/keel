"""Bounded supply observation, fair planning, and posting-only canaries.

No answers, hold clearing, queue moves, READY promotion, browser execution, or
submission. The only optional queue writes are the existing verifier's typed
posting observations. Completed shadow windows can inform pacing, never fit
policy or applicant authority. History is local evidence, not authentication.
"""
from __future__ import annotations

import copy
import re
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import muse_bridge as bridge
import pipeline_service as pipeline
import ready_gate
from fit_policy import main_floor
from genuine_pat import is_verify_only
from safe_io import atomic_json, digest, file_lock, utc_now, aware_time

SCHEMA = 'keel.supply-recovery.v1'
STAGES = ('held', 'questions', 'verification', 'materials', 'prepared', 'ready')
MAX_ROLES = 25
MAX_REQUESTS = 10
MAX_RUNS = 128
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')


def _bound(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(name + ' outside bounded canary policy')


def verification_only(entry):
    """Inspect an empty-question workflow token without treating it as consent.

    Strip only the queue-label token on a copy. Genuine blocker prose remains
    in the canonical classifier input; current structured questions must be
    positively empty. This can justify posting reads, never a READY transition.
    """
    if not isinstance(entry.get('unresolved'), list) or entry['unresolved']:
        return False
    if any(entry.get(key) not in (None, [], '') for key in ready_gate.QUESTION_FIELDS):
        return False
    candidate = copy.deepcopy(entry)
    for key in ('status_reason', 'gate_note', 'queue_notes'):
        value = candidate.get(key)
        if isinstance(value, str):
            candidate[key] = re.sub(r'\bneeds[-_ ]input\b', '', value, flags=re.I)
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            candidate[key] = [re.sub(r'\bneeds[-_ ]input\b', '', v, flags=re.I) for v in value]
        elif value is not None:
            return False
    return is_verify_only(candidate)


def _packet(state, entry, now):
    for folder in ('data/launch-packets/buffer', 'data/launch-packets'):
        # A role identifier is not a path capability.
        if not IDENT.fullmatch(entry['role_id']):
            return False
        try:
            packet = bridge.loads(bridge.secure_bytes(state['root'], folder + '/' + entry['role_id'] + '.json'))
            if ready_gate.packet_admission(packet, entry, state['bank'], workspace=state['root'],
                                           now=now, for_execution=False)['allowed']:
                return True
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return False


def _inventory(state, *, host_provider=None, now=None):
    now = now or utc_now()
    homes = Counter(entry['role_id'] for _, entry in state['entries'])
    posting_counts = Counter(pipeline._key(entry) for _, entry in state['entries'])
    records, by_id = [], {}
    for home, entry in state['entries']:
        rid = entry['role_id']
        if rid in by_id:
            continue
        by_id[rid] = entry
        key = pipeline._key(entry)
        static = ready_gate.entry_admission(entry, origin='strategic' if home == 'strategic' else 'standard',
                                             require_ready=False, workspace=state['root'], now=now)
        reasons = [code for code in static['reason_codes']
                   if code not in {'posting_not_current', 'posting_verification_unconfirmed'}]
        if homes[rid] != 1:
            reasons.append('duplicate_queue_home')
        if home == 'rejected':
            reasons.append('rejected_queue')
        if pipeline._held(entry) or entry.get('browser_task_id') or entry.get('attempt_id'):
            reasons.append('active_or_terminal_or_owned')
        if ready_gate.ledger_holds(entry, state['ledger']):
            reasons.append('ledger_hold')
        if key is None:
            reasons.append('posting_identity_missing')
        elif posting_counts[key] != 1:
            reasons.append('posting_identity_duplicate')
        current = pipeline.posting_is_current(entry, key, now=now) if key else False
        next_at = pipeline._next_time(entry, key)
        if next_at == 'invalid':
            reasons.append('verification_cooldown_invalid')
        elif not current and next_at is not None and next_at > now:
            reasons.append('verification_cooldown')
        if entry.get('verification_event_pending'):
            reasons.append('verification_event_pending')
        packet = _packet(state, entry, now) if not reasons and current else False
        host = bridge.observe(host_provider, bridge.request_for(state, entry, now=now), now=now)
        observed = host['record']
        if observed and (observed['task_state'] != 'CLEAR' or observed['attempt_state'] != 'CLEAR'):
            reasons.append('host_runtime_hold')
        if observed and observed['approval_state'] == 'REFUSED':
            reasons.append('host_approval_refused')
        questions = any(entry.get(field) not in (None, [], '') for field in ready_gate.QUESTION_FIELDS)
        if reasons:
            stage = 'questions' if questions and all(code.startswith('unanswered_questions:') for code in reasons) else 'held'
        elif home == 'needs_input' and not verification_only(entry):
            stage = 'questions'
            reasons.append('question_or_classification_unconfirmed')
        elif not current:
            stage = 'verification'
            reasons.append('posting_check_required')
        elif not packet:
            stage = 'materials'
            reasons.append('scoped_material_packet_required')
        elif entry.get('status') not in ready_gate.READY_STATES:
            stage = 'prepared'
            reasons.append('ready_transition_not_observed')
        else:
            stage = 'ready'
        launchable = None
        if observed is not None:
            launchable = bool(stage == 'ready' and packet and not reasons and
                              observed['form_state'] == 'VERIFIED' and observed['materials_state'] == 'READY'
                              and observed['approval_state'] == 'APPROVED')
        records.append({'role_id': rid, 'home': home, 'stage': stage,
                        'reason_codes': sorted(set(reasons)), 'row_sha256': digest(entry),
                        'source_ref': ':'.join(key[:2]) if key else None,
                        'posting_identity': list(key) if key else None,
                        'fit_score': entry.get('fit_score'), 'prepared_artifact': packet and not any(reason != 'ready_transition_not_observed' for reason in reasons),
                        'host_scope': host['scope'], 'host_reason': host['reason'],
                        'launchable_observed': launchable,
                        'verification_only_candidate': home == 'needs_input' and stage == 'verification'})
    return records, by_id


def _state_path(root):
    folder = root / 'data/supply-recovery'
    if folder.is_symlink() or folder.exists() and (not folder.is_dir() or folder.resolve() != folder):
        raise ValueError('unsafe supply history directory')
    return folder / 'history.json'


def _history(root):
    path = _state_path(root)
    if not path.exists() and not path.is_symlink():
        return {'schema': SCHEMA, 'runs': [], 'last_selected': {}}
    value = bridge.loads(bridge.secure_bytes(root, path.relative_to(root).as_posix(), limit=2 * 1024 * 1024))
    if (not isinstance(value, dict) or set(value) != {'schema', 'runs', 'last_selected'}
            or value['schema'] != SCHEMA or not isinstance(value['runs'], list)
            or len(value['runs']) > MAX_RUNS or not isinstance(value['last_selected'], dict)
            or len(value['last_selected']) > bridge.MAX_HOST_ROWS):
        raise ValueError('supply history is corrupt or exceeds capacity')
    seen = set()
    for run in value['runs']:
        if (not isinstance(run, dict) or not isinstance(run.get('run_id'), str)
                or not IDENT.fullmatch(run['run_id']) or run['run_id'] in seen
                or run.get('state') not in {'STARTED', 'COMPLETE'}
                or run.get('integrity_sha256') != digest({k: v for k, v in run.items() if k != 'integrity_sha256'})):
            raise ValueError('supply history run invalid')
        seen.add(run['run_id'])
        started = aware_time(run.get('started_at'))
        ids = run.get('selected_role_ids')
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ROLES
                or any(not isinstance(rid, str) or not rid.strip() for rid in ids)
                or len(set(ids)) != len(ids)
                or run.get('verification_run_id') != run['run_id'] + '-verify'
                or any(not isinstance(run.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', run[key])
                       for key in ('policy_sha256', 'before_manifest'))):
            raise ValueError('supply history binding invalid')
        if run['state'] == 'COMPLETE':
            if aware_time(run.get('finished_at')) <= started:
                raise ValueError('supply history interval invalid')
            window = run.get('window')
            if (not isinstance(window, dict) or type(window.get('conservation_pass')) is not bool
                    or not isinstance(window.get('sources'), dict)
                    or type(window.get('reader_dispatches')) is not int
                    or not 0 <= window['reader_dispatches'] <= MAX_REQUESTS
                    or window.get('before_manifest') != run['before_manifest']):
                raise ValueError('supply history window invalid')
            for source, stats in window['sources'].items():
                if (not isinstance(source, str) or not isinstance(stats, dict)
                        or set(stats) != {'dispatches', 'observed_prepared_gains'}
                        or any(type(v) is not int or v < 0 for v in stats.values())):
                    raise ValueError('supply history counters invalid')
            if sum(stats['dispatches'] for stats in window['sources'].values()) != window['reader_dispatches']:
                raise ValueError('supply history dispatches do not reconcile')
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in value['last_selected'].items()):
        raise ValueError('supply history fairness state invalid')
    for selected_at in value['last_selected'].values():
        aware_time(selected_at)
    return value


def _seal(run):
    run['integrity_sha256'] = digest({k: v for k, v in run.items() if k != 'integrity_sha256'})
    return run


def _feedback(history, manifest):
    windows = [run for run in history['runs'] if run['state'] == 'COMPLETE'
               and run.get('window', {}).get('conservation_pass')
               and run.get('window', {}).get('policy_unchanged')
               and run.get('policy_sha256') == digest({'floor': manifest['main_fit_floor'],
                                                     'code': manifest['code_manifest_sha256']})]
    sources = defaultdict(lambda: {'dispatches': 0, 'observed_prepared_gains': 0})
    # Two independently completed windows are required before yield affects pacing.
    if len(windows) >= 2:
        for run in windows:
            for source, stats in run['window']['sources'].items():
                sources[source]['dispatches'] += stats['dispatches']
                sources[source]['observed_prepared_gains'] += stats['observed_prepared_gains']
    yield_by_source = {ref: stats['observed_prepared_gains'] / stats['dispatches']
                       for ref, stats in sources.items() if stats['dispatches'] > 0}
    return {'reconciled_windows': len(windows), 'enabled': len(windows) >= 2,
            'observed_yield_by_source': yield_by_source,
            'scope': 'Observed preparation gains per reader dispatch; not causal attribution or submission success.'}


def plan(workspace, *, limit=MAX_ROLES, max_requests=MAX_REQUESTS, host_provider=None, now=None):
    _bound(limit, 'limit', MAX_ROLES)
    _bound(max_requests, 'max_requests', MAX_REQUESTS)
    now = now or utc_now()
    state = bridge.read_state(workspace)
    records, by_id = _inventory(state, host_provider=host_provider, now=now)
    history = _history(state['root'])
    feedback = _feedback(history, state['manifest'])
    eligible = [record for record in records if record['stage'] == 'verification']
    # Reserve half the cohort for least recently selected work. Dry plans do
    # not advance this clock; only completed canaries do. Yield breaks ties in
    # the remaining capacity without granting policy or queue authority.
    aging = lambda r: (history['last_selected'].get(r['role_id'], ''), r['role_id'])
    fair = sorted(eligible, key=aging)
    priority = sorted(eligible, key=lambda r: (-feedback['observed_yield_by_source'].get(r['source_ref'], 0), *aging(r)))
    ordered = fair[:(limit + 1)//2] + priority + fair
    selected, seen, sources = [], set(), set()
    # One source reserves two dispatch slots for the bounded timeout retry.
    # Pagination may need more; the actual reader cap still governs the run.
    for record in ordered:
        if record['role_id'] in seen or len(selected) >= limit:
            continue
        ref = record['source_ref']
        reserve = 1 if max_requests == 1 else 2
        if ref not in sources and (len(sources) + 1) * reserve > max_requests:
            continue
        seen.add(record['role_id']); sources.add(ref); selected.append(record)
    counts = dict(Counter(record['stage'] for record in records))
    known = [r['launchable_observed'] for r in records]
    result = {'schema': SCHEMA, 'mode': 'READ_ONLY_PLAN', 'observed_at': now.isoformat(),
              'manifest': state['manifest'], 'records': records, 'stage_counts': counts,
              'queue_rows': len(state['entries']), 'distinct_roles': len(records),
              'conservation_pass': sum(counts.values()) == len(records) == len(state['entries']),
              'selected': selected, 'selected_count': len(selected), 'limit': limit,
              'max_reader_dispatches': max_requests, 'planned_sources': sorted(sources),
              'feedback': feedback, 'pending_runs': [r['run_id'] for r in history['runs'] if r['state'] == 'STARTED'],
              'launchable_ready': sum(known) if all(v is not None for v in known) and host_provider is not None else None,
              'prepared_artifacts': sum(r['prepared_artifact'] for r in records),
              'submission_authorized': False, 'execution_authorized': False,
              'promoted_to_ready': 0, 'network_reads': 0}
    result['plan_id'] = digest({k: v for k, v in result.items() if k not in {'observed_at', 'plan_id'}})
    return result


def compare(before, after, dispatches=None):
    for item in (before, after):
        if item.get('schema') != SCHEMA or not isinstance(item.get('records'), list):
            raise ValueError('supply window contract invalid')
        if len({r['role_id'] for r in item['records']}) != len(item['records']):
            raise ValueError('supply window roles are duplicated')
    if before['manifest']['workspace_sha256'] != after['manifest']['workspace_sha256']:
        raise ValueError('supply window workspace changed')
    old = {r['role_id']: r for r in before['records']}
    new = {r['role_id']: r for r in after['records']}
    common = set(old) & set(new)
    transitions = Counter(old[r]['stage'] + '->' + new[r]['stage'] for r in common)
    policy_unchanged = all(before['manifest'][key] == after['manifest'][key]
                           for key in ('main_fit_floor', 'code_manifest_sha256'))
    retargeted = {r for r in common if old[r].get('posting_identity') != new[r].get('posting_identity')}
    gains = [r for r in common - retargeted if policy_unchanged
             and not old[r]['prepared_artifact'] and new[r]['prepared_artifact']]
    sources = defaultdict(lambda: {'dispatches': 0, 'observed_prepared_gains': 0})
    for rid in gains:
        source = new[rid]['source_ref']
        if source: sources[source]['observed_prepared_gains'] += 1
    for source, amount in (dispatches or {}).items():
        if not isinstance(source, str) or type(amount) is not int or amount < 0:
            raise ValueError('observed dispatch count invalid')
        sources[source]['dispatches'] = amount
    total = sum(s['dispatches'] for s in sources.values())
    return {'before_manifest': before['manifest']['sha256'], 'after_manifest': after['manifest']['sha256'],
            'conservation_pass': bool(before['conservation_pass'] and after['conservation_pass']
                                      and len(common) + len(set(old) - set(new)) == len(old)
                                      and len(common) + len(set(new) - set(old)) == len(new)),
            'policy_unchanged': policy_unchanged, 'retargeted_roles': len(retargeted),
            'added_roles': len(set(new) - set(old)), 'removed_roles': len(set(old) - set(new)),
            'transitions': dict(transitions), 'observed_prepared_gains': len(gains),
            'sources': dict(sources), 'reader_dispatches': total,
            'prepared_gains_per_100_dispatches': 100 * len(gains)/total if total else None,
            'credits_used': None, 'model_tokens_used_by_controller': 0,
            'prepared_gains_per_model_token': None, 'causal_attribution': False,
            'launchable_delta': (after['launchable_ready'] - before['launchable_ready'])
                                if before['launchable_ready'] is not None and after['launchable_ready'] is not None else None}


def _dispatch_sources(attempts):
    from urllib.parse import urlsplit
    result = Counter()
    for attempt in attempts:
        p = urlsplit(attempt['source_url']); parts = p.path.strip('/').split('/')
        source = None
        if p.hostname == 'boards-api.greenhouse.io' and parts[:2] == ['v1', 'boards'] and len(parts) > 2:
            source = 'greenhouse:' + parts[2]
        elif p.hostname in {'api.lever.co', 'api.eu.lever.co'} and parts[:2] == ['v0', 'postings'] and len(parts) > 2:
            source = ('lever_eu:' if p.hostname == 'api.eu.lever.co' else 'lever:') + parts[2]
        elif p.hostname == 'api.ashbyhq.com' and parts[:2] == ['posting-api', 'job-board'] and len(parts) > 2:
            source = 'ashby:' + parts[2]
        result[source or 'unattributed'] += 1
    return dict(result)


def canary(workspace, *, plan_id, run_id=None, limit=MAX_ROLES, max_requests=MAX_REQUESTS,
           timeout=30, live=False, reader=None, host_provider=None, now=None):
    _bound(timeout, 'timeout', 120)
    before = plan(workspace, limit=limit, max_requests=max_requests, host_provider=host_provider, now=now)
    if before['plan_id'] != plan_id or not before['conservation_pass']:
        raise ValueError('supply plan changed or inventory does not conserve')
    if before['pending_runs']:
        raise ValueError('unfinished canary requires reconciliation; no automatic replay')
    selected_ids = [r['role_id'] for r in before['selected']]
    if not selected_ids:
        return {'schema': SCHEMA, 'status': 'NO_CANDIDATES', 'network_reads': 0,
                'promoted_to_ready': 0, 'submission_authorized': False}
    if live and any(r['host_scope'] != 'AUTHORITATIVE_PROVIDER' for r in before['selected']):
        raise ValueError('live posting commits require authoritative host task/attempt observations')
    root = bridge.bound_workspace(workspace)
    run_id = run_id or 'canary-' + uuid.uuid4().hex
    if not isinstance(run_id, str) or not IDENT.fullmatch(run_id):
        raise ValueError('invalid canary run identity')
    path = _state_path(root)
    verification_run = run_id + '-verify'
    reader = reader or pipeline.PublicBoardReader(timeout, max_requests=max_requests, retry_timeouts=True)
    if getattr(reader, 'max_requests', None) is None or reader.max_requests > max_requests:
        raise ValueError('reader is not inside the canary request budget')
    state = bridge.read_state(root)
    if state['manifest']['sha256'] != before['manifest']['sha256']:
        raise ValueError('workspace changed before canary')
    expected = {entry['role_id']: copy.deepcopy(entry) for _, entry in state['entries'] if entry['role_id'] in selected_ids}
    run = {'run_id': run_id, 'verification_run_id': verification_run, 'state': 'STARTED',
           'started_at': utc_now().isoformat(), 'selected_role_ids': selected_ids,
           'policy_sha256': digest({'floor': before['manifest']['main_fit_floor'],
                                    'code': before['manifest']['code_manifest_sha256']}),
           'before_manifest': before['manifest']['sha256']}
    if live:
        with file_lock(path.parent / 'history.lock'):
            history = _history(root)
            if len(history['runs']) >= MAX_RUNS or any(r['state'] == 'STARTED' or r['run_id'] == run_id for r in history['runs']):
                raise ValueError('history capacity, duplicate run or pending reconciliation')
            history['runs'].append(_seal(run)); atomic_json(path, history)
    # A crash after STARTED leaves a visible hold. A new invocation will not
    # spend more budget or overwrite its evidence under the same identity.
    def commit_guard(row):
        observed = bridge.observe(host_provider, bridge.request_for(state, row))['record']
        return bool(observed and observed['task_state'] == 'CLEAR'
                    and observed['attempt_state'] == 'CLEAR' and observed['approval_state'] != 'REFUSED')

    result = pipeline.verify(root, limit=limit, timeout=timeout, live=live, reader=reader,
                             role_ids=selected_ids, expected_rows=expected,
                             expected_inputs={name: value for name, value in state['manifest']['input_hashes'].items()
                                              if not name.startswith('data/queues/') and name != 'data/application-ledger.json'},
                             run_id=verification_run, commit_guard=commit_guard if live else None)
    after = plan(root, limit=limit, max_requests=max_requests, host_provider=host_provider)
    window = compare(before, after, _dispatch_sources(result['request_attempts']))
    if live:
        with file_lock(path.parent / 'history.lock'):
            history = _history(root)
            found = [r for r in history['runs'] if r['run_id'] == run_id]
            if len(found) != 1 or found[0] != run:
                raise ValueError('canary journal changed during execution')
            completed = _seal({**run, 'state': 'COMPLETE', 'finished_at': utc_now().isoformat(),
                               'window': window, 'verification': result})
            found[0].clear(); found[0].update(completed)
            for rid in selected_ids: history['last_selected'][rid] = completed['finished_at']
            atomic_json(path, history)
    return {'schema': SCHEMA, 'status': 'COMPLETE', 'run_id': run_id, 'dry_run': not live,
            'verification': result, 'window': window, 'promoted_to_ready': 0,
            'submission_authorized': False, 'execution_authorized': False,
            'scope': 'Posting observations only; gains are concurrent observations, not causally attributed.'}


def recovery_status(workspace):
    """Read-only evidence for an interrupted canary; never close/replay it."""
    state = bridge.read_state(workspace)
    history = _history(state['root'])
    entries = {entry['role_id']: entry for _, entry in state['entries']}
    reports = []
    for run in history['runs']:
        if run['state'] != 'STARTED': continue
        observed = []
        for rid in run['selected_role_ids']:
            attempt = (entries.get(rid) or {}).get('verification_attempt')
            if isinstance(attempt, dict) and str(attempt.get('observation_id', '')).startswith(run['verification_run_id'] + ':'):
                observed.append(rid)
        reports.append({'run_id': run['run_id'], 'selected': len(run['selected_role_ids']),
                        'matching_committed_attempts': len(observed), 'state': 'RECONCILIATION_REQUIRED',
                        'authority_to_replay': False})
    return {'schema': SCHEMA, 'mode': 'READ_ONLY', 'pending': reports,
            'submission_authorized': False, 'network_reads': 0}
