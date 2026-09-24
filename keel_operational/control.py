"""Shared stop ledger for explicitly mediated Keel writers.

This is a host-account trust boundary, not an OS sandbox or Muse permission
service. Install the provider/barrier in every participating writer. Stops do
not retroactively cancel an already issued external tool call. The canonical
sink is a trusted, idempotent host callback; JSON cannot acknowledge itself.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time

from keel_agent.state import _create_private
from keel_muse.common import canonical, clone, decode_json, digest, new_home
from keel_muse.coordinator import exact, ident, sha, stamp
from keel_muse.session import _identity, _private
from tools.bench_inventory import directory_fd, read_file

DB_NAME = 'control.sqlite3'
KEY_NAME = 'control.key'
LOCK_NAME = 'control.lock'
ATTEMPT_FIELDS = {'schema', 'attempt_id', 'worker_id', 'workspace_id', 'account_id',
                  'role_id', 'resource_id', 'operation', 'input_sha256', 'source_sha256'}
STOP_FIELDS = {'schema', 'event_id', 'workspace_id', 'kind', 'attempt_id',
               'account_id', 'role_id', 'resource_id', 'observation_sha256'}


class ControlError(ValueError):
    pass


def need(value, reason):
    if not value:
        raise ControlError(reason)


def attempt_document(value):
    value = clone(value)
    exact(value, ATTEMPT_FIELDS)
    need(value['schema'] == 'keel.operational.attempt.v1', 'attempt_schema_invalid')
    for name in ATTEMPT_FIELDS - {'schema', 'input_sha256', 'source_sha256'}:
        ident(value[name])
    sha(value['input_sha256']); sha(value['source_sha256'])
    return value


def stop_document(value):
    value = clone(value)
    exact(value, STOP_FIELDS)
    need(value['schema'] == 'keel.operational.stop.v1', 'stop_schema_invalid')
    need(value['kind'] in ('UNKNOWN', 'RATE_429'), 'stop_kind_invalid')
    for name in ('event_id', 'workspace_id', 'account_id', 'role_id', 'resource_id'):
        ident(value[name])
    if value['attempt_id'] is not None:
        ident(value['attempt_id'])
    need(value['kind'] != 'UNKNOWN' or value['attempt_id'] is not None, 'unknown_requires_exact_attempt')
    sha(value['observation_sha256'])
    return value


def host_snapshot(value, binding, now):
    value = clone(value)
    exact(value, {'schema', 'workspace_id', 'attempt_sha256', 'source_sha256',
                 'consent', 'no_ai', 'approval_current', 'holds', 'unknown_attempt',
                 'rate_limited', 'issued_at', 'expires_at'})
    need(value['schema'] == 'keel.operational.host-snapshot.v1', 'host_schema_invalid')
    need(value['workspace_id'] == binding['workspace_id']
         and value['attempt_sha256'] == digest(binding)
         and value['source_sha256'] == binding['source_sha256'], 'host_binding_mismatch')
    for key in ('consent', 'no_ai', 'approval_current', 'unknown_attempt', 'rate_limited'):
        need(type(value[key]) is bool, 'host_boolean_invalid')
    need(type(value['holds']) is list and len(value['holds']) <= 128, 'host_holds_invalid')
    for item in value['holds']:
        ident(item)
    issued, expiry = stamp(value['issued_at']), stamp(value['expires_at'])
    need(issued <= now < expiry and expiry - issued <= 90, 'host_snapshot_not_fresh')
    need(value['consent'] and value['approval_current'] and not value['no_ai']
         and not value['holds'] and not value['unknown_attempt'] and not value['rate_limited'], 'host_gate_blocked')
    return value


class StopLedger:
    """Multi-process passive-open ledger, with no clear or retry operation.

    ``admit`` reserves, ``dispatch`` consumes one local intent, ``finish`` records
    the observation. Neither method authenticates a human or grants Muse native
    permission. Fresh canonical gate snapshots are constructor-only callbacks.
    All IDs are workspace scoped; UNKNOWN locks a role across accounts and an
    account/resource pair. Both stops and admission are serializable locally.
    """
    def __init__(self, home, workspace_id, *, snapshot_provider=None,
                 canonical_sink=None, clock=time.time):
        self.home = Path(home).absolute()
        self.workspace_id = ident(workspace_id)
        need(callable(clock), 'trusted_clock_required')
        need(snapshot_provider is None or callable(snapshot_provider), 'trusted_snapshot_callback_required')
        need(canonical_sink is None or callable(canonical_sink), 'trusted_sink_callback_required')
        self.clock, self.provider, self.sink = clock, snapshot_provider, canonical_sink
        parent = directory_fd(self.home)
        try:
            _private(os.fstat(parent), directory=True)
            self._home_identity = _identity(os.fstat(parent))
            self._file_identities = {name: _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
                                     for name in (DB_NAME, KEY_NAME, LOCK_NAME)}
        finally:
            os.close(parent)
        self.snapshot()

    @classmethod
    def create(cls, home, workspace_id, *, snapshot_provider=None, canonical_sink=None, clock=time.time):
        ident(workspace_id)
        need(callable(clock), 'trusted_clock_required')
        now = stamp(clock())
        home = new_home(home)
        parent = directory_fd(home)
        try:
            anchored = Path('/proc/self/fd') / str(parent)
            key = secrets.token_bytes(32)
            for name, raw in ((KEY_NAME, key), (DB_NAME, b''), (LOCK_NAME, b'')):
                _create_private(anchored / name, raw)
            db = sqlite3.connect(str(anchored / DB_NAME), isolation_level=None)
            try:
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('CREATE TABLE control (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, mac TEXT NOT NULL)')
                state = {'schema': 'keel.operational.control-state.v1', 'workspace_id': workspace_id,
                         'sequence': 0, 'last_now': now, 'event_head_sha256': '0' * 64,
                         'rate_limited': False, 'attempts': {}, 'holds': {}, 'inbox': {}, 'outbox': {}}
                cls._save(db, key, state, now, 'initialize')
            finally:
                db.close()
            os.fsync(parent)
        finally:
            os.close(parent)
        return cls(home, workspace_id, snapshot_provider=snapshot_provider, canonical_sink=canonical_sink, clock=clock)

    def _parent(self):
        fd = directory_fd(self.home)
        try:
            _private(os.fstat(fd), directory=True)
            need(_identity(os.fstat(fd)) == self._home_identity, 'control_directory_replaced')
            for name in (DB_NAME, KEY_NAME, LOCK_NAME):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                _private(info)
                need(_identity(info) == self._file_identities[name], 'control_file_replaced')
            for name in (DB_NAME + '-journal', DB_NAME + '-wal', DB_NAME + '-shm'):
                try:
                    _private(os.stat(name, dir_fd=fd, follow_symlinks=False))
                except FileNotFoundError:
                    pass
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _barrier(self, exclusive):
        parent = self._parent()
        lock = None
        try:
            lock = os.open(Path('/proc/self/fd') / str(parent) / LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
            _private(os.fstat(lock))
            fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            need(_identity(os.fstat(lock)) == _identity(os.stat(LOCK_NAME, dir_fd=parent, follow_symlinks=False)),
                 'control_barrier_replaced')
            # Clock is intentionally refreshed by each transaction AFTER this
            # potentially blocking lock. Maintenance does not manufacture time.
            yield
            need(_identity(os.fstat(lock)) == _identity(os.stat(LOCK_NAME, dir_fd=parent, follow_symlinks=False)),
                 'control_barrier_replaced')
            need(_identity(self.home.stat()) == self._home_identity, 'control_directory_replaced')
        finally:
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.close(lock)
            os.close(parent)

    @contextmanager
    def writer(self):
        """Wrap all participating local store mutations, including old stores."""
        with self._barrier(False):
            yield self

    @contextmanager
    def maintenance(self):
        """Quiesce participating writers; do not mutate this ledger inside."""
        with self._barrier(True):
            yield self.snapshot()

    @contextmanager
    def _connection(self, readonly=False):
        parent = self._parent()
        db, file_fd = None, None
        try:
            key = read_file(self.home, KEY_NAME)
            need(len(key) == 32, 'invalid_control_key')
            file_fd = os.open(DB_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            before = _identity(os.fstat(file_fd))
            path = Path('/proc/self/fd') / str(parent) / DB_NAME
            db = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True,
                                 timeout=15, isolation_level=None)
            need(_identity(os.stat(DB_NAME, dir_fd=parent, follow_symlinks=False)) == before, 'control_database_replaced')
            db.execute('PRAGMA trusted_schema=OFF')
            if not readonly:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
            yield db, key
            need(_identity(os.stat(DB_NAME, dir_fd=parent, follow_symlinks=False)) == before, 'control_database_replaced')
            need(_identity(self.home.stat()) == self._home_identity and read_file(self.home, KEY_NAME) == key,
                 'control_storage_changed')
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
        rows = db.execute('SELECT singleton,payload,mac FROM control').fetchall()
        need(len(rows) == 1 and rows[0][0] == 1, 'control_inventory_invalid')
        raw, mac = rows[0][1:]
        need(type(raw) is str and type(mac) is str and hmac.compare_digest(
            hmac.new(key, raw.encode(), hashlib.sha256).hexdigest(), mac), 'control_integrity_failure')
        state = decode_json(raw)
        need(state.get('schema') == 'keel.operational.control-state.v1'
             and state['workspace_id'] == self.workspace_id, 'control_scope_mismatch')
        return state

    @staticmethod
    def _save(db, key, state, now, action):
        need(now >= state['last_now'], 'control_clock_rollback')
        state['sequence'] += 1
        state['last_now'] = now
        state['event_head_sha256'] = digest({'previous': state['event_head_sha256'],
            'sequence': state['sequence'], 'action': action, 'state_sha256': digest(state)})
        raw = canonical(state)
        db.execute('INSERT INTO control VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload,mac=excluded.mac',
                   (raw.decode(), hmac.new(key, raw, hashlib.sha256).hexdigest()))

    def _now(self, state):
        now = stamp(self.clock())
        need(now >= state['last_now'], 'control_clock_rollback')
        return now

    def _mutate(self, action, callback):
        with self.writer():
            with self._connection() as (db, key):
                state = self._load(db, key)
                now = self._now(state)
                result, changed = callback(state, now)
                if changed:
                    self._save(db, key, state, self._now(state), action)
                return clone(result)

    def snapshot(self):
        """Read-only; safe while holding maintenance's exclusive barrier."""
        with self._connection(True) as (db, key):
            state = self._load(db, key)
        payload = {'schema': 'keel.operational.control-snapshot.v1', 'state': state,
                   'event_head_sha256': state['event_head_sha256']}
        return {**payload, 'checkpoint_sha256': digest(payload), 'execution_authorized': False,
                'consistency_scope': 'participating_writers_only'}

    @staticmethod
    def _resources(binding):
        return ('role:' + binding['role_id'], 'resource:' + digest([binding['account_id'], binding['resource_id']]))

    @classmethod
    def _blocked(cls, state, binding, *, except_attempt=None):
        if state['rate_limited']:
            return 'global_rate_429'
        keys = cls._resources(binding)
        if any(key in state['holds'] for key in keys):
            return 'unknown_role_or_resource'
        for aid, row in state['attempts'].items():
            if aid != except_attempt and row['status'] in ('RESERVED', 'ISSUED'):
                if set(keys) & set(cls._resources(row['binding'])):
                    return 'role_or_resource_busy'
        return None

    def _fresh_host(self, binding, state):
        need(self.provider is not None, 'trusted_snapshot_provider_required')
        value = self.provider(clone(binding))
        now = self._now(state)  # provider may block; no admission on stale time.
        host_snapshot(value, binding, now)
        return value, now

    def admit(self, binding, *, lease_seconds=30):
        binding = attempt_document(binding)
        need(binding['workspace_id'] == self.workspace_id, 'workspace_mismatch')
        need(type(lease_seconds) is int and 1 <= lease_seconds <= 300, 'lease_seconds_invalid')
        def apply(state, now):
            prior = state['attempts'].get(binding['attempt_id'])
            if prior is not None:
                need(prior['binding'] == binding, 'attempt_id_binding_conflict')
                return {'status': prior['status'], 'duplicate': True, 'token': None,
                        'execution_authorized': False}, False
            reason = self._blocked(state, binding)
            if reason:
                return {'status': 'BLOCKED', 'reason': reason, 'token': None, 'execution_authorized': False}, False
            need(len(state['attempts']) < 4096, 'attempt_backpressure')
            snapshot, now = self._fresh_host(binding, state)
            token = secrets.token_hex(32)
            state['attempts'][binding['attempt_id']] = {'binding': binding,
                'status': 'RESERVED', 'token_sha256': digest(token), 'lease_seconds': lease_seconds,
                'lease_until': now + lease_seconds, 'admitted_at': now, 'issued_at': None,
                'snapshot_sha256': digest(snapshot), 'receipt_sha256': None, 'reason': None}
            return {'status': 'RESERVED', 'attempt_id': binding['attempt_id'], 'token': token,
                    'lease_until': now + lease_seconds, 'execution_authorized': False}, True
        return self._mutate('admit', apply)

    def dispatch(self, attempt_id, token):
        ident(attempt_id); sha(token)
        def apply(state, now):
            row = state['attempts'].get(attempt_id)
            need(row is not None and hmac.compare_digest(row['token_sha256'], digest(token)), 'attempt_or_token_invalid')
            if row['status'] != 'RESERVED':
                return {'status': 'WAIT', 'attempt_status': row['status'], 'execution_authorized': False}, False
            if now >= row['lease_until']:
                row.update(status='BLOCKED', reason='reservation_expired')
                return {'status': 'BLOCKED', 'reason': row['reason'], 'execution_authorized': False}, True
            reason = self._blocked(state, row['binding'], except_attempt=attempt_id)
            if reason:
                row.update(status='BLOCKED', reason=reason)
                return {'status': 'BLOCKED', 'reason': reason, 'execution_authorized': False}, True
            snapshot, now = self._fresh_host(row['binding'], state)
            if now >= row['lease_until']:
                row.update(status='BLOCKED', reason='reservation_expired_during_provider')
                return {'status': 'BLOCKED', 'reason': row['reason'], 'execution_authorized': False}, True
            row.update(status='ISSUED', issued_at=now, lease_until=now + row['lease_seconds'], snapshot_sha256=digest(snapshot))
            return {'status': 'ISSUED', 'attempt_id': attempt_id, 'lease_until': row['lease_until'],
                    'intent_sha256': digest(row), 'execution_authorized': False}, True
        return self._mutate('dispatch', apply)

    def check_dispatch(self, attempt_id, token):
        """Final fresh check after another store commits, before returning a tool request.

        Never emits a second intent or authorizes execution. A later external
        stop still cannot cancel an action already invoked by the host.
        """
        ident(attempt_id); sha(token)
        def apply(state, now):
            row = state['attempts'].get(attempt_id)
            need(row is not None and hmac.compare_digest(row['token_sha256'], digest(token)), 'attempt_or_token_invalid')
            if row['status'] != 'ISSUED':
                return {'status': 'BLOCKED', 'reason': 'attempt_not_issued', 'execution_authorized': False}, False
            if now >= row['lease_until']:
                self._unknown(state, row, 'issued_lease_expired_before_return', now)
                return {'status': 'BLOCKED', 'reason': row['reason'], 'execution_authorized': False}, True
            reason = self._blocked(state, row['binding'], except_attempt=attempt_id)
            if reason:
                self._unknown(state, row, reason, now)
                return {'status': 'BLOCKED', 'reason': reason, 'execution_authorized': False}, True
            snapshot, now = self._fresh_host(row['binding'], state)
            if now >= row['lease_until']:
                self._unknown(state, row, 'issued_lease_expired_during_final_provider', now)
                return {'status': 'BLOCKED', 'reason': row['reason'], 'execution_authorized': False}, True
            return {'status': 'CURRENT', 'attempt_id': attempt_id, 'lease_until': row['lease_until'],
                    'host_snapshot_expires_at': snapshot['expires_at'],
                    'execution_authorized': False}, False
        return self._mutate('check_dispatch', apply)

    @classmethod
    def _unknown(cls, state, row, reason, now):
        if row['status'] == 'UNKNOWN':
            return
        row.update(status='UNKNOWN', reason=reason)
        for resource in cls._resources(row['binding']):
            state['holds'].setdefault(resource, []).append(row['binding']['attempt_id'])
        b = row['binding']
        event = {'schema': 'keel.operational.stop.v1',
                 'event_id': 'unknown-' + digest({'attempt': b, 'kind': 'UNKNOWN'}),
                 'workspace_id': b['workspace_id'], 'kind': 'UNKNOWN', 'attempt_id': b['attempt_id'],
                 'account_id': b['account_id'], 'role_id': b['role_id'], 'resource_id': b['resource_id'],
                 'observation_sha256': digest({'reason': reason, 'attempt': b, 'occurred_at': now})}
        cls._queue(state, event, now)

    @staticmethod
    def _queue(state, event, now):
        eid = event['event_id']
        if eid not in state['outbox']:
            state['outbox'][eid] = {'event': clone(event), 'event_sha256': digest(event),
                'status': 'PENDING', 'deliveries': 0, 'delivery_token': None,
                'lease_until': None, 'created_at': now, 'ack': None, 'last_error': None}

    @classmethod
    def _stop(cls, state, event, now):
        prior = state['inbox'].get(event['event_id'])
        if prior:
            need(prior['event_sha256'] == digest(event), 'stop_event_id_conflict')
            return prior['receipt'], False
        row = state['attempts'].get(event['attempt_id'])
        if event['kind'] == 'UNKNOWN':
            need(row is not None, 'unknown_attempt_not_found')
            need(all(row['binding'][key] == event[key] for key in ('account_id', 'role_id', 'resource_id')),
                 'unknown_attempt_binding_mismatch')
            need(row['status'] in ('ISSUED', 'UNKNOWN', 'RECORDED'), 'unknown_requires_issued_attempt')
            cls._unknown(state, row, 'reported_unknown', now)
        else:
            state['rate_limited'] = True
            for candidate in state['attempts'].values():
                if candidate['status'] == 'ISSUED':
                    cls._unknown(state, candidate, 'global_rate_429_after_dispatch', now)
                elif candidate['status'] == 'RESERVED':
                    candidate.update(status='BLOCKED', reason='global_rate_429')
        cls._queue(state, event, now)
        receipt = {'status': 'STOP_RECORDED', 'event_id': event['event_id'],
                   'event_sha256': digest(event), 'canonical_acknowledged': False,
                   'execution_authorized': False}
        state['inbox'][event['event_id']] = {'event_sha256': digest(event), 'receipt': receipt}
        return receipt, True

    def record_stop(self, event):
        event = stop_document(event)
        need(event['workspace_id'] == self.workspace_id, 'workspace_mismatch')
        return self._mutate('record_stop', lambda state, now: self._stop(state, event, now))

    def finish(self, attempt_id, token, *, status, receipt_sha256=None):
        ident(attempt_id); sha(token)
        need(status in ('RECORDED', 'UNKNOWN', 'RATE_429'), 'finish_status_invalid')
        if receipt_sha256 is not None:
            sha(receipt_sha256)
        need(status != 'RECORDED' or receipt_sha256 is not None, 'recorded_requires_receipt_hash')
        def apply(state, now):
            row = state['attempts'].get(attempt_id)
            need(row is not None and hmac.compare_digest(row['token_sha256'], digest(token)), 'attempt_or_token_invalid')
            if status == 'RATE_429':
                b = row['binding']
                observation = receipt_sha256 or digest({'attempt': b, 'status': status})
                event = {'schema': 'keel.operational.stop.v1', 'event_id': 'rate-' + digest([attempt_id, observation]),
                    'workspace_id': self.workspace_id, 'kind': 'RATE_429', 'attempt_id': attempt_id,
                    'account_id': b['account_id'], 'role_id': b['role_id'], 'resource_id': b['resource_id'],
                    'observation_sha256': observation}
                receipt, changed = self._stop(state, event, now)
                return {**receipt, 'attempt_status': row['status']}, changed
            if row['status'] == 'RECORDED' and status == 'UNKNOWN':
                self._unknown(state, row, 'late_uncertainty_after_recorded', now)
                return {'status': 'UNKNOWN', 'reason': row['reason'], 'execution_authorized': False}, True
            if row['status'] == 'RECORDED':
                need(status == 'RECORDED' and row['receipt_sha256'] == receipt_sha256, 'recorded_result_conflict')
                return {'status': 'RECORDED', 'duplicate': True, 'execution_authorized': False}, False
            if row['status'] == 'UNKNOWN':
                return {'status': 'UNKNOWN', 'reason': row['reason'], 'execution_authorized': False}, False
            need(row['status'] == 'ISSUED', 'finish_requires_issued_attempt')
            if status == 'UNKNOWN' or now >= row['lease_until']:
                self._unknown(state, row, 'reported_unknown' if status == 'UNKNOWN' else 'late_observation', now)
            else:
                row.update(status='RECORDED', receipt_sha256=receipt_sha256)
            return {'status': row['status'], 'reason': row['reason'], 'execution_authorized': False}, True
        return self._mutate('finish', apply)

    def recover(self):
        def apply(state, now):
            changed = []
            for aid, row in state['attempts'].items():
                if row['status'] in ('RESERVED', 'ISSUED') and now >= row['lease_until']:
                    if row['status'] == 'ISSUED':
                        self._unknown(state, row, 'issued_lease_expired', now)
                    else:
                        row.update(status='BLOCKED', reason='reservation_expired')
                    changed.append(aid)
            return {'status': 'RECOVERED', 'changed_attempts': changed, 'execution_authorized': False}, bool(changed)
        return self._mutate('recover', apply)

    def flush_outbox(self, *, limit=16, lease_seconds=30):
        """At-least-once delivery. Sink MUST deduplicate stable event_id/hash.

        A crash after sink commit and before local ack causes another callback.
        No caller-supplied ack API exists. Sink return is still a host report,
        not independent proof of what an unrelated canonical system persisted.
        """
        need(self.sink is not None, 'trusted_canonical_sink_required')
        need(type(limit) is int and 1 <= limit <= 128, 'delivery_limit_invalid')
        need(type(lease_seconds) is int and 1 <= lease_seconds <= 300, 'lease_seconds_invalid')
        delivered, attempted = [], []
        for _ in range(limit):
            def claim(state, now):
                for eid, row in state['outbox'].items():
                    if eid in attempted or row['status'] == 'ACKNOWLEDGED':
                        continue
                    if row['status'] == 'DELIVERING' and now < row['lease_until']:
                        continue
                    row.update(status='DELIVERING', delivery_token=secrets.token_hex(32),
                               lease_until=now + lease_seconds, deliveries=row['deliveries'] + 1)
                    return {'event_id': eid, **row}, True
                return None, False
            claimed = self._mutate('outbox_claim', claim)
            if claimed is None:
                break
            attempted.append(claimed['event_id'])
            ack, error = None, None
            try:
                with self.writer():
                    ack = clone(self.sink(clone(claimed['event'])))
                exact(ack, {'schema', 'workspace_id', 'event_id', 'event_sha256', 'status', 'canonical_record_sha256'})
                need(ack['schema'] == 'keel.operational.canonical-ack.v1'
                     and ack['workspace_id'] == self.workspace_id and ack['event_id'] == claimed['event_id']
                     and ack['event_sha256'] == claimed['event_sha256'] and ack['status'] == 'APPLIED',
                     'canonical_ack_binding_invalid')
                sha(ack['canonical_record_sha256'])
            except Exception as exc:
                ack, error = None, type(exc).__name__
            def observe(state, now):
                row = state['outbox'][claimed['event_id']]
                if row['delivery_token'] != claimed['delivery_token']:
                    return {'event_id': claimed['event_id'], 'status': 'FENCED'}, False
                row.update(status='ACKNOWLEDGED' if ack is not None else 'PENDING', ack=ack,
                           last_error=error, delivery_token=None, lease_until=None)
                return {'event_id': claimed['event_id'], 'status': row['status'],
                        'canonical_acknowledged_by_callback': ack is not None}, True
            delivered.append(self._mutate('outbox_observe', observe))
        return {'schema': 'keel.operational.delivery.v1', 'deliveries': delivered,
                'execution_authorized': False, 'independent_canonical_verification': False}


