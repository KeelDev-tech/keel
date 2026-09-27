"""Durable fair admission to the real public-source intake pipeline.

Local POSIX, standard library, owner-controlled state. This schedules public
reads and PARKED intake only. It never retries application actions or clears
their holds. A completed source is committed independently; a later 429 stops
the run and retains earlier complete-source commits. The file lock excludes
cooperating controllers, not arbitrary code running as the same OS user.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import stat
import time
import uuid

import pipeline_service as pipeline
from safe_io import atomic_json, canonical, digest, file_lock, loads

SCHEMA = 'keel.source-scheduler.v1'
MAX_STATE_BYTES = 256 * 1024
MAX_SEQUENCE = 10**12
SUCCESS_REFRESH_SECONDS = 300
RATE_LIMIT_HOLD_SECONDS = 300
FLAGS = {'execution_authorized': False, 'submission_authorized': False,
         'paid_services_required': False}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**12


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _paths(workspace):
    root = Path(os.path.abspath(workspace))
    _require(root.is_dir() and root.resolve(strict=True) == root, 'scheduler workspace must be an existing real directory')
    folder = root / 'data'
    folder.mkdir(mode=0o700, exist_ok=True)
    _require(folder.is_dir() and not folder.is_symlink(), 'scheduler data directory must be real')
    return root, folder / 'source-scheduler.json', folder / 'source-scheduler.lock'


def _load(path, now):
    if not path.exists():
        _require(not path.is_symlink(), 'scheduler state symlink forbidden')
        return {'schema': SCHEMA, 'sequence': 0, 'last_now': now, 'cooldown_until': 0,
                'sources': {}, 'inflight': None, 'last_recovery': None}
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode), 'scheduler state must be a regular file')
        raw = stream.read(MAX_STATE_BYTES + 1)
    _require(len(raw) <= MAX_STATE_BYTES, 'scheduler state byte budget exhausted')
    state = loads(raw)
    _require(type(state) is dict and set(state) == {'schema', 'sequence', 'last_now',
        'cooldown_until', 'sources', 'inflight', 'last_recovery'} and state['schema'] == SCHEMA,
        'scheduler state schema invalid')
    _require(_integer(state['sequence'], MAX_SEQUENCE) and _number(state['last_now'])
             and now >= state['last_now'] and _number(state['cooldown_until']), 'scheduler clock or sequence invalid')
    _require(type(state['sources']) is dict and len(state['sources']) <= pipeline.MAX_BOARDS,
             'scheduler source capacity exhausted')
    for ref, row in state['sources'].items():
        pipeline.parse_source(ref)
        _require(type(row) is dict and set(row) == {'last_attempt', 'next_at', 'failures', 'last_status', 'last_added'}
            and _integer(row['last_attempt'], state['sequence']) and _number(row['next_at'])
            and _integer(row['failures'], 10) and (row['last_status'] is None or
            type(row['last_status']) is str and len(row['last_status']) <= 64)
            and _integer(row['last_added'], 2000), 'scheduler source state invalid')
    flight = state['inflight']
    if flight is not None:
        _require(type(flight) is dict and set(flight) == {'run_id', 'selected', 'completed', 'binding', 'started_at'}
            and type(flight['run_id']) is str and len(flight['run_id']) == 32
            and type(flight['binding']) is str and len(flight['binding']) == 64
            and _number(flight['started_at']) and type(flight['selected']) is list
            and 1 <= len(flight['selected']) <= 16 and all(type(ref) is str for ref in flight['selected'])
            and len(set(flight['selected'])) == len(flight['selected'])
            and all(ref in state['sources'] for ref in flight['selected'])
            and type(flight['completed']) is list and flight['completed'] == flight['selected'][:len(flight['completed'])],
            'scheduler in-flight state invalid')
    recovery = state['last_recovery']
    _require(recovery is None or type(recovery) is dict and set(recovery) ==
             {'run_id', 'at', 'queue_sha256', 'uncertain_sources'}
             and type(recovery['run_id']) is str and len(recovery['run_id']) == 32
             and _number(recovery['at']) and type(recovery['queue_sha256']) is str
             and len(recovery['queue_sha256']) == 64 and type(recovery['uncertain_sources']) is list
             and len(recovery['uncertain_sources']) <= 16
             and all(type(ref) is str and len(ref) <= 256 for ref in recovery['uncertain_sources']),
             'scheduler recovery state invalid')
    return state


def _save(path, state, now):
    _require(_number(now) and now >= state['last_now'], 'scheduler clock regressed')
    state['last_now'] = now
    _require(len(canonical(state)) <= MAX_STATE_BYTES, 'scheduler state byte budget exhausted')
    atomic_json(path, state)


def _stamp(clock):
    now = clock()
    _require(_number(now), 'scheduler clock invalid')
    return now


def _reply(status, **extra):
    return {'schema': SCHEMA, 'status': status, 'commit_scope': 'per_complete_source',
            'added': 0, 'requests': 0, **extra, **FLAGS}


def scheduled_discover(workspace, *, max_boards=4, max_new=200, timeout=120,
                       max_requests=50, titles=(), locations=(), reader=None, clock=time.time):
    """Run a bounded fair batch, retaining the cursor and cooldown across restarts.

    Every selected board gets a positive intake allocation, whose sum is at most
    max_new. Unavailable sources back off without starving never-read sources.
    A prior interrupted controller causes HELD_RECOVERY; ``recover_interrupted``
    explicitly reconciles local queue shape before allowing idempotent rereads.
    Caller-provided readers are trusted host adapters, subject to the same
    PublicBoardReader interface and budgets. Deadlines are cooperative.
    """
    pipeline._bounded(max_boards, 'max_boards', 16)
    pipeline._bounded(max_new, 'max_new', 2000)
    pipeline._bounded(max_requests, 'max_requests', 1000)
    pipeline._budget(timeout)
    for terms in (titles, locations):
        _require(type(terms) in (tuple, list) and len(terms) <= 32 and
                 all(type(term) is str and 0 < len(term) <= 200 for term in terms), 'scheduler filters invalid')
    root, path, lock = _paths(workspace)
    if reader is not None:
        _require(isinstance(reader, pipeline.PublicBoardReader) and reader.requests == 0 and not reader.stopped,
                 'scheduler needs a fresh bounded PublicBoardReader')
        _require(reader.max_requests <= max_requests, 'reader request budget exceeds scheduler budget')
        # Tighten, never extend, an injected reader's configured deadline.
        reader.deadline = min(reader.deadline, time.monotonic() + timeout)
    with file_lock(lock, timeout=0):
        now = _stamp(clock)
        state = _load(path, now)
        if state['inflight'] is not None:
            return _reply('HELD_RECOVERY', interrupted_run_id=state['inflight']['run_id'])
        if now < state['cooldown_until']:
            return _reply('HELD_HTTP_429', next_eligible_at=state['cooldown_until'])
        sources = pipeline._sources(root)
        configured = {source['ref']: source for source in sources}
        # Removed/disabled sources cannot occupy the bounded active registry.
        state['sources'] = {ref: state['sources'].get(ref, {'last_attempt': 0, 'next_at': 0,
            'failures': 0, 'last_status': None, 'last_added': 0}) for ref in configured}
        eligible = [ref for ref, row in state['sources'].items() if row['next_at'] <= now]
        eligible.sort(key=lambda ref: (state['sources'][ref]['last_attempt'], ref))
        selected = eligible[:min(max_boards, max_new)]
        if not selected:
            _save(path, state, now)
            return _reply('IDLE', registered_sources=len(sources), next_eligible_at=
                          min((row['next_at'] for row in state['sources'].values()), default=None))
        if (root / 'DEMO_ONLY.json').exists() and reader is None:
            raise ValueError('synthetic workspace cannot perform live source reads')
        reader = reader or pipeline.PublicBoardReader(timeout=timeout, max_requests=max_requests)
        binding = digest({'sources': [configured[ref] for ref in selected], 'titles': list(titles),
                          'locations': list(locations), 'max_new': max_new, 'max_requests': max_requests})
        state['inflight'] = {'run_id': uuid.uuid4().hex, 'selected': selected, 'completed': [],
                             'binding': binding, 'started_at': now}
        _require(state['sequence'] + len(selected) <= MAX_SEQUENCE, 'scheduler lifetime capacity exhausted')
        # Reserve fair turns before effects. Ambiguous local effects remain held
        # after a crash; the queue's exact identity deduplication handles rereads.
        previous_attempts = {ref: state['sources'][ref]['last_attempt'] for ref in selected}
        for ref in selected:
            state['sequence'] += 1
            state['sources'][ref]['last_attempt'] = state['sequence']
        _save(path, state, now)
        run_id, reports = state['inflight']['run_id'], []
        allocations = [max_new // len(selected) + (index < max_new % len(selected))
                       for index in range(len(selected))]
        held = None
        for ref, allocation in zip(selected, allocations):
            try:
                pipeline._deadline(reader)
            except TimeoutError:
                held = 'HELD_DEADLINE'
                break
            # Exhaustion before a board read is not a failed source attempt.
            # Leave its backoff untouched and return its reserved fair turn.
            if pipeline._request_budget_blocks(reader, ref):
                held = 'HELD_REQUEST_BUDGET'
                break
            dispatches_before = reader.requests
            result = pipeline.discover(root, max_new=allocation, timeout=timeout, titles=titles,
                                       locations=locations, reader=reader, source_refs=[ref])
            stamp = _stamp(clock)
            _require(stamp >= now, 'scheduler clock regressed')
            if (result['status'] == 'HELD_DEADLINE' and reader.requests == dispatches_before
                    and not result.get('sources')):
                # The deadline can expire after admission but before read().
                # Do not invent a failed source attempt from that empty pass.
                # A dispatched read or completed cached source retains the
                # existing attempt/backoff accounting, even if it later times out.
                held = 'HELD_DEADLINE'
                break
            if reader.stopped and reader.blocked_by_host_cooldown:
                # A persisted host hold did not complete or fail this source's
                # new read. Return its turn and retain its old backoff state.
                state['cooldown_until'] = stamp + RATE_LIMIT_HOLD_SECONDS
                held = 'HELD_HTTP_429'
                break
            row = state['sources'][ref]
            source_complete = result['status'] == 'COMPLETE'
            row['last_status'], row['last_added'] = result['status'], result['added']
            row['failures'] = 0 if source_complete else min(10, row['failures'] + 1)
            row['next_at'] = stamp + (SUCCESS_REFRESH_SECONDS if source_complete else min(3600, 60 * 2**(row['failures'] - 1)))
            if reader.stopped:
                state['cooldown_until'] = stamp + RATE_LIMIT_HOLD_SECONDS
                held = 'HELD_HTTP_429'
            elif result['status'].startswith('HELD_'):
                held = ('HELD_REQUEST_BUDGET' if reader.requests >= reader.max_requests
                        else result['status'])
            reports.append({'ref': ref, 'allocation': allocation, 'result': result})
            state['inflight']['completed'].append(ref)
            _save(path, state, stamp)
            if held:
                break
        # A shared request budget/deadline/429 may stop before peers are read.
        # Return their turns; otherwise a slow first board can starve its peers.
        for ref in selected[len(reports):]:
            state['sources'][ref]['last_attempt'] = previous_attempts[ref]
        state['inflight'] = None
        _save(path, state, _stamp(clock))
        return _reply(held or ('PARTIAL_SOURCES' if any(row['result']['status'] != 'COMPLETE'
                    for row in reports) else 'COMPLETE'), run_id=run_id,
            added=sum(row['result']['added'] for row in reports), requests=reader.requests,
            selected=selected, attempted=[row['ref'] for row in reports], sources=reports,
            registered_sources=len(sources), deferred_sources=len(sources) - len(reports),
            next_eligible_at=state['cooldown_until'] if held == 'HELD_HTTP_429' else None,
            recovery_required=False)


def recover_interrupted(workspace, *, clock=time.time):
    """Reconcile local intake storage; no application status or hold is changed.

    It is safe to reread these public sources because discover compares exact
    identities against every current queue and ledger at commit. An interrupted
    run's added count remains UNKNOWN; recovery does not invent a receipt.
    """
    root, path, lock = _paths(workspace)
    with file_lock(lock, timeout=0):
        now = _stamp(clock)
        state = _load(path, now)
        flight = state['inflight']
        if flight is None:
            return _reply('IDLE', recovered=False)
        with pipeline.queue_lock(timeout=10, owner='source-scheduler:reconcile'):
            documents, ledger = pipeline._documents(root)
            queue_hash = digest({'queues': {str(p.relative_to(root)): doc for p, doc in documents.items()},
                                 'ledger': ledger})
        uncertain = flight['selected'][len(flight['completed']):]
        for ref in uncertain:
            state['sources'][ref]['next_at'] = now
            state['sources'][ref]['last_status'] = 'UNKNOWN_LOCAL_INTAKE'
        state['last_recovery'] = {'run_id': flight['run_id'], 'at': now,
                                  'queue_sha256': queue_hash, 'uncertain_sources': uncertain}
        state['inflight'] = None
        # A crash may have interrupted the 429 checkpoint. Conservatively hold
        # all rereads for the same cooldown; safe_http's host hold also remains.
        state['cooldown_until'] = max(state['cooldown_until'], now + RATE_LIMIT_HOLD_SECONDS)
        _save(path, state, now)
        return _reply('RECOVERED_COOLDOWN', recovered=True, previous_added=None,
                      previous_outcome='UNKNOWN', uncertain_sources=uncertain,
                      next_eligible_at=state['cooldown_until'], queue_sha256=queue_hash)
