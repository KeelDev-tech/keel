"""Bounded, exact reuse of host-registered pure work and incremental planning.

Reuse returns inert preparation data, never an approval or permission to act.
The trusted host registers pure operations and pins all material inputs. A task
cannot register itself as pure. Leases suppress concurrent duplicate work;
recovery can overlap a stalled worker, so fencing prevents stale publication but
does not promise exactly-once execution. Do not register side-effecting work.

Storage uses Keel's existing private SQLite boundary. Limits count canonical key
and artifact bytes, not SQLite pages, indexes or journal overhead.
"""
from contextlib import contextmanager
import hashlib
import heapq
import json
import math
import secrets
import time

from keel_machine.common import MachineError, PrivateDB, canonical, ident, require, sha


KEY_SCHEMA = "keel.efficiency.reuse-key.v1"
STORE_SCHEMA = "keel.efficiency.singleflight.v1"
PURPOSES = frozenset({"analysis", "preparation", "test"})
KEY_FIELDS = frozenset({"schema", "account_id", "scope", "purpose", "operation",
                        "implementation_sha256", "model_sha256", "source_sha256",
                        "policy_sha256", "input_sha256", "dependencies"})
MAX_KEY_BYTES = 65536
MAX_VALUE_BYTES = 262144
MAX_TTL_SECONDS = 30 * 86400
MAX_LEASE_SECONDS = 3600
MAX_RUN_SECONDS = 86400


