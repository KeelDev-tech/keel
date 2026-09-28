"""Durable, shared resource reservations for trusted local dispatchers.

All resource values are non-negative integers. Credits use provider-defined
millionths, never a guessed token-to-credit conversion. Missing limits are zero.
Every request consumes its scope and all ancestors in one SQLite transaction.
Independent roots represent independent, operator-created budget windows; hosts
must route all relevant work through the same root to enforce a global cap.

The database and its directory must be operator-owned. This module does not
defend against hostile filesystem changes or dispatch outside the ledger. A
reservation is an estimate, not a provider-side cap. Actual overages are recorded
truthfully and lock future admissions. Unknown usage never refunds its reserve.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid


RESOURCES = ("calls", "input_tokens", "output_tokens", "compute_ms", "external_credit_micros")
MAX_INTEGER = 2**53 - 1
SCHEMA = "keel.efficiency.ledger.v1"
CHECKPOINT_SCHEMA = "keel.efficiency.checkpoint.v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z")
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class LedgerError(ValueError):
    """Invalid input, corrupt state, or unavailable ledger."""


class ConflictError(LedgerError):
    """An immutable binding or request lifecycle conflicts with this operation."""


class BudgetExceeded(LedgerError):
    """At least one ancestor is locked or has insufficient available resources."""


def _require(condition, message):
    if not condition:
        raise LedgerError(message)


def _ident(value):
    _require(type(value) is str and _ID.fullmatch(value) is not None, "invalid identifier")
    return value


def _vector(value, *, unknown=False):
    _require(type(value) is dict and not (set(value) - set(RESOURCES)), "invalid resource vector")
    result = {}
    for name in RESOURCES:
        item = value.get(name, None if unknown else 0)
        _require((unknown and item is None) or
                 (type(item) is int and 0 <= item <= MAX_INTEGER), "invalid resource value: " + name)
        result[name] = item
    return result


def _json(value):
    """Bound metadata before serialization, rejecting cycles and lossy coercions."""
    nodes = [0]
    active = set()

    def visit(item, depth):
        nodes[0] += 1
        _require(depth <= 16 and nodes[0] <= 4096, "metadata complexity exceeded")
        kind = type(item)
        if kind in (dict, list):
            _require(id(item) not in active, "cyclic metadata")
            active.add(id(item))
            if kind is dict:
                _require(all(type(key) is str for key in item), "metadata keys must be strings")
                for key, child in item.items():
                    _require(len(key) <= 16384, "metadata key too large")
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
            active.remove(id(item))
        elif kind is int:
            _require(abs(item) <= MAX_INTEGER, "metadata integer out of range")
        elif kind is float:
            _require(math.isfinite(item) and abs(item) <= MAX_INTEGER, "metadata number invalid")
        elif kind is str:
            _require(len(item) <= 16384, "metadata string too large")
        else:
            _require(item is None or kind is bool, "metadata is not strict JSON")

    visit(value, 0)
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, OverflowError) as exc:
        raise LedgerError("invalid metadata") from exc
    _require(len(result.encode("utf-8")) <= 16384, "metadata exceeds 16384 bytes")
    return result


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _filesystem_path(path):
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise LedgerError("ledger requires a filesystem path") from exc
    _require(type(raw) is str and raw and "\x00" not in raw and raw != ":memory:" and
             not raw.startswith("file:"), "ledger requires a persistent filesystem path")
    return str(Path(raw).absolute())


class ResourceLedger:
    """SQLite transactions serialize reservations across threads and processes.

    Scope configuration is immutable. New IDs explicitly open new budget windows;
    wall-clock passage cannot silently reset a budget or release unknown charges.
    No automatic retry, network operation, payment, or execution occurs here.
    """

    def __init__(self, path):
        self.path = _filesystem_path(path)
        self._readonly = False
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            _require(stat.S_ISREG(os.stat(self.path).st_mode), "ledger path is not a regular file")
        else:
            os.close(fd)
        with self._transaction(initialize=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS efficiency_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema TEXT NOT NULL, instance_id TEXT NOT NULL)")
            columns = {column["name"]: column["type"].upper()
                       for column in db.execute("PRAGMA table_info(efficiency_meta)")}
            legacy_meta = "instance_id" not in columns
            if legacy_meta:
                db.execute("ALTER TABLE efficiency_meta ADD COLUMN instance_id TEXT")
            else:
                _require(columns["instance_id"] == "TEXT", "invalid ledger instance column")
            row = db.execute("SELECT schema,instance_id FROM efficiency_meta WHERE singleton=1").fetchone()
            if row is None:
                db.execute("INSERT INTO efficiency_meta(singleton,schema,instance_id) VALUES(1,?,?)",
                           (SCHEMA, uuid.uuid4().hex))
            else:
                _require(row[0] == SCHEMA, "unsupported ledger schema")
                if legacy_meta:
                    db.execute("UPDATE efficiency_meta SET instance_id=? WHERE singleton=1", (uuid.uuid4().hex,))
                else:
                    _require(type(row["instance_id"]) is str and
                             _TOKEN.fullmatch(row["instance_id"]) is not None,
                             "invalid ledger instance identity")
            db.execute("""CREATE TABLE IF NOT EXISTS efficiency_scopes (
                scope_id TEXT PRIMARY KEY, parent_id TEXT REFERENCES efficiency_scopes(scope_id),
                limits_json TEXT NOT NULL, used_json TEXT NOT NULL, reserved_json TEXT NOT NULL,
                locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)), lock_reason TEXT,
                created_ns INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS efficiency_requests (
                request_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES efficiency_scopes(scope_id),
                binding TEXT NOT NULL, estimate_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('RESERVED','DISPATCHED','UNKNOWN','SETTLED','CANCELLED')),
                usage_json TEXT NOT NULL, remaining_json TEXT NOT NULL, overages_json TEXT NOT NULL,
                reason TEXT, created_ns INTEGER NOT NULL, updated_ns INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS efficiency_request_scope ON efficiency_requests(scope_id,state)")
            db.execute("""CREATE TABLE IF NOT EXISTS efficiency_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
                event TEXT NOT NULL, payload_json TEXT NOT NULL, created_ns INTEGER NOT NULL,
                event_id TEXT)""")
            columns = {column["name"]: column["type"].upper()
                       for column in db.execute("PRAGMA table_info(efficiency_events)")}
            if "event_id" not in columns:
                # Historical payloads are immutable. Only new events receive a
                # nonce, distinguishing an appended branch after a DB restore.
                db.execute("ALTER TABLE efficiency_events ADD COLUMN event_id TEXT")
            else:
                _require(columns["event_id"] == "TEXT", "invalid ledger event identity column")

    @classmethod
    def open_readonly(cls, path):
        """Open an existing current-schema ledger without initialization or repair.

        No scope, event, schema or accounting state is written. SQLite may need
        access to WAL coordination sidecars; this is not a guarantee of zero
        filesystem activity. Do not use immutable mode for a changing ledger.
        Legacy schema migration remains an explicit writable-host operation.
        """
        ledger = cls.__new__(cls)
        ledger.path = _filesystem_path(path)
        ledger._readonly = True
        try:
            info = os.stat(ledger.path)
        except OSError as exc:
            raise LedgerError("existing resource ledger is unavailable") from exc
        _require(stat.S_ISREG(info.st_mode), "ledger path is not a regular file")
        with ledger._transaction(readonly=True) as db:
            ledger._validate_readonly_schema(db)
            ledger._instance_id(db)
        return ledger

    @staticmethod
    def _validate_readonly_schema(db):
        # Only fixed, bounded metadata is inspected. This is schema admission,
        # not a claim that every historical row passed an integrity audit.
        required = {
            "efficiency_meta": {"singleton": "INTEGER", "schema": "TEXT", "instance_id": "TEXT"},
            "efficiency_scopes": {"scope_id": "TEXT", "parent_id": "TEXT", "limits_json": "TEXT",
                                  "used_json": "TEXT", "reserved_json": "TEXT", "locked": "INTEGER",
                                  "lock_reason": "TEXT", "created_ns": "INTEGER"},
            "efficiency_requests": {"request_id": "TEXT", "scope_id": "TEXT", "binding": "TEXT",
                                    "estimate_json": "TEXT", "metadata_json": "TEXT", "state": "TEXT",
                                    "usage_json": "TEXT", "remaining_json": "TEXT", "overages_json": "TEXT",
                                    "reason": "TEXT", "created_ns": "INTEGER", "updated_ns": "INTEGER"},
            "efficiency_events": {"sequence": "INTEGER", "request_id": "TEXT", "event": "TEXT",
                                  "payload_json": "TEXT", "created_ns": "INTEGER", "event_id": "TEXT"},
        }
        for name, expected in required.items():
            table = db.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
            _require(table is not None and table["type"] == "table",
                     "resource ledger schema is incomplete; writable host validation required")
            columns = {row["name"]: row["type"].upper()
                       for row in db.execute("SELECT name,type FROM pragma_table_info(?)", (name,))}
            if ((name == "efficiency_meta" and "instance_id" not in columns) or
                    (name == "efficiency_events" and "event_id" not in columns)):
                raise LedgerError("resource ledger migration required; read-only inspection cannot migrate")
            _require(all(columns.get(column) == kind for column, kind in expected.items()),
                     "resource ledger schema is incompatible; writable host validation required")

    @contextmanager
    def _transaction(self, *, initialize=False, readonly=False):
        _require(not self._readonly or readonly, "read-only ledger cannot mutate state")
        db = None
        try:
            if readonly:
                # mode=ro also prevents a deleted ledger from being silently
                # recreated while a stored checkpoint is being validated.
                db = sqlite3.connect(Path(self.path).as_uri() + "?mode=ro", uri=True,
                                     timeout=30, isolation_level=None)
            else:
                db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if readonly:
                db.execute("PRAGMA query_only=ON")
            else:
                db.execute("PRAGMA synchronous=FULL")
            if initialize:
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error as exc:
            if db is not None:
                db.rollback()
            raise LedgerError("resource ledger transaction failed") from exc
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _scope(db, scope_id):
        row = db.execute("SELECT * FROM efficiency_scopes WHERE scope_id=?", (scope_id,)).fetchone()
        _require(row is not None, "scope does not exist")
        return row

    def _chain(self, db, scope_id):
        result, seen = [], set()
        while scope_id is not None:
            _require(scope_id not in seen and len(seen) < 64, "invalid scope ancestry")
            seen.add(scope_id)
            row = self._scope(db, scope_id)
            result.append(row)
            scope_id = row["parent_id"]
        return result

    @staticmethod
    def _request_row(db, request_id):
        row = db.execute("SELECT * FROM efficiency_requests WHERE request_id=?", (request_id,)).fetchone()
        _require(row is not None, "request does not exist")
        return row

    @staticmethod
    def _request_dict(row):
        return {"request_id": row["request_id"], "scope_id": row["scope_id"], "state": row["state"],
                "estimate": json.loads(row["estimate_json"]), "usage": json.loads(row["usage_json"]),
                "remaining": json.loads(row["remaining_json"]), "metadata": json.loads(row["metadata_json"]),
                "overages": json.loads(row["overages_json"]), "reason": row["reason"],
                "binding_sha256": row["binding"], "created_ns": row["created_ns"],
                "updated_ns": row["updated_ns"]}

    @staticmethod
    def _scope_dict(row):
        limits = json.loads(row["limits_json"])
        used, reserved = json.loads(row["used_json"]), json.loads(row["reserved_json"])
        return {"scope_id": row["scope_id"], "parent_id": row["parent_id"], "limits": limits,
                "used": used, "reserved": reserved, "available": {
                    name: max(0, limits[name] - used[name] - reserved[name]) for name in RESOURCES},
                "locked": bool(row["locked"]), "lock_reason": row["lock_reason"]}

    @staticmethod
    def _event(db, request_id, event, payload):
        db.execute("INSERT INTO efficiency_events(request_id,event,payload_json,created_ns,event_id) VALUES(?,?,?,?,?)",
                   (request_id, event, _dump(payload), time.time_ns(), uuid.uuid4().hex))

    @staticmethod
    def _instance_id(db):
        row = db.execute("SELECT schema,instance_id FROM efficiency_meta WHERE singleton=1").fetchone()
        _require(row is not None and row["schema"] == SCHEMA,
                 "unsupported ledger schema")
        _require(type(row["instance_id"]) is str and
                 _TOKEN.fullmatch(row["instance_id"]) is not None,
                 "invalid ledger instance identity")
        return row["instance_id"]

    @staticmethod
    def _event_digest(row):
        _require(type(row["sequence"]) is int and 0 < row["sequence"] <= MAX_INTEGER,
                 "invalid ledger event sequence")
        _require(row["event_id"] is None or (type(row["event_id"]) is str and
                 _TOKEN.fullmatch(row["event_id"]) is not None),
                 "invalid ledger event identity")
        return hashlib.sha256(_dump(dict(row)).encode("utf-8")).hexdigest()

    def checkpoint(self):
        """Return an indexed, read-only anchor for the append-only event history.

        Persist this outside the ledger and validate it before later work. The
        instance ID detects replacement; the event anchor detects restoration
        before that event, including a new branch that reuses its sequence.
        This detects ordinary database restoration, not hostile table editing or
        coordinated restoration of both the ledger and its external checkpoint.
        An empty-ledger anchor cannot detect restoration within the empty state.
        """
        with self._transaction(readonly=True) as db:
            instance_id = self._instance_id(db)
            row = db.execute("SELECT * FROM efficiency_events ORDER BY sequence DESC LIMIT 1").fetchone()
            return {"schema": CHECKPOINT_SCHEMA, "instance_id": instance_id,
                    "event_sequence": row["sequence"] if row is not None else 0,
                    "event_sha256": self._event_digest(row) if row is not None else None}

    def validate_checkpoint(self, checkpoint):
        """Validate one retained anchor with indexed lookups, without writes.

        Later appended events are allowed. Missing or changed history raises
        ConflictError; malformed checkpoint input raises LedgerError.
        """
        _require(type(checkpoint) is dict and set(checkpoint) ==
                 {"schema", "instance_id", "event_sequence", "event_sha256"},
                 "invalid ledger checkpoint")
        _require(checkpoint["schema"] == CHECKPOINT_SCHEMA,
                 "unsupported ledger checkpoint schema")
        _require(type(checkpoint["instance_id"]) is str and
                 _TOKEN.fullmatch(checkpoint["instance_id"]) is not None,
                 "invalid checkpoint instance identity")
        sequence = checkpoint["event_sequence"]
        digest = checkpoint["event_sha256"]
        _require(type(sequence) is int and 0 <= sequence <= MAX_INTEGER,
                 "invalid checkpoint event sequence")
        _require((sequence == 0 and digest is None) or
                 (sequence > 0 and type(digest) is str and _DIGEST.fullmatch(digest) is not None),
                 "invalid checkpoint event digest")
        with self._transaction(readonly=True) as db:
            if self._instance_id(db) != checkpoint["instance_id"]:
                raise ConflictError("ledger instance differs from retained checkpoint")
            if sequence:
                row = db.execute("SELECT * FROM efficiency_events WHERE sequence=?", (sequence,)).fetchone()
                if row is None:
                    raise ConflictError("ledger history is missing a checkpoint event")
                if self._event_digest(row) != digest:
                    raise ConflictError("ledger history differs from retained checkpoint event")
            return True

    def create_scope(self, scope_id, limits, parent_id=None):
        """Create or return identical immutable scope configuration."""
        _ident(scope_id)
        limits = _vector(limits)
        if parent_id is not None:
            _ident(parent_id)
        with self._transaction() as db:
            existing = db.execute("SELECT * FROM efficiency_scopes WHERE scope_id=?", (scope_id,)).fetchone()
            if existing is not None:
                if existing["limits_json"] != _dump(limits) or existing["parent_id"] != parent_id:
                    raise ConflictError("scope configuration is immutable")
                return self._scope_dict(existing)
            if parent_id is not None:
                _require(scope_id != parent_id, "scope cannot parent itself")
                _require(len(self._chain(db, parent_id)) < 64, "scope ancestry exceeds 64 levels")
            zeros = _dump(_vector({}))
            db.execute("INSERT INTO efficiency_scopes VALUES(?,?,?,?,?,0,NULL,?)",
                       (scope_id, parent_id, _dump(limits), zeros, zeros, time.time_ns()))
            return self._scope_dict(self._scope(db, scope_id))

    def reserve(self, request_id, scope_id, resources, metadata=None):
        """Reserve all ancestors atomically; identical request IDs do not spend twice."""
        _ident(request_id)
        _ident(scope_id)
        estimate = _vector(resources)
        metadata = {} if metadata is None else metadata
        _require(type(metadata) is dict, "metadata must be a JSON object")
        metadata_json = _json(metadata)
        binding = hashlib.sha256(_dump({"scope_id": scope_id, "estimate": estimate,
                                       "metadata": json.loads(metadata_json)}).encode()).hexdigest()
        with self._transaction() as db:
            existing = db.execute("SELECT * FROM efficiency_requests WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                if existing["binding"] != binding:
                    raise ConflictError("request ID is bound to different work")
                return self._request_dict(existing)
            chain = self._chain(db, scope_id)
            for row in chain:
                state = self._scope_dict(row)
                if state["locked"]:
                    raise BudgetExceeded("scope is locked: " + row["scope_id"])
                for name in RESOURCES:
                    if state["used"][name] + state["reserved"][name] + estimate[name] > state["limits"][name]:
                        raise BudgetExceeded("insufficient " + name + " in scope " + row["scope_id"])
            for row in chain:
                reserved = json.loads(row["reserved_json"])
                for name in RESOURCES:
                    reserved[name] += estimate[name]
                db.execute("UPDATE efficiency_scopes SET reserved_json=? WHERE scope_id=?",
                           (_dump(reserved), row["scope_id"]))
            now = time.time_ns()
            db.execute("INSERT INTO efficiency_requests VALUES(?,?,?,?,?,'RESERVED',?,?,?,NULL,?,?)",
                       (request_id, scope_id, binding, _dump(estimate), metadata_json,
                        _dump(_vector({}, unknown=True)), _dump(estimate), _dump({}), now, now))
            self._event(db, request_id, "RESERVED", estimate)
            return self._request_dict(self._request_row(db, request_id))

    def mark_dispatched(self, request_id):
        """Grant dispatch exactly once; duplicate callers must never send again."""
        _ident(request_id)
        with self._transaction() as db:
            row = self._request_row(db, request_id)
            if row["state"] != "RESERVED":
                raise ConflictError("request is not available for dispatch")
            # A prior overage may have locked an ancestor after this reservation.
            if any(scope["locked"] for scope in self._chain(db, row["scope_id"])):
                raise BudgetExceeded("scope locked before dispatch")
            db.execute("UPDATE efficiency_requests SET state='DISPATCHED',updated_ns=? WHERE request_id=?",
                       (time.time_ns(), request_id))
            self._event(db, request_id, "DISPATCHED", {})
            return self._request_dict(self._request_row(db, request_id))

    def settle(self, request_id, usage):
        """Record known dimensions; omitted/None values retain their reservations.

        Recorded dimensions are immutable. Identical replay is idempotent. Later
        usage evidence can fill unknown dimensions through ``reconcile``.
        """
        return self._settle(request_id, usage, reconciliation=False)

    def reconcile(self, request_id, usage):
        """Fill previously unknown usage from authoritative evidence, never guesses."""
        return self._settle(request_id, usage, reconciliation=True)

    def _settle(self, request_id, usage, *, reconciliation):
        _ident(request_id)
        incoming = _vector(usage, unknown=True)
        with self._transaction() as db:
            row = self._request_row(db, request_id)
            allowed = ("UNKNOWN", "SETTLED") if reconciliation else ("DISPATCHED", "UNKNOWN", "SETTLED")
            if row["state"] not in allowed:
                raise ConflictError("request cannot be settled in current state")
            estimate, known = json.loads(row["estimate_json"]), json.loads(row["usage_json"])
            remaining, overages = json.loads(row["remaining_json"]), json.loads(row["overages_json"])
            release, charge = _vector({}), _vector({})
            changed = False
            for name in RESOURCES:
                actual = incoming[name]
                if actual is None:
                    continue
                if known[name] is not None:
                    if known[name] != actual:
                        raise ConflictError("recorded usage is immutable: " + name)
                    continue
                known[name] = actual
                release[name], charge[name], remaining[name] = remaining[name], actual, 0
                if actual > estimate[name]:
                    overages[name] = actual - estimate[name]
                changed = True
            state = "SETTLED" if all(value is not None for value in known.values()) else "UNKNOWN"
            for scope in self._chain(db, row["scope_id"]):
                used, reserved = json.loads(scope["used_json"]), json.loads(scope["reserved_json"])
                for name in RESOURCES:
                    _require(reserved[name] >= release[name], "reservation accounting is inconsistent")
                    used[name] += charge[name]
                    reserved[name] -= release[name]
                lock = bool(scope["locked"]) or bool(overages)
                reason = scope["lock_reason"] or ("reservation overage: " + request_id if overages else None)
                db.execute("UPDATE efficiency_scopes SET used_json=?,reserved_json=?,locked=?,lock_reason=? WHERE scope_id=?",
                           (_dump(used), _dump(reserved), int(lock), reason, scope["scope_id"]))
            if changed or state != row["state"]:
                db.execute("UPDATE efficiency_requests SET usage_json=?,remaining_json=?,overages_json=?,state=?,updated_ns=? WHERE request_id=?",
                           (_dump(known), _dump(remaining), _dump(overages), state, time.time_ns(), request_id))
                self._event(db, request_id, "RECONCILED" if reconciliation else "SETTLED", incoming)
            return self._request_dict(self._request_row(db, request_id))

    def mark_unknown(self, request_id, reason=""):
        """Persist ambiguous dispatch; never expire or release unresolved usage."""
        _ident(request_id)
        _require(type(reason) is str and len(reason) <= 512, "unknown reason too large")
        with self._transaction() as db:
            row = self._request_row(db, request_id)
            if row["state"] not in ("DISPATCHED", "UNKNOWN"):
                raise ConflictError("only dispatched requests can have unknown usage")
            if row["state"] != "UNKNOWN" or row["reason"] != reason:
                db.execute("UPDATE efficiency_requests SET state='UNKNOWN',reason=?,updated_ns=? WHERE request_id=?",
                           (reason, time.time_ns(), request_id))
                self._event(db, request_id, "UNKNOWN", {"reason": reason})
            return self._request_dict(self._request_row(db, request_id))

    def cancel(self, request_id):
        """Release a request only when no dispatch permission has been issued."""
        _ident(request_id)
        with self._transaction() as db:
            row = self._request_row(db, request_id)
            if row["state"] == "CANCELLED":
                return self._request_dict(row)
            if row["state"] != "RESERVED":
                raise ConflictError("dispatched reservations cannot be cancelled")
            remaining = json.loads(row["remaining_json"])
            for scope in self._chain(db, row["scope_id"]):
                reserved = json.loads(scope["reserved_json"])
                for name in RESOURCES:
                    _require(reserved[name] >= remaining[name], "reservation accounting is inconsistent")
                    reserved[name] -= remaining[name]
                db.execute("UPDATE efficiency_scopes SET reserved_json=? WHERE scope_id=?",
                           (_dump(reserved), scope["scope_id"]))
            db.execute("UPDATE efficiency_requests SET state='CANCELLED',remaining_json=?,updated_ns=? WHERE request_id=?",
                       (_dump(_vector({})), time.time_ns(), request_id))
            self._event(db, request_id, "CANCELLED", {})
            return self._request_dict(self._request_row(db, request_id))

    def request(self, request_id):
        _ident(request_id)
        with self._transaction(readonly=True) as db:
            return self._request_dict(self._request_row(db, request_id))

    def snapshot(self, scope_id=None):
        """Return an atomic accounting snapshot; scopes include descendant usage."""
        if scope_id is not None:
            _ident(scope_id)
        with self._transaction(readonly=True) as db:
            if scope_id is not None:
                return self._scope_dict(self._scope(db, scope_id))
            scopes = db.execute("SELECT * FROM efficiency_scopes ORDER BY scope_id").fetchall()
            states = db.execute("SELECT state,COUNT(*) AS count FROM efficiency_requests GROUP BY state").fetchall()
            return {"schema": SCHEMA, "scopes": [self._scope_dict(row) for row in scopes],
                    "requests_by_state": {row["state"]: row["count"] for row in states},
                    "event_count": db.execute("SELECT COUNT(*) FROM efficiency_events").fetchone()[0]}
