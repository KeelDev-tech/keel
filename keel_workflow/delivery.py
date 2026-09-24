"""Exact-artifact approval and durable *simulation* delivery metadata.

This module never transmits applications. Approval and reconciliation inputs are
assertions by a trusted host, not authenticated identities or proof of authority.
An eventual host must authenticate those assertions and obtain fresh account,
policy and revocation context immediately before its own execution boundary.
The outbox is for the host's canonical logger; this is not a submitted ledger.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Mapping, Protocol
from dataclasses import dataclass
from datetime import datetime
import uuid


MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_BYTES = 1024 * 1024
REVISION_KEYS = frozenset({"answer", "fact", "evidence", "policy"})
SIMULATION_ACTION = "SIMULATE_SUBMISSION"


class DeliveryError(ValueError):
    """The requested transition is invalid or lacks required evidence."""


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise DeliveryError("Expected finite canonical JSON data") from exc


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise DeliveryError(f"{field} must be a nonempty, unpadded string")
    if len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise DeliveryError(f"Invalid {field}")
    return value


def _time(value: Any) -> float:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DeliveryError("Timestamp must use an aware ISO datetime") from exc
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise DeliveryError("Timestamp datetime must include a timezone")
        value = value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise DeliveryError("Timestamp must be a finite nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise DeliveryError("Timestamp must be a finite nonnegative number")
    return float(value)


def _revisions(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != REVISION_KEYS:
        raise DeliveryError("Revisions require exactly answer, fact, evidence, policy")
    return {key: _text(value[key], f"revision.{key}") for key in sorted(value)}


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_attachment(value: bytes | Path) -> bytes:
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, Path):
        absolute = Path(os.path.abspath(value))
        # Reject symlinks in every path component, then O_NOFOLLOW the final file.
        # A trusted host must also control parent directories against replacement.
        if absolute.resolve(strict=True) != absolute:
            raise DeliveryError("Attachment symlinks are not accepted")
        fd = os.open(absolute, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ATTACHMENT_BYTES:
                raise DeliveryError("Attachment must be a bounded regular file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_ATTACHMENT_BYTES + 1)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise DeliveryError("Attachment changed while snapshotting")
        finally:
            os.close(fd)
    else:
        raise DeliveryError("Attachment input must be bytes or pathlib.Path")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise DeliveryError("Attachment exceeds size limit")
    return data


@dataclass(frozen=True)
class Bundle:
    """Immutable canonical document and exact private copies of attachment bytes."""

    canonical_json: str
    attachments: tuple[tuple[str, bytes], ...]

    @property
    def sha256(self) -> str:
        return _hash(self.canonical_json.encode("utf-8"))

    @property
    def document(self) -> dict[str, Any]:
        return json.loads(self.canonical_json)

    def to_dict(self) -> dict[str, Any]:
        return {"bundle_sha256": self.sha256, "document": self.document,
                "execution_authorized": False}


def build_bundle(*, workspace_id: str, role_id: str, action: str,
                 destination: str, account_id: str, revisions: Mapping[str, str],
                 content: Mapping[str, Any],
                 attachments: Mapping[str, bytes | Path] | None = None) -> Bundle:
    if not isinstance(content, Mapping):
        raise DeliveryError("Content must be a JSON object")
    scope = {key: _text(value, key) for key, value in {
        "workspace_id": workspace_id, "role_id": role_id, "action": action,
        "destination": destination, "account_id": account_id}.items()}
    blobs: list[tuple[str, bytes]] = []
    total = 0
    if attachments is not None and not isinstance(attachments, Mapping):
        raise DeliveryError("Attachments must be a name-to-bytes/path mapping")
    for name, value in sorted((attachments or {}).items()):
        _text(name, "attachment name")
        if name in {".", ".."} or "/" in name or "\\" in name:
            raise DeliveryError("Attachment name must be a plain filename")
        data = _read_attachment(value)
        total += len(data)
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise DeliveryError("Total attachment size exceeds limit")
        blobs.append((name, data))
    document = {"schema": "keel.bundle.v1", **scope,
                "revisions": _revisions(revisions), "content": dict(content),
                "attachments": [{"name": name, "size": len(data), "sha256": _hash(data)}
                                for name, data in blobs]}
    canonical = _json(document)
    if len(canonical.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise DeliveryError("Bundle metadata exceeds size limit")
    return Bundle(canonical, tuple(blobs))


def _validate_bundle(bundle: Bundle) -> dict[str, Any]:
    if type(bundle) is not Bundle or type(bundle.attachments) is not tuple:
        raise DeliveryError("Expected an immutable Bundle")
    try:
        document = bundle.document
        if any(type(item) is not tuple or len(item) != 2 for item in bundle.attachments):
            raise DeliveryError("Malformed attachment snapshot")
        if len(dict(bundle.attachments)) != len(bundle.attachments):
            raise DeliveryError("Duplicate attachment names")
        rebuilt = build_bundle(**{key: document[key] for key in (
            "workspace_id", "role_id", "action", "destination", "account_id",
            "revisions", "content")}, attachments=dict(bundle.attachments))
        if rebuilt != bundle:
            raise DeliveryError("Bundle document and attachment bytes do not match")
        return document
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise DeliveryError("Malformed bundle") from exc


def validate_bundle(bundle: Bundle) -> dict[str, Any]:
    """Recompute all attachment hashes and canonical content; return safe copy."""
    return _validate_bundle(bundle)


class FutureAuthenticatedAdapter(Protocol):
    """Integration shape only; no implementation or invocation is shipped.

    Host implementations must verify exact bytes, fresh identity/authority,
    independent gates and idempotency at their execution boundary. Declaring this
    interface grants no execution permission, and the simulator rejects it.
    """

    def deliver(self, *, bundle: Bundle, attempt_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class SyntheticAdapter:
    """Deterministic local fake; never invokes a connector or network client."""

    mode: str = "confirmed"

    def __post_init__(self) -> None:
        if self.mode not in {"confirmed", "timeout", "not_sent"}:
            raise DeliveryError("Unknown synthetic adapter mode")


class DeliveryStore:
    """SQLite artifact snapshots, host assertions and simulation attempt metadata.

    Use one instance per thread. Multiple instances/processes serialize competing
    reservations with BEGIN IMMEDIATE and a partial unique index. No timeout
    automatically clears UNKNOWN. UNKNOWN also covers a crash before dispatch;
    a trusted reconciliation with explicit evidence must resolve it.
    """

    def __init__(self, db_path: str | Path):
        if str(db_path) in {"", ":memory:"} or str(db_path).startswith("file:"):
            raise DeliveryError("A durable local database path is required")
        path = Path(os.path.abspath(db_path))
        if path.resolve(strict=False) != path:
            raise DeliveryError("Database symlinks are not accepted")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise DeliveryError("Database must be a regular local file")
            # SQLite's WAL/SHM permissions inherit this mode. The trusted host
            # must control the parent directory against file replacement.
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.connection = sqlite3.connect(str(path), timeout=20, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_bundles (
          sha256 TEXT PRIMARY KEY, document TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_attachments (
          bundle_sha256 TEXT NOT NULL REFERENCES workflow_bundles(sha256),
          name TEXT NOT NULL, content BLOB NOT NULL, PRIMARY KEY(bundle_sha256,name));
        CREATE TABLE IF NOT EXISTS workflow_approvals (
          approval_id TEXT PRIMARY KEY, bundle_sha256 TEXT NOT NULL REFERENCES workflow_bundles(sha256),
          approver_id TEXT NOT NULL, authority_ref TEXT NOT NULL,
          created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS workflow_attempts (
          attempt_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, scope_key TEXT NOT NULL,
          bundle_sha256 TEXT NOT NULL REFERENCES workflow_bundles(sha256),
          approval_id TEXT NOT NULL REFERENCES workflow_approvals(approval_id),
          idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
          created_at REAL NOT NULL, receipt_json TEXT, evidence_ref TEXT,
          UNIQUE(workspace_id,idempotency_key));
        CREATE UNIQUE INDEX IF NOT EXISTS workflow_one_unresolved_or_confirmed
          ON workflow_attempts(scope_key) WHERE status IN ('UNKNOWN','SIMULATED_CONFIRMED');
        CREATE TABLE IF NOT EXISTS workflow_events (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
          created_at REAL NOT NULL, data_json TEXT NOT NULL);
        """)

    def close(self) -> None:
        self.connection.close()

    def _event(self, kind: str, now: float, **data: Any) -> None:
        self.connection.execute(
            "INSERT INTO workflow_events(kind,created_at,data_json) VALUES(?,?,?)",
            (kind, now, _json({**data, "simulation": True, "execution_authorized": False})))

    def _remember_bundle(self, bundle: Bundle) -> None:
        existing = self.connection.execute(
            "SELECT document FROM workflow_bundles WHERE sha256=?", (bundle.sha256,)).fetchone()
        if existing:
            if self.load_bundle(bundle.sha256) != bundle:
                raise DeliveryError("Persisted bundle integrity check failed")
            return
        self.connection.execute("INSERT INTO workflow_bundles VALUES(?,?)",
                                (bundle.sha256, bundle.canonical_json))
        self.connection.executemany("INSERT INTO workflow_attachments VALUES(?,?,?)",
                                   ((bundle.sha256, name, data) for name, data in bundle.attachments))

    def load_bundle(self, sha256: str) -> Bundle:
        row = self.connection.execute("SELECT document FROM workflow_bundles WHERE sha256=?", (sha256,)).fetchone()
        if row is None:
            raise DeliveryError("Unknown bundle")
        attachments = self.connection.execute(
            "SELECT name,content FROM workflow_attachments WHERE bundle_sha256=? ORDER BY name", (sha256,)).fetchall()
        bundle = Bundle(row["document"], tuple((r["name"], bytes(r["content"])) for r in attachments))
        _validate_bundle(bundle)
        if bundle.sha256 != sha256:
            raise DeliveryError("Persisted bundle digest mismatch")
        return bundle

    def approve(self, bundle: Bundle, *, approver_id: str, authority_ref: str,
                expires_at: float, now: float) -> dict[str, Any]:
        document = _validate_bundle(bundle)
        if document["action"] != SIMULATION_ACTION:
            raise DeliveryError("This release only records simulation approvals")
        now, expires_at = _time(now), _time(expires_at)
        if expires_at <= now:
            raise DeliveryError("Approval must expire in the future")
        approver_id, authority_ref = _text(approver_id, "approver_id"), _text(authority_ref, "authority_ref")
        approval_id = "approval-" + uuid.uuid4().hex
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._remember_bundle(bundle)
            self.connection.execute("INSERT INTO workflow_approvals VALUES(?,?,?,?,?,?,0)",
                                    (approval_id, bundle.sha256, approver_id, authority_ref, now, expires_at))
            self._event("simulation_approval_recorded", now, approval_id=approval_id, bundle_sha256=bundle.sha256)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return {"approval_id": approval_id, "bundle_sha256": bundle.sha256,
                "approver_id": approver_id, "authority_ref": authority_ref,
                "created_at": now, "expires_at": expires_at, "simulation": True,
                "execution_authorized": False, "authority_authenticated": False}

    def revoke(self, approval_id: str, *, reason: str, now: float) -> None:
        now, reason = _time(now), _text(reason, "reason")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT * FROM workflow_approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None or now < row["created_at"]:
                raise DeliveryError("Unknown approval or invalid revocation time")
            self.connection.execute("UPDATE workflow_approvals SET revoked=1 WHERE approval_id=?", (approval_id,))
            self._event("simulation_approval_revoked", now, approval_id=approval_id, reason=reason)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _reserve(self, bundle: Bundle, *, approval_id: str, idempotency_key: str,
                 current_revisions: Mapping[str, str], current_account_id: str,
                 current_authority_ref: str, now: float) -> tuple[dict[str, Any], bool]:
        document = _validate_bundle(bundle)
        now = _time(now)
        _text(idempotency_key, "idempotency_key")
        if document["action"] != SIMULATION_ACTION:
            raise DeliveryError("Only SIMULATE_SUBMISSION is permitted")
        if _revisions(current_revisions) != document["revisions"]:
            raise DeliveryError("Revision changed; a new bundle and approval are required")
        if _text(current_account_id, "current_account_id") != document["account_id"]:
            raise DeliveryError("Account identity changed")
        _text(current_authority_ref, "current_authority_ref")
        # A changed endpoint or account does not create a new application.
        # The trusted host must provide the stable canonical role identity.
        scope = _hash(_json({key: document[key] for key in (
            "workspace_id", "role_id")}).encode())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            approval = self.connection.execute("SELECT * FROM workflow_approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if approval is None or approval["bundle_sha256"] != bundle.sha256:
                raise DeliveryError("Approval is not bound to this exact bundle")
            if approval["revoked"] or not approval["created_at"] <= now < approval["expires_at"]:
                raise DeliveryError("Approval revoked, expired, or not yet valid")
            if approval["authority_ref"] != current_authority_ref:
                raise DeliveryError("Authority context changed")
            if self.load_bundle(bundle.sha256) != bundle:
                raise DeliveryError("Persisted artifact snapshot changed")
            previous = self.connection.execute(
                "SELECT * FROM workflow_attempts WHERE workspace_id=? AND idempotency_key=?",
                (document["workspace_id"], idempotency_key)).fetchone()
            if previous:
                if previous["bundle_sha256"] != bundle.sha256 or previous["approval_id"] != approval_id:
                    raise DeliveryError("Idempotency key was already bound to different input")
                self.connection.commit()
                return self._attempt_dict(previous), False
            unresolved = self.connection.execute(
                "SELECT attempt_id,status FROM workflow_attempts WHERE scope_key=? AND status IN ('UNKNOWN','SIMULATED_CONFIRMED')",
                (scope,)).fetchone()
            if unresolved:
                raise DeliveryError(f"Destination already has {unresolved['status']} attempt {unresolved['attempt_id']}")
            attempt_id = "attempt-" + uuid.uuid4().hex
            self.connection.execute("INSERT INTO workflow_attempts VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
                                    (attempt_id, document["workspace_id"], scope, bundle.sha256, approval_id,
                                     idempotency_key, "UNKNOWN", now))
            self._event("simulation_attempt_reserved", now, attempt_id=attempt_id,
                        bundle_sha256=bundle.sha256, status="UNKNOWN")
            self.connection.commit()
            return self.get_attempt(attempt_id), True
        except BaseException:
            self.connection.rollback()
            raise

    @staticmethod
    def _attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        receipt_json = result.pop("receipt_json")
        result["receipt"] = json.loads(receipt_json) if receipt_json else None
        result.update(simulation=True, execution_authorized=False, submitted=False)
        return result

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM workflow_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise DeliveryError("Unknown attempt")
        return self._attempt_dict(row)

    def run_simulation(self, bundle: Bundle, *, approval_id: str, idempotency_key: str,
                       adapter: SyntheticAdapter, current_revisions: Mapping[str, str],
                       current_account_id: str, current_authority_ref: str,
                       now: float) -> dict[str, Any]:
        if type(adapter) is not SyntheticAdapter:
            raise DeliveryError("Only the built-in synthetic adapter can run")
        attempt, created = self._reserve(bundle, approval_id=approval_id, idempotency_key=idempotency_key,
                                        current_revisions=current_revisions, current_account_id=current_account_id,
                                        current_authority_ref=current_authority_ref, now=now)
        if not created or adapter.mode == "timeout":
            return attempt
        evidence = "synthetic://" + attempt["attempt_id"] + "/" + adapter.mode
        receipt = self.make_synthetic_receipt(attempt["attempt_id"], evidence_ref=evidence) if adapter.mode == "confirmed" else None
        return self.reconcile(attempt["attempt_id"], outcome=adapter.mode, evidence_ref=evidence,
                              reconciler_id="built-in-simulator", now=now, receipt=receipt)

    def make_synthetic_receipt(self, attempt_id: str, *, evidence_ref: str) -> dict[str, Any]:
        attempt = self.get_attempt(attempt_id)
        bundle = self.load_bundle(attempt["bundle_sha256"])
        document = bundle.document
        return {"receipt_id": "synthetic-" + attempt_id, "attempt_id": attempt_id,
                "bundle_sha256": bundle.sha256, "evidence_ref": _text(evidence_ref, "evidence_ref"),
                **{key: document[key] for key in ("workspace_id", "role_id", "action", "destination", "account_id")},
                "simulation": True, "execution_authorized": False}

    def reconcile(self, attempt_id: str, *, outcome: str, evidence_ref: str,
                  reconciler_id: str, now: float,
                  receipt: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Trusted host boundary, not user/model-callable evidence verification.

        'not_sent' means the host has verified absence of the simulated effect,
        not merely absence of a receipt. A nonempty reference alone does not
        establish that fact. Real delivery integration is intentionally absent.
        """
        now = _time(now)
        evidence_ref, reconciler_id = _text(evidence_ref, "evidence_ref"), _text(reconciler_id, "reconciler_id")
        if outcome not in {"confirmed", "not_sent"}:
            raise DeliveryError("Reconciliation requires confirmed or not_sent evidence")
        if outcome == "not_sent" and receipt is not None:
            raise DeliveryError("Not-sent reconciliation cannot carry a success receipt")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            attempt = self.get_attempt(attempt_id)
            if now < attempt["created_at"]:
                raise DeliveryError("Reconciliation predates attempt")
            target = "SIMULATED_CONFIRMED" if outcome == "confirmed" else "NOT_SENT"
            if outcome == "confirmed":
                expected = self.make_synthetic_receipt(attempt_id, evidence_ref=evidence_ref)
                if not isinstance(receipt, Mapping) or _json(dict(receipt)) != _json(expected):
                    raise DeliveryError("Receipt is not bound to this exact simulation attempt")
            if attempt["status"] == target:
                if attempt["evidence_ref"] != evidence_ref or attempt["receipt"] != receipt:
                    raise DeliveryError("A terminal attempt cannot be overwritten")
                self.connection.commit()
                return attempt
            if attempt["status"] != "UNKNOWN":
                raise DeliveryError("Only UNKNOWN attempts can be reconciled")
            self.connection.execute(
                "UPDATE workflow_attempts SET status=?,receipt_json=?,evidence_ref=? WHERE attempt_id=?",
                (target, _json(dict(receipt)) if receipt is not None else None, evidence_ref, attempt_id))
            self._event("simulation_attempt_reconciled", now, attempt_id=attempt_id,
                        bundle_sha256=attempt["bundle_sha256"], status=target,
                        evidence_ref=evidence_ref, reconciler_id=reconciler_id)
            self.connection.commit()
            return self.get_attempt(attempt_id)
        except BaseException:
            self.connection.rollback()
            raise

    def events(self, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise DeliveryError("after_sequence must be a nonnegative integer")
        rows = self.connection.execute("SELECT * FROM workflow_events WHERE sequence>? ORDER BY sequence", (after_sequence,)).fetchall()
        return [{"sequence": row["sequence"], "kind": row["kind"], "created_at": row["created_at"],
                 "data": json.loads(row["data_json"])} for row in rows]