def _number(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum


def _key(key):
    require(type(key) is dict and set(key) == KEY_FIELDS, "reuse_key_fields_invalid")
    require(key["schema"] == KEY_SCHEMA, "reuse_key_schema_invalid")
    for name in ("account_id", "scope", "operation"):
        ident(key[name])
    require(type(key["purpose"]) is str and key["purpose"] in PURPOSES,
            "reuse_purpose_not_pure")
    for name in ("implementation_sha256", "model_sha256", "source_sha256",
                 "policy_sha256", "input_sha256"):
        sha(key[name])
    dependencies = key["dependencies"]
    require(type(dependencies) is dict and len(dependencies) <= 128,
            "reuse_dependencies_invalid")
    for name, fingerprint in dependencies.items():
        ident(name)
        sha(fingerprint)
    raw = canonical(key, limit=MAX_KEY_BYTES)
    return raw, hashlib.sha256(raw).hexdigest()


def build_reuse_key(*, account_id, scope, purpose, operation, implementation_sha256,
                    model_sha256, source_sha256, policy_sha256, input_sha256,
                    dependencies=None):
    """Build an exact key. Model pin includes inference settings and prompt version.

    A deterministic non-model operation still supplies a stable explicit model
    fingerprint (for example SHA256 of its canonical no-model descriptor).
    Source/policy pins must reflect the current evidence and host policy snapshot.
    """
    key = {"schema": KEY_SCHEMA, "account_id": account_id, "scope": scope,
           "purpose": purpose, "operation": operation,
           "implementation_sha256": implementation_sha256, "model_sha256": model_sha256,
           "source_sha256": source_sha256, "policy_sha256": policy_sha256,
           "input_sha256": input_sha256,
           "dependencies": {} if dependencies is None else dependencies}
    raw, _ = _key(key)
    return json.loads(raw)


class SingleFlightStore:
    """A durable leader lease and exact artifact cache shared by local workers.

    ``operations`` is trusted host configuration mapping pure operation names to
    implementation hashes. All workers opening a store must use the same pins
    and bounds. Changing registration requires a new store. Work must recheck
    current authorization before using preparation results for any action.
    """

    def __init__(self, path, *, operations, clock=time.time, max_entries=1024,
                 max_bytes=16777216, max_run_seconds=3600):
        require(callable(clock), "reuse_clock_required")
        require(type(operations) is dict and 1 <= len(operations) <= 256,
                "reuse_pure_registry_required")
        for operation, pin in operations.items():
            ident(operation)
            sha(pin)
        require(type(max_entries) is int and 1 <= max_entries <= 100000,
                "reuse_entry_limit_invalid")
        require(type(max_bytes) is int and 1 <= max_bytes <= 268435456,
                "reuse_byte_limit_invalid")
        require(_number(max_run_seconds, MAX_RUN_SECONDS) and max_run_seconds > 0,
                "reuse_run_limit_invalid")
        self.operations = dict(operations)
        self.registry_sha256 = hashlib.sha256(canonical(operations)).hexdigest()
        self.clock = clock
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.max_run_seconds = max_run_seconds
        self.db = PrivateDB(path)
        with self.db.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS efficiency_reuse_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema TEXT NOT NULL,
                registry_sha256 TEXT NOT NULL, max_entries INTEGER NOT NULL,
                max_bytes INTEGER NOT NULL, max_run_seconds REAL NOT NULL,
                last_now REAL NOT NULL, sequence INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS efficiency_reuse_entries (
                key_sha256 TEXT PRIMARY KEY, key_json BLOB NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('RUNNING','READY','HELD')),
                owner_id TEXT, lease_token TEXT, fence INTEGER NOT NULL,
                created_at REAL NOT NULL, lease_until REAL, run_until REAL,
                expires_at REAL, value_json BLOB, value_sha256 TEXT,
                accessed_sequence INTEGER NOT NULL, hold_reason TEXT)""")
            now = self._clock()
            row = db.execute("SELECT * FROM efficiency_reuse_meta WHERE singleton=1").fetchone()
            if row is None:
                require(db.execute("SELECT COUNT(*) FROM efficiency_reuse_entries").fetchone()[0] == 0,
                        "reuse_metadata_missing_with_entries")
                db.execute("INSERT INTO efficiency_reuse_meta VALUES(1,?,?,?,?,?,?,?)",
                           (STORE_SCHEMA, self.registry_sha256, max_entries, max_bytes,
                            max_run_seconds, now, 0))
            else:
                self._metadata(row, now)
                db.execute("UPDATE efficiency_reuse_meta SET last_now=? WHERE singleton=1", (now,))

    def _clock(self):
        try:
            now = self.clock()
        except Exception:
            raise MachineError("reuse_clock_unavailable") from None
        require(_number(now, 2**53 - 1), "reuse_clock_invalid")
        return now

    def _metadata(self, row, now):
        require(row is not None and row["schema"] == STORE_SCHEMA
                and row["registry_sha256"] == self.registry_sha256
                and row["max_entries"] == self.max_entries and row["max_bytes"] == self.max_bytes
                and row["max_run_seconds"] == self.max_run_seconds,
                "reuse_configuration_mismatch")
        require(_number(row["last_now"], now), "reuse_clock_regressed_or_corrupt")
        require(type(row["sequence"]) is int and 0 <= row["sequence"] < 2**63 - 1,
                "reuse_sequence_corrupt")

    @contextmanager
    def _transaction(self):
        with self.db.transaction() as db:
            now = self._clock()
            row = db.execute("SELECT * FROM efficiency_reuse_meta WHERE singleton=1").fetchone()
            self._metadata(row, now)
            db.execute("UPDATE efficiency_reuse_meta SET last_now=? WHERE singleton=1", (now,))
            db.execute("DELETE FROM efficiency_reuse_entries WHERE state='READY' AND expires_at<=?", (now,))
            db.execute("DELETE FROM efficiency_reuse_entries WHERE state='RUNNING' AND run_until<=?", (now,))
            yield db, now
            end = self._clock()
            require(end >= now, "reuse_clock_regressed_or_corrupt")
            db.execute("UPDATE efficiency_reuse_meta SET last_now=? WHERE singleton=1", (end,))

    @staticmethod
    def _sequence(db):
        value = db.execute("SELECT sequence FROM efficiency_reuse_meta WHERE singleton=1").fetchone()[0]
        require(type(value) is int and 0 <= value < 2**63 - 1, "reuse_sequence_corrupt")
        db.execute("UPDATE efficiency_reuse_meta SET sequence=? WHERE singleton=1", (value + 1,))
        return value + 1

    def _registered_key(self, key):
        raw, digest = _key(key)
        require(self.operations.get(key["operation"]) == key["implementation_sha256"],
                "reuse_operation_not_registered")
        return raw, digest

    @staticmethod
    def _usage(db):
        row = db.execute("SELECT COUNT(*),COALESCE(SUM(length(key_json)+COALESCE(length(value_json),0)),0) FROM efficiency_reuse_entries").fetchone()
        return {"entries": row[0], "bytes": row[1]}

    @staticmethod
    def _report(status, key_digest, **extra):
        return {"status": status, "key_sha256": key_digest,
                "execution_authorized": False, **extra}

    def _hold(self, db, digest, reason):
        db.execute("""UPDATE efficiency_reuse_entries SET state='HELD', owner_id=NULL,
            lease_token=NULL,lease_until=NULL,run_until=NULL,expires_at=NULL,
            value_json=NULL,value_sha256=NULL,hold_reason=? WHERE key_sha256=?""", (reason, digest))
        return self._report("HELD", digest, reason=reason)

    def _read(self, db, raw, digest, now):
        # Inspect lengths before materializing an artifact from an altered database.
        row = db.execute("""SELECT key_sha256,state,owner_id,lease_token,fence,created_at,
            lease_until,run_until,expires_at,value_sha256,accessed_sequence,hold_reason,
            length(key_json) AS key_size,length(value_json) AS value_size
            FROM efficiency_reuse_entries WHERE key_sha256=?""", (digest,)).fetchone()
        if row is None:
            return None, None
        if row["key_size"] != len(raw) or not (type(row["fence"]) is int and row["fence"] > 0
                and _number(row["created_at"], now)):
            return row, self._hold(db, digest, "REUSE_METADATA_CORRUPT")
        key_raw = db.execute("SELECT key_json FROM efficiency_reuse_entries WHERE key_sha256=?", (digest,)).fetchone()[0]
        if type(key_raw) is not bytes or key_raw != raw:
            return row, self._hold(db, digest, "REUSE_KEY_INTEGRITY_MISMATCH")
        if row["state"] == "HELD":
            return row, self._report("HELD", digest, reason="REUSE_ENTRY_HELD")
        if row["state"] == "RUNNING":
            valid = (type(row["owner_id"]) is str and type(row["lease_token"]) is str
                     and len(row["lease_token"]) == 64
                     and _number(row["lease_until"], 2**53 - 1)
                     and _number(row["run_until"], 2**53 - 1)
                     and row["created_at"] < row["lease_until"] <= row["run_until"]
                     and row["run_until"] <= row["created_at"] + self.max_run_seconds
                     and row["value_size"] is None)
            if not valid:
                return row, self._hold(db, digest, "REUSE_LEASE_CORRUPT")
            return row, self._report("WAIT", digest, lease_until=row["lease_until"])
        if not (row["state"] == "READY" and type(row["value_size"]) is int
                and 0 < row["value_size"] <= MAX_VALUE_BYTES
                and _number(row["expires_at"], 2**53 - 1) and row["expires_at"] > now):
            return row, self._hold(db, digest, "REUSE_ARTIFACT_METADATA_CORRUPT")
        value_raw = db.execute("SELECT value_json FROM efficiency_reuse_entries WHERE key_sha256=?", (digest,)).fetchone()[0]
        if type(value_raw) is not bytes or hashlib.sha256(value_raw).hexdigest() != row["value_sha256"]:
            return row, self._hold(db, digest, "REUSE_ARTIFACT_INTEGRITY_MISMATCH")
        try:
            value = json.loads(value_raw)
            require(canonical(value, limit=MAX_VALUE_BYTES) == value_raw, "reuse_noncanonical_artifact")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return row, self._hold(db, digest, "REUSE_ARTIFACT_ENCODING_INVALID")
        return row, self._report("HIT", digest, value=value, value_sha256=row["value_sha256"],
                                 expires_at=row["expires_at"])

    def _room(self, db, now, *, extra_entries, extra_bytes, except_digest=None):
        # Check irreducible occupancy before removing reusable artifacts. A
        # single impossible result must not empty otherwise useful cache data.
        pinned = db.execute("""SELECT COUNT(*),COALESCE(SUM(length(key_json)+COALESCE(length(value_json),0)),0)
            FROM efficiency_reuse_entries WHERE state='HELD' OR key_sha256=?
            OR (state='RUNNING' AND lease_until>?)""", (except_digest or "", now)).fetchone()
        if pinned[0] + extra_entries > self.max_entries or pinned[1] + extra_bytes > self.max_bytes:
            return False
        usage = self._usage(db)
        while usage["entries"] + extra_entries > self.max_entries or usage["bytes"] + extra_bytes > self.max_bytes:
            # Live leaders and corruption holds cannot be evicted. Expired pure
            # leaders are recoverable; a new global fence defeats the ABA race.
            row = db.execute("""SELECT key_sha256 FROM efficiency_reuse_entries
                WHERE (state='READY' OR (state='RUNNING' AND lease_until<=?))
                AND key_sha256!=? ORDER BY accessed_sequence,key_sha256 LIMIT 1""",
                             (now, except_digest or "")).fetchone()
            if row is None:
                return False
            db.execute("DELETE FROM efficiency_reuse_entries WHERE key_sha256=?", (row[0],))
            usage = self._usage(db)
        return True

    def claim(self, key, owner_id, *, lease_seconds=60):
        raw, digest = self._registered_key(key)
        ident(owner_id)
        require(_number(lease_seconds, MAX_LEASE_SECONDS) and lease_seconds > 0,
                "reuse_lease_invalid")
        require(len(raw) <= self.max_bytes, "reuse_key_exceeds_byte_budget")
        with self._transaction() as (db, now):
            row, report = self._read(db, raw, digest, now)
            if report is not None:
                if report["status"] == "HIT":
                    db.execute("UPDATE efficiency_reuse_entries SET accessed_sequence=? WHERE key_sha256=?",
                               (self._sequence(db), digest))
                    return report
                if report["status"] == "HELD" or row["lease_until"] > now:
                    return report
            if row is None and not self._room(db, now, extra_entries=1, extra_bytes=len(raw)):
                return self._report("HELD", digest, reason="REUSE_CAPACITY_HELD")
            fence, token = self._sequence(db), secrets.token_hex(32)
            run_until = now + self.max_run_seconds
            lease_until = min(now + lease_seconds, run_until)
            require(now < lease_until <= run_until < 2**53, "reuse_deadline_not_representable")
            db.execute("""INSERT OR REPLACE INTO efficiency_reuse_entries VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (digest, raw, "RUNNING", owner_id, token,
                fence, now, lease_until, run_until, None, None, None, fence, None))
            return self._report("CLAIMED", digest, owner_id=owner_id, lease_token=token,
                                fence=fence, lease_until=lease_until, run_until=run_until,
                                recovered=row is not None)

    @staticmethod
    def _credentials(owner_id, lease_token, fence):
        ident(owner_id)
        sha(lease_token)
        require(type(fence) is int and 0 < fence < 2**63, "reuse_fence_invalid")

    @staticmethod
    def _owns(row, owner_id, lease_token, fence, now):
        return (row is not None and row["state"] == "RUNNING" and row["owner_id"] == owner_id
                and row["lease_token"] == lease_token and row["fence"] == fence
                and row["lease_until"] > now and row["run_until"] > now)

    def complete(self, key, *, owner_id, lease_token, fence, value, ttl_seconds=3600):
        raw, digest = self._registered_key(key)
        self._credentials(owner_id, lease_token, fence)
        value_raw = canonical(value, limit=MAX_VALUE_BYTES)
        require(_number(ttl_seconds, MAX_TTL_SECONDS) and ttl_seconds > 0, "reuse_ttl_invalid")
        with self._transaction() as (db, now):
            row, report = self._read(db, raw, digest, now)
            if report is not None and report["status"] == "HELD":
                return report
            if not self._owns(row, owner_id, lease_token, fence, now):
                return self._report("STALE", digest)
            if not self._room(db, now, extra_entries=0, extra_bytes=len(value_raw), except_digest=digest):
                return self._report("HELD", digest, reason="REUSE_CAPACITY_HELD")
            expires_at = now + ttl_seconds
            require(now < expires_at < 2**53, "reuse_deadline_not_representable")
            value_digest = hashlib.sha256(value_raw).hexdigest()
            db.execute("""UPDATE efficiency_reuse_entries SET state='READY', owner_id=NULL,
                lease_token=NULL,lease_until=NULL,run_until=NULL,expires_at=?,value_json=?,
                value_sha256=?,accessed_sequence=? WHERE key_sha256=?""",
                       (expires_at, value_raw, value_digest, self._sequence(db), digest))
            return self._report("STORED", digest, value_sha256=value_digest, expires_at=expires_at)

    def heartbeat(self, key, *, owner_id, lease_token, fence, lease_seconds=60):
        raw, digest = self._registered_key(key)
        self._credentials(owner_id, lease_token, fence)
        require(_number(lease_seconds, MAX_LEASE_SECONDS) and lease_seconds > 0, "reuse_lease_invalid")
        with self._transaction() as (db, now):
            row, report = self._read(db, raw, digest, now)
            if report is not None and report["status"] == "HELD":
                return report
            if not self._owns(row, owner_id, lease_token, fence, now):
                return self._report("STALE", digest)
            deadline = min(max(row["lease_until"], now + lease_seconds), row["run_until"])
            require(now < deadline, "reuse_deadline_not_representable")
            db.execute("UPDATE efficiency_reuse_entries SET lease_until=? WHERE key_sha256=?", (deadline, digest))
            return self._report("RENEWED", digest, lease_until=deadline, run_until=row["run_until"])

    def fail(self, key, *, owner_id, lease_token, fence):
        """Release pure work after failure; preserve no exception text or secrets."""
        raw, digest = self._registered_key(key)
        self._credentials(owner_id, lease_token, fence)
        with self._transaction() as (db, now):
            row, report = self._read(db, raw, digest, now)
            if report is not None and report["status"] == "HELD":
                return report
            if not self._owns(row, owner_id, lease_token, fence, now):
                return self._report("STALE", digest)
            db.execute("DELETE FROM efficiency_reuse_entries WHERE key_sha256=?", (digest,))
            return self._report("RELEASED", digest)

    def stats(self):
        """Report usage and lazily prune completed TTLs/absolute run deadlines.

        Integrity holds intentionally retain their bounded key until operator
        investigation; regular cache eviction cannot erase a corruption hold.
        """
        with self._transaction() as (db, now):
            usage = self._usage(db)
            require(usage["entries"] <= self.max_entries and usage["bytes"] <= self.max_bytes,
                    "reuse_storage_bounds_corrupt")
            states = {r[0]: r[1] for r in db.execute("SELECT state,COUNT(*) FROM efficiency_reuse_entries GROUP BY state")}
            return {"schema": STORE_SCHEMA, **usage, "states": states,
                    "max_entries": self.max_entries, "max_bytes": self.max_bytes,
                    "byte_accounting": "canonical_keys_and_artifacts", "observed_at": now,
                    "execution_authorized": False}


