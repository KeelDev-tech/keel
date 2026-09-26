"""Opt-in canonical SQLite authority; no identity authentication or transport.

Only a trusted, isolated host may own this database and the callbacks. Existing
controllers must be cut over explicitly, or implement HostAdapter against their
own canonical transaction. This module never imports simulation approvals.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from typing import Callable

from .envelope import ActionEnvelope, identifier
from .host import Denied, HostAdapter, Outcome, Reservation

SCHEMA = "keel.canonical-authority.v1"
SUBJECT_KINDS = frozenset({"identity", "delegation", "credential", "approval"})


def _int(value, name):
    if type(value) is not int or value < 0:
        raise Denied("invalid_" + name)
    return value


def _clock_value(clock):
    raw = clock()
    if type(raw) not in (int, float) or not math.isfinite(raw) or not 0 <= raw < 2**53:
        raise Denied("invalid_authority_clock")
    return int(raw)


def _no_symlink_ancestors(path):
    for parent in path.parents:
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise Denied("authority_ancestor_must_not_be_symlink")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _private(path, *, directory=False):
    metadata = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(metadata.st_mode) or metadata.st_mode & 0o077
            or not directory and metadata.st_nlink != 1):
        raise Denied("authority_storage_must_be_private_and_not_symlink")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise Denied("authority_storage_owner_mismatch")
    return metadata.st_dev, metadata.st_ino


@dataclass(frozen=True)
class HostValidation:
    """Fresh host observation, returned by trusted code, not supplied by workers.

    The host verifies real actor/session, full exact approval context, role,
    source/form/content/destination, consent, lease, policy and revocation state.
    A checked_at timestamp or identity string is not authentication itself.
    """
    actor: str
    attempt_id: str
    envelope_digest: str
    policy_revision: str
    authority_revision: str
    checked_at: int
    expires_at: int
    lease_until: int
    consent: bool
    destination_current: bool
    content_current: bool
    safe_mode: bool
    no_ai: bool
    rate_limited: bool
    budget_scope: str
    # Every delegated authority and credential used must be included, not secrets.
    authority_subjects: tuple[tuple[str, str], ...] = ()


class SQLiteAuthority:
    """Explicitly selected sole canonical authority for participating workers.

    Opening is passive: no tables/files/default grants are created. create()
    requires authenticated operator authorization through the host callback.
    This protects consistency between cooperating processes, not from a hostile
    process with the same OS privileges or from rollback of the whole database.
    """
    def __init__(self, path, *, canonical_store_id: str,
                 operator_validator: Callable, clock: Callable = time.time):
        if not callable(operator_validator) or not callable(clock):
            raise ValueError("trusted_operator_validator_and_clock_required")
        identifier(canonical_store_id)
        self.path = Path(path).absolute()
        _no_symlink_ancestors(self.path)
        self.store_id = canonical_store_id
        self.operator_validator = operator_validator
        self.clock = clock
        self._parent_id = _private(self.path.parent, directory=True)
        self._file_id = _private(self.path)
        with self.transaction() as (db, now):
            meta = db.execute("SELECT schema,store_id FROM authority_meta WHERE singleton=1").fetchone()
            if meta is None or tuple(meta) != (SCHEMA, canonical_store_id):
                raise Denied("canonical_store_identity_mismatch")

    @classmethod
    def create(cls, path, *, canonical_store_id, operator_validator,
               operator_context, clock=time.time):
        if not callable(operator_validator) or not callable(clock):
            raise ValueError("trusted_operator_validator_and_clock_required")
        identifier(canonical_store_id)
        path = Path(path).absolute()
        _no_symlink_ancestors(path)
        _private(path.parent, directory=True)
        now = _clock_value(clock)
        request = {"operation": "initialize_canonical_authority", "store_id": canonical_store_id,
                   "path": str(path)}
        principal = operator_validator(request, operator_context, now)
        if type(principal) is not str or not principal.strip():
            raise Denied("operator_authentication_required")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        db = sqlite3.connect(path, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE authority_meta (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema TEXT NOT NULL,
              store_id TEXT NOT NULL, last_now INTEGER NOT NULL, rate_limited INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE authority_approvals (
              approval_id TEXT PRIMARY KEY, purpose TEXT NOT NULL, actor TEXT NOT NULL,
              action TEXT NOT NULL, binding TEXT NOT NULL, approver TEXT NOT NULL,
              issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
              used_at INTEGER, revoked INTEGER NOT NULL DEFAULT 0, context TEXT NOT NULL);
            CREATE INDEX authority_approval_lookup ON authority_approvals(purpose,actor,action,binding);
            CREATE TABLE authority_attempts (
              attempt_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, actor TEXT NOT NULL,
              envelope_digest TEXT NOT NULL, approval_id TEXT NOT NULL UNIQUE,
              nonce TEXT NOT NULL UNIQUE, reservation_id TEXT NOT NULL UNIQUE,
              budget_scope TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('RESERVED','UNKNOWN','SUBMITTED','NOT_SUBMITTED')),
              created_at INTEGER NOT NULL, dispatched_at INTEGER, evidence_id TEXT,
              FOREIGN KEY(approval_id) REFERENCES authority_approvals(approval_id));
            CREATE INDEX authority_role_state ON authority_attempts(role_id,state);
            CREATE TABLE authority_revocations (
              kind TEXT NOT NULL, subject_id TEXT NOT NULL, revoked_at INTEGER NOT NULL,
              reason TEXT NOT NULL, PRIMARY KEY(kind,subject_id));
            CREATE TABLE authority_budgets (
              scope TEXT PRIMARY KEY, max_dispatches INTEGER NOT NULL CHECK(max_dispatches>=0),
              dispatched INTEGER NOT NULL DEFAULT 0 CHECK(dispatched>=0));
            CREATE TABLE authority_events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
              subject_id TEXT NOT NULL, recorded_at INTEGER NOT NULL, detail TEXT NOT NULL);
            """)
            db.execute("INSERT INTO authority_meta(singleton,schema,store_id,last_now) VALUES(1,?,?,?)",
                       (SCHEMA, canonical_store_id, now))
            db.execute("INSERT INTO authority_events(kind,subject_id,recorded_at,detail) VALUES(?,?,?,?)",
                       ("initialized", canonical_store_id, now, _json({"operator": principal})))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return cls(path, canonical_store_id=canonical_store_id,
                   operator_validator=operator_validator, clock=clock)

    def _storage(self):
        _no_symlink_ancestors(self.path)
        if (_private(self.path.parent, directory=True) != self._parent_id
                or _private(self.path) != self._file_id):
            raise Denied("authority_storage_replaced")
        for suffix in ("-journal", "-wal", "-shm"):
            companion = self.path.with_name(self.path.name + suffix)
            if companion.exists() or companion.is_symlink():
                _private(companion)

    def now(self, db):
        now = _clock_value(self.clock)
        previous = db.execute("SELECT last_now FROM authority_meta WHERE singleton=1").fetchone()
        if previous is None or now < previous[0]:
            raise Denied("authority_clock_regressed")
        db.execute("UPDATE authority_meta SET last_now=? WHERE singleton=1", (now,))
        return now

    @contextmanager
    def transaction(self):
        self._storage()
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                             isolation_level=None, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            meta = db.execute("SELECT schema,store_id FROM authority_meta WHERE singleton=1").fetchone()
            if meta is None or tuple(meta) != (SCHEMA, self.store_id):
                raise Denied("canonical_store_identity_mismatch")
            now = self.now(db)  # Observe time after waiting for the writer lock.
            yield db, now
            self._storage()
            self.now(db)
            db.commit()
        except BaseException:
            # Preserve only the observed clock watermark after rolling back an
            # unsuccessful operation; a later clock regression must not revive
            # an approval that already failed expiry checks in another process.
            observed = None
            if db.in_transaction:
                try:
                    row = db.execute("SELECT last_now,schema,store_id FROM authority_meta WHERE singleton=1").fetchone()
                    if row is not None and tuple(row)[1:] == (SCHEMA, self.store_id):
                        observed = row[0]
                except sqlite3.Error:
                    pass
                db.rollback()
            if observed is not None:
                try:
                    self._storage()
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("UPDATE authority_meta SET last_now=MAX(last_now,?) WHERE singleton=1", (observed,))
                    db.commit()
                except (OSError, sqlite3.Error, Denied):
                    if db.in_transaction:
                        db.rollback()
            raise
        finally:
            db.close()

    def authorize(self, db, request, operator_context):
        principal = self.operator_validator(dict(request), operator_context, self.now(db))
        if type(principal) is not str or not principal.strip():
            raise Denied("operator_authentication_required")
        self.now(db)
        return principal.strip()

    @staticmethod
    def event(db, kind, subject, now, detail):
        db.execute("INSERT INTO authority_events(kind,subject_id,recorded_at,detail) VALUES(?,?,?,?)",
                   (kind, subject, now, _json(detail)))

    @staticmethod
    def check_subjects(db, subjects):
        for kind, subject in subjects:
            if kind not in SUBJECT_KINDS:
                raise Denied("unknown_authority_subject_kind")
            identifier(subject)
            if db.execute("SELECT 1 FROM authority_revocations WHERE kind=? AND subject_id=?",
                          (kind, subject)).fetchone():
                raise Denied("authority_revoked")

    def revoke(self, kind, subject_id, *, reason, operator_context):
        if kind not in SUBJECT_KINDS or type(reason) is not str or not reason.strip():
            raise ValueError("revocation_kind_and_reason_required")
        identifier(subject_id)
        with self.transaction() as (db, now):
            principal = self.authorize(db, {"operation": "revoke", "kind": kind,
                                      "subject_id": subject_id, "reason": reason}, operator_context)
            db.execute("INSERT OR IGNORE INTO authority_revocations VALUES(?,?,?,?)",
                       (kind, subject_id, self.now(db), reason))
            if kind == "approval":
                db.execute("UPDATE authority_approvals SET revoked=1 WHERE approval_id=?", (subject_id,))
            self.event(db, "revoked", subject_id, self.now(db), {"kind": kind, "operator": principal, "reason": reason})

    def set_budget(self, scope, maximum, *, operator_context):
        identifier(scope); _int(maximum, "budget")
        with self.transaction() as (db, now):
            principal = self.authorize(db, {"operation": "set_budget", "scope": scope,
                                           "maximum": maximum}, operator_context)
            db.execute("INSERT INTO authority_budgets(scope,max_dispatches) VALUES(?,?) "
                       "ON CONFLICT(scope) DO UPDATE SET max_dispatches=excluded.max_dispatches",
                       (scope, maximum))
            self.event(db, "budget_changed", scope, self.now(db), {"operator": principal, "maximum": maximum})

    def record_429(self, *, evidence_id, validate_evidence):
        """Sticky absolute stop. No time-based reset or public reset API."""
        identifier(evidence_id)
        if not callable(validate_evidence):
            raise ValueError("trusted_rate_limit_evidence_validator_required")
        with self.transaction() as (db, now):
            if validate_evidence(db, evidence_id, now) is not True:
                raise Denied("rate_limit_evidence_unverified")
            db.execute("UPDATE authority_meta SET rate_limited=1 WHERE singleton=1")
            self.event(db, "rate_429", evidence_id, self.now(db), {})

    def inspect_attempt(self, attempt_id):
        identifier(attempt_id)
        with self.transaction() as (db, now):
            row = db.execute("SELECT * FROM authority_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            return dict(row) if row else None


class DurableHostAdapter(HostAdapter):
    """Exactly-once dispatch admission, not exactly-once external side effects.

    Callbacks execute inside the canonical write transaction and must not invoke
    transports or start nested authority transactions. Validate real host facts
    from this same canonical transaction or a host-owned serialized authority.
    """
    def __init__(self, authority: SQLiteAuthority, *, host_validator,
                 outcome_validator, freshness_seconds=30, no_dispatch_validator=None):
        if type(authority) is not SQLiteAuthority or not callable(host_validator) or not callable(outcome_validator):
            raise ValueError("canonical_authority_and_trusted_validators_required")
        if type(freshness_seconds) is not int or not 1 <= freshness_seconds <= 30:
            raise ValueError("freshness_must_be_1_to_30_seconds")
        if no_dispatch_validator is not None and not callable(no_dispatch_validator):
            raise ValueError("trusted_no_dispatch_validator_required")
        self.no_dispatch_validator = no_dispatch_validator
        self.authority = authority
        self.host_validator = host_validator
        self.outcome_validator = outcome_validator
        self.freshness_seconds = freshness_seconds

    def _fresh(self, db, envelope, phase):
        now = self.authority.now(db)
        value = self.host_validator(db, envelope, phase, now)
        now = self.authority.now(db)  # A blocking validator cannot stretch its TTL.
        if type(value) is not HostValidation:
            raise Denied("trusted_host_validation_required")
        for field in ("checked_at", "expires_at", "lease_until"):
            _int(getattr(value, field), field)
        if (value.actor != envelope.actor or value.attempt_id != envelope.attempt_id
                or value.envelope_digest != envelope.digest
                or value.policy_revision != envelope.policy_revision
                or not value.checked_at <= now < min(value.expires_at, value.lease_until, envelope.expires_at)
                or now - value.checked_at > self.freshness_seconds):
            raise Denied("host_binding_or_freshness_changed")
        for field in ("consent", "destination_current", "content_current"):
            if getattr(value, field) is not True:
                raise Denied("host_gate_not_confirmed")
        for field in ("safe_mode", "no_ai", "rate_limited"):
            if getattr(value, field) is not False:
                raise Denied("host_hold_active")
        identifier(value.authority_revision); identifier(value.budget_scope)
        if type(value.authority_subjects) is not tuple:
            raise Denied("immutable_authority_subjects_required")
        subjects = tuple(sorted(set(value.authority_subjects +
                    (("identity", envelope.actor), ("approval", envelope.approval_id)))))
        self.authority.check_subjects(db, subjects)
        if db.execute("SELECT rate_limited FROM authority_meta WHERE singleton=1").fetchone()[0]:
            raise Denied("absolute_429_hold")
        return {"authority_revision": value.authority_revision, "budget_scope": value.budget_scope,
                "subjects": [list(item) for item in subjects]}, now

    @staticmethod
    def _envelope(envelope, now):
        if type(envelope) is not ActionEnvelope:
            raise Denied("invalid_envelope")
        parsed = ActionEnvelope.from_request(envelope.to_request(), actor=envelope.actor, now=now)
        if parsed != envelope:
            raise Denied("envelope_changed")

    def grant_envelope(self, envelope, *, operator_context):
        """Operator entry point. Envelope already carries a unique approval ID."""
        with self.authority.transaction() as (db, now):
            self._envelope(envelope, now)
            context, now = self._fresh(db, envelope, "grant")
            principal = self.authority.authorize(db, {"operation": "grant_envelope",
                        "approval_id": envelope.approval_id, "envelope_digest": envelope.digest,
                        "expires_at": envelope.expires_at}, operator_context)
            refreshed, now = self._fresh(db, envelope, "grant")
            if refreshed != context:
                raise Denied("host_authority_changed_during_approval")
            db.execute("INSERT INTO authority_approvals VALUES(?,?,?,?,?,?,?,?,NULL,0,?)",
                       (envelope.approval_id, "envelope", envelope.actor, envelope.action,
                        envelope.digest, principal, now, envelope.expires_at, _json(context)))
            self.authority.event(db, "envelope_approved", envelope.approval_id, now,
                                 {"digest": envelope.digest, "operator": principal})

    def _approval(self, db, envelope, phase, *, consumed):
        context, now = self._fresh(db, envelope, phase)
        row = db.execute("SELECT * FROM authority_approvals WHERE approval_id=?", (envelope.approval_id,)).fetchone()
        if (row is None or row["purpose"] != "envelope" or row["binding"] != envelope.digest
                or row["actor"] != envelope.actor or row["action"] != envelope.action
                or row["revoked"] or not row["issued_at"] <= now < row["expires_at"]
                or (row["used_at"] is not None) != consumed or json.loads(row["context"]) != context):
            raise Denied("exact_current_approval_required")
        return context, now

    def reserve(self, envelope, now):
        with self.authority.transaction() as (db, current):
            self._envelope(envelope, current)
            context, current = self._approval(db, envelope, "reserve", consumed=False)
            if db.execute("SELECT 1 FROM authority_attempts WHERE attempt_id=? OR nonce=? OR "
                          "(role_id=? AND state IN ('RESERVED','UNKNOWN','SUBMITTED'))",
                          (envelope.attempt_id, envelope.nonce, envelope.role_id)).fetchone():
                raise Denied("attempt_nonce_or_role_already_reserved")
            budget = db.execute("SELECT * FROM authority_budgets WHERE scope=?", (context["budget_scope"],)).fetchone()
            if budget is None or budget["dispatched"] >= budget["max_dispatches"]:
                raise Denied("dispatch_budget_exhausted_or_unconfigured")
            reservation = Reservation("reservation-" + secrets.token_hex(16), envelope.digest, envelope.attempt_id)
            db.execute("INSERT INTO authority_attempts VALUES(?,?,?,?,?,?,?,?,?, ?,NULL,NULL)",
                       (envelope.attempt_id, envelope.role_id, envelope.actor, envelope.digest,
                        envelope.approval_id, envelope.nonce, reservation.reservation_id,
                        context["budget_scope"], "RESERVED", current))
            db.execute("UPDATE authority_approvals SET used_at=? WHERE approval_id=?", (current, envelope.approval_id))
            self.authority.event(db, "attempt_reserved", envelope.attempt_id, current, {"digest": envelope.digest})
            return reservation

    @staticmethod
    def _attempt(db, reservation, envelope):
        if (type(reservation) is not Reservation or reservation.envelope_digest != envelope.digest
                or reservation.attempt_id != envelope.attempt_id):
            raise Denied("reservation_binding_mismatch")
        row = db.execute("SELECT * FROM authority_attempts WHERE attempt_id=?", (envelope.attempt_id,)).fetchone()
        if (row is None or row["reservation_id"] != reservation.reservation_id
                or row["envelope_digest"] != envelope.digest or row["approval_id"] != envelope.approval_id):
            raise Denied("canonical_reservation_missing_or_changed")
        return row

    def begin_dispatch(self, reservation, envelope, now):
        with self.authority.transaction() as (db, current):
            self._envelope(envelope, current)
            row = self._attempt(db, reservation, envelope)
            if row["state"] != "RESERVED":
                raise Denied("dispatch_may_already_have_happened")
            context, current = self._approval(db, envelope, "dispatch", consumed=True)
            updated = db.execute("UPDATE authority_budgets SET dispatched=dispatched+1 "
                                 "WHERE scope=? AND dispatched<max_dispatches", (context["budget_scope"],))
            if updated.rowcount != 1:
                raise Denied("dispatch_budget_exhausted")
            db.execute("UPDATE authority_attempts SET state='UNKNOWN',dispatched_at=? WHERE attempt_id=?",
                       (current, envelope.attempt_id))
            self.authority.event(db, "dispatch_unknown", envelope.attempt_id, current, {})
            # Context manager commits synchronously before this digest is returned.
            return envelope.digest

    def cancel_reserved(self, reservation, envelope, *, reason, operator_context):
        """Cancel only an attempt that never crossed the durable dispatch commit.

        Consumed approval and nonce stay consumed. UNKNOWN/terminal attempts
        require evidence reconciliation, and cannot use this cancellation path.
        """
        if type(reason) is not str or not reason.strip():
            raise ValueError("cancellation_reason_required")
        with self.authority.transaction() as (db, current):
            row = self._attempt(db, reservation, envelope)
            if row["state"] != "RESERVED" or row["dispatched_at"] is not None:
                raise Denied("only_proven_predispatch_reservations_can_be_cancelled")
            principal = self.authority.authorize(db, {"operation": "cancel_reserved",
                         "attempt_id": envelope.attempt_id, "envelope_digest": envelope.digest,
                         "reason": reason}, operator_context)
            evidence_id = "host-cancel-" + secrets.token_hex(16)
            db.execute("UPDATE authority_attempts SET state='NOT_SUBMITTED',evidence_id=? WHERE attempt_id=?",
                       (evidence_id, envelope.attempt_id))
            self.authority.event(db, "predispatch_cancelled", envelope.attempt_id, self.authority.now(db),
                                 {"operator": principal, "reason": reason, "evidence_id": evidence_id})

    def resolve_pending_not_dispatched(self, reservation, envelope, *, evidence_id,
                                       reason, operator_context):
        """Resolve an uncertain predispatch commit only with independent proof.

        This requires an authenticated operator AND a separately configured host
        proof verifier. Missing receipt, model assertions and elapsed time are
        never evidence that no side effect happened. The old approval stays used.
        """
        identifier(evidence_id)
        if self.no_dispatch_validator is None:
            raise Denied("independent_no_dispatch_verifier_unbound")
        if type(reason) is not str or not reason.strip():
            raise ValueError("reconciliation_reason_required")
        with self.authority.transaction() as (db, current):
            row = self._attempt(db, reservation, envelope)
            if row["state"] != "UNKNOWN" or row["dispatched_at"] is not None:
                raise Denied("only_uncertain_predispatch_commits_use_this_recovery")
            principal = self.authority.authorize(db, {"operation": "resolve_pending_not_dispatched",
                         "attempt_id": envelope.attempt_id, "envelope_digest": envelope.digest,
                         "evidence_id": evidence_id, "reason": reason}, operator_context)
            if self.no_dispatch_validator(db, envelope, evidence_id, self.authority.now(db)) is not True:
                raise Denied("independent_no_dispatch_proof_required")
            db.execute("UPDATE authority_attempts SET state='NOT_SUBMITTED',evidence_id=? WHERE attempt_id=?",
                       (evidence_id, envelope.attempt_id))
            self.authority.event(db, "pending_proven_not_dispatched", envelope.attempt_id, self.authority.now(db),
                                 {"operator": principal, "reason": reason, "evidence_id": evidence_id})

    def record_outcome(self, reservation, envelope, outcome, now):
        if type(outcome) is not Outcome or outcome.status not in {"submitted", "not_submitted"}:
            raise Denied("invalid_outcome")
        identifier(outcome.evidence_id)
        with self.authority.transaction() as (db, current):
            row = self._attempt(db, reservation, envelope)
            target = outcome.status.upper()
            if row["state"] == target and row["evidence_id"] == outcome.evidence_id:
                return  # Exact duplicate is harmless, including after approval expires.
            if row["state"] != "UNKNOWN" or row["dispatched_at"] is None:
                raise Denied("terminal_or_undispatched_attempt_cannot_be_overwritten")
            # Revocation/expiry prevent new dispatch, not recording real late facts.
            if self.outcome_validator(db, envelope, outcome, current) is not True:
                raise Denied("host_evidence_verification_required")
            current = self.authority.now(db)
            db.execute("UPDATE authority_attempts SET state=?,evidence_id=? WHERE attempt_id=?",
                       (target, outcome.evidence_id, envelope.attempt_id))
            self.authority.event(db, "outcome_recorded", envelope.attempt_id, current,
                                 {"status": target, "evidence_id": outcome.evidence_id})

    def note_unknown(self, reservation, envelope, now):
        with self.authority.transaction() as (db, current):
            row = self._attempt(db, reservation, envelope)
            if row["state"] in {"SUBMITTED", "NOT_SUBMITTED", "UNKNOWN"}:
                return
            db.execute("UPDATE authority_attempts SET state='UNKNOWN' WHERE attempt_id=?", (envelope.attempt_id,))
            self.authority.event(db, "uncertain_commit", envelope.attempt_id, current, {})
