"""Private, durable host state for the local agent; no network or shell execution.

The key authenticates possession by this OS account, not upstream truth or a
human identity. SQLite transactions fence workers and local fixture actions.
An operator who controls the database, key or process controls this boundary.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
from typing import Any

from maintenance_workbench.keel_maint.contracts import canonical, digest, strict_json

SCHEMA_VERSION = 1
JOB_KINDS = frozenset({"READ", "REVIEW", "PREPARE", "SIMULATE", "LOCAL_SUBMIT"})
SAFE_RETRY_KINDS = JOB_KINDS - {"LOCAL_SUBMIT"}
APPROVAL_ACTIONS = frozenset({"PREPARE", "LOCAL_SUBMIT"})
BINDING_FIELDS = frozenset({"role_id", "material_hash", "action", "destination", "account_id"})
SNAPSHOT_FIELDS = frozenset({"schema_version", "workspace_id", "source_revision", "sequence",
    "captured_at", "expires_at", "body_sha256", "previous_sha256", "key_id", "body", "signature"})
MAX_SNAPSHOT_SECONDS = 90
MAX_LEASE_SECONDS = 3600


class StateError(ValueError):
    """Invalid, stale, conflicting or unauthorized local state transition."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StateError(message)


def _text(value: Any, label: str, maximum: int = 2048) -> str:
    _require(type(value) is str and 0 < len(value) <= maximum and value == value.strip()
             and all(ord(c) >= 32 for c in value), f"invalid {label}")
    return value


def _hash(value: Any, label: str = "hash") -> str:
    _require(type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value),
             f"invalid {label}")
    return value


def _time(value: Any) -> float:
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StateError("invalid timestamp") from exc
    if isinstance(value, datetime):
        _require(value.utcoffset() is not None, "timestamp must include timezone")
        value = value.timestamp()
    _require(type(value) in (int, float) and math.isfinite(value) and value >= 0, "invalid timestamp")
    return float(value)


def _iso(value: Any) -> str:
    return datetime.fromtimestamp(_time(value), timezone.utc).isoformat()


def _json(value: Any) -> str:
    try:
        return canonical(value).decode("utf-8")
    except ValueError as exc:
        raise StateError(str(exc)) from exc


def _private_file(path: Path) -> None:
    _require(path.absolute() == path.resolve(strict=True), "symlinks are not allowed for state files")
    info = path.stat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "state must be a regular unlinked file")
    _require(stat.S_IMODE(info.st_mode) == 0o600, "state files must have mode 0600")
    if hasattr(os, "getuid"):
        _require(info.st_uid == os.getuid(), "state file must belong to this OS user")


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require(path.absolute() == path.resolve(strict=True), "symlink directories are not allowed")
    info = path.stat()
    _require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) & 0o077 == 0,
             "state parent directory must be private (0700)")
    if hasattr(os, "getuid"):
        _require(info.st_uid == os.getuid(), "state directory must belong to this OS user")


def _create_private(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)