def _graph(graph, max_nodes, max_edges):
    require(type(graph) is dict and len(graph) <= max_nodes, "incremental_nodes_invalid")
    normalized, edges = {}, 0
    for name, node in graph.items():
        ident(name)
        require(type(node) is dict and set(node) == {"fingerprint", "needs"},
                "incremental_node_schema_invalid")
        sha(node["fingerprint"])
        needs = node["needs"]
        require(type(needs) is list and len(needs) <= max_nodes
                and all(type(dep) is str for dep in needs)
                and len(needs) == len(set(needs)), "incremental_dependencies_invalid")
        for dep in needs:
            ident(dep)
        edges += len(needs)
        require(edges <= max_edges, "incremental_edges_exceeded")
        normalized[name] = {"fingerprint": node["fingerprint"], "needs": sorted(needs)}
    node_ids = set(normalized)
    require(all(set(node["needs"]) <= node_ids for node in normalized.values()),
            "incremental_missing_dependency")
    outgoing = {name: [] for name in normalized}
    counts = {name: len(node["needs"]) for name, node in normalized.items()}
    ready = [name for name in normalized if counts[name] == 0]
    heapq.heapify(ready)
    for name, node in normalized.items():
        for dep in node["needs"]:
            outgoing[dep].append(name)
    ordered = []
    while ready:
        name = heapq.heappop(ready)
        ordered.append(name)
        for child in outgoing[name]:
            counts[child] -= 1
            if counts[child] == 0:
                heapq.heappush(ready, child)
    require(len(ordered) == len(normalized), "incremental_cycle")
    return normalized, ordered


