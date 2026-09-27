"""Indexed durable productivity history, with bounded reads and exact run IDs.

SQLite transactions atomically update a run, counters and local ledger anchors.
Checksums detect malformed stored records, not a hostile operator editing both
content and hashes. This local store is not an external authenticity service.
"""
from contextlib import contextmanager
import copy
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from urllib.parse import quote
import uuid

from safe_io import atomic_json, canonical, digest, loads

SCHEMA = 'keel.productivity.history.v1'
MAX_ROW_BYTES = 64 * 1024
MAX_PAGE = 100


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _regular(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'productivity_history_file_invalid')


def _read(path, limit=16384):
    _regular(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, 'productivity_history_document_too_large')
    return loads(raw)


def _encoded(value):
    raw = canonical(value)
    require(len(raw) <= MAX_ROW_BYTES, 'productivity_history_row_too_large')
    return raw, digest(value)


def _decoded(raw, checksum):
    require(type(raw) is bytes and len(raw) <= MAX_ROW_BYTES, 'productivity_history_row_invalid')
    value = loads(raw)
    require(digest(value) == checksum, 'productivity_history_checksum_invalid')
    return value


class RunMap:
    """Only touched rows reside in memory; writes are persisted by History.save."""
    def __init__(self, history):
        self.history = history
        self.cache = {}

    def get(self, run_id, default=None):
        if run_id not in self.cache:
            found = self.history.get(run_id)
            if found is None:
                return default
            self.cache[run_id] = found['row']
        return self.cache[run_id]

    def __getitem__(self, run_id):
        found = self.get(run_id)
        if found is None:
            raise KeyError(run_id)
        return found

    def __setitem__(self, run_id, row):
        self.cache[run_id] = row

    def __len__(self):
        return self.history.counts()['runs']

    def values(self):
        # Only for fault injection and the selected-run compatibility surface.
        # Whole history traversal belongs to the bounded pagination API.
        return self.cache.values()