class LocalState:
    """One private SQLite database and host key per workspace.

    ``now`` is supplied by the trusted host clock, never model output. Mutations
    reject a clock earlier than the last successful transaction. The host key
    and database must be backed up together; loss never silently reprovisions.
    """

    def __init__(self, path: str | Path, workspace_id: str, *, key_path: str | Path | None = None):
        self.path = Path(path).absolute()
        self.workspace_id = _text(workspace_id, "workspace_id", 256)
        self.key_path = Path(key_path).absolute() if key_path else self.path.with_suffix(self.path.suffix + ".key")
        _private_directory(self.path.parent)
        _private_directory(self.key_path.parent)
        existed = self.path.exists() or self.path.is_symlink()
        if existed:
            _private_file(self.path)
            _require(self.key_path.exists(), "existing database has no host key; restore the original key")
        if not self.key_path.exists():
            try:
                _create_private(self.key_path, secrets.token_bytes(32))
            except FileExistsError:
                pass  # A simultaneous constructor provisioned the same key.
        _private_file(self.key_path)
        self._key = self.key_path.read_bytes()
        _require(len(self._key) == 32, "host key must contain exactly 32 random bytes")
        self.key_id = hashlib.sha256(self._key).hexdigest()
        if not existed:
            try:
                _create_private(self.path, b"")
            except FileExistsError:
                pass
        _private_file(self.path)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (
                    sequence INTEGER PRIMARY KEY, source_revision TEXT NOT NULL,
                    envelope_sha256 TEXT NOT NULL UNIQUE, captured_at REAL NOT NULL,
                    expires_at REAL NOT NULL, envelope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                    material_hash TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
                    worker_id TEXT, token INTEGER NOT NULL DEFAULT 0, lease_until REAL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, dispatched_at REAL,
                    result TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY, binding TEXT NOT NULL, created_at REAL NOT NULL,
                    expires_at REAL NOT NULL, revoked_at REAL, consumed_at REAL, consumed_job TEXT);
                CREATE TABLE IF NOT EXISTS role_dispatches (
                    role_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, approval_id TEXT NOT NULL,
                    state TEXT NOT NULL, evidence TEXT, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                    occurred_at REAL NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            expected = {"schema_version": str(SCHEMA_VERSION), "workspace_id": self.workspace_id,
                        "key_id": self.key_id}
            for name, value in expected.items():
                row = db.execute("SELECT value FROM metadata WHERE name=?", (name,)).fetchone()
                if row is not None:
                    _require(row[0] == value, f"database {name} mismatch")
                else:
                    db.execute("INSERT INTO metadata(name,value) VALUES (?,?)", (name, value))
            db.commit()

    def _connect(self):
        _private_file(self.path)
        _private_file(self.key_path)
        _require(hmac.compare_digest(hashlib.sha256(self.key_path.read_bytes()).hexdigest(), self.key_id),
                 "host key changed; refusing state access")
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _transaction(self, now):
        now = _time(now)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._clock(db, now)
            yield db, now
            db.execute("INSERT INTO metadata(name,value) VALUES ('last_now',?) "
                       "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (str(now),))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _clock(db, now):
        row = db.execute("SELECT value FROM metadata WHERE name='last_now'").fetchone()
        _require(row is None or now >= float(row[0]), "host clock rollback detected")

    def _event(self, db, event_type, payload, now):
        event_id = "evt_" + secrets.token_hex(16)
        db.execute("INSERT INTO events(event_id,occurred_at,event_type,payload) VALUES (?,?,?,?)",
                   (event_id, now, event_type, _json(payload)))
        return event_id

    def sign_snapshot(self, body, source_revision, sequence, captured_at, expires_at):
        """Sign a bounded observation; signing does not admit it or authorize work."""
        _text(source_revision, "source_revision", 256)
        _require(type(sequence) is int and 1 <= sequence <= 2**63-1, "invalid snapshot sequence")
        _require(type(body) is dict and {"flow"} <= body.keys() and body.keys() <= {"flow", "assurance", "trust"},
                 "snapshot body requires flow and optional assurance/trust")
        body = strict_json(_json({"flow": body["flow"], "assurance": body.get("assurance"), "trust": body.get("trust")}))
        with closing(self._connect()) as db:
            row = db.execute("SELECT envelope_sha256 FROM snapshots ORDER BY sequence DESC LIMIT 1").fetchone()
        unsigned = {"schema_version": SCHEMA_VERSION, "workspace_id": self.workspace_id,
            "source_revision": source_revision, "sequence": sequence, "captured_at": _iso(captured_at),
            "expires_at": _iso(expires_at), "body_sha256": digest(body),
            "previous_sha256": row[0] if row else None, "key_id": self.key_id, "body": body}
        self._validate_snapshot_body(unsigned, now=_time(captured_at))
        return {**unsigned, "signature": hmac.new(self._key, canonical(unsigned), hashlib.sha256).hexdigest()}

    def _validate_snapshot_body(self, envelope, *, now):
        captured, expiry = _time(envelope["captured_at"]), _time(envelope["expires_at"])
        _require(captured <= now < expiry and 0 < expiry-captured <= MAX_SNAPSHOT_SECONDS,
                 "snapshot stale, future or expiry beyond 90 seconds")
        _require(envelope["workspace_id"] == self.workspace_id, "snapshot workspace mismatch")
        _require(envelope["schema_version"] == SCHEMA_VERSION and type(envelope["schema_version"]) is int,
                 "unsupported snapshot schema")
        body = envelope["body"]
        _require(type(body) is dict and set(body) == {"flow", "assurance", "trust"}, "invalid snapshot body")
        flow = body["flow"]
        _require(type(flow) is dict and flow.get("source_revision") == envelope["source_revision"],
                 "flow source revision mismatch")
        _require(flow.get("complete") is True and flow.get("attempt_history_complete") is True,
                 "complete canonical export and attempt history required")
        _require(0 <= now - _time(flow.get("observed_at")) <= MAX_SNAPSHOT_SECONDS, "flow stale or future")
        from keel_flow.board import build
        # Validate observations separately. The v0.7 scope evaluator binds the
        # reviewed lead's material identity while allowing a fresh operational
        # observation timestamp. The legacy combined board binds that timestamp
        # into lead_sha256 and cannot serve as the v0.7 stable-scope validator.
        report = build(flow, now=datetime.fromtimestamp(now, timezone.utc))
        if body["assurance"] is not None:
            assurance = body["assurance"]
            _require(type(assurance) is dict and assurance.get("export_sha256") == digest(flow)
                     and assurance.get("source_revision") == flow["source_revision"],
                     "assurance canonical export binding mismatch")
            _require(assurance.get("complete") is True and
                     0 <= now - _time(assurance.get("observed_at")) <= MAX_SNAPSHOT_SECONDS,
                     "assurance incomplete, stale or future")
            from keel_assurance.core import evaluate
            evaluate(assurance, now=datetime.fromtimestamp(now, timezone.utc))
        if body["trust"] is not None:
            trust = body["trust"]
            _require(type(trust) is dict and trust.get("workspace_id") == self.workspace_id,
                     "trust workspace mismatch")
            _require(digest(trust.get("flow_export")) == digest(flow), "trust flow binding mismatch")
            _require(trust.get("complete") is True and
                     0 <= now - _time(trust.get("observed_at")) <= MAX_SNAPSHOT_SECONDS,
                     "trust incomplete, stale or future")
            from keel_trust.report import build as trust_report
            trust_report(trust, now=datetime.fromtimestamp(now, timezone.utc))
        return report

    def _verify_snapshot(self, envelope):
        envelope = strict_json(_json(envelope))
        _require(type(envelope) is dict and set(envelope) == SNAPSHOT_FIELDS, "invalid signed snapshot fields")
        signature = _hash(envelope["signature"], "signature")
        _require(envelope["key_id"] == self.key_id, "snapshot key mismatch")
        unsigned = {k: v for k, v in envelope.items() if k != "signature"}
        expected = hmac.new(self._key, canonical(unsigned), hashlib.sha256).hexdigest()
        _require(hmac.compare_digest(expected, signature), "snapshot signature mismatch")
        _require(envelope["body_sha256"] == digest(envelope["body"]), "snapshot body hash mismatch")
        sequence = envelope["sequence"]
        _require(type(sequence) is int and 1 <= sequence <= 2**63-1, "invalid snapshot sequence")
        _text(envelope["source_revision"], "source_revision", 256)
        return envelope

    def import_snapshot(self, envelope, *, now):
        envelope = self._verify_snapshot(envelope)
        sequence = envelope["sequence"]
        with self._transaction(now) as (db, current):
            report = self._validate_snapshot_body(envelope, now=current)
            prior = db.execute("SELECT * FROM snapshots ORDER BY sequence DESC LIMIT 1").fetchone()
            collision = db.execute("SELECT envelope_sha256 FROM snapshots WHERE sequence=?", (sequence,)).fetchone()
            if collision is not None:
                raise StateError("snapshot replay" if collision[0] == digest(envelope) else "snapshot sequence digest conflict")
            _require(sequence == (prior["sequence"] + 1 if prior else 1), "snapshot rollback or missing sequence history")
            _require(envelope["previous_sha256"] == (prior["envelope_sha256"] if prior else None),
                     "snapshot predecessor mismatch")
            if prior:
                _require(_time(envelope["captured_at"]) >= prior["captured_at"], "snapshot capture rollback")
                if envelope["source_revision"] != prior["source_revision"]:
                    _require(db.execute("SELECT 1 FROM snapshots WHERE source_revision=? LIMIT 1",
                                        (envelope["source_revision"],)).fetchone() is None,
                             "source revision rollback")
            db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)", (sequence, envelope["source_revision"],
                digest(envelope), _time(envelope["captured_at"]), _time(envelope["expires_at"]), _json(envelope)))
            self._event(db, "SNAPSHOT_IMPORTED", {"sequence": sequence, "body_sha256": envelope["body_sha256"],
                        "source_revision": envelope["source_revision"], "execution_authorized": False}, current)
            return {"sequence": sequence, "body_sha256": envelope["body_sha256"], "body": envelope["body"],
                    "flow_report": report, "execution_authorized": False}

    def latest_snapshot(self, *, now):
        with closing(self._connect()) as db:
            self._clock(db, _time(now))
            row = db.execute("SELECT envelope,envelope_sha256 FROM snapshots ORDER BY sequence DESC LIMIT 1").fetchone()
        _require(row is not None, "no canonical snapshot imported")
        envelope = self._verify_snapshot(strict_json(row["envelope"]))
        _require(digest(envelope) == row["envelope_sha256"], "stored snapshot digest conflict")
        self._validate_snapshot_body(envelope, now=_time(now))
        return envelope

    def enqueue(self, kind, payload, *, idempotency_key, material_hash, now):
        _require(kind in JOB_KINDS, "job kind is not allowlisted")
        _require(type(payload) is dict, "job payload must be an object")
        _text(idempotency_key, "idempotency_key", 256)
        _hash(material_hash, "material_hash")
        encoded = _json(payload)
        with self._transaction(now) as (db, current):
            row = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                _require((row["kind"], row["payload"], row["material_hash"]) == (kind, encoded, material_hash),
                         "idempotency key material or payload conflict")
                return self._job(row)
            job_id = "job_" + secrets.token_hex(16)
            db.execute("INSERT INTO jobs(job_id,kind,idempotency_key,material_hash,payload,state,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,'QUEUED',?,?)", (job_id, kind, idempotency_key, material_hash, encoded, current, current))
            self._event(db, "JOB_ENQUEUED", {"job_id": job_id, "kind": kind, "material_hash": material_hash}, current)
            return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    @staticmethod
    def _job(row):
        result = dict(row)
        for field in ("payload", "result"):
            if result[field] is not None:
                result[field] = strict_json(result[field])
        return result

    def get_job(self, job_id):
        _text(job_id, "job_id")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        _require(row is not None, "unknown job")
        return self._job(row)

    def claim(self, worker_id, *, now, lease_seconds=60, kinds=None):
        _text(worker_id, "worker_id", 256)
        self._lease(lease_seconds)
        allowed = sorted(JOB_KINDS if kinds is None else set(kinds))
        _require(bool(allowed) and set(allowed) <= JOB_KINDS, "invalid claim kind filter")
        slots = ",".join("?" for _ in allowed)
        retry = ",".join("?" for _ in SAFE_RETRY_KINDS)
        with self._transaction(now) as (db, current):
            row = db.execute(f"SELECT * FROM jobs WHERE kind IN ({slots}) AND "
                f"(state='QUEUED' OR (state='RUNNING' AND lease_until<=? AND "
                f"(kind IN ({retry}) OR (kind='LOCAL_SUBMIT' AND dispatched_at IS NULL)))) "
                "ORDER BY created_at,job_id LIMIT 1", (*allowed, current, *sorted(SAFE_RETRY_KINDS))).fetchone()
            if row is None:
                return None
            token = row["token"] + 1
            db.execute("UPDATE jobs SET state='RUNNING',worker_id=?,token=?,lease_until=?,updated_at=? WHERE job_id=?",
                       (worker_id, token, current+lease_seconds, current, row["job_id"]))
            self._event(db, "JOB_CLAIMED", {"job_id": row["job_id"], "worker_id": worker_id, "token": token}, current)
            return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone())

    @staticmethod
    def _lease(seconds):
        _require(type(seconds) is int and 1 <= seconds <= MAX_LEASE_SECONDS, "invalid lease seconds")

    @staticmethod
    def _owned(db, job_id, token, now, *, allow_dispatched=False):
        _require(type(token) is int and token > 0, "invalid fencing token")
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        states = {"RUNNING", "UNKNOWN"} if allow_dispatched else {"RUNNING"}
        _require(row is not None and row["token"] == token and row["state"] in states and row["lease_until"] > now,
                 "job lease expired, fenced or not running")
        return row

    def heartbeat(self, job_id, token, *, now, lease_seconds=60):
        self._lease(lease_seconds)
        with self._transaction(now) as (db, current):
            self._owned(db, job_id, token, current, allow_dispatched=True)
            db.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE job_id=?", (current+lease_seconds, current, job_id))
            return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def complete(self, job_id, token, result, *, now):
        encoded = _json(result)
        with self._transaction(now) as (db, current):
            row = self._owned(db, job_id, token, current, allow_dispatched=True)
            if row["kind"] == "LOCAL_SUBMIT":
                _require(row["state"] == "UNKNOWN", "local submit was not admitted with approval")
                self._receipt(result, row, "CONFIRMED_LOCAL")
                state = "CONFIRMED_LOCAL"
                db.execute("UPDATE role_dispatches SET state=?,evidence=?,updated_at=? WHERE job_id=?",
                           (state, encoded, current, job_id))
            else:
                _require(row["state"] == "RUNNING", "dispatched action requires explicit reconciliation")
                state = "COMPLETED"
            db.execute("UPDATE jobs SET state=?,result=?,updated_at=?,lease_until=NULL WHERE job_id=?",
                       (state, encoded, current, job_id))
            self._event(db, "JOB_" + state, {"job_id": job_id, "result_sha256": digest(result)}, current)
            return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def fail(self, job_id, token, error, *, now):
        _text(error, "error")
        with self._transaction(now) as (db, current):
            row = self._owned(db, job_id, token, current, allow_dispatched=True)
            state = "UNKNOWN" if row["dispatched_at"] is not None else "FAILED"
            db.execute("UPDATE jobs SET state=?,error=?,updated_at=?,lease_until=NULL WHERE job_id=?",
                       (state, error, current, job_id))
            self._event(db, "JOB_" + state, {"job_id": job_id, "error": error}, current)
            return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    @staticmethod
    def _binding(values):
        _require(type(values) is dict and set(values) == BINDING_FIELDS, "invalid approval binding fields")
        result = {key: _text(value, key) for key, value in values.items()}
        _hash(result["material_hash"], "material_hash")
        _require(result["action"] in APPROVAL_ACTIONS, "approval action is not allowlisted")
        return result

    def approve(self, role_id, material_hash, action, destination, account_id, *, expires_at, now):
        binding = self._binding(dict(role_id=role_id, material_hash=material_hash, action=action,
                                    destination=destination, account_id=account_id))
        expires = _time(expires_at)
        with self._transaction(now) as (db, current):
            _require(current < expires <= current+86400, "approval must expire within 24 hours")
            approval_id = "approval_" + secrets.token_hex(16)
            db.execute("INSERT INTO approvals(approval_id,binding,created_at,expires_at) VALUES (?,?,?,?)",
                       (approval_id, _json(binding), current, expires))
            self._event(db, "APPROVAL_CREATED", {"approval_id": approval_id, "binding": binding}, current)
            return approval_id

    def revoke_approval(self, approval_id, *, now):
        with self._transaction(now) as (db, current):
            row = db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            _require(row is not None, "unknown approval")
            if row["revoked_at"] is None:
                db.execute("UPDATE approvals SET revoked_at=? WHERE approval_id=?", (current, approval_id))
                self._event(db, "APPROVAL_REVOKED", {"approval_id": approval_id}, current)
            return {"approval_id": approval_id, "revoked": True, "already_consumed": row["consumed_at"] is not None}

    def _consume(self, db, approval_id, binding, now, job_id=None):
        row = db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        _require(row is not None and row["created_at"] <= now < row["expires_at"] and row["revoked_at"] is None
                 and row["consumed_at"] is None, "approval missing, expired, revoked or consumed")
        _require(row["binding"] == _json(binding), "approval scope mismatch")
        db.execute("UPDATE approvals SET consumed_at=?,consumed_job=? WHERE approval_id=?", (now, job_id, approval_id))
        self._event(db, "APPROVAL_CONSUMED", {"approval_id": approval_id, "job_id": job_id}, now)

    def consume_approval(self, approval_id, bindings, *, now):
        binding = self._binding(bindings)
        _require(binding["action"] == "PREPARE", "LOCAL_SUBMIT approval requires atomic admit_local_submit")
        with self._transaction(now) as (db, current):
            self._consume(db, approval_id, binding, current)
            return {"approval_id": approval_id, "consumed": True}

    def admit_local_submit(self, job_id, token, approval_id, bindings, *, now):
        """Consume exact approval and reserve role before fixture browser dispatch.

        The host must additionally validate the loopback fixture destination.
        This state transition alone authorizes no internet submission.
        """
        binding = self._binding(bindings)
        _require(binding["action"] == "LOCAL_SUBMIT", "only local fixture submit can be admitted")
        with self._transaction(now) as (db, current):
            row = self._owned(db, job_id, token, current)
            _require(row["kind"] == "LOCAL_SUBMIT" and row["material_hash"] == binding["material_hash"],
                     "job kind or material binding mismatch")
            payload = strict_json(row["payload"])
            _require(all(payload.get(k) == v for k, v in binding.items()), "job payload approval binding mismatch")
            existing = db.execute("SELECT * FROM role_dispatches WHERE role_id=?", (binding["role_id"],)).fetchone()
            _require(existing is None or existing["state"] == "NOT_SENT", "role already dispatched or outcome unknown")
            self._consume(db, approval_id, binding, current, job_id)
            db.execute("INSERT INTO role_dispatches(role_id,job_id,approval_id,state,updated_at) VALUES (?,?,?,'UNKNOWN',?) "
                       "ON CONFLICT(role_id) DO UPDATE SET job_id=excluded.job_id,approval_id=excluded.approval_id,"
                       "state='UNKNOWN',evidence=NULL,updated_at=excluded.updated_at",
                       (binding["role_id"], job_id, approval_id, current))
            db.execute("UPDATE jobs SET state='UNKNOWN',dispatched_at=?,updated_at=? WHERE job_id=?", (current, current, job_id))
            self._event(db, "LOCAL_DISPATCH_ADMITTED", {"job_id": job_id, "approval_id": approval_id,
                        "role_id": binding["role_id"], "material_hash": binding["material_hash"]}, current)
            return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    @staticmethod
    def _receipt(evidence, job, outcome):
        _require(type(evidence) is dict and evidence.get("outcome") == outcome
                 and evidence.get("external_submission") is False, "explicit local-only receipt required")
        _text(evidence.get("evidence_ref"), "evidence_ref")
        _hash(evidence.get("evidence_sha256"), "evidence_sha256")
        _require(evidence.get("job_id") == job["job_id"], "receipt job binding mismatch")
        payload = strict_json(job["payload"])
        _require(all(evidence.get(key) == payload.get(key) for key in BINDING_FIELDS),
                 "receipt material, role, destination, account or action binding mismatch")

    def reconcile_local(self, job_id, evidence, outcome, *, now):
        """Trusted-host reconciliation only; arbitrary model claims are not proof."""
        _require(outcome in {"NOT_SENT", "CONFIRMED_LOCAL"}, "invalid local reconciliation outcome")
        _require(type(evidence) is dict and evidence.get("outcome") == outcome
                 and evidence.get("external_submission") is False, "explicit local reconciliation evidence required")
        _text(evidence.get("evidence_ref"), "evidence_ref")
        with self._transaction(now) as (db, current):
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            _require(row is not None and row["kind"] == "LOCAL_SUBMIT" and row["state"] == "UNKNOWN",
                     "only unknown local dispatch can be reconciled")
            self._receipt(evidence, row, outcome)
            role = db.execute("SELECT * FROM role_dispatches WHERE job_id=?", (job_id,)).fetchone()
            _require(role is not None and role["state"] == "UNKNOWN", "local dispatch role record missing")
            db.execute("UPDATE role_dispatches SET state=?,evidence=?,updated_at=? WHERE job_id=?",
                       (outcome, _json(evidence), current, job_id))
            db.execute("UPDATE jobs SET state=?,result=?,updated_at=?,lease_until=NULL WHERE job_id=?",
                       (outcome, _json(evidence), current, job_id))
            self._event(db, "LOCAL_RECONCILED", {"job_id": job_id, "outcome": outcome,
                        "evidence_sha256": digest(evidence)}, current)
            return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def events(self, *, after=0, limit=100):
        _require(type(after) is int and after >= 0 and type(limit) is int and 1 <= limit <= 1000,
                 "invalid event cursor or limit")
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit)).fetchall()
        return [{**dict(row), "workspace_id": self.workspace_id, "payload": strict_json(row["payload"])} for row in rows]