def plan_incremental(previous, current, *, changed=(), max_nodes=4096, max_edges=16384):
    """Plan changed nodes and descendants without executing or trusting artifacts.

    Fingerprints must bind operation, input, model, policy, evidence and relevant
    external revisions. A reusable node still needs a valid, scoped cache hit.
    Changes to a dependency edge dirty that node and all its descendants.
    """
    require(type(max_nodes) is int and 1 <= max_nodes <= 100000, "incremental_node_limit_invalid")
    require(type(max_edges) is int and 0 <= max_edges <= 1000000, "incremental_edge_limit_invalid")
    old, _ = _graph(previous, max_nodes, max_edges)
    new, order = _graph(current, max_nodes, max_edges)
    require(type(changed) in (list, tuple, set, frozenset) and len(changed) <= max_nodes,
            "incremental_changes_invalid")
    for name in changed:
        ident(name)
    changed = set(changed)
    require(changed <= set(old) | set(new), "incremental_changed_node_unknown")
    dirty, reasons = set(), {}
    for name in order:
        node_reasons = []
        if name not in old:
            node_reasons.append("new_node")
        elif old[name] != new[name]:
            node_reasons.append("revision_or_dependencies_changed")
        if name in changed:
            node_reasons.append("explicit_change")
        if any(dep in dirty for dep in new[name]["needs"]):
            node_reasons.append("dependency_changed")
        if node_reasons:
            dirty.add(name)
            reasons[name] = node_reasons
    return {"schema": "keel.efficiency.incremental-plan.v1", "order": order,
            "recompute": [name for name in order if name in dirty],
            "reuse_candidates": [name for name in order if name not in dirty],
            "removed": sorted(set(old) - set(new)), "reasons": reasons,
            "execution_authorized": False}
