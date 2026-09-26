"""Durable qualified NativeSession admission and actual canonical stop adapters.

The wrapper covers registered workers only. Neither JSON, an HMAC nor this
agent-editable process authenticates Muse, Sentinel, human decisions or an OS
security boundary. Native tools still enforce their own permissions at invocation.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import fcntl
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time

from keel_agent.state import _create_private
from keel_loki.recovery import RecoveryJournal
from keel_loki.actions import ActionGateway
from keel_muse.coordinator import Coordinator
from keel_muse.session import NativeSession, _private, _identity
from keel_muse.common import (MuseError, canonical, clone, decode_json, digest,
                              new_home, require_dict, require_hash, require_id, require_int)
from tools.bench_inventory import directory_fd, read_file

DB_NAME = 'operational-runtime.sqlite3'
KEY_NAME = 'operational-runtime.key'


class RuntimeError(MuseError):
    pass


class _Store:
    """Passively opened, private, descriptor-anchored singleton plus host-key MAC."""
    def __init__(self, home, workspace_id):
        self.home = Path(home).absolute()
        self.workspace_id = require_id(workspace_id) or workspace_id
        parent = directory_fd(self.home)
        try:
            _private(os.fstat(parent), directory=True)
            self._home_identity = _identity(os.fstat(parent))
        finally:
            os.close(parent)
        with self.connection(readonly=True) as (db, key):
            self.load(db, key)

    @classmethod
    def create(cls, home, workspace_id, state):
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
                db.execute('CREATE TABLE operational_runtime (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, mac TEXT NOT NULL)')
                raw = canonical(state)
                db.execute('INSERT INTO operational_runtime VALUES (1,?,?)',
                           (raw.decode(), hmac.new(key, raw, hashlib.sha256).hexdigest()))
            finally:
                db.close()
            os.fsync(parent)
        finally:
            os.close(parent)
        return cls(home, workspace_id)

    @contextmanager
    def connection(self, *, readonly=False):
        parent = directory_fd(self.home)
        db = fd = None
        try:
            _private(os.fstat(parent), directory=True)
            if _identity(os.fstat(parent)) != self._home_identity:
                raise RuntimeError('runtime_directory_replaced')
            for name in (DB_NAME, KEY_NAME, DB_NAME + '-journal', DB_NAME + '-wal', DB_NAME + '-shm'):
                try:
                    _private(os.stat(name, dir_fd=parent, follow_symlinks=False))
                except FileNotFoundError:
                    if name in (DB_NAME, KEY_NAME):
                        raise RuntimeError('runtime_state_or_key_missing')
            key = read_file(self.home, KEY_NAME)
            if len(key) != 32:
                raise RuntimeError('runtime_key_invalid')
            fd = os.open(DB_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            identity = _identity(os.fstat(fd))
            path = Path('/proc/self/fd') / str(parent) / DB_NAME
            db = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                                 uri=True, isolation_level=None, timeout=15)
            if _identity(os.stat(DB_NAME, dir_fd=parent, follow_symlinks=False)) != identity:
                raise RuntimeError('runtime_database_replaced')
            db.execute('PRAGMA trusted_schema=OFF')
            if not readonly:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
            yield db, key
            if (_identity(os.stat(DB_NAME, dir_fd=parent, follow_symlinks=False)) != identity
                    or _identity(self.home.stat()) != self._home_identity or read_file(self.home, KEY_NAME) != key):
                raise RuntimeError('runtime_storage_changed_during_operation')
            if not readonly:
                db.execute('COMMIT')
        except BaseException:
            if db is not None and db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            if db is not None:
                db.close()
            if fd is not None:
                os.close(fd)
            os.close(parent)

    def load(self, db, key):
        rows = db.execute('SELECT singleton,payload,mac FROM operational_runtime').fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise RuntimeError('runtime_inventory_invalid')
        raw, mac = rows[0][1:]
        if not hmac.compare_digest(hmac.new(key, raw.encode(), hashlib.sha256).hexdigest(), mac):
            raise RuntimeError('runtime_integrity_failure')
        state = decode_json(raw)
        if (state.get('schema') != 'keel.operational.runtime-state.v1'
                or state['config']['workspace_id'] != self.workspace_id
                or digest(state['config']) != state['config_sha256']):
            raise RuntimeError('runtime_configuration_invalid')
        return state

    def save(self, db, key, state):
        state['sequence'] += 1
        raw = canonical(state)
        if len(raw) > 3 * 1024 * 1024:
            raise RuntimeError('runtime_state_bound_exceeded')
        db.execute('UPDATE operational_runtime SET payload=?,mac=? WHERE singleton=1',
                   (raw.decode(), hmac.new(key, raw, hashlib.sha256).hexdigest()))

    def read(self):
        with self.connection(readonly=True) as (db, key):
            return self.load(db, key)


def inspect_home(home, workspace_id):
    """Passive authenticated logical checkpoint; contains no answers/upload bytes."""
    state = _Store(home, workspace_id).read()
    return {'schema': 'keel.operational.runtime-snapshot.v1', 'workspace_id': workspace_id,
            'state': state, 'checkpoint_sha256': digest(state), 'execution_authorized': False}


def _ack(event, record):
    return {'schema': 'keel.operational.canonical-ack.v1', 'workspace_id': event['workspace_id'],
            'event_id': event['event_id'], 'event_sha256': digest(event), 'status': 'APPLIED',
            'canonical_record_sha256': digest(record)}


def coordinator_sink(coordinator):
    """Trusted live-controller sink: actual ingest then read committed state."""
    if not isinstance(coordinator, Coordinator) or coordinator._readonly:
        raise RuntimeError('writable_coordinator_controller_required')
    def write(event):
        kind = 'RATE_429' if event['kind'] == 'RATE_429' else 'HOLD'
        reason = 'operational_unknown_' + event['attempt_id'] if kind == 'HOLD' else None
        document = {'schema': 'keel.muse.event.v1', 'event_id': event['event_id'],
                    'workspace_id': event['workspace_id'], 'scope_id': event['role_id'],
                    'kind': kind, 'payload': {} if kind == 'RATE_429' else {'reason': reason}}
        receipt = coordinator.ingest(document)
        snapshot = coordinator.snapshot()
        state = snapshot['state']
        applied = (state['rate_limited'] is True if kind == 'RATE_429'
                   else reason in state['holds'].get(event['role_id'], []))
        if not applied:
            raise RuntimeError('canonical_coordinator_stop_not_observed')
        # Stable acknowledged identity remains invariant when other events arrive.
        return _ack(event, {'ingest_receipt': receipt, 'stop_observed': True})
    return write


def recovery_sink(journal, scope_jobs, *, clock=None):
    """Write the real existing journal. Missing scope bindings fail closed."""
    if not isinstance(journal, RecoveryJournal) or journal._readonly:
        raise RuntimeError('writable_recovery_controller_required')
    if type(scope_jobs) is not dict or not scope_jobs:
        raise RuntimeError('explicit_scope_to_existing_job_mapping_required')
    scope_jobs = clone(scope_jobs)
    for scope, jobs in scope_jobs.items():
        require_id(scope)
        if type(jobs) is not list or not jobs or len(set(jobs)) != len(jobs):
            raise RuntimeError('nonempty_unique_recovery_jobs_required')
        for job in jobs:
            require_id(job)
    clock = clock or (lambda: int(time.time()))
    def write(event):
        if event['workspace_id'] != journal.workspace_id:
            raise RuntimeError('canonical_workspace_mismatch')
        before = journal.snapshot()['state']
        if event['kind'] == 'RATE_429':
            if before['rate_limited'] is not True:
                journal.record_429(now=clock())
            jobs = []
        else:
            jobs = scope_jobs.get(event['role_id'])
            if not jobs:
                raise RuntimeError('canonical_recovery_scope_unmapped')
            for job in jobs:
                if job not in before['jobs']:
                    raise RuntimeError('canonical_recovery_job_missing')
                if before['jobs'][job]['held'] is not True:
                    journal.set_hold(job, now=clock())
        snapshot = journal.snapshot()
        applied = (snapshot['state']['rate_limited'] is True if event['kind'] == 'RATE_429'
                   else all(snapshot['state']['jobs'][job]['held'] is True for job in jobs))
        if not applied:
            raise RuntimeError('canonical_recovery_stop_not_observed')
        return _ack(event, {'event_sha256': digest(event), 'jobs': jobs, 'stop_observed': True})
    return write


def combined_sink(*sinks):
    """All configured trusted adapters must actually acknowledge exact stop."""
    if not sinks or not all(callable(sink) for sink in sinks):
        raise RuntimeError('trusted_canonical_callbacks_required')
    def write(event):
        records = []
        for sink in sinks:
            ack = clone(sink(clone(event)))
            require_dict(ack, {'schema', 'workspace_id', 'event_id', 'event_sha256', 'status', 'canonical_record_sha256'})
            if (ack['schema'] != 'keel.operational.canonical-ack.v1' or ack['workspace_id'] != event['workspace_id']
                    or ack['event_id'] != event['event_id'] or ack['event_sha256'] != digest(event)
                    or ack['status'] != 'APPLIED'):
                raise RuntimeError('canonical_sink_ack_mismatch')
            require_hash(ack['canonical_record_sha256'])
            records.append(ack['canonical_record_sha256'])
        return _ack(event, records)
    return write


class OperationalSession:
    """Successive-process wrapper around an immutable 0.13 NativeSession.

    Each shared admission is persisted before native dispatch. If a process is
    lost in either commit gap, the durable association forbids another issuance.
    Only a bound observation closes the intent; explicit recovery tightens to
    UNKNOWN. Opening the object does not recover or provision any state.
    """
    def __init__(self, home, workspace_id, *, native_home, ledger,
                 qualification_provider, source_provider, host_snapshot_provider,
                 control_gate_provider, clock=None):
        self.clock = clock or (lambda: int(time.time()))
        for callback in (qualification_provider, source_provider, host_snapshot_provider,
                         control_gate_provider, self.clock):
            if not callable(callback):
                raise RuntimeError('trusted_runtime_callbacks_required')
        self.workspace_id = workspace_id
        from .control import StopLedger
        self.ledger = StopLedger(ledger.home, workspace_id, snapshot_provider=control_gate_provider,
                                 canonical_sink=ledger.sink, clock=self.clock)
        self.qualification_provider = qualification_provider
        self.source_provider = source_provider
        self.host_snapshot_provider = host_snapshot_provider
        self.control_gate_provider = control_gate_provider
        self._last_clock = None
        with ledger.writer():
            self.store = _Store(home, workspace_id)
            self.native = NativeSession(native_home, workspace_id, clock=self.clock)
            self._bind(self.store.read())

    @classmethod
    def create(cls, home, *, native_home, ledger, workspace_id, session_id, scope_id,
               qualification_provider, source_provider, host_snapshot_provider,
               control_gate_provider, contract, values, host_approvals, attachments=None,
               transport_mode='HOST_NATIVE_REPORTED', native_gate=None,
               lease_seconds=30, expires_at=None, clock=None):
        require_id(workspace_id); require_id(session_id); require_id(scope_id)
        clock = clock or (lambda: int(time.time()))
        for provider in (qualification_provider, source_provider, host_snapshot_provider,
                         control_gate_provider, clock):
            if not callable(provider):
                raise RuntimeError('trusted_runtime_callbacks_required')
        # State stores are independently inventoried and restorable, never nested.
        left, right = Path(home).absolute(), Path(native_home).absolute()
        if left == right or left in right.parents or right in left.parents:
            raise RuntimeError('separate_runtime_and_native_store_roots_required')
        with ledger.writer():
            source = source_provider(); require_hash(source)
            bundle = clone(qualification_provider())
            cls._bundle(bundle)
            now = clock(); require_int(now)
            cls._qualify(bundle, source, now, transport_mode, native_gate)
            host = clone(host_snapshot_provider())
            now = clock(); require_int(now)
            cls._qualify(bundle, source, now, transport_mode, native_gate)
            grant = ActionGateway(contract, host, host_approvals).issue(values, now=now, ttl=300)
            effective_expiry = min(bundle['plan']['expires_at'], grant['plan']['valid_until'],
                                   now + 3600, expires_at if expires_at is not None else now + 3600)
            native = NativeSession.create(native_home, workspace_id=workspace_id, source_sha256=source,
                        manifest=bundle['manifest'], contract=contract, values=values,
                        host_approvals=host_approvals, host_snapshot=host, attachments=attachments,
                        transport_mode=transport_mode, native_gate=native_gate, now=now,
                        lease_seconds=lease_seconds, expires_at=effective_expiry, clock=clock)
            config = {'workspace_id': workspace_id, 'session_id': session_id, 'scope_id': scope_id,
                      'source_sha256': source, 'native_config_sha256': native.status()['config_sha256'],
                      'account_id': contract['account_id'], 'resource_id': contract['form_id'],
                      'lease_seconds': lease_seconds,
                      'manifest_sha256': digest(bundle['manifest']),
                      'qualification_report_sha256': bundle['expected_report_sha256'],
                      'qualification_plan_sha256': bundle['expected_plan_sha256'],
                      'transport_mode': transport_mode, 'native_gate': native_gate}
            state = {'schema': 'keel.operational.runtime-state.v1', 'config': config,
                     'config_sha256': digest(config), 'sequence': 0, 'last_now': now,
                     'active': None, 'attempts': {}, 'halted_reason': None,
                     'last_native_status': native.status()['status']}
            _Store.create(home, workspace_id, state)
        return cls(home, workspace_id, native_home=native_home, ledger=ledger,
                   qualification_provider=qualification_provider, source_provider=source_provider,
                   host_snapshot_provider=host_snapshot_provider, control_gate_provider=control_gate_provider,
                   clock=clock)

    @staticmethod
    def _bundle(bundle):
        require_dict(bundle, {'report', 'expected_report_sha256', 'plan', 'expected_plan_sha256',
                              'manifest', 'tool_schemas', 'host_config'})
        require_hash(bundle['expected_report_sha256']); require_hash(bundle['expected_plan_sha256'])

    @staticmethod
    def _qualify(bundle, source, now, mode, gate):
        from .qualification import verify
        OperationalSession._bundle(bundle)
        config = bundle['host_config']
        if config['transport_mode'] != mode or config['native_gate'] != gate:
            raise RuntimeError('qualification_transport_does_not_match_session')
        checked = verify(**bundle, source_sha256=source, now=now,
                         required_operations=bundle['manifest']['operations'], allow_injected=mode == 'INJECTED')
        if not checked['admissible']:
            raise RuntimeError('current_host_qualification_required')
        return checked

    def _now(self, state=None):
        now = self.clock(); require_int(now)
        prior = self._last_clock
        if prior is not None and now < prior or state is not None and now < state['last_now']:
            raise RuntimeError('trusted_runtime_clock_regressed')
        self._last_clock = now
        return now

    def _bind(self, state):
        if (state['config']['workspace_id'] != self.workspace_id
                or self.native.status()['config_sha256'] != state['config']['native_config_sha256']
                or self.ledger.workspace_id != self.workspace_id):
            raise RuntimeError('runtime_native_or_control_binding_mismatch')

    def _fresh(self, state):
        source = self.source_provider(); require_hash(source)
        config = state['config']
        bundle = clone(self.qualification_provider()); self._bundle(bundle)
        if (source != config['source_sha256'] or digest(bundle['manifest']) != config['manifest_sha256']
                or bundle['expected_report_sha256'] != config['qualification_report_sha256']
                or bundle['expected_plan_sha256'] != config['qualification_plan_sha256']):
            raise RuntimeError('runtime_source_manifest_or_qualification_pin_changed')
        host = clone(self.host_snapshot_provider())
        # Timestamp after every potentially blocking provider and barrier/DB wait.
        now = self._now(state)
        if self.source_provider() != source:
            raise RuntimeError('runtime_source_changed_during_provider')
        self._qualify(bundle, source, now, config['transport_mode'], config['native_gate'])
        return source, host, now

    def _report(self, state, native=None, *, reason=None):
        native = self.native.status() if native is None else native
        result = {'schema': 'keel.operational.runtime.v1', 'workspace_id': self.workspace_id,
                  'session_id': state['config']['session_id'], 'scope_id': state['config']['scope_id'],
                  'status': native['status'], 'reason': reason or native['reason'],
                  'native': native, 'active_attempt_id': state['active'],
                  'runtime_checkpoint_sha256': digest(state), 'sequence': state['sequence'],
                  'execution_authorized': False, 'submission_authorized': False,
                  'production_deployed': False, 'native_permission_authenticated': False,
                  'human_approval_authenticated': False, 'automatic_retry': False,
                  'account_muse_integration': 'NOT_VERIFIED',
                  'control_scope': 'REGISTERED_WRAPPED_WORKERS_ONLY'}
        shared = self.ledger.snapshot()['state']
        resources = ('role:' + state['config']['scope_id'],
                     'resource:' + digest([state['config']['account_id'], state['config']['resource_id']]))
        result['shared_rate_limited'] = shared['rate_limited']
        result['shared_scope_held'] = any(key in shared['holds'] for key in resources)
        result['canonical_stop_deliveries_pending'] = sum(x['status'] != 'ACKNOWLEDGED' for x in shared['outbox'].values())
        result['native_status_is_dispatch_authority'] = False
        if reason or state.get('halted_reason'):
            result['status'] = 'UNKNOWN' if state['active'] else 'BLOCKED'
            result['reason'] = reason or state['halted_reason']
        return result

    def status(self):
        with self.ledger.writer():
            state = self.store.read(); self._bind(state)
            return self._report(state)

    @contextmanager
    def _operation(self):
        # A runtime store serializes its multi-store commits. The maintenance
        # barrier covers the whole operation, including gaps between commits.
        with self.ledger.writer():
            parent = directory_fd(self.store.home)
            fd = None
            try:
                fd = os.open(DB_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                _private(os.fstat(fd))
                fcntl.flock(fd, fcntl.LOCK_EX)
                state = self.store.read(); self._bind(state)
                self._now(state)
                yield state
            finally:
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                os.close(parent)

    def _persist(self, state):
        state['last_now'] = self._now(state)
        with self.store.connection() as (db, key):
            self.store.save(db, key, state)

    def _delivery(self):
        pending = [x for x in self.ledger.snapshot()['state']['outbox'].values()
                   if x['status'] != 'ACKNOWLEDGED']
        if pending and self.ledger.sink is not None:
            self.ledger.flush_outbox(limit=128)
        return sum(x['status'] != 'ACKNOWLEDGED' for x in self.ledger.snapshot()['state']['outbox'].values())

    def _binding(self, state, request):
        config = state['config']
        pin = digest(request)
        return {'schema': 'keel.operational.attempt.v1',
                'attempt_id': 'native-' + digest([config['session_id'], config['native_config_sha256'], pin]),
                'worker_id': config['session_id'], 'workspace_id': self.workspace_id,
                'account_id': config['account_id'], 'role_id': config['scope_id'],
                'resource_id': config['resource_id'], 'operation': request['operation'],
                'input_sha256': pin, 'source_sha256': config['source_sha256']}

    def _unknown(self, state, reason):
        aid = state['active']
        if aid is not None:
            row = state['attempts'][aid]
            shared = self.ledger.snapshot()['state']['attempts'].get(aid)
            if shared and shared['status'] in ('ISSUED', 'RECORDED', 'UNKNOWN'):
                binding = row['binding']
                self.ledger.record_stop({'schema': 'keel.operational.stop.v1',
                    'event_id': 'runtime-' + digest([aid, reason]), 'workspace_id': self.workspace_id,
                    'kind': 'UNKNOWN', 'attempt_id': aid, 'account_id': binding['account_id'],
                    'role_id': binding['role_id'], 'resource_id': binding['resource_id'],
                    'observation_sha256': digest({'reason': reason, 'attempt_id': aid})})
            row['stage'] = 'ABANDONED'
        state['halted_reason'] = reason
        self._persist(state)
        self._delivery()

    def next_request(self, *, dispatch=False, native_permission=None):
        if type(dispatch) is not bool:
            raise RuntimeError('dispatch_must_be_boolean')
        with self._operation() as state:
            if state['halted_reason']:
                return self._report(state)
            if (self.store.home.parent / 'OFFLINE_RESTORE.json').exists():
                return self._report(state, reason='restored_runtime_requires_explicit_recovery_and_new_qualification')
            if state['active']:
                # The intent may have committed even if the previous process
                # never printed it. Never return a request from this branch.
                report = self._report(state)
                report['status'] = 'WAITING_OBSERVATION'
                return report
            if self._delivery():
                return self._report(state, reason='canonical_stop_delivery_pending')
            try:
                source, host, now = self._fresh(state)
            except (ValueError, KeyError, TypeError) as exc:
                return self._report(state, reason='qualification_or_host_gate_' + type(exc).__name__)
            proposal = self.native.next_request(source_sha256=source, host_snapshot=host, now=now)
            if not dispatch or proposal['status'] != 'PROPOSED':
                return self._report(state, proposal)
            request = proposal['request']
            binding = self._binding(state, request)
            aid = binding['attempt_id']
            if aid in state['attempts']:
                return self._report(state, reason='prior_attempt_is_not_reissued')
            if len(state['attempts']) >= 512:
                return self._report(state, reason='runtime_attempt_budget_exhausted')
            state['active'] = aid
            row = {'binding': binding, 'request_sha256': digest(request), 'token': None,
                   'stage': 'PREPARING', 'response_sha256': None}
            state['attempts'][aid] = row
            self._persist(state)  # Association exists even if admission output is lost.
            try:
                admitted = self.ledger.admit(binding, lease_seconds=state['config']['lease_seconds'])
                if admitted['status'] != 'RESERVED' or admitted['token'] is None:
                    row['stage'] = 'BLOCKED'
                    state['active'] = None
                    state['halted_reason'] = admitted.get('reason', 'shared_admission_not_new')
                    self._persist(state)
                    return self._report(state)
                row['token'] = admitted['token']; row['stage'] = 'RESERVED'
                self._persist(state)
                issued = self.ledger.dispatch(aid, row['token'])
                if issued['status'] != 'ISSUED':
                    state['halted_reason'] = issued.get('reason', 'shared_dispatch_not_issued')
                    state['active'] = None; row['stage'] = 'BLOCKED'
                    self._persist(state)
                    return self._report(state)
                row['stage'] = 'CONTROL_ISSUED'; self._persist(state)
                source, host, now = self._fresh(state)
                native = self.native.next_request(source_sha256=source, host_snapshot=host, now=now,
                                                   native_permission=native_permission, dispatch=True)
                if (not native['dispatch_issued_once'] or native['request_sha256'] != row['request_sha256']):
                    self._unknown(state, 'native_intent_not_returned_after_control_dispatch')
                    return self._report(state)
                row['stage'] = 'NATIVE_ISSUED'; self._persist(state)
                # Recheck after every blocking commit/provider. This cannot
                # revoke a request that has already left the process.
                source, host, now = self._fresh(state)
                # NativeSession applies current source/form/approval gates but
                # never emits the issued request a second time.
                checked = self.native.next_request(source_sha256=source, host_snapshot=host, now=now)
                if checked['status'] != 'WAITING_OBSERVATION':
                    self._unknown(state, 'native_gate_changed_before_delivery')
                    return self._report(state)
                pending = self._delivery()
                check = self.ledger.check_dispatch(aid, row['token'])
                source, host, now = self._fresh(state)
                checked = self.native.next_request(source_sha256=source, host_snapshot=host, now=now)
                now = self._now(state)
                if (checked['status'] != 'WAITING_OBSERVATION' or check['status'] != 'CURRENT' or now >= native['lease_until']
                        or now >= check.get('lease_until', now)
                        or now >= check.get('host_snapshot_expires_at', now)
                        or source != native['source_sha256'] or pending):
                    self.native.recover(now=now)
                    self._unknown(state, 'dispatch_withheld_after_final_gate')
                    return self._report(state)
                row['delivery_deadline'] = min(native['lease_until'], check['lease_until'], check['host_snapshot_expires_at'])
                self._persist(state)
                native['lease_until'] = row['delivery_deadline']
                result = self._report(state, native)
                if (self._now(state) >= row['delivery_deadline'] or result['shared_rate_limited']
                        or result['shared_scope_held'] or result['canonical_stop_deliveries_pending']):
                    self.native.recover(now=self._now(state))
                    self._unknown(state, 'dispatch_withheld_after_final_report')
                    return self._report(state)
                result['shared_attempt_id'] = aid
                result['host_must_recheck_at_actual_invocation'] = True
                return result
            except Exception as exc:
                # No exception silently drops a potentially issued association.
                self._unknown(state, 'runtime_dispatch_interrupted_' + type(exc).__name__)
                return self._report(state)

    def observe(self, response, *, request_sha256):
        require_hash(request_sha256)
        response = clone(response)
        with self._operation() as state:
            pairs = [(aid, row) for aid, row in state['attempts'].items()
                     if row['request_sha256'] == request_sha256]
            if len(pairs) != 1:
                raise RuntimeError('observation_not_bound_to_runtime_attempt')
            aid, row = pairs[0]
            is_rate = type(response) is dict and response.get('status') == 'rate_limited'
            if row['token'] is None:
                raise RuntimeError('shared_admission_token_missing_recovery_required')
            try:
                source, host, now = self._fresh(state)
            except (ValueError, KeyError, TypeError):
                # The immutable NativeSession validates exact issued receipt
                # bindings for a late 429 before evaluating source/host drift.
                if not is_rate:
                    self._unknown(state, 'observation_host_or_qualification_changed')
                    return self._report(state)
                source, host, now = state['config']['source_sha256'], {}, self._now(state)
            if not is_rate and row.get('delivery_deadline') is not None and now >= row['delivery_deadline']:
                self._unknown(state, 'observation_after_delivered_deadline')
                return self._report(state)
            native = self.native.observe(response, request_sha256=request_sha256,
                                          source_sha256=source, host_snapshot=host, now=now)
            native_rate = native['rate_limited'] is True
            if native_rate:
                result = self.ledger.finish(aid, row['token'], status='RATE_429', receipt_sha256=digest(response))
                row['stage'] = 'UNKNOWN'; state['halted_reason'] = 'native_rate_limited'
            elif native['status'] in ('BLOCKED', 'UNKNOWN'):
                binding = row['binding']
                result = self.ledger.record_stop({'schema': 'keel.operational.stop.v1',
                    'event_id': 'observation-' + digest([aid, digest(response)]), 'workspace_id': self.workspace_id,
                    'kind': 'UNKNOWN', 'attempt_id': aid, 'account_id': binding['account_id'],
                    'role_id': binding['role_id'], 'resource_id': binding['resource_id'],
                    'observation_sha256': digest(response)})
                row['stage'] = 'UNKNOWN'; state['halted_reason'] = 'native_observation_uncertain'
            else:
                result = self.ledger.finish(aid, row['token'], status='RECORDED', receipt_sha256=digest(response))
                row['stage'] = result['status']
                if result['status'] == 'RECORDED':
                    if state['active'] == aid:
                        state['active'] = None
                else:
                    state['halted_reason'] = 'shared_observation_uncertain'
            row['response_sha256'] = digest(response)
            state['last_native_status'] = native['status']
            self._persist(state)
            pending = self._delivery()
            report = self._report(state, native)
            report['canonical_stop_deliveries_pending'] = pending
            report['shared_observation_status'] = result['status']
            return report

    def recover(self):
        with self._operation() as state:
            self.ledger.recover()
            native = self.native.recover(now=self._now(state))
            if state['active']:
                self._unknown(state, 'explicit_runtime_recovery_after_unresolved_intent')
            else:
                state['halted_reason'] = 'explicit_runtime_recovery_requires_new_session'
                self._persist(state)
            report = self._report(state, native)
            report['canonical_stop_deliveries_pending'] = self._delivery()
            return report


def fixture_environment(home, *, now=1000, source_sha256=None):
    """Synthetic trusted callback fixture for tests; no production authority."""
    from .control import StopLedger
    from .qualification import fixture_inputs, freeze_plan, run
    from keel_loki.forms import bind_fixture
    from keel_muse.browser import InjectedFixture
    home = new_home(home)
    inputs = fixture_inputs()
    if now != inputs['now']:
        original = inputs['fixture']
        inputs['fixture'] = bind_fixture(original, now=now)
        inputs['fixture']['attachments'] = original['attachments']
        inputs['now'] = now
    if source_sha256 is not None:
        inputs['source_sha256'] = source_sha256
    plan = freeze_plan(**inputs)
    fixture = InjectedFixture(inputs['fixture'])
    def capture(request):
        observed = fixture(request)
        return {'raw': b'synthetic original tool response\n' + canonical(observed),
                'normalized': observed,
                'native_tool_name': inputs['manifest']['native_tool_names'][request['operation']]}
    report = run(home / 'qualification', plan, expected_plan_sha256=digest(plan), capture=capture,
                 clock=lambda: inputs['now'], **{k: inputs[k] for k in
                 ('manifest', 'tool_schemas', 'host_config', 'source_sha256', 'now')})
    bundle = {'report': report, 'expected_report_sha256': digest(report), 'plan': plan,
              'expected_plan_sha256': digest(plan), **{k: inputs[k] for k in
              ('manifest', 'tool_schemas', 'host_config')}}
    current = {'now': now, 'source': inputs['source_sha256'], 'bundle': bundle}
    def gate(binding):
        return {'schema': 'keel.operational.host-snapshot.v1', 'workspace_id': binding['workspace_id'],
                'attempt_sha256': digest(binding), 'source_sha256': current['source'],
                'consent': True, 'no_ai': False, 'approval_current': True, 'holds': [],
                'unknown_attempt': False, 'rate_limited': False,
                'issued_at': current['now'], 'expires_at': current['now'] + 60}
    ledger = StopLedger.create(home / 'control', 'fixture-workspace', snapshot_provider=gate,
                               clock=lambda: current['now'])
    def make(name, *, scope='fixture-role', shared=None, configured_fixture=None):
        fixture = configured_fixture or InjectedFixture(inputs['fixture'])
        data = fixture.fixture
        options = dict(native_home=home / (name + '-native'), ledger=shared or ledger,
                       workspace_id='fixture-workspace', session_id=name, scope_id=scope,
                       qualification_provider=lambda: current['bundle'], source_provider=lambda: current['source'],
                       host_snapshot_provider=fixture.host_snapshot, control_gate_provider=gate,
                       clock=lambda: current['now'])
        session = OperationalSession.create(home / (name + '-runtime'),
                      contract=data['contract'], values=data['values'], host_approvals=data['approvals'],
                      attachments=data['attachments'], transport_mode='INJECTED', **options)
        return session, fixture, options
    return {'home': home, 'inputs': inputs, 'current': current, 'bundle': bundle,
            'ledger': ledger, 'gate': gate, 'make': make}


def demo(home, *, source_sha256=None):
    env = fixture_environment(home, source_sha256=source_sha256)
    session, fixture, _ = env['make']('complete')
    report = session.status()
    steps = 0
    while report['status'] not in ('SIMULATED', 'BLOCKED', 'UNKNOWN', 'PARTIAL') and steps < 32:
        proposal = session.next_request()
        issued = session.next_request(dispatch=True, native_permission=fixture.permission(proposal['native']['request']))
        if not issued['native']['dispatch_issued_once']:
            report = issued
            break
        response = fixture(issued['native']['request'])
        report = session.observe(response, request_sha256=issued['native']['request_sha256'])
        steps += 1
    # Separate role+resource to verify the stop spans distinct workers.
    stopped, source_fixture, _ = env['make']('stopped', scope='role-stop')
    p = stopped.next_request()
    issued = stopped.next_request(dispatch=True, native_permission=source_fixture.permission(p['native']['request']))
    request = issued['native']['request']
    receipt = {'schema': 'keel.muse.native-receipt.v1', 'request_id': request['request_id'],
               'request_sha256': digest(request), 'status': 'rate_limited',
               **{k: request[k] for k in ('snapshot_id', 'snapshot_revision', 'target_ref', 'operation')}}
    stop = stopped.observe(receipt, request_sha256=digest(request))
    blocked, another, _ = env['make']('another', scope='role-other')
    forbidden = blocked.next_request()
    checks = {'all_fixture_operations_observed': report['status'] == 'SIMULATED' and steps == 23,
              'actual_form_readback': report['native']['readback']['status'] == 'EXACT_REPORTED_READBACK',
              'global_stop_persisted': env['ledger'].snapshot()['state']['rate_limited'] is True,
              'other_worker_stopped': forbidden['status'] == 'BLOCKED' and forbidden['native']['request'] is None,
              'missing_sink_is_visible': stop['canonical_stop_deliveries_pending'] > 0,
              'no_submission_authority': report['submission_authorized'] is False}
    return {'schema': 'keel.operational.runtime-demo.v1', 'status': 'PASS' if all(checks.values()) else 'FAIL',
            'checks': checks, 'synthetic': True, 'fixture_steps': steps,
            'real_native_browser_actions': 0, 'real_model_calls': 0, 'real_canonical_writes': 0,
            'execution_authorized': False, 'submission_authorized': False,
            'account_muse_integration': 'NOT_VERIFIED', 'actual_native_browser': 'NOT_RUN'}