def demo(home):
    now = [1800000000.0]
    def binding(aid, role='role-1', resource='form-1'):
        return {'schema': 'keel.operational.attempt.v1', 'attempt_id': aid, 'worker_id': 'fixture-worker',
            'workspace_id': 'fixture-workspace', 'account_id': 'fixture-account', 'role_id': role,
            'resource_id': resource, 'operation': 'prepare', 'input_sha256': digest(aid), 'source_sha256': digest('fixture')}
    def provider(b):
        return {'schema': 'keel.operational.host-snapshot.v1', 'workspace_id': b['workspace_id'],
            'attempt_sha256': digest(b), 'source_sha256': b['source_sha256'], 'consent': True,
            'no_ai': False, 'approval_current': True, 'holds': [], 'unknown_attempt': False,
            'rate_limited': False, 'issued_at': now[0], 'expires_at': now[0] + 60}
    applied = {}
    def sink(event):
        applied.setdefault(event['event_id'], digest(event))
        return {'schema': 'keel.operational.canonical-ack.v1', 'workspace_id': event['workspace_id'],
            'event_id': event['event_id'], 'event_sha256': digest(event), 'status': 'APPLIED',
            'canonical_record_sha256': applied[event['event_id']]}
    ledger = StopLedger.create(home, 'fixture-workspace', snapshot_provider=provider, canonical_sink=sink, clock=lambda: now[0])
    first = ledger.admit(binding('first'))
    issued = ledger.dispatch('first', first['token'])
    now[0] += 31
    ledger.recover()
    blocked = ledger.admit(binding('duplicate-role', resource='another-form'))
    ledger.finish('first', first['token'], status='RATE_429', receipt_sha256=digest('late-429'))
    global_block = ledger.admit(binding('other-role', 'role-2', 'form-2'))
    delivery = ledger.flush_outbox()
    reopened = StopLedger(home, 'fixture-workspace', clock=lambda: now[0]).snapshot()
    checks = {'issued_intent': issued['status'] == 'ISSUED',
              'unknown_holds_role': blocked.get('reason') == 'unknown_role_or_resource',
              'late_429_workspace_stop': global_block.get('reason') == 'global_rate_429',
              'restart_preserves_stop': reopened['state']['rate_limited'],
              'callback_delivery_ack': len(delivery['deliveries']) >= 2 and all(x['status'] == 'ACKNOWLEDGED' for x in delivery['deliveries'])}
    return {'schema': 'keel.operational.control-demo.v1', 'status': 'PASS' if all(checks.values()) else 'FAIL',
            'checks': checks, 'synthetic': True, 'execution_authorized': False,
            'real_canonical_writes': 0, 'real_native_browser_actions': 0,
            'checkpoint_sha256': reopened['checkpoint_sha256']}
