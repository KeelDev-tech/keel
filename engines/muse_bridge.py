"""Bounded read-only Muse observation contract, not a runtime/authentication adapter.

An integration may inject an authoritative, bounded local provider. Imported
JSON is an untrusted claim even if its digests match. Neither path executes a
browser or grants submission permission. Missing evidence remains UNKNOWN.
"""
from __future__ import annotations

import os
import stat
import hashlib
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Protocol

import queue_io
from fit_policy import main_floor
from safe_io import MAX_JSON_BYTES, digest, loads, rows, fresh, utc_now

SCHEMA = 'keel.muse-observation.v1'
QUEUES = ('standard', 'strategic', 'needs_input', 'rejected')
INPUTS = tuple('data/queues/' + name + '-queue.json' for name in QUEUES) + (
    'data/application-ledger.json', 'data/answer_bank.json', 'data/policy.json',
    'data/applicant_profile.json', 'data/employer-blocklist.md')
CODE_FILES = ('fit_policy.py', 'queue_intake.py', 'ready_gate.py', 'queue_io.py',
              'pipeline_service.py', 'muse_bridge.py', 'supply_recovery.py')
MAX_HOST_ROWS = 50000
HOST_FRESH_SECONDS = 300


class HostProvider(Protocol):
    def observe(self, request: dict) -> dict:
        """Exact bounded lookup; authenticate the host and validate receipts here.

        No fuzzy identity matching, browser actions, answer inference, or writes.
        A returned approval is a validated current host receipt, not a packet
        flag. The source library cannot establish those properties itself.
        """


def secure_bytes(root, relative, *, limit=MAX_JSON_BYTES):
    """Bounded regular-file read, refusing symlinks/FIFOs in every component."""
    relative = PurePosixPath(relative)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('invalid observation path')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(leaf, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('observation input must be a regular file')
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('observation input exceeds size limit')
        return data
    finally:
        os.close(fd)


def bound_workspace(workspace):
    root = Path(workspace).absolute()
    if not root.is_dir() or root.resolve(strict=True) != root:
        raise ValueError('canonical workspace must be a real directory')
    expected = root / 'hidden_files/queue.lock'
    if Path(queue_io.get_lock_path()).absolute() != expected:
        raise ValueError('runtime queue lock belongs to a different workspace')
    # queue_lock opens its diagnostic inode; refuse an unsafe path first.
    folder = expected.parent
    if folder.exists() and (folder.is_symlink() or folder.resolve() != folder):
        raise ValueError('unsafe queue lock directory')
    if expected.is_symlink() or expected.exists() and not expected.is_file():
        raise ValueError('unsafe queue lock file')
    return root


def code_manifest():
    root = Path(__file__).resolve().parent
    hashes = {name: hashlib.sha256(secure_bytes(root, name)).hexdigest() for name in CODE_FILES}
    return {'files': hashes, 'sha256': digest(hashes)}


def read_state(workspace):
    """One coherent complete snapshot; no recovery, bank, queue or event writes."""
    root = bound_workspace(workspace)
    with queue_io.queue_lock(owner='muse:census', recover=False):
        raw = {name: secure_bytes(root, name) for name in INPUTS}
        values = {name: loads(body) for name, body in raw.items() if name.endswith('.json')}
        documents = {name: values['data/queues/' + name + '-queue.json'] for name in QUEUES}
        entries = [(name, entry) for name, document in documents.items() for entry in rows(document)]
        if len(entries) > MAX_HOST_ROWS:
            raise ValueError('inventory exceeds bounded host contract')
        ledger = rows(values['data/application-ledger.json'])
        if any(not isinstance(entry.get('role_id'), str) or not entry['role_id'].strip()
               for _, entry in entries):
            raise ValueError('inventory contains invalid role identity')
        for name in ('data/answer_bank.json', 'data/policy.json', 'data/applicant_profile.json'):
            if not isinstance(values[name], dict):
                raise ValueError('workspace configuration must be an object')
        code = code_manifest()
        manifest = {'workspace_sha256': digest(str(root)),
                    'input_hashes': {name: hashlib.sha256(body).hexdigest() for name, body in raw.items()},
                    'code_manifest_sha256': code['sha256'], 'main_fit_floor': main_floor()}
        manifest['sha256'] = digest(manifest)
        return {'root': root, 'entries': entries, 'documents': documents, 'ledger': ledger,
                'bank': values['data/answer_bank.json'], 'manifest': manifest, 'code': code}


def request_for(state, entry, *, now=None):
    from pipeline_service import _key
    return {'schema': SCHEMA, 'role_id': entry['role_id'], 'row_sha256': digest(entry),
            'posting_identity': list(_key(entry)) if _key(entry) is not None else None,
            'manifest_sha256': state['manifest']['sha256'],
            'code_manifest_sha256': state['code']['sha256'],
            'observed_at': (now or utc_now()).isoformat()}


def validate_observation(record, request, *, now=None):
    """Validate shape/binding/freshness; this does not authenticate the author."""
    if not isinstance(record, dict):
        raise ValueError('host observation must be an object')
    for key in ('schema', 'role_id', 'row_sha256', 'posting_identity',
                'manifest_sha256', 'code_manifest_sha256'):
        if record.get(key) != request[key]:
            raise ValueError('host observation target changed')
    if not fresh(record.get('observed_at'), HOST_FRESH_SECONDS, now=now):
        raise ValueError('host observation is stale or future dated')
    allowed = {'task_state': {'CLEAR', 'ACTIVE', 'UNKNOWN'},
               'attempt_state': {'CLEAR', 'ACTIVE', 'TERMINAL', 'UNKNOWN'},
               'form_state': {'VERIFIED', 'HUMAN_REQUIRED', 'UNKNOWN'},
               'materials_state': {'READY', 'AMBIGUOUS', 'UNKNOWN'},
               'approval_state': {'APPROVED', 'REFUSED', 'UNKNOWN'}}
    for key, values in allowed.items():
        if not isinstance(record.get(key), str) or record[key] not in values:
            raise ValueError('host observation has an unknown contract state')
    refs = record.get('evidence_refs')
    if (not isinstance(refs, dict) or set(refs) != set(allowed)
            or any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in refs.values())):
        raise ValueError('exact host evidence references required')
    if record['approval_state'] == 'APPROVED':
        from safe_io import aware_time
        if aware_time(record.get('approval_expires_at')) <= (now or utc_now()):
            raise ValueError('host approval receipt expired')
    return record