class History:
    def __init__(self, folder, workspace, validator, *, writable=False):
        self.folder = Path(folder)
        self.path = self.folder / 'history.sqlite3'
        self.manifest_path = self.folder / 'history.json'
        self.workspace = str(workspace)
        self.validator = validator
        self.writable = writable
        self.manifest = _read(self.manifest_path)
        self._validate_manifest(self.manifest)
        _regular(self.path)
        with self._connection() as db:
            meta = self._meta(db)
            require(meta['manifest'] == self.manifest, 'productivity_history_manifest_mismatch')

    def _validate_manifest(self, value):
        require(type(value) is dict and set(value) == {'schema', 'instance_id', 'database', 'workspace', 'legacy_sha256'}
                and value['schema'] == SCHEMA and value['database'] == 'history.sqlite3'
                and value['workspace'] == self.workspace and type(value['instance_id']) is str
                and len(value['instance_id']) == 32 and all(c in '0123456789abcdef' for c in value['instance_id'])
                and (value['legacy_sha256'] is None or type(value['legacy_sha256']) is str and len(value['legacy_sha256']) == 64),
                'productivity_history_manifest_invalid')

    @contextmanager
    def _connection(self, *, write=False):
        require(not write or self.writable, 'productivity_history_readonly')
        _regular(self.path)
        db = None
        try:
            db = sqlite3.connect('file:' + quote(str(self.path)) + '?mode=' + ('rw' if write else 'ro'),
                                 uri=True, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            if not write:
                db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.execute('COMMIT')
        except sqlite3.Error as exc:
            raise ValueError('productivity_history_database_invalid') from exc
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _meta(db):
        row = db.execute("SELECT body,body_sha256 FROM metadata WHERE singleton=1").fetchone()
        require(row is not None, 'productivity_history_metadata_missing')
        value = _decoded(row['body'], row['body_sha256'])
        require(type(value) is dict and set(value) == {'schema', 'last_now', 'namespace', 'stages', 'manifest', 'bootstrap'}
                and value['schema'] == SCHEMA and type(value['bootstrap']) is bool,
                'productivity_history_metadata_invalid')
        return value

    @classmethod
    def create(cls, folder, workspace, state, validator, *, legacy_sha256=None):
        """Publish a complete initialized DB, then its pinned manifest.

        Interrupted temp builds are never selected. A fully published bootstrap
        DB can finish manifest publication on a later explicit writable open.
        A missing DB after manifest publication is always corruption.
        """
        folder = Path(folder)
        database, manifest_path = folder / 'history.sqlite3', folder / 'history.json'
        require(not database.exists() and not database.is_symlink() and not manifest_path.exists()
                and not manifest_path.is_symlink(), 'productivity_history_already_present')
        manifest = {'schema': SCHEMA, 'instance_id': uuid.uuid4().hex, 'database': 'history.sqlite3',
                    'workspace': str(workspace), 'legacy_sha256': legacy_sha256}
        meta = {'schema': SCHEMA, 'last_now': state['last_now'], 'namespace': state['namespace'],
                'stages': state['stages'], 'manifest': manifest, 'bootstrap': True}
        descriptor, temporary = tempfile.mkstemp(prefix='.history-building-', suffix='.sqlite3', dir=folder)
        os.close(descriptor)
        try:
            db = sqlite3.connect(temporary, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1),body BLOB NOT NULL,body_sha256 TEXT NOT NULL)')
                db.execute('CREATE TABLE runs(sequence INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL UNIQUE,ledger_hash TEXT NOT NULL,scope_id TEXT NOT NULL,phase TEXT NOT NULL,pending INTEGER NOT NULL CHECK(pending IN(0,1)),body BLOB NOT NULL,body_sha256 TEXT NOT NULL)')
                db.execute('CREATE INDEX runs_namespace ON runs(ledger_hash,scope_id,sequence)')
                db.execute('CREATE INDEX runs_ledger ON runs(ledger_hash,sequence)')
                db.execute('CREATE INDEX runs_pending ON runs(sequence) WHERE pending=1')
                db.execute('CREATE TABLE summaries(namespace TEXT PRIMARY KEY,body BLOB NOT NULL,body_sha256 TEXT NOT NULL)')
                db.execute('CREATE TABLE namespaces(ledger_hash TEXT NOT NULL,scope_id TEXT NOT NULL,PRIMARY KEY(ledger_hash,scope_id))')
                db.execute('CREATE TABLE anchors(ledger_hash TEXT PRIMARY KEY,body BLOB NOT NULL,body_sha256 TEXT NOT NULL)')
                db.execute('INSERT INTO metadata VALUES(1,?,?)', _encoded(meta))
                instance = object.__new__(cls)
                instance.validator = validator
                instance.workspace = str(workspace)
                for run_id, row in state['runs'].items():
                    instance._write_row(db, run_id, row)
                if not state['runs']:
                    instance._summary(db, '*', create=True)
                db.execute('COMMIT')
            finally:
                db.close()
            os.replace(temporary, database)
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            atomic_json(manifest_path, manifest)
            instance = cls(folder, workspace, validator, writable=True)
            instance.finalize_bootstrap()
            return instance
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def finish_bootstrap(cls, folder, workspace, validator, *, legacy_sha256):
        folder = Path(folder)
        path = folder / 'history.sqlite3'
        _regular(path)
        db = sqlite3.connect('file:' + quote(str(path)) + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            meta = cls._meta(db)
            require(meta['bootstrap'] is True and meta['manifest']['workspace'] == str(workspace)
                    and meta['manifest']['legacy_sha256'] == legacy_sha256, 'productivity_history_incomplete')
        except sqlite3.Error as exc:
            raise ValueError('productivity_history_incomplete') from exc
        finally:
            db.close()
        atomic_json(folder / 'history.json', meta['manifest'])
        history = cls(folder, workspace, validator, writable=True)
        history.finalize_bootstrap()
        return history

    def finalize_bootstrap(self):
        with self._connection(write=True) as db:
            meta = self._meta(db)
            if meta['bootstrap']:
                meta['bootstrap'] = False
                db.execute('UPDATE metadata SET body=?,body_sha256=? WHERE singleton=1', _encoded(meta))

    def state(self, now):
        with self._connection() as db:
            meta = self._meta(db)
            require(meta['manifest'] == self.manifest and type(meta['last_now']) in (int, float)
                    and now >= meta['last_now'], 'productivity_history_clock_or_manifest_invalid')
        return {'schema': 'keel.productivity.v1', 'last_now': meta['last_now'], 'namespace': meta['namespace'],
                'stages': meta['stages'], 'runs': RunMap(self), '_history': self, '_anchors': {}}

    @staticmethod
    def _namespace(row):
        return digest(row['accounting_namespace'])

    @staticmethod
    def _empty_summary():
        return {'runs': 0, 'pending': 0, 'unknown': 0, 'cancelled': 0, 'stages': {}}

    def _summary(self, db, namespace, *, create=False):
        row = db.execute('SELECT body,body_sha256 FROM summaries WHERE namespace=?', (namespace,)).fetchone()
        if row is None:
            value = self._empty_summary()
            if create:
                db.execute('INSERT INTO summaries VALUES(?,?,?)', (namespace, *_encoded(value)))
            return value
        value = _decoded(row['body'], row['body_sha256'])
        require(type(value) is dict and set(value) == {'runs', 'pending', 'unknown', 'cancelled', 'stages'}
                and all(type(value[key]) is int and value[key] >= 0 for key in value if key != 'stages')
                and type(value['stages']) is dict, 'productivity_history_summary_invalid')
        return value

    def _contribute(self, db, row, sign):
        for namespace in ('*', self._namespace(row)):
            summary = self._summary(db, namespace, create=True)
            summary['runs'] += sign
            summary['pending'] += sign * int(self._is_pending(row))
            summary['cancelled'] += sign * int(row['phase'] == 'CANCELLED')
            if row['result'] is None and row['phase'] != 'CANCELLED':
                summary['unknown'] += sign
            report = row['result']
            if report is not None:
                entry = summary['stages'].setdefault(report['stage'], {'runs': 0, 'requests': 0, 'elapsed_ms': 0,
                    'added_leads': 0, 'committed_posting_presence': 0, 'statuses': {}, 'external_credit_micros': None,
                    'progress_unknown_runs': 0})
                entry['runs'] += sign
                entry['requests'] += sign * report['usage']['calls']
                entry['elapsed_ms'] += sign * report['usage']['compute_ms']
                for key in ('added_leads', 'committed_posting_presence'):
                    entry[key] += sign * (report['progress'][key] or 0)
                entry['progress_unknown_runs'] += sign * int(any(value is None for value in report['progress'].values()))
                statuses = entry['statuses']
                statuses[report['status']] = statuses.get(report['status'], 0) + sign
                if statuses[report['status']] == 0:
                    del statuses[report['status']]
                if entry['runs'] == 0:
                    del summary['stages'][report['stage']]
            db.execute('UPDATE summaries SET body=?,body_sha256=? WHERE namespace=?', (*_encoded(summary), namespace))

    @staticmethod
    def _is_pending(row):
        return row['phase'] not in ('COMPLETE', 'CANCELLED') or bool(row['result'] and
               row['result']['manual_recovery_required'] and row['recovery'] is None)

    def _row(self, record):
        row = _decoded(record['body'], record['body_sha256'])
        self.validator(record['run_id'], row, self.workspace)
        require(record['phase'] == row['phase'] and record['pending'] == int(self._is_pending(row))
                and record['ledger_hash'] == row['accounting_namespace']['ledger_path_sha256']
                and record['scope_id'] == row['accounting_namespace']['scope_id'], 'productivity_history_index_mismatch')
        return {'sequence': record['sequence'], 'run_id': record['run_id'], 'row': row}

    def _write_row(self, db, run_id, row):
        self.validator(run_id, row, self.workspace)
        db.execute('INSERT OR IGNORE INTO namespaces VALUES(?,?)', (row['accounting_namespace']['ledger_path_sha256'], row['accounting_namespace']['scope_id']))
        old = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if old is not None:
            previous = self._row(old)['row']
            require(previous['binding'] == row['binding'] and previous['request_id'] == row['request_id']
                    and previous['accounting_namespace'] == row['accounting_namespace'], 'productivity_history_run_binding_changed')
            transitions = {'INTENT': {'INTENT', 'RECORDED', 'CANCELLED'},
                           'RECORDED': {'RECORDED', 'COMPLETE'}, 'COMPLETE': {'COMPLETE'}, 'CANCELLED': {'CANCELLED'}}
            require(row['phase'] in transitions[previous['phase']], 'productivity_history_phase_regressed')
            if previous['result'] is not None:
                require(row['result'] == previous['result'] and row['receipt_sha256'] == previous['receipt_sha256'],
                        'productivity_history_receipt_changed')
            if previous['recovery'] is not None:
                require(row['recovery'] == previous['recovery'], 'productivity_history_recovery_changed')
            if previous == row:
                return
            self._contribute(db, previous, -1)
        db.execute('INSERT INTO runs(run_id,ledger_hash,scope_id,phase,pending,body,body_sha256) VALUES(?,?,?,?,?,?,?) '
                   'ON CONFLICT(run_id) DO UPDATE SET phase=excluded.phase,pending=excluded.pending,body=excluded.body,body_sha256=excluded.body_sha256',
                   (run_id, row['accounting_namespace']['ledger_path_sha256'], row['accounting_namespace']['scope_id'],
                    row['phase'], int(self._is_pending(row)), *_encoded(row)))
        self._contribute(db, row, 1)

    def save(self, state, now):
        with self._connection(write=True) as db:
            meta = self._meta(db)
            require(now >= meta['last_now'] and meta['manifest'] == self.manifest, 'productivity_history_clock_or_manifest_invalid')
            require(meta['namespace'] is None or state['namespace'] == meta['namespace'], 'productivity_history_workspace_changed')
            for run_id, row in state['runs'].cache.items():
                self._write_row(db, run_id, row)
            for ledger_hash, checkpoint in state['_anchors'].items():
                db.execute('INSERT INTO anchors VALUES(?,?,?) ON CONFLICT(ledger_hash) DO UPDATE SET body=excluded.body,body_sha256=excluded.body_sha256',
                           (ledger_hash, *_encoded(checkpoint)))
            meta.update(last_now=now, namespace=state['namespace'], stages=state['stages'], bootstrap=False)
            db.execute('UPDATE metadata SET body=?,body_sha256=? WHERE singleton=1', _encoded(meta))
        state['last_now'] = now

    def get(self, run_id):
        with self._connection() as db:
            row = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
            return None if row is None else self._row(row)

    def pending(self, limit=MAX_PAGE):
        with self._connection() as db:
            records = db.execute('SELECT * FROM runs WHERE pending=1 ORDER BY sequence LIMIT ?', (limit,)).fetchall()
            rows = [self._row(record) for record in records]
            count = self._summary(db, '*')['pending']
            require(bool(count) == bool(rows) and count >= len(rows), 'productivity_history_pending_count_mismatch')
            return [row['run_id'] for row in rows], count

    def counts(self, namespace=None):
        with self._connection() as db:
            return self._summary(db, '*' if namespace is None else digest(namespace))

    def coverage(self, limit=MAX_PAGE):
        with self._connection() as db:
            rows = db.execute("SELECT ledger_hash,scope_id FROM namespaces ORDER BY ledger_hash,scope_id LIMIT ?", (limit + 1,)).fetchall()
        return [{'ledger_path_sha256': row['ledger_hash'], 'scope_id': row['scope_id']} for row in rows[:limit]], len(rows) > limit

    def anchor(self, ledger_hash):
        with self._connection() as db:
            row = db.execute('SELECT body,body_sha256 FROM anchors WHERE ledger_hash=?', (ledger_hash,)).fetchone()
            return None if row is None else _decoded(row['body'], row['body_sha256'])

    def set_anchor(self, ledger_hash, checkpoint):
        with self._connection(write=True) as db:
            db.execute('INSERT INTO anchors VALUES(?,?,?) ON CONFLICT(ledger_hash) DO UPDATE SET body=excluded.body,body_sha256=excluded.body_sha256',
                       (ledger_hash, *_encoded(checkpoint)))

    def page(self, *, after_sequence=0, limit=100, namespace=None, ledger_hash=None):
        require(type(after_sequence) is int and after_sequence >= 0 and type(limit) is int and 1 <= limit <= MAX_PAGE,
                'productivity_history_page_invalid')
        with self._connection() as db:
            if namespace is not None:
                records = db.execute('SELECT * FROM runs WHERE sequence>? AND ledger_hash=? AND scope_id=? ORDER BY sequence LIMIT ?',
                    (after_sequence, namespace['ledger_path_sha256'], namespace['scope_id'], limit + 1)).fetchall()
            elif ledger_hash is not None:
                records = db.execute('SELECT * FROM runs WHERE sequence>? AND ledger_hash=? ORDER BY sequence LIMIT ?',
                    (after_sequence, ledger_hash, limit + 1)).fetchall()
            else:
                records = db.execute('SELECT * FROM runs WHERE sequence>? ORDER BY sequence LIMIT ?',
                    (after_sequence, limit + 1)).fetchall()
            rows = [self._row(record) for record in records[:limit]]
        return {'rows': rows, 'next_sequence': rows[-1]['sequence'] if rows else after_sequence,
                'has_more': len(records) > limit}
