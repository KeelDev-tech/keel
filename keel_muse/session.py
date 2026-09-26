"""Durable, preparation-only JSON handoff to agent-visible native browser tools.

This module never invokes a browser. A trusted host maps each issued request to
an actually available native tool, whose own permission gate remains in control.
JSON declarations and observations do not authenticate Sentinel or a human.
The private host key detects unkeyed edits, not rollback by its owner.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time

from keel_agent.state import _create_private
from keel_loki.actions import ActionGateway
from keel_loki.forms import validate_contract
from tools.bench_inventory import directory_fd, read_file
from .browser import MODES, _permission, _readback, _request, _snapshot
from .capabilities import validate_manifest
from .common import (MuseError, canonical, clone, decode_json, digest, new_home,
                     require_dict, require_hash, require_id, require_int)

NATIVE_GATE = 'NATIVE_TOOL_ENFORCES_PERMISSION'
DB_NAME = 'native-session.sqlite'
KEY_NAME = 'native-session.key'
TERMINAL = {'BLOCKED', 'UNKNOWN', 'SIMULATED', 'PREPARED_REPORTED', 'PARTIAL'}
RECEIPT_FIELDS = {'schema', 'request_id', 'request_sha256', 'status',
                  'snapshot_id', 'snapshot_revision', 'target_ref', 'operation'}


class SessionError(MuseError):
    pass


def _identity(info):
    return info.st_dev, info.st_ino


def _private(info, *, directory=False):
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or info.st_uid != os.getuid() or not directory and info.st_nlink != 1):
        raise SessionError('private_owned_session_storage_required')


def _validate_attachments(attachments, actions):
    attachments = clone({} if attachments is None else attachments)
    expected = {a['field_id'] for a in actions if a['operation'] == 'attach_file'}
    if type(attachments) is not dict or set(attachments) != expected:
        raise SessionError('exact_approved_attachment_payloads_required')
    for action in actions:
        if action['operation'] != 'attach_file':
            continue
        encoded = attachments[action['field_id']]
        if type(encoded) is not str or len(encoded) > 7 * 1024 * 1024:
            raise SessionError('bounded_canonical_attachment_encoding_required')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise SessionError('invalid_attachment_encoding') from exc
        if (base64.b64encode(raw).decode() != encoded or len(raw) != action['value']['size']
                or hashlib.sha256(raw).hexdigest() != action['value']['sha256']):
            raise SessionError('attachment_payload_does_not_match_human_approved_hash')
    return attachments


class NativeSession:
    """One host-owned SQLite session, opened without restart or replay effects.

    Host-supplied source hashes, clocks, canonical policy snapshots and approvals
    are trusted inputs, never model authorization. `next_request(dispatch=True)`
    consumes a durable intent; its response can be acted on once. A process crash
    after that commit is uncertain even if its output never reached the agent.
    An operator must explicitly recover an abandoned controller. Ordinary CLI
    process exit/open does not imply abandonment. There is no submission verb.
    """

    def __init__(self, home, workspace_id, *, clock=None):
        require_id(workspace_id)
        self.home = Path(home).absolute()
        self.workspace_id = workspace_id
        self.clock = clock or (lambda: int(time.time()))
        if not callable(self.clock):
            raise SessionError('trusted_clock_callback_required')
        self._last_clock = None
        parent = directory_fd(self.home)
        try:
            info = os.fstat(parent)
            _private(info, directory=True)
            self._home_identity = _identity(info)
        finally:
            os.close(parent)
        # Opening/status has no epoch increment, schema provisioning or writes.
        with self._connection(readonly=True) as (db, key):
            self._load(db, key)

    @classmethod
    def create(cls, home, *, workspace_id, source_sha256, manifest, contract,
               values, host_approvals, host_snapshot, now, attachments=None,
               transport_mode='HOST_NATIVE_REPORTED', native_gate=None,
               lease_seconds=30, expires_at=None, clock=None):
        require_id(workspace_id)
        require_hash(source_sha256)
        require_int(now)
        clock = clock or (lambda: int(time.time()))
        checked = clock()
        require_int(checked)
        now = max(now, checked)
        require_int(lease_seconds, 1, 300)
        manifest = validate_manifest(manifest)
        contract = validate_contract(contract)
        values, host_approvals, host_snapshot = map(clone, (values, host_approvals, host_snapshot))
        if transport_mode not in MODES:
            raise SessionError('known_transport_mode_required')
        if (manifest['mapping_status'] != 'HOST_MAPPING_CONFIGURED'
                or manifest['snapshot_action_binding'] is not True):
            raise SessionError('HOST_MAPPING_REQUIRED')
        if transport_mode == 'HOST_NATIVE_REPORTED' and native_gate != NATIVE_GATE:
            raise SessionError('HOST_MAPPING_REQUIRED_native_permission_gate')
        if transport_mode == 'INJECTED' and (native_gate is not None or host_snapshot.get('synthetic') is not True):
            raise SessionError('injected_session_requires_synthetic_host_and_fixture_permission')
        grant = ActionGateway(contract, host_snapshot, host_approvals).issue(values, now=now, ttl=300)
        if any(a['operation'] not in manifest['operations'] for a in grant['actions']):
            raise SessionError('HOST_MAPPING_REQUIRED_unsupported_operation')
        attachments = _validate_attachments(attachments, grant['actions'])
        expires_at = min(now + 3600, grant['plan']['valid_until']) if expires_at is None else expires_at
        require_int(expires_at, now + 1, min(now + 3600, grant['plan']['valid_until']))
        config = {'workspace_id': workspace_id, 'source_sha256': source_sha256,
                  'manifest': manifest, 'contract': contract, 'values': values,
                  'host_approvals': host_approvals, 'attachments': attachments,
                  'transport_mode': transport_mode, 'native_gate': native_gate,
                  'lease_seconds': lease_seconds, 'expires_at': expires_at,
                  'plan': grant['plan'], 'actions': grant['actions']}
        state = {'schema': 'keel.muse.native-session-state.v1', 'config': config,
                 'config_sha256': digest(config), 'status': 'READY', 'reason': None,
                 'cursor': 0, 'snapshot': None, 'snapshot_revision': 0, 'seen_snapshots': [],
                 'pending': None, 'receipts': {}, 'readback': None, 'issued_requests': 0,
                 'issued_bindings': {}, 'rate_limit_observations': {},
                 'action_attempts': 0, 'rate_limited': False, 'sequence': 0,
                 'last_now': now, 'last_host_issued_at': host_snapshot['issued_at']}
        # Keep a bounded margin for requests, snapshots and audit receipts.
        if len(canonical(state)) > 3 * 1024 * 1024:
            raise SessionError('session_configuration_too_large')
        home = new_home(home)
        parent = directory_fd(home)
        try:
            anchored = Path('/proc/self/fd') / str(parent)
            key = secrets.token_bytes(32)
            _create_private(anchored / KEY_NAME, key)
            _create_private(anchored / DB_NAME, b'')
            db = sqlite3.connect(str(anchored / DB_NAME), isolation_level=None)
            try:
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('CREATE TABLE session (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, mac TEXT NOT NULL)')
                raw = canonical(state)
                mac = hmac.new(key, raw, hashlib.sha256).hexdigest()
                db.execute('INSERT INTO session VALUES (1, ?, ?)', (raw.decode(), mac))
            finally:
                db.close()
            os.fsync(parent)
        finally:
            os.close(parent)
        return cls(home, workspace_id, clock=clock)

    @contextmanager
    def _connection(self, *, readonly=False):
        parent = directory_fd(self.home)
        db = None
        file_fd = None
        try:
            info = os.fstat(parent)
            _private(info, directory=True)
            if _identity(info) != self._home_identity:
                raise SessionError('session_directory_replaced')
            for name in (DB_NAME, KEY_NAME, DB_NAME + '-journal', DB_NAME + '-wal', DB_NAME + '-shm'):
                try:
                    _private(os.stat(name, dir_fd=parent, follow_symlinks=False))
                except FileNotFoundError:
                    if name in (DB_NAME, KEY_NAME):
                        raise SessionError('session_state_or_key_missing')
            key = read_file(self.home, KEY_NAME)
            if len(key) != 32:
                raise SessionError('invalid_session_key')
            file_fd = os.open(DB_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            before = os.fstat(file_fd)
            _private(before)
            anchored = Path('/proc/self/fd') / str(parent) / DB_NAME
            db = sqlite3.connect(anchored.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True,
                                 timeout=15, isolation_level=None)
            if _identity(os.stat(DB_NAME, dir_fd=parent, follow_symlinks=False)) != _identity(before):
                raise SessionError('session_database_replaced')
            db.execute('PRAGMA trusted_schema=OFF')
            if not readonly:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
            yield db, key
            if (_identity(os.stat(DB_NAME, dir_fd=parent, follow_symlinks=False)) != _identity(before)
                    or _identity(self.home.stat()) != self._home_identity
                    or read_file(self.home, KEY_NAME) != key):
                raise SessionError('session_storage_changed_during_operation')
            if not readonly:
                db.execute('COMMIT')
        except BaseException:
            if db is not None and db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            if db is not None:
                db.close()
            if file_fd is not None:
                os.close(file_fd)
            os.close(parent)

    def _load(self, db, key):
        rows = db.execute('SELECT singleton, payload, mac FROM session').fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise SessionError('invalid_session_inventory')
        raw, mac = rows[0][1:]
        if type(raw) is not str or type(mac) is not str or not hmac.compare_digest(
                hmac.new(key, raw.encode(), hashlib.sha256).hexdigest(), mac):
            raise SessionError('session_integrity_failure')
        state = decode_json(raw)
        if (state.get('schema') != 'keel.muse.native-session-state.v1'
                or state['config']['workspace_id'] != self.workspace_id
                or digest(state['config']) != state['config_sha256']):
            raise SessionError('session_configuration_or_workspace_changed')
        return state

    def _save(self, db, key, state):
        state['sequence'] += 1
        raw = canonical(state)
        mac = hmac.new(key, raw, hashlib.sha256).hexdigest()
        db.execute('UPDATE session SET payload=?, mac=? WHERE singleton=1', (raw.decode(), mac))

    def _report(self, state, *, request=False, idempotent=False):
        config, pending = state['config'], state['pending']
        report = {'schema': 'keel.muse.native-session.v1', 'workspace_id': self.workspace_id,
                  'status': state['status'], 'reason': state['reason'], 'sequence': state['sequence'],
                  'transport_mode': config['transport_mode'], 'synthetic': config['plan']['synthetic'] or config['transport_mode'] == 'INJECTED',
                  'source_sha256': config['source_sha256'], 'config_sha256': state['config_sha256'],
                  'plan_sha256': digest(config['plan']), 'contract_sha256': digest(config['contract']),
                  'action_count': len(config['actions']), 'action_attempts': state['action_attempts'],
                  'actions_reported_applied': state['cursor'], 'issued_requests': state['issued_requests'],
                  'pending_state': pending['state'] if pending else None,
                  'request_sha256': digest(pending['request']) if pending else None,
                  'lease_until': pending['lease_until'] if pending else None,
                  'request': clone(pending['request']) if request and pending else None,
                  'dispatch_issued_once': bool(request and pending and pending['state'] == 'ISSUED'),
                  'native_tool_name': config['manifest']['native_tool_names'][pending['request']['operation']] if request and pending else None,
                  'native_gate': config['native_gate'], 'readback': clone(state['readback']),
                  'idempotent_observation': idempotent, 'rate_limited': state['rate_limited'],
                  'automatic_retry': False, 'submission_authorized': False, 'execution_authorized': False,
                  'production_deployed': False, 'human_approval_authenticated': False,
                  'native_permission_authenticated': False, 'native_browser_independently_verified': False,
                  'account_muse_integration': 'NOT_VERIFIED',
                  'actual_native_browser': 'HOST_REPORTED_NOT_VERIFIED' if config['transport_mode'] == 'HOST_NATIVE_REPORTED' and state['receipts'] else 'NOT_RUN',
                  'global_host_hold_written': False, 'local_storage_integrity': 'HOST_KEY_MAC',
                  'rollback_independently_detected': False, 'real_model_calls': 0}
        report['halt_event'] = ({'schema': 'keel.muse.native-halt.v1', 'reason': state['reason'],
                                 'workspace_id': self.workspace_id, 'contract_sha256': digest(config['contract']),
                                 'source_sha256': config['source_sha256'], 'unknown_effect': state['status'] == 'UNKNOWN',
                                 'rate_limited': state['rate_limited'], 'action_attempts': state['action_attempts'],
                                 'automatic_retry': False, 'execution_authorized': False}
                                if state['status'] in ('BLOCKED', 'UNKNOWN') else None)
        if pending and pending['state'] == 'ISSUED' and not request and state['status'] not in TERMINAL:
            report['status'] = 'WAITING_OBSERVATION'
        return report

    def status(self):
        """Read-only snapshot; elapsed time is handled by next/observe/recover."""
        with self._connection(readonly=True) as (db, key):
            return self._report(self._load(db, key))

    @staticmethod
    def _stop(state, reason):
        pending = state['pending']
        uncertain = state['status'] == 'UNKNOWN' or pending and pending['state'] == 'ISSUED' and pending['request']['operation'] != 'accessibility_snapshot'
        state['status'] = 'UNKNOWN' if uncertain else 'BLOCKED'
        state['reason'] = reason
        state['rate_limited'] |= 'rate_limited' in reason or '429' in reason

    @staticmethod
    def _clock(state, now):
        require_int(now)
        if now < state['last_now']:
            raise SessionError('host_clock_regressed')
        state['last_now'] = now

    def _live_now(self, now):
        require_int(now)
        checked = self.clock()
        require_int(checked)
        if self._last_clock is not None and checked < self._last_clock:
            raise SessionError('trusted_live_clock_regressed')
        self._last_clock = checked
        return max(now, checked)

    @staticmethod
    def _receipt_matches(response, request_sha256, binding):
        require_dict(response, RECEIPT_FIELDS)
        if (response['schema'] != 'keel.muse.native-receipt.v1'
                or type(response['snapshot_revision']) is not type(binding['snapshot_revision'])
                or any(response[k] != binding[k] for k in binding)
                or response['request_sha256'] != request_sha256):
            raise SessionError('unverified_native_action_receipt')

    @staticmethod
    def _fresh(state, source_sha256, host_snapshot, now):
        config = state['config']
        require_hash(source_sha256)
        if source_sha256 != config['source_sha256']:
            raise SessionError('running_source_changed')
        if now >= config['expires_at']:
            raise SessionError('session_expired')
        gateway = ActionGateway(config['contract'], host_snapshot, config['host_approvals'])
        if host_snapshot['rate_limited']:
            raise SessionError('host_rate_limited')
        grant = gateway.issue(config['values'], now=now, ttl=300)
        if (host_snapshot['issued_at'] < state['last_host_issued_at']
                or digest(grant['plan']) != digest(config['plan'])
                or digest(grant['actions']) != digest(config['actions'])):
            raise SessionError('host_policy_regressed_or_plan_changed')
        state['last_host_issued_at'] = host_snapshot['issued_at']
        return gateway, grant

    def next_request(self, *, source_sha256, host_snapshot, now, native_permission=None, dispatch=False):
        """Propose, then issue once. ISSUED requests are never returned again.

        HOST_NATIVE_REPORTED requires delegation to the mapped native tool gate;
        it rejects permission JSON. INJECTED uses fixture-only permission records.
        The caller must use the actual trusted clock and current canonical state.
        """
        result = self._next_request(source_sha256=source_sha256, host_snapshot=host_snapshot, now=now,
                                    native_permission=native_permission, dispatch=dispatch)
        if result['dispatch_issued_once']:
            # This runs AFTER SQLite COMMIT, including any fsync/lock delay.
            reason = None
            try:
                if self._live_now(now) >= result['lease_until']:
                    reason = 'dispatch_expired_during_commit'
            except (ValueError, TypeError):
                reason = 'trusted_clock_failed_after_intent_commit'
            if reason:
                with self._connection() as (db, key):
                    state = self._load(db, key)
                    self._stop(state, reason)
                    self._save(db, key, state)
                    result = self._report(state)
        return result

    def _next_request(self, *, source_sha256, host_snapshot, now, native_permission=None, dispatch=False):
        if type(dispatch) is not bool:
            raise SessionError('dispatch_must_be_boolean')
        with self._connection() as (db, key):
            state = self._load(db, key)
            if state['status'] in TERMINAL:
                return self._report(state)
            try:
                self._clock(state, now)
                now = self._live_now(now)
                self._clock(state, now)
                gateway, grant = self._fresh(state, source_sha256, host_snapshot, now)
                pending = state['pending']
                if pending and pending['state'] == 'ISSUED':
                    if now >= pending['lease_until']:
                        self._stop(state, 'issued_request_deadline_elapsed_explicit_recovery_required')
                    self._save(db, key, state)
                    return self._report(state)
                if pending is None:
                    if dispatch:
                        raise SessionError('proposal_required_before_dispatch')
                    if state['issued_requests'] >= 512:
                        raise SessionError('session_request_budget_exhausted')
                    snapshot = state['snapshot']
                    if snapshot is None:
                        request = _request('accessibility_snapshot', state['config']['contract'])
                    else:
                        _snapshot(snapshot, state['config']['contract'], now,
                                  previous=snapshot['snapshot_revision'] - 1, seen=set(), mode=state['config']['transport_mode'])
                        action = grant['actions'][state['cursor']]
                        target = next(r for r in snapshot['fields'] if r['descriptor']['id'] == action['field_id'])
                        if not target['active'] or not target['editable']:
                            raise SessionError('target_inactive_readonly_or_unverified')
                        request = _request(action['operation'], state['config']['contract'], snapshot=snapshot,
                                           action=action, attachment=state['config']['attachments'].get(action['field_id']))
                    pending = {'state': 'PROPOSED', 'request': request, 'lease_until': None}
                    state['pending'] = pending
                    state['status'] = 'PROPOSED'
                if dispatch:
                    config, request = state['config'], pending['request']
                    now = self._live_now(now)
                    self._clock(state, now)
                    gateway, grant = self._fresh(state, source_sha256, host_snapshot, now)
                    if config['transport_mode'] == 'HOST_NATIVE_REPORTED':
                        if native_permission is not None or config['native_gate'] != NATIVE_GATE:
                            raise SessionError('native_permission_JSON_is_not_Sentinel_authority')
                    else:
                        _permission(native_permission, request, now)
                    if request['operation'] != 'accessibility_snapshot':
                        snapshot = state['snapshot']
                        _snapshot(snapshot, config['contract'], now, previous=snapshot['snapshot_revision'] - 1,
                                  seen=set(), mode=config['transport_mode'])
                        # Fresh process-local grant only. Earlier cursor entries
                        # consume no tools and cannot be returned for execution.
                        for prior in grant['actions'][:state['cursor'] + 1]:
                            gateway.consume(grant['capability'], prior, now=now)
                        if digest(request['action']) != digest(grant['actions'][state['cursor']]):
                            raise SessionError('durable_cursor_action_changed')
                    deadline = min(now + config['lease_seconds'], config['expires_at'],
                                   host_snapshot['expires_at'], grant['capability']['expires_at'])
                    if config['transport_mode'] == 'INJECTED':
                        deadline = min(deadline, native_permission['expires_at'])
                    if request['operation'] != 'accessibility_snapshot':
                        deadline = min(deadline, state['snapshot']['expires_at'], state['snapshot']['observed_at'] + 10)
                    if deadline <= now:
                        raise SessionError('native_dispatch_freshness_deadline_elapsed')
                    pending['state'] = 'ISSUED'
                    pending['lease_until'] = deadline
                    if request['operation'] != 'accessibility_snapshot':
                        state['action_attempts'] += 1
                    state['issued_requests'] += 1
                    state['issued_bindings'][digest(request)] = {k: request[k] for k in
                        ('request_id', 'operation', 'snapshot_id', 'snapshot_revision', 'target_ref')}
                    state['status'] = 'ISSUED'
                self._save(db, key, state)
                # The context manager commits before this value reaches caller.
                return self._report(state, request=True)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                self._stop(state, str(exc) if isinstance(exc, SessionError) else 'host_or_protocol_gate_' + type(exc).__name__)
                self._save(db, key, state)
                return self._report(state)

    def observe(self, response, *, request_sha256, source_sha256, host_snapshot, now):
        """Consume an exact bound observation once; never infer an unobserved effect."""
        with self._connection() as (db, key):
            state = self._load(db, key)
            try:
                require_hash(request_sha256)
                response = clone(response)
                response_sha256 = digest(response)
                # A scoped 429 is a monotone stop signal, even after recovery,
                # source drift, expiry or an earlier conflicting observation.
                # It never authorizes or completes work. Keep only bounded
                # request identities in history, not historical upload bytes.
                binding = state['issued_bindings'].get(request_sha256)
                if type(response) is dict and response.get('status') == 'rate_limited' and binding is not None:
                    self._receipt_matches(response, request_sha256, binding)
                    if state['rate_limit_observations'].get(request_sha256) == response_sha256:
                        return self._report(state, idempotent=True)
                    self._stop(state, 'native_rate_limited')
                    if binding['operation'] != 'accessibility_snapshot':
                        state['status'] = 'UNKNOWN'
                    state['rate_limit_observations'][request_sha256] = response_sha256
                    self._save(db, key, state)
                    return self._report(state)
                prior = state['receipts'].get(request_sha256)
                if prior is not None:
                    if prior != response_sha256:
                        raise SessionError('conflicting_replayed_observation')
                    return self._report(state, idempotent=True)
                if state['status'] in TERMINAL:
                    return self._report(state)
                self._clock(state, now)
                now = self._live_now(now)
                self._clock(state, now)
                self._fresh(state, source_sha256, host_snapshot, now)
                pending = state['pending']
                if (pending is None or pending['state'] != 'ISSUED'
                        or digest(pending['request']) != request_sha256):
                    raise SessionError('observation_without_matching_issued_request')
                if now >= pending['lease_until']:
                    raise SessionError('issued_request_deadline_elapsed_explicit_recovery_required')
                request, config = pending['request'], state['config']
                if request['operation'] == 'accessibility_snapshot':
                    if type(response) is dict and response.get('schema') == 'keel.muse.native-receipt.v1':
                        self._receipt_matches(response, request_sha256, binding)
                        if response['status'] not in ('denied', 'ask', 'unknown', 'human_takeover'):
                            raise SessionError('snapshot_observation_required')
                        raise SessionError('native_' + response['status'])
                    snapshot = _snapshot(response, config['contract'], now, previous=state['snapshot_revision'],
                                         seen=set(state['seen_snapshots']), mode=config['transport_mode'])
                    state['snapshot_revision'] = snapshot['snapshot_revision']
                    state['seen_snapshots'].append(snapshot['snapshot_id'])
                    state['snapshot'] = snapshot
                    if state['cursor'] == len(config['actions']):
                        state['readback'] = _readback(snapshot, config['contract'], config['plan'],
                                                       config['transport_mode'], config['manifest']['attachment_readback'])
                        result = state['readback']['status']
                        state['status'] = ('BLOCKED' if result == 'MISMATCH' else 'PARTIAL' if result == 'PARTIAL'
                                           else 'SIMULATED' if config['transport_mode'] == 'INJECTED' else 'PREPARED_REPORTED')
                        if result == 'MISMATCH':
                            state['reason'] = 'final_native_readback_mismatch'
                    else:
                        state['status'] = 'READY'
                else:
                    self._receipt_matches(response, request_sha256, binding)
                    if response['status'] != 'applied':
                        if response['status'] not in ('denied', 'ask', 'unknown', 'rate_limited', 'human_takeover'):
                            raise SessionError('unknown_native_action_status')
                        raise SessionError('native_' + response['status'])
                    state['cursor'] += 1
                    state['snapshot'] = None
                    state['status'] = 'READY'
                state['receipts'][request_sha256] = response_sha256
                state['pending'] = None
                self._save(db, key, state)
                return self._report(state)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                self._stop(state, str(exc) if isinstance(exc, SessionError) else 'host_or_protocol_gate_' + type(exc).__name__)
                self._save(db, key, state)
                return self._report(state)

    def recover(self, *, now):
        """Explicitly abandon the old controller; never retry an issued effect.

        Proposals and read-only snapshot requests may be discarded. An issued
        effect becomes UNKNOWN even before its deadline, and remains terminal.
        Native global holds require separate trusted canonical host recording.
        """
        with self._connection() as (db, key):
            state = self._load(db, key)
            if state['status'] in TERMINAL:
                return self._report(state)
            self._clock(state, self._live_now(now))
            pending = state['pending']
            if pending and pending['state'] == 'ISSUED' and pending['request']['operation'] != 'accessibility_snapshot':
                self._stop(state, 'controller_abandoned_after_effect_intent')
            else:
                state['pending'] = None
                state['snapshot'] = None
                state['status'] = 'READY'
                state['reason'] = None
            self._save(db, key, state)
            return self._report(state)


def demo(home):
    """Execute the file-handshake logic against only the in-memory fixture."""
    from .browser import InjectedFixture
    from .capabilities import fixture_manifest
    from .common import atomic_json
    home = new_home(home)
    fixture = InjectedFixture()
    data, source = fixture.fixture, digest('synthetic-session-source')
    def create(name):
        return NativeSession.create(home / name, workspace_id='fixture-session', source_sha256=source,
                                    manifest=fixture_manifest(), contract=data['contract'], values=data['values'],
                                    host_approvals=data['approvals'], host_snapshot=fixture.host_snapshot(),
                                    attachments=data['attachments'], transport_mode='INJECTED', now=data['now'],
                                    clock=lambda: data['now'])
    session = create('complete')
    report = session.status()
    duplicate = False
    for _ in range(256):
        if report['status'] in TERMINAL:
            break
        args = {'source_sha256': source, 'host_snapshot': fixture.host_snapshot(), 'now': data['now']}
        proposal = session.next_request(**args)
        issued = session.next_request(**args, native_permission=fixture.permission(proposal['request']), dispatch=True)
        response = fixture(issued['request'])
        report = session.observe(response, request_sha256=issued['request_sha256'], **args)
        repeated = session.observe(response, request_sha256=issued['request_sha256'], **args)
        duplicate = repeated['idempotent_observation']
        # New object models ordinary independent CLI invocations, without recovery.
        session = NativeSession(home / 'complete', 'fixture-session', clock=lambda: data['now'])
    abandoned = create('abandoned')
    args = {'source_sha256': source, 'host_snapshot': fixture.host_snapshot(), 'now': data['now']}
    proposed = abandoned.next_request(**args)
    issued = abandoned.next_request(**args, dispatch=True, native_permission=fixture.permission(proposed['request']))
    abandoned.observe(fixture(issued['request']), request_sha256=issued['request_sha256'], **args)
    proposed = abandoned.next_request(**args)
    abandoned.next_request(**args, dispatch=True, native_permission=fixture.permission(proposed['request']))
    recovered = NativeSession(home / 'abandoned', 'fixture-session', clock=lambda: data['now']).recover(now=data['now'])
    result = {'schema': 'keel.muse.native-session-demo.v1', 'synthetic': True, 'preparation': report,
              'idempotent_observation': duplicate, 'recovered_effect_status': recovered['status'],
              'recovery_automatic_retry': recovered['automatic_retry'], 'real_browser_actions': 0,
              'real_model_calls': 0, 'external_network_calls': 0, 'execution_authorized': False,
              'account_muse_integration': 'NOT_VERIFIED', 'actual_native_browser': 'NOT_RUN'}
    atomic_json(home / 'native-session-demo.json', result)
    return result