def observe(provider, request, *, now=None):
    if provider is None:
        return {'scope': 'UNOBSERVED', 'record': None, 'reason': 'host_provider_unavailable'}
    try:
        record = validate_observation(provider.observe(dict(request)), request, now=now)
        return {'scope': 'AUTHORITATIVE_PROVIDER', 'record': record, 'reason': None}
    except Exception:
        # Provider errors must not become posting death or terminal ownership.
        return {'scope': 'UNOBSERVED', 'record': None, 'reason': 'host_observation_unconfirmed'}


def imported_claims(workspace, path, state, *, now=None):
    root = state['root']
    requested = Path(path)
    if not requested.is_absolute():
        requested = root / requested
    try:
        relative = requested.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError('host snapshot must be inside the canonical workspace') from exc
    payload = loads(secure_bytes(root, relative))
    if (not isinstance(payload, dict) or payload.get('schema') != SCHEMA
            or payload.get('manifest_sha256') != state['manifest']['sha256']
            or not fresh(payload.get('observed_at'), HOST_FRESH_SECONDS, now=now)):
        raise ValueError('host snapshot contract or binding invalid')
    records = payload.get('observations')
    if not isinstance(records, list) or len(records) > MAX_HOST_ROWS:
        raise ValueError('host snapshot rows invalid')
    by_id = {entry['role_id']: entry for _, entry in state['entries']}
    counts = Counter(entry['role_id'] for _, entry in state['entries'])
    seen = set()
    for record in records:
        if not isinstance(record, dict) or record.get('role_id') not in by_id:
            raise ValueError('host snapshot has an unknown role')
        rid = record['role_id']
        if counts[rid] != 1:
            raise ValueError('host snapshot role has ambiguous queue ownership')
        if rid in seen:
            raise ValueError('host snapshot has duplicate observations')
        seen.add(rid)
        validate_observation(record, request_for(state, by_id[rid], now=now), now=now)
    return {'scope': 'IMPORTED_UNAUTHENTICATED', 'observations': len(records),
            'authenticated': False, 'execution_authorized': False}


def census(workspace, *, host_snapshot=None, now=None):
    state = read_state(workspace)
    homes = Counter(entry['role_id'] for _, entry in state['entries'])
    claims = imported_claims(workspace, host_snapshot, state, now=now) if host_snapshot else None
    return {'schema': SCHEMA, 'mode': 'READ_ONLY',
            'observed_at': (now or utc_now()).isoformat(), 'manifest': state['manifest'],
            'code_manifest': state['code'], 'queue_rows': len(state['entries']),
            'distinct_roles': len(homes), 'duplicate_homes': sum(n != 1 for n in homes.values()),
            'host_snapshot': claims, 'launchable_ready': None,
            'host_scope': 'Runtime adapter unavailable; imported snapshots cannot grant authority.',
            'execution_authorized': False, 'submission_authorized': False, 'network_reads': 0}
