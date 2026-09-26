"""Local observations with explicit host authentication and immutable payloads.

The database coordinates cooperating trusted host processes. It is neither an
OS sandbox nor a source of submission permission. JSON cannot install verifiers.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

SCHEMA = "keel.observation.v1"
STORE_SCHEMA = "keel.observation-store.v1"
KINDS = frozenset({"source", "opportunity", "application", "attempt", "effort", "outcome", "window", "revocation", "evidence", "measurement"})
ASSURANCES = frozenset({"authenticated_producer", "provider_verified", "human_reported", "host_measured"})
LINEAGE = ("source_id", "opportunity_id", "application_id", "attempt_id")
ENTITIES = ("source", "opportunity", "application", "attempt")
MAX_EVENT_BYTES = 65536
MAX_EXPORT = 10000


class ObservationError(ValueError):
    """Invalid observation, inaccessible storage, or failed trust boundary."""


def require(condition, message):
    if not condition:
        raise ObservationError(message)


def token(value):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value), "invalid_identifier")
    return value


def sha(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "invalid_sha256")
    return value


def timestamp(value):
    require(type(value) is str and len(value) <= 40, "invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.utcoffset() is not None, "timezone_required")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ObservationError("invalid_timestamp") from None


def reference(value):
    require(type(value) is str and 0 < len(value.strip()) <= 2048 and "\x00" not in value, "invalid_evidence_reference")
    return value


def canonical_event(event):
    try:
        encoded = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ObservationError("invalid_json") from None
    require(len(encoded) <= MAX_EVENT_BYTES, "event_too_large")
    return encoded


def event_digest(event):
    return hashlib.sha256(canonical_event(event)).hexdigest()


def _fields(obj, fields):
    require(type(obj) is dict and set(obj) == set(fields.split()), "invalid_fields")


def _number(value, maximum=1000000):
    require(type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value), "invalid_human_minutes_or_score")


def validate_event(event, *, now):
    """Strict, bounded JSON schema. Validation never authenticates a producer."""
    raw = canonical_event(event)
    event = json.loads(raw)
    _fields(event, "schema event_id account_id producer_id kind occurred_at lineage revisions payload")
    require(event["schema"] == SCHEMA and type(event["kind"]) is str and event["kind"] in KINDS, "unsupported_schema_or_kind")
    for name in ("event_id", "account_id", "producer_id"):
        token(event[name])
    occurred = timestamp(event["occurred_at"])
    require(occurred.timestamp() <= now, "future_observation")
    lineage, kind, payload = event["lineage"], event["kind"], event["payload"]
    _fields(lineage, "source_id opportunity_id application_id attempt_id")
    missing = False
    for name in LINEAGE:
        if lineage[name] is None:
            missing = True
        else:
            token(lineage[name])
            require(not missing, "lineage_gap")
    depth = sum(lineage[name] is not None for name in LINEAGE)
    if kind in ENTITIES:
        require(depth == ENTITIES.index(kind) + 1, "invalid_entity_lineage")
    elif kind == "outcome":
        require(depth == 4, "outcome_requires_exact_attempt_lineage")
    elif kind == "revocation":
        require(depth == 0, "revocation_uses_target_event")
    else:
        require(depth > 0, "source_lineage_required")
    revisions = event["revisions"]
    require(type(revisions) is dict and len(revisions) <= 32, "invalid_revisions")
    for name, value in revisions.items():
        require(type(name) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name), "invalid_revision_name")
        sha(value)
    if kind == "source":
        _fields(payload, "name evidence_ref")
        reference(payload["name"])
    elif kind == "opportunity":
        _fields(payload, "qualified evidence_ref")
        require(payload["qualified"] is None or type(payload["qualified"]) is bool, "invalid_qualification")
    elif kind == "application":
        _fields(payload, "submitted_at fit_score evidence_ref")
        if payload["submitted_at"] is not None:
            require(timestamp(payload["submitted_at"]) <= occurred, "future_submission")
        if payload["fit_score"] is not None:
            _number(payload["fit_score"], 100)
    elif kind in ("attempt", "evidence"):
        _fields(payload, "evidence_ref")
    elif kind == "effort":
        _fields(payload, "human_minutes measurement task_ids evidence_ref")
        _number(payload["human_minutes"])
        require(payload["measurement"] in ("human_reported", "host_measured"), "actual_effort_measurement_required")
        require(type(payload["task_ids"]) is list and len(payload["task_ids"]) <= 128, "invalid_task_ids")
        require(len(set(token(item) for item in payload["task_ids"])) == len(payload["task_ids"]), "duplicate_task_id")
    elif kind == "outcome":
        _fields(payload, "outcome evidence_ref provider_receipt_id receipt_sha256")
        require(type(payload["outcome"]) is str and payload["outcome"] in {"SUBMISSION_CONFIRMED", "ACKNOWLEDGMENT", "INTERVIEW", "OFFER", "REJECTION", "ASSESSMENT", "INFO_REQUEST", "OTHER"}, "invalid_outcome")
        if payload["provider_receipt_id"] is not None:
            reference(payload["provider_receipt_id"])
        if payload["receipt_sha256"] is not None:
            sha(payload["receipt_sha256"])
        require((payload["provider_receipt_id"] is None) == (payload["receipt_sha256"] is None), "receipt_binding_incomplete")
    elif kind == "measurement":
        _fields(payload, "target_event_id target_sha256 previous_event_id previous_sha256 values evidence_ref")
        token(payload["target_event_id"]); sha(payload["target_sha256"])
        require(payload["target_event_id"] != event["event_id"], "measurement_cannot_target_itself")
        require((payload["previous_event_id"] is None) == (payload["previous_sha256"] is None), "measurement_predecessor_incomplete")
        if payload["previous_event_id"] is not None:
            token(payload["previous_event_id"]); sha(payload["previous_sha256"])
            require(payload["previous_event_id"] != event["event_id"], "measurement_cannot_precede_itself")
        values = payload["values"]
        require(type(values) is dict, "invalid_measurement_values")
        if set(values) == {"qualified"}:
            require(values["qualified"] is None or type(values["qualified"]) is bool, "invalid_qualification")
        else:
            _fields(values, "submitted_at fit_score")
            if values["submitted_at"] is not None:
                require(timestamp(values["submitted_at"]) <= occurred, "future_submission")
            if values["fit_score"] is not None:
                _number(values["fit_score"], 100)
    elif kind == "window":
        _fields(payload, "window_start window_end observed_through complete evidence_ref")
        require(type(payload["complete"]) is bool, "invalid_completeness")
        start, end, through = (timestamp(payload[name]) for name in ("window_start", "window_end", "observed_through"))
        require(start < end and start <= through <= occurred, "invalid_observation_window")
        require((end - start).total_seconds() <= 180 * 86400, "window_too_long")
        require(not payload["complete"] or end <= through, "incomplete_window_cannot_claim_complete")
    else:
        _fields(payload, "target_event_id target_sha256 reason")
        token(payload["target_event_id"])
        sha(payload["target_sha256"])
        reference(payload["reason"])
        require(payload["target_event_id"] != event["event_id"], "self_revocation")
    if kind != "revocation":
        reference(payload["evidence_ref"])
    return event


@dataclass(frozen=True)
class Verification:
    """Return value from trusted host code; never constructed from uploaded JSON.

    event_sha256 binds exactly the canonical bytes given to the callback.
    Assurance describes what the host established, not what the payload claims.
    """
    account_id: str
    producer_id: str
    subject: str
    event_sha256: str
    allowed_kinds: tuple[str, ...]
    verified_at: float
    expires_at: float
    assurance: str = "authenticated_producer"


def _private(path, *, directory=False):
    metadata = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    require(expected(metadata.st_mode) and not metadata.st_mode & 0o077 and
            (directory or metadata.st_nlink == 1), "storage_must_be_private_and_not_linked")
    require(not hasattr(os, "getuid") or metadata.st_uid == os.getuid(), "storage_owner_mismatch")
    return metadata.st_dev, metadata.st_ino


def _ancestors(path):
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode), "symlink_ancestor_forbidden")
        require(not hasattr(os, "getuid") or info.st_uid in (0, os.getuid()), "untrusted_ancestor_owner")
        require(not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX, "writable_nonsticky_ancestor_forbidden")


def _clock(clock):
    now = clock()
    require(type(now) in (int, float) and 0 <= now < 2**53 and math.isfinite(now), "invalid_clock")
    return now


class ObservationStore:
    """Account-scoped event store. Creation/import has no authentication power."""
    def __init__(self, path, *, store_id, verifier=None, clock=time.time):
        self.path = Path(os.path.abspath(path))
        self.store_id = token(store_id)
        require(verifier is None or callable(verifier), "verifier_must_be_trusted_callable")
        require(callable(clock), "invalid_clock")
        self.verifier, self.clock = verifier, clock
        _ancestors(self.path)
        self._parent = _private(self.path.parent, directory=True)
        self._file = _private(self.path)
        with self._transaction():
            pass

    @classmethod
    def create(cls, path, *, store_id, verifier=None, clock=time.time):
        path = Path(os.path.abspath(path))
        token(store_id)
        require(verifier is None or callable(verifier), "verifier_must_be_trusted_callable")
        _ancestors(path)
        _private(path.parent, directory=True)
        now = _clock(clock)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        db = sqlite3.connect(path, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1),schema TEXT NOT NULL,store_id TEXT NOT NULL,last_now REAL NOT NULL);
            CREATE TABLE observations(account_id TEXT NOT NULL,event_id TEXT NOT NULL,selected_digest TEXT NOT NULL,last_sequence INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(account_id,event_id));
            CREATE TABLE variants(account_id TEXT NOT NULL,event_id TEXT NOT NULL,digest TEXT NOT NULL,document TEXT NOT NULL,verification TEXT,recorded_at REAL NOT NULL,PRIMARY KEY(account_id,event_id,digest));
            CREATE TABLE entities(account_id TEXT NOT NULL,kind TEXT NOT NULL,entity_id TEXT NOT NULL,event_id TEXT NOT NULL,PRIMARY KEY(account_id,kind,entity_id));
            CREATE TABLE measurement_heads(account_id TEXT NOT NULL,target_event_id TEXT NOT NULL,head_event_id TEXT NOT NULL,PRIMARY KEY(account_id,target_event_id));
            CREATE TABLE receipt_bindings(account_id TEXT NOT NULL,receipt_id TEXT NOT NULL,event_id TEXT NOT NULL,binding TEXT NOT NULL,PRIMARY KEY(account_id,receipt_id));
            CREATE TABLE holds(account_id TEXT NOT NULL,event_id TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(account_id,event_id,reason));
            CREATE TABLE changes(sequence INTEGER PRIMARY KEY AUTOINCREMENT,account_id TEXT NOT NULL,event_id TEXT NOT NULL,operation TEXT NOT NULL,recorded_at REAL NOT NULL,detail TEXT NOT NULL);
            CREATE INDEX observations_changed ON observations(account_id,last_sequence);
            """)
            db.execute("INSERT INTO metadata VALUES(1,?,?,?)", (STORE_SCHEMA, store_id, now))
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return cls(path, store_id=store_id, verifier=verifier, clock=clock)

    def _storage(self):
        _ancestors(self.path)
        require(_private(self.path.parent, directory=True) == self._parent and _private(self.path) == self._file, "storage_replaced")
        for suffix in ("-journal", "-wal", "-shm"):
            companion = self.path.with_name(self.path.name + suffix)
            if companion.exists() or companion.is_symlink():
                _private(companion)

    @contextmanager
    def _transaction(self):
        self._storage()
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            meta = db.execute("SELECT schema,store_id,last_now FROM metadata WHERE singleton=1").fetchone()
            require(meta is not None and tuple(meta[:2]) == (STORE_SCHEMA, self.store_id), "store_identity_mismatch")
            now = _clock(self.clock)
            require(now >= meta[2], "clock_regressed")
            db.execute("UPDATE metadata SET last_now=? WHERE singleton=1", (now,))
            yield db, now
            self._storage()
            require(_clock(self.clock) >= now, "clock_regressed")
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _verify(self, event, context, now):
        require(callable(self.verifier), "authenticated_ingestion_requires_host_verifier")
        require(getattr(self.verifier, "store_id", self.store_id) == self.store_id, "verifier_store_binding_mismatch")
        raw = canonical_event(event)
        try:
            decision = self.verifier(raw, context, now)
        except Exception:
            raise ObservationError("host_verification_failed") from None
        require(type(decision) is Verification, "host_verification_required")
        require(decision.account_id == event["account_id"] and decision.producer_id == event["producer_id"] and
                decision.event_sha256 == hashlib.sha256(raw).hexdigest(), "verification_binding_mismatch")
        token(decision.subject)
        require(type(decision.allowed_kinds) is tuple and all(type(kind) is str and kind in KINDS for kind in decision.allowed_kinds) and
                event["kind"] in decision.allowed_kinds, "producer_scope_denied")
        require(type(decision.assurance) is str and decision.assurance in ASSURANCES, "invalid_assurance")
        require(all(type(value) in (int, float) and 0 <= value < 2**53 and math.isfinite(value) for value in (decision.verified_at, decision.expires_at)) and
                timestamp(event["occurred_at"]).timestamp() <= decision.verified_at <= now < decision.expires_at,
                "verification_expired_or_future")
        if event["kind"] == "effort":
            require(decision.assurance == event["payload"]["measurement"], "effort_assurance_mismatch")
        return asdict(decision)

    def _changed(self, db, account, event_id, operation, now, detail):
        cursor = db.execute("INSERT INTO changes(account_id,event_id,operation,recorded_at,detail) VALUES(?,?,?,?,?)",
                            (account, event_id, operation, now, json.dumps(detail, sort_keys=True)))
        db.execute("UPDATE observations SET last_sequence=? WHERE account_id=? AND event_id=?", (cursor.lastrowid, account, event_id))

    def _inspect(self, db, account, event_id, now, seen=None):
        row = db.execute("SELECT o.last_sequence,v.* FROM observations o JOIN variants v ON v.account_id=o.account_id AND v.event_id=o.event_id AND v.digest=o.selected_digest WHERE o.account_id=? AND o.event_id=?", (account, event_id)).fetchone()
        if row is None:
            return None
        event = json.loads(row["document"])
        require(event_digest(event) == row["digest"] and event.get("account_id") == account and event.get("event_id") == event_id, "stored_payload_digest_mismatch")
        verification = json.loads(row["verification"]) if row["verification"] else None
        holds = {r[0] for r in db.execute("SELECT reason FROM holds WHERE account_id=? AND event_id=?", (account, event_id))}
        if verification is not None:
            require(type(verification) is dict and verification.get("event_sha256") == row["digest"] and verification.get("account_id") == account and verification.get("producer_id") == event["producer_id"] and verification.get("assurance") in ASSURANCES, "stored_verification_binding_mismatch")
        trust = "VERIFIED" if verification else "UNVERIFIED"
        if verification is None:
            holds.add("PRODUCER_UNVERIFIED")
        elif now >= verification["expires_at"]:
            holds.add("VERIFICATION_EXPIRED")
        seen = set() if seen is None else seen.copy()
        require(event_id not in seen, "cyclic_lineage")
        seen.add(event_id)
        kind = event["kind"]
        parent_depth = ENTITIES.index(kind) if kind in ENTITIES else (0 if kind == "revocation" else 4)
        for depth, entity_kind in enumerate(ENTITIES[:parent_depth]):
            entity_id = event["lineage"][LINEAGE[depth]]
            if entity_id is None:
                break
            entity = db.execute("SELECT event_id FROM entities WHERE account_id=? AND kind=? AND entity_id=?", (account, entity_kind, entity_id)).fetchone()
            if entity is None:
                holds.add("LINEAGE_UNVERIFIED")
                continue
            parent = self._inspect(db, account, entity[0], now, seen)
            if parent is None or parent["status"] != "CURRENT":
                holds.add("ANCESTOR_HELD")
            elif any(parent["event"]["lineage"][key] != event["lineage"][key] for key in LINEAGE[:depth + 1]):
                holds.add("LINEAGE_MISMATCH")
        result = {"sequence": row["last_sequence"], "event": event, "digest": row["digest"], "trust": trust,
                  "status": "UNVERIFIED" if not verification else "HELD" if holds else "CURRENT",
                  "holds": sorted(holds), "verification": verification}
        if kind == "measurement":
            head = db.execute("SELECT head_event_id FROM measurement_heads WHERE account_id=? AND target_event_id=?",
                              (account, event["payload"]["target_event_id"])).fetchone()
            result["measurement_current"] = bool(head and head[0] == event_id)
        return result

    def _check_lineage(self, db, event, now):
        depth = ENTITIES.index(event["kind"]) if event["kind"] in ENTITIES else (0 if event["kind"] == "revocation" else 4)
        for idx, kind in enumerate(ENTITIES[:depth]):
            ident = event["lineage"][LINEAGE[idx]]
            if ident is None:
                break
            parent = db.execute("SELECT event_id FROM entities WHERE account_id=? AND kind=? AND entity_id=?", (event["account_id"], kind, ident)).fetchone()
            require(parent is not None, "lineage_not_authenticated_for_account")
            record = self._inspect(db, event["account_id"], parent[0], now)
            require(record["status"] == "CURRENT", "ancestor_not_current")
            require(all(record["event"]["lineage"][key] == event["lineage"][key] for key in LINEAGE[:idx + 1]), "lineage_mismatch")
            require(timestamp(record["event"]["occurred_at"]) <= timestamp(event["occurred_at"]), "observation_precedes_ancestor")

    def ingest(self, event, *, context=None, authenticate=False):
        """Persist one event atomically. Auth is explicit, never inferred from JSON.

        Same payload replay is idempotent. An authenticated replay can promote an
        identical import or replace its unverified selection while retaining both
        immutable variants. Two authenticated payloads for one ID create a hold.
        """
        require(type(authenticate) is bool, "invalid_authentication_mode")
        with self._transaction() as (db, now):
            event = validate_event(event, now=now)
            account, event_id = event["account_id"], event["event_id"]
            digest, raw = event_digest(event), canonical_event(event).decode("utf-8")
            verification = self._verify(event, context, now) if authenticate else None
            record = db.execute("SELECT selected_digest FROM observations WHERE account_id=? AND event_id=?", (account, event_id)).fetchone()
            variant = db.execute("SELECT verification FROM variants WHERE account_id=? AND event_id=? AND digest=?", (account, event_id, digest)).fetchone()
            authenticated = db.execute("SELECT digest FROM variants WHERE account_id=? AND event_id=? AND verification IS NOT NULL", (account, event_id)).fetchall()
            collision = bool(verification and any(item[0] != digest for item in authenticated))
            if verification and not collision:
                self._check_lineage(db, event, now)
                if event["kind"] == "measurement":
                    payload = event["payload"]
                    target = self._inspect(db, account, payload["target_event_id"], now)
                    require(target is not None and target["status"] == "CURRENT" and target["digest"] == payload["target_sha256"], "measurement_target_not_current")
                    target_kind = target["event"]["kind"]
                    require(target_kind in ("opportunity", "application") and target["event"]["lineage"] == event["lineage"], "measurement_target_lineage_or_kind_mismatch")
                    wanted = {"qualified"} if target_kind == "opportunity" else {"submitted_at", "fit_score"}
                    require(set(payload["values"]) == wanted, "measurement_target_fields_mismatch")
                    if variant is None or variant[0] is None:
                        head = db.execute("SELECT head_event_id FROM measurement_heads WHERE account_id=? AND target_event_id=?", (account, payload["target_event_id"])).fetchone()
                        if head is None:
                            require(payload["previous_event_id"] is None, "measurement_head_changed")
                        else:
                            previous = self._inspect(db, account, head[0], now)
                            require(previous["status"] == "CURRENT" and head[0] == payload["previous_event_id"] and previous["digest"] == payload["previous_sha256"], "measurement_head_changed")
                            require(timestamp(previous["event"]["occurred_at"]) <= timestamp(event["occurred_at"]), "measurement_time_regressed")
                if event["kind"] == "revocation":
                    target = self._inspect(db, account, event["payload"]["target_event_id"], now)
                    require(target is not None and target["digest"] == event["payload"]["target_sha256"], "revocation_target_mismatch")
            if variant is not None and (verification is None or variant[0] == json.dumps(verification, sort_keys=True)):
                result = self._inspect(db, account, event_id, now)
                return {**result, "ingestion": "DUPLICATE"}
            if record is None:
                db.execute("INSERT INTO observations(account_id,event_id,selected_digest) VALUES(?,?,?)", (account, event_id, digest))
            if variant is None:
                db.execute("INSERT INTO variants VALUES(?,?,?,?,?,?)", (account, event_id, digest, raw, json.dumps(verification, sort_keys=True) if verification else None, now))
            elif verification is not None:
                db.execute("UPDATE variants SET verification=? WHERE account_id=? AND event_id=? AND digest=?", (json.dumps(verification, sort_keys=True), account, event_id, digest))
            operation = "IMPORTED" if verification is None else "AUTHENTICATED"
            if verification and not authenticated:
                db.execute("UPDATE observations SET selected_digest=? WHERE account_id=? AND event_id=?", (digest, account, event_id))
            if collision:
                db.execute("INSERT OR IGNORE INTO holds VALUES(?,?,?)", (account, event_id, "AUTHENTICATED_PAYLOAD_CONFLICT"))
                operation = "HELD_CONFLICT"
            elif verification and event["kind"] in ENTITIES:
                entity_id = event["lineage"][LINEAGE[ENTITIES.index(event["kind"])]]
                prior = db.execute("SELECT event_id FROM entities WHERE account_id=? AND kind=? AND entity_id=?", (account, event["kind"], entity_id)).fetchone()
                if prior is not None and prior[0] != event_id:
                    for conflicting_id in (event_id, prior[0]):
                        db.execute("INSERT OR IGNORE INTO holds VALUES(?,?,?)", (account, conflicting_id, "ENTITY_ID_CONFLICT"))
                        self._changed(db, account, conflicting_id, "HELD_CONFLICT", now, {"entity_id": entity_id})
                    operation = "HELD_CONFLICT"
                else:
                    db.execute("INSERT OR IGNORE INTO entities VALUES(?,?,?,?)", (account, event["kind"], entity_id, event_id))
            elif verification and event["kind"] == "measurement" and (variant is None or variant[0] is None):
                db.execute("INSERT INTO measurement_heads VALUES(?,?,?) ON CONFLICT(account_id,target_event_id) DO UPDATE SET head_event_id=excluded.head_event_id", (account, event["payload"]["target_event_id"], event_id))
            elif verification and verification["assurance"] == "provider_verified" and event["kind"] == "outcome" and event["payload"]["provider_receipt_id"] is not None:
                receipt_id = event["payload"]["provider_receipt_id"]
                binding = json.dumps({"lineage": event["lineage"], "outcome": event["payload"]["outcome"],
                                      "content_sha256": event["payload"]["receipt_sha256"]}, sort_keys=True)
                prior = db.execute("SELECT event_id,binding FROM receipt_bindings WHERE account_id=? AND receipt_id=?", (account, receipt_id)).fetchone()
                if prior is not None and prior[0] != event_id:
                    prior_binding = json.loads(prior[1])
                    new_binding = json.loads(binding)
                    prior_current = self._inspect(db, account, prior[0], now)["status"] == "CURRENT"
                    acceptance_upgrade = (prior_current and prior_binding["outcome"] == "ACKNOWLEDGMENT" and new_binding["outcome"] == "SUBMISSION_CONFIRMED" and
                                          prior_binding["lineage"] == new_binding["lineage"] and prior_binding["content_sha256"] == new_binding["content_sha256"])
                    if acceptance_upgrade:
                        db.execute("UPDATE receipt_bindings SET event_id=?,binding=? WHERE account_id=? AND receipt_id=?", (event_id, binding, account, receipt_id))
                    else:
                        reason = "RECEIPT_ID_CONFLICT" if not prior_current or prior[1] != binding else "DUPLICATE_RECEIPT"
                        affected = (event_id, prior[0]) if reason == "RECEIPT_ID_CONFLICT" else (event_id,)
                        for conflicting_id in affected:
                            db.execute("INSERT OR IGNORE INTO holds VALUES(?,?,?)", (account, conflicting_id, reason))
                            self._changed(db, account, conflicting_id, "HELD_CONFLICT", now, {"receipt_id": receipt_id})
                        operation = "HELD_CONFLICT"
                else:
                    db.execute("INSERT OR IGNORE INTO receipt_bindings VALUES(?,?,?,?)", (account, receipt_id, event_id, binding))
            elif verification and event["kind"] == "revocation":
                target_id = event["payload"]["target_event_id"]
                db.execute("INSERT OR IGNORE INTO holds VALUES(?,?,?)", (account, target_id, "REVOKED"))
                self._changed(db, account, target_id, "REVOKED", now, {"by_event_id": event_id})
            self._changed(db, account, event_id, operation, now, {"digest": digest, "verification": verification})
            return {**self._inspect(db, account, event_id, now), "ingestion": operation}

    def inspect(self, account_id, event_id):
        token(account_id); token(event_id)
        with self._transaction() as (db, now):
            return self._inspect(db, account_id, event_id, now)

    def events(self, account_id, *, after_sequence=0, limit=1000):
        """Current projection page ordered by last direct change, account-scoped.

        Revoked/expired ancestors are evaluated at read time. Consumers MUST
        inspect referenced events before use, even when their cursor is current.
        """
        token(account_id)
        require(type(after_sequence) is int and after_sequence >= 0, "invalid_cursor")
        require(type(limit) is int and 1 <= limit <= MAX_EXPORT, "invalid_limit")
        with self._transaction() as (db, now):
            keys = db.execute("SELECT event_id FROM observations WHERE account_id=? AND last_sequence>? ORDER BY last_sequence LIMIT ?", (account_id, after_sequence, limit)).fetchall()
            return [self._inspect(db, account_id, key[0], now) for key in keys]

    def export(self, account_id, *, after_sequence=0, limit=1000):
        rows = self.events(account_id, after_sequence=after_sequence, limit=limit)
        return {"schema": "keel.observation-export.v1", "store_id": self.store_id, "account_id": account_id,
                "events": rows, "next_sequence": rows[-1]["sequence"] if rows else after_sequence,
                "page_full": len(rows) == limit, "execution_authorized": False}
