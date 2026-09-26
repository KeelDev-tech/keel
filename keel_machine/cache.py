"""Bounded persistent cache of inert pure-computation results.

Purity is a trusted-host registration property, not something arbitrary JSON can
prove. Nothing in this module runs callbacks or confers execution authority.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import time

from .common import MachineError, PrivateDB, canonical, ident, require, sha

KEY_SCHEMA = "keel.machine.cache-key.v1"
CACHE_SCHEMA = "keel.machine.computation-cache.v1"
PURPOSES = frozenset({"analysis", "preparation", "test"})
KEY_FIELDS = frozenset({"schema", "account_id", "scope", "purpose", "operation", "implementation_sha256",
                        "config_sha256", "inputs_sha256", "dependencies", "contract_sha256"})
MAX_KEY_BYTES = 65536
MAX_VALUE_BYTES = 262144
MAX_TTL_SECONDS = 30 * 86400


def _key(value):
    require(type(value) is dict and set(value) == KEY_FIELDS, "cache_key_fields_invalid")
    require(value["schema"] == KEY_SCHEMA, "cache_key_schema_invalid")
    for name in ("account_id", "scope", "operation"):
        ident(value[name])
    require(type(value["purpose"]) is str and value["purpose"] in PURPOSES, "cache_purpose_not_pure_computation")
    for name in ("implementation_sha256", "config_sha256", "inputs_sha256", "contract_sha256"):
        sha(value[name])
    dependencies = value["dependencies"]
    require(type(dependencies) is dict and len(dependencies) <= 128, "cache_dependencies_invalid")
    for dependency_id, revision in dependencies.items():
        ident(dependency_id)
        sha(revision)
    raw = canonical(value, limit=MAX_KEY_BYTES)
    return raw, hashlib.sha256(raw).hexdigest()


def _number(value, maximum):
    return type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value)


class ComputationCache:
    """Private SQLite cache with TTL, deterministic LRU, and sticky holds.

    max_bytes bounds live canonical key/result bytes, not SQLite physical file
    pages or index overhead. Expired/evicted values lose their comparison history.
    Held keys consume bounded capacity and are not silently forgotten by LRU.
    """
    def __init__(self, path, *, clock=time.time, max_entries=1024, max_bytes=16777216):
        require(callable(clock), "cache_clock_required")
        require(type(max_entries) is int and 1 <= max_entries <= 100000, "cache_entry_limit_invalid")
        require(type(max_bytes) is int and 1 <= max_bytes <= 268435456, "cache_byte_limit_invalid")
        self.clock, self.max_entries, self.max_bytes = clock, max_entries, max_bytes
        self.db = PrivateDB(path)
        with self.db.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS computation_cache_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema TEXT NOT NULL,
                max_entries INTEGER NOT NULL, max_bytes INTEGER NOT NULL,
                last_now REAL NOT NULL, access_sequence INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS computation_cache_entries (
                key_sha256 TEXT PRIMARY KEY, key_json BLOB NOT NULL, value_json BLOB,
                output_sha256 TEXT, state TEXT NOT NULL CHECK(state IN ('READY','HELD')),
                hold_reason TEXT, created_at REAL NOT NULL, expires_at REAL,
                accessed_at REAL NOT NULL, access_sequence INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS computation_cache_lru ON computation_cache_entries(state,access_sequence,key_sha256)")
            row = db.execute("SELECT * FROM computation_cache_meta WHERE singleton=1").fetchone()
            now = self._clock()
            if row is None:
                count = db.execute("SELECT COUNT(*) FROM computation_cache_entries").fetchone()[0]
                require(count == 0, "cache_metadata_missing_with_entries")
                db.execute("INSERT INTO computation_cache_meta VALUES(1,?,?,?,?,?)",
                           (CACHE_SCHEMA, max_entries, max_bytes, now, 0))
            else:
                self._metadata(row, now)
                db.execute("UPDATE computation_cache_meta SET last_now=? WHERE singleton=1", (now,))
            self._expire(db, now)

    def _clock(self):
        try:
            now = self.clock()
        except Exception:
            raise MachineError("cache_clock_unavailable") from None
        require(_number(now, 2**53 - 1), "cache_clock_invalid")
        return now

    def _metadata(self, row, now):
        require(row is not None and row["schema"] == CACHE_SCHEMA and row["max_entries"] == self.max_entries and
                row["max_bytes"] == self.max_bytes, "cache_metadata_or_configuration_mismatch")
        require(_number(row["last_now"], 2**53 - 1) and now >= row["last_now"], "cache_clock_regressed_or_corrupt")
        require(type(row["access_sequence"]) is int and 0 <= row["access_sequence"] < 2**63 - 1, "cache_sequence_invalid")

    @contextmanager
    def _transaction(self):
        with self.db.transaction() as db:
            now = self._clock()
            row = db.execute("SELECT * FROM computation_cache_meta WHERE singleton=1").fetchone()
            self._metadata(row, now)
            db.execute("UPDATE computation_cache_meta SET last_now=? WHERE singleton=1", (now,))
            self._expire(db, now)
            yield db, now
            end = self._clock()
            require(end >= now, "cache_clock_regressed_or_corrupt")
            db.execute("UPDATE computation_cache_meta SET last_now=? WHERE singleton=1", (end,))

    @staticmethod
    def _expire(db, now):
        return db.execute("DELETE FROM computation_cache_entries WHERE state='READY' AND expires_at<=?", (now,)).rowcount

    @staticmethod
    def _sequence(db):
        row = db.execute("SELECT access_sequence FROM computation_cache_meta WHERE singleton=1").fetchone()
        require(row is not None and type(row[0]) is int and 0 <= row[0] < 2**63 - 1, "cache_sequence_invalid")
        result = row[0] + 1
        db.execute("UPDATE computation_cache_meta SET access_sequence=? WHERE singleton=1", (result,))
        return result

    @staticmethod
    def _usage(db):
        row = db.execute("SELECT COUNT(*),COALESCE(SUM(length(key_json)+COALESCE(length(value_json),0)),0),SUM(CASE WHEN state='HELD' THEN 1 ELSE 0 END) FROM computation_cache_entries").fetchone()
        return {"entries": row[0], "bytes": row[1], "held_entries": row[2] or 0}

    @staticmethod
    def _metadata_row(db, key_digest):
        return db.execute("SELECT key_sha256,output_sha256,state,hold_reason,created_at,expires_at,accessed_at,access_sequence,length(key_json) AS key_size,length(value_json) AS value_size FROM computation_cache_entries WHERE key_sha256=?", (key_digest,)).fetchone()

    def _hold(self, db, key_raw, key_digest, row, reason, now):
        output_hash = row["output_sha256"]
        if type(output_hash) is not str or len(output_hash) != 64 or any(c not in "0123456789abcdef" for c in output_hash):
            output_hash = None
        # Cached results are reproducible. Discard corrupt/contradictory payloads,
        # retaining a bounded quarantine key, original digest and hold reason.
        db.execute("UPDATE computation_cache_entries SET key_json=?,value_json=NULL,output_sha256=?,state='HELD',hold_reason=?,expires_at=NULL,accessed_at=?,access_sequence=? WHERE key_sha256=?",
                   (key_raw, output_hash, reason, now, self._sequence(db), key_digest))
        return {"status": "HELD", "key_sha256": key_digest, "output_sha256": output_hash, "hold_reasons": [reason]}

    def _read(self, db, key_raw, key_digest, now):
        row = self._metadata_row(db, key_digest)
        if row is None:
            return None, {"status": "MISS", "key_sha256": key_digest, "output_sha256": None}
        if row["state"] == "HELD":
            reason = row["hold_reason"]
            output_hash = row["output_sha256"]
            hash_valid = output_hash is None or (type(output_hash) is str and len(output_hash) == 64 and all(c in "0123456789abcdef" for c in output_hash))
            if type(reason) is not str or not reason or len(reason) > 128 or not hash_valid or row["key_size"] != len(key_raw) or row["value_size"] is not None:
                return row, self._hold(db, key_raw, key_digest, row, "CACHE_HOLD_METADATA_CORRUPT", now)
            return row, {"status": "HELD", "key_sha256": key_digest, "output_sha256": output_hash, "hold_reasons": [reason]}
        valid = (row["state"] == "READY" and type(row["key_size"]) is int and 0 < row["key_size"] <= MAX_KEY_BYTES and
                 type(row["value_size"]) is int and 0 < row["value_size"] <= MAX_VALUE_BYTES and
                 _number(row["created_at"], now) and _number(row["accessed_at"], now) and
                 row["created_at"] <= row["accessed_at"] and _number(row["expires_at"], 2**53 - 1) and
                 row["expires_at"] > now and type(row["access_sequence"]) is int and row["access_sequence"] > 0)
        if not valid:
            return row, self._hold(db, key_raw, key_digest, row, "CACHE_ENTRY_METADATA_CORRUPT", now)
        payload = db.execute("SELECT key_json,value_json FROM computation_cache_entries WHERE key_sha256=?", (key_digest,)).fetchone()
        if type(payload[0]) is not bytes or payload[0] != key_raw or hashlib.sha256(payload[0]).hexdigest() != key_digest:
            return row, self._hold(db, key_raw, key_digest, row, "CACHE_KEY_INTEGRITY_MISMATCH", now)
        output = payload[1]
        if type(output) is not bytes or hashlib.sha256(output).hexdigest() != row["output_sha256"]:
            return row, self._hold(db, key_raw, key_digest, row, "CACHE_OUTPUT_INTEGRITY_MISMATCH", now)
        try:
            value = json.loads(output)
            require(canonical(value, limit=MAX_VALUE_BYTES) == output, "cache_output_not_canonical")
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return row, self._hold(db, key_raw, key_digest, row, "CACHE_OUTPUT_ENCODING_INVALID", now)
        return row, {"status": "HIT", "value": value, "output_sha256": row["output_sha256"], "key_sha256": key_digest}

    def get(self, key):
        key_raw, key_digest = _key(key)
        with self._transaction() as (db, now):
            _, report = self._read(db, key_raw, key_digest, now)
            if report["status"] == "HIT":
                db.execute("UPDATE computation_cache_entries SET accessed_at=?,access_sequence=? WHERE key_sha256=?", (now, self._sequence(db), key_digest))
            return report

    def put(self, key, value, *, ttl_seconds=3600):
        key_raw, key_digest = _key(key)
        output = canonical(value, limit=MAX_VALUE_BYTES)
        output_digest = hashlib.sha256(output).hexdigest()
        require(_number(ttl_seconds, MAX_TTL_SECONDS) and ttl_seconds > 0, "cache_ttl_invalid")
        require(len(key_raw) + len(output) <= self.max_bytes, "cache_entry_exceeds_byte_budget")
        with self._transaction() as (db, now):
            expires = now + ttl_seconds
            require(expires > now and expires < 2**53, "cache_ttl_not_representable")
            existing, report = self._read(db, key_raw, key_digest, now)
            if report["status"] == "HELD":
                return report
            if report["status"] == "HIT":
                if report["output_sha256"] != output_digest:
                    return self._hold(db, key_raw, key_digest, existing, "NONDETERMINISTIC_OUTPUT", now)
                db.execute("UPDATE computation_cache_entries SET expires_at=?,accessed_at=?,access_sequence=? WHERE key_sha256=?", (expires, now, self._sequence(db), key_digest))
                return {"status": "REFRESHED", "key_sha256": key_digest, "output_sha256": output_digest, "expires_at": expires, "evicted_entries": 0}
            pinned = db.execute("SELECT COUNT(*),COALESCE(SUM(length(key_json)+COALESCE(length(value_json),0)),0) FROM computation_cache_entries WHERE state!='READY'").fetchone()
            if pinned[0] + 1 > self.max_entries or pinned[1] + len(key_raw) + len(output) > self.max_bytes:
                return {"status": "HELD", "key_sha256": key_digest, "output_sha256": None, "hold_reasons": ["CAPACITY_HELD"], "evicted_entries": 0}
            evicted = 0
            while True:
                usage = self._usage(db)
                if usage["entries"] + 1 <= self.max_entries and usage["bytes"] + len(key_raw) + len(output) <= self.max_bytes:
                    break
                victim = db.execute("SELECT key_sha256 FROM computation_cache_entries WHERE state='READY' ORDER BY access_sequence,key_sha256 LIMIT 1").fetchone()
                if victim is None:
                    return {"status": "HELD", "key_sha256": key_digest, "output_sha256": None, "hold_reasons": ["CAPACITY_HELD"], "evicted_entries": evicted}
                db.execute("DELETE FROM computation_cache_entries WHERE key_sha256=?", (victim[0],))
                evicted += 1
            db.execute("INSERT INTO computation_cache_entries VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (key_digest, key_raw, output, output_digest, "READY", None, now, expires, now, self._sequence(db)))
            return {"status": "STORED", "key_sha256": key_digest, "output_sha256": output_digest, "expires_at": expires, "evicted_entries": evicted}

    def stats(self):
        with self._transaction() as (db, now):
            usage = self._usage(db)
            require(usage["entries"] <= self.max_entries and usage["bytes"] <= self.max_bytes, "cache_storage_bounds_corrupt")
            return {"schema": CACHE_SCHEMA, **usage, "ready_entries": usage["entries"] - usage["held_entries"],
                    "max_entries": self.max_entries, "max_bytes": self.max_bytes, "observed_at": now,
                    "byte_accounting": "canonical_keys_and_results", "execution_authorized": False}
