"""Owner-controlled durable execution of fixed pure local computation graphs.

The current OS user controls this namespace. Input pins are not authenticated
facts, people, approvals or execution rights. Only reviewed builtins are called;
external effect authority and its SQLite store are deliberately uninvolved.
"""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import time

from keel_muse.coordinator import Coordinator, task_document
from .adapters import make_coordinator_handler
from .builtins import registered_operations
from .cache import ComputationCache
from .common import MachineError, PrivateDB, _ancestors, _private, canonical, clone, digest, ident, require
from .graph import FLAGS, GraphRunner, GuardDecision, validate_plan

SCHEMA = 'keel.machine.local-runtime.v1'
MAX_RUNS = 64
MAX_WORK_SESSIONS = 512


class LocalGraphRuntime:
    """Passive opens; only ``work`` obtains a controller lock and starts an epoch."""
    def __init__(self, home, *, clock=time.time):
        self.home = Path(os.path.abspath(home))
        self._clock = clock
        require(callable(clock), 'runtime_clock_required')
        _ancestors(self.home / 'runtime.sqlite3'); _private(self.home, True)
        self._file_identities = {}
        for name in ('runtime.sqlite3', 'computation.sqlite3', 'worker.lock',
                     'coordinator/coordinator.sqlite3', 'coordinator/coordinator.sqlite3.key'):
            require((self.home / name).exists(), 'runtime_incomplete')
            self._file_identities[name] = _private(self.home / name)
        self._directory_identities = {name: _private(self.home / name, True) for name in ('', 'coordinator')}
        self.db = PrivateDB(self.home / 'runtime.sqlite3')
        self._controller = None
        self._context = None
        with self._transaction() as (db, _):
            meta = self._metadata(db)
            self.workspace_id = ident(meta['workspace_id'])
            self.account_id = ident(meta['account_id'])
        # A passive open never reprovisions missing coordinator state or keys.
        self._reader().snapshot()

    @classmethod
    def create(cls, home, workspace_id, account_id, *, clock=time.time):
        ident(workspace_id); ident(account_id)
        home = Path(os.path.abspath(home))
        require(home.parent.resolve(strict=True) == home.parent, 'runtime_parent_symlink')
        _ancestors(home)
        home.mkdir(mode=0o700, exist_ok=False)
        private = PrivateDB(home / 'runtime.sqlite3')
        now = cls._stamp(clock())
        with private.transaction() as db:
            db.execute('CREATE TABLE runtime_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema TEXT NOT NULL, workspace_id TEXT NOT NULL, account_id TEXT NOT NULL, last_now REAL NOT NULL, work_sessions INTEGER NOT NULL)')
            db.execute('INSERT INTO runtime_meta VALUES(1,?,?,?,?,0)', (SCHEMA, workspace_id, account_id, now))
            db.execute('CREATE TABLE runtime_runs (run_id TEXT PRIMARY KEY, plan BLOB NOT NULL, plan_sha256 TEXT NOT NULL, admitted_at REAL NOT NULL, held INTEGER NOT NULL DEFAULT 0, hold_reason TEXT, report BLOB, receipt_sha256 TEXT)')
        fd = os.open(home / 'worker.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        ComputationCache(home / 'computation.sqlite3', clock=clock)
        # Provision once. The dummy callback is unreachable: there are no tasks.
        Coordinator(home / 'coordinator', workspace_id,
                    {'machine': lambda context: {'status': 'BLOCKED', 'receipt_sha256': None}},
                    lambda task: None, clock=clock, max_running=1, max_pending=64, max_calls=64)
        directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return cls(home, clock=clock)

    @staticmethod
    def _stamp(value):
        require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**12,
                'runtime_clock_invalid')
        return float(value)

    @staticmethod
    def _metadata(db):
        rows = db.execute('SELECT * FROM runtime_meta').fetchall()
        require(len(rows) == 1 and rows[0]['singleton'] == 1 and rows[0]['schema'] == SCHEMA,
                'runtime_metadata_invalid')
        return rows[0]

    def _storage(self):
        for name, identity in self._directory_identities.items():
            require(_private(self.home / name, True) == identity, 'runtime_storage_replaced')
        for name, identity in self._file_identities.items():
            require((self.home / name).exists(), 'runtime_incomplete')
            require(_private(self.home / name) == identity, 'runtime_storage_replaced')

    @contextmanager
    def _transaction(self):
        self._storage()
        with self.db.transaction() as db:
            meta = self._metadata(db)
            if hasattr(self, 'workspace_id'):
                require(meta['workspace_id'] == self.workspace_id and meta['account_id'] == self.account_id,
                        'runtime_namespace_changed')
            now = self._stamp(self._clock())
            require(now >= meta['last_now'], 'runtime_clock_regressed')
            yield db, now
            after = self._stamp(self._clock())
            require(after >= now, 'runtime_clock_regressed')
            db.execute('UPDATE runtime_meta SET last_now=? WHERE singleton=1', (after,))

    def _reader(self):
        self._storage()
        return Coordinator.open_readonly(self.home / 'coordinator', self.workspace_id)

    def _row(self, db, run_id):
        row = db.execute('SELECT * FROM runtime_runs WHERE run_id=?', (ident(run_id),)).fetchone()
        require(row is not None, 'runtime_run_missing')
        require(type(row['plan']) is bytes and len(row['plan']) <= 8192, 'runtime_plan_storage_invalid')
        plan, _, _ = validate_plan(json.loads(row['plan']))
        require(digest(plan) == row['plan_sha256'] and plan['run_id'] == run_id
                and plan['account_id'] == self.account_id, 'runtime_plan_changed')
        require(row['held'] in (0, 1), 'runtime_hold_invalid')
        return row, plan

    def _task(self, plan):
        return task_document({'schema': 'keel.muse.task.v1', 'task_id': plan['run_id'],
            'workspace_id': self.workspace_id, 'scope_id': plan['scope'], 'handler_id': 'machine',
            'account_id': self.account_id, 'resource_id': 'pure-local-computation',
            'dependencies': {'graph_plan:' + plan['run_id']: digest(plan)},
            'payload': {'graph': plan}, 'priority': 0})

    def submit(self, plan):
        plan, nodes, _ = validate_plan(plan)
        require(plan['account_id'] == self.account_id, 'runtime_account_mismatch')
        require(len(plan['run_id']) <= 110, 'runtime_run_id_too_long')
        operations = registered_operations()
        require(all(node['operation'] in operations for node in nodes.values()), 'runtime_operation_unregistered')
        self._task(plan)  # Includes the existing 8 KiB Coordinator payload cap.
        with self._transaction() as (db, now):
            prior = db.execute('SELECT * FROM runtime_runs WHERE run_id=?', (plan['run_id'],)).fetchone()
            if prior is not None:
                require(prior['plan_sha256'] == digest(plan) and bytes(prior['plan']) == canonical(plan),
                        'runtime_run_id_conflict')
                status = 'HELD' if prior['held'] else 'DUPLICATE'
            else:
                require(db.execute('SELECT COUNT(*) FROM runtime_runs').fetchone()[0] < MAX_RUNS,
                        'runtime_capacity_exhausted')
                db.execute('INSERT INTO runtime_runs(run_id,plan,plan_sha256,admitted_at) VALUES(?,?,?,?)',
                           (plan['run_id'], canonical(plan), digest(plan), now))
                status = 'QUEUED'
        return {'schema': SCHEMA, 'status': status, 'run_id': plan['run_id'],
                'plan_sha256': digest(plan), **FLAGS}

    def hold(self, run_id, reason='owner_hold'):
        ident(reason)
        with self._transaction() as (db, _):
            row, _ = self._row(db, run_id)
            # Sticky. A repeated hold cannot overwrite its original explanation.
            if not row['held']:
                db.execute('UPDATE runtime_runs SET held=1,hold_reason=? WHERE run_id=?', (reason, run_id))
            reason = row['hold_reason'] if row['held'] else reason
        return {'schema': SCHEMA, 'status': 'HELD', 'run_id': run_id, 'reason': reason, **FLAGS}

    def _snapshot_provider(self, task):
        with self._transaction() as (db, now):
            row, plan = self._row(db, task['task_id'])
            require(task == self._task(plan), 'runtime_task_mismatch')
            active = not row['held']
            return {'schema': 'keel.muse.host-snapshot.v1', **{key: task[key] for key in
                ('workspace_id', 'scope_id', 'account_id', 'resource_id', 'dependencies')},
                # These describe this inert local namespace only, never an
                # application approval, external consent or named person's identity.
                'consent': active, 'no_ai': False, 'approval_current': active,
                'holds': [] if active else ['owner_hold'], 'unknown_attempt': False,
                'rate_limited': False, 'issued_at': now, 'expires_at': now + 30}

    def _fence(self, plan):
        require(self._context is not None and self._controller is not None, 'runtime_callback_missing')
        context = self._context
        require(context['task'] == self._task(plan), 'runtime_callback_binding_invalid')
        state = self._controller.snapshot()['state']
        now = self._stamp(self._clock())
        current = state['tasks'][plan['run_id']]
        require(now >= state['last_now'] and state['generation'] == context['generation']
                and current['status'] == 'STARTED' and current['owner'] == context['worker_id']
                and current['fence'] == context['fence'] and now < current['lease_until']
                and not state['rate_limited'] and not state['holds'].get(plan['scope']),
                'runtime_callback_fenced')
        require(state['sources'].get(plan['scope'], {}).get('graph_plan:' + plan['run_id']) == digest(plan),
                'runtime_source_changed')

    def _guard(self, request, now):
        allowed = False
        with self._transaction() as (db, observed):
            row, plan = self._row(db, request['run_id'])
            require(request['account_id'] == self.account_id and request['scope'] == plan['scope']
                    and request['purpose'] == plan['purpose'] and request['plan_sha256'] == digest(plan),
                    'runtime_guard_binding_invalid')
            nodes = {node['id']: node for node in plan['nodes']}
            requested = request['nodes'] if request['phase'] == 'graph-return' else {request['node_id']: request}
            require(set(requested) == (set(nodes) if request['phase'] == 'graph-return' else {request['node_id']}),
                    'runtime_guard_nodes_invalid')
            for name, value in requested.items():
                require(name in nodes and value['operation'] == nodes[name]['operation']
                        and value['revisions'] == nodes[name]['revisions'], 'runtime_guard_revision_changed')
            # All node bindings and the sticky hold share this single transaction.
            self._fence(plan)
            allowed = not row['held']
        return GuardDecision(allowed, digest(request), min(now, observed) + 30, 'local_owner_scope')

    def _record_result(self, report):
        receipt = digest(report)
        with self._transaction() as (db, _):
            row, plan = self._row(db, report['run_id'])
            require(not row['held'] and report['plan_sha256'] == digest(plan)
                    and report['status'] == 'COMPLETED', 'runtime_result_held')
            self._fence(plan)
            if row['receipt_sha256'] is not None:
                require(row['receipt_sha256'] == receipt and bytes(row['report']) == canonical(report),
                        'runtime_result_conflict')
            else:
                db.execute('UPDATE runtime_runs SET report=?,receipt_sha256=? WHERE run_id=?',
                           (canonical(report), receipt, plan['run_id']))
        return receipt

    @contextmanager
    def _worker_lock(self):
        self._storage()
        path = self.home / 'worker.lock'
        identity = self._file_identities['worker.lock']
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            require((info.st_dev, info.st_ino) == identity, 'runtime_lock_replaced')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MachineError('runtime_worker_busy') from None
            require(_private(path) == identity, 'runtime_lock_replaced')
            yield
        finally:
            os.close(descriptor)

    def work(self, max_tasks=16):
        require(type(max_tasks) is int and 1 <= max_tasks <= MAX_RUNS, 'runtime_work_budget_invalid')
        with self._worker_lock():
            with self._transaction() as (db, _):
                require(self._metadata(db)['work_sessions'] < MAX_WORK_SESSIONS, 'runtime_session_capacity_exhausted')
                db.execute('UPDATE runtime_meta SET work_sessions=work_sessions+1 WHERE singleton=1')
            cache = ComputationCache(self.home / 'computation.sqlite3', clock=self._clock)
            runner = GraphRunner(cache, registered_operations(), self._guard, clock=self._clock)
            adapter = make_coordinator_handler(runner, self._record_result, per_run_dependency=True)
            def handler(context):
                self._context = clone(context)
                try:
                    return adapter(context)
                finally:
                    self._context = None
            self._controller = Coordinator(self.home / 'coordinator', self.workspace_id, {'machine': handler},
                self._snapshot_provider, clock=self._clock, max_running=1, max_pending=64, max_calls=64)
            try:
                # Idempotent outbox reconciliation: an interruption at either
                # durable boundary is safely resumed without duplicate invocation.
                with self._transaction() as (db, _):
                    plans = [self._row(db, row['run_id'])[1] for row in
                             db.execute('SELECT run_id FROM runtime_runs ORDER BY admitted_at,run_id').fetchall()]
                for plan in plans:
                    self._controller.ingest({'schema': 'keel.muse.event.v1',
                        'event_id': 'source:' + plan['run_id'], 'workspace_id': self.workspace_id,
                        'scope_id': plan['scope'], 'kind': 'SOURCE', 'payload': {
                            'source_id': 'graph_plan:' + plan['run_id'], 'revision_sha256': digest(plan),
                            'expected_previous_sha256': None}})
                    self._controller.enqueue(self._task(plan))
                outcomes = []
                for _ in range(max_tasks):
                    outcome = self._controller.worker_once('local-machine-worker', lease_seconds=360)
                    if outcome['status'] == 'IDLE':
                        break
                    outcomes.append(outcome)
                    if outcome['status'] == 'UNKNOWN':
                        break
                status = ('UNKNOWN' if any(o['status'] == 'UNKNOWN' for o in outcomes) else
                          'BLOCKED' if any(o['status'] != 'RECORDED' or o.get('outcome', {}).get('status') != 'RECORDED'
                                           for o in outcomes) else 'COMPLETED' if outcomes else 'IDLE')
                return {'schema': SCHEMA, 'status': status, 'outcomes': outcomes,
                        'tasks_observed': len(outcomes), **FLAGS}
            finally:
                self._controller = None

    def status(self):
        state = self._reader().snapshot()['state']
        with self._transaction() as (db, _):
            runs = []
            for entry in db.execute('SELECT run_id FROM runtime_runs ORDER BY admitted_at,run_id').fetchall():
                row, plan = self._row(db, entry['run_id'])
                task = state['tasks'].get(plan['run_id'])
                status = self._result_status(row, task)
                if status == 'COMPLETED':
                    self._validated_report(row, plan, task)
                runs.append({'run_id': plan['run_id'], 'scope': plan['scope'], 'status': status,
                             'plan_sha256': row['plan_sha256'], 'receipt_sha256': row['receipt_sha256'],
                             'reason': row['hold_reason'] if row['held'] else task.get('reason') if task else None})
            sessions = self._metadata(db)['work_sessions']
        return {'schema': SCHEMA, 'status': 'OBSERVED', 'workspace_id': self.workspace_id,
                'account_id': self.account_id, 'identity_assurance': 'local_os_owner_only',
                'coordinator_generation': state['generation'], 'work_sessions': sessions,
                'max_runs': MAX_RUNS, 'max_work_sessions': MAX_WORK_SESSIONS, 'runs': runs, **FLAGS}

    @staticmethod
    def _result_status(row, task):
        if row['held']:
            return 'HELD'
        if task is None:
            return 'QUEUED'
        if task['status'] == 'RECORDED':
            return 'COMPLETED' if task['outcome']['status'] == 'RECORDED' and row['receipt_sha256'] else 'HELD'
        return task['status']

    @staticmethod
    def _validated_report(row, plan, task):
        require(type(row['report']) is bytes and len(row['report']) <= 262144,
                'runtime_report_storage_invalid')
        report = json.loads(row['report'])
        require(digest(report) == row['receipt_sha256'] == task['outcome']['receipt_sha256']
                and report['plan_sha256'] == digest(plan) and report['run_id'] == plan['run_id'],
                'runtime_receipt_mismatch')
        return report

    def result(self, run_id):
        ident(run_id)
        state = self._reader().snapshot()['state']
        with self._transaction() as (db, _):
            row, plan = self._row(db, run_id)
            task = state['tasks'].get(run_id)
            status = self._result_status(row, task)
            report = None
            if status == 'COMPLETED':
                report = self._validated_report(row, plan, task)
        return {'schema': SCHEMA, 'status': status, 'run_id': run_id,
                'receipt_sha256': row['receipt_sha256'] if status == 'COMPLETED' else None,
                'report': report, **FLAGS}
