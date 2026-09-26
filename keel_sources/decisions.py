"""Explicit, local operator decisions over immutable source review packets.

Actor names are supplied by the trusted host; they are not authentication.
Every result remains non-authorizing and makes no source-authenticity claim.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from keel_agent.revisions import (
    PREAPPROVAL_COMPONENTS, SourceError, _canonical, _component,
    _material_record, _time, _token, revision_digest,
)
from .capture import validate_semantics


MAX_REQUEST_SECONDS = 86400
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FLAGS = {"execution_authorized": False, "source_authenticity_verified": False}


def _json(value):
    return _canonical(value).decode("utf-8")


def _expiry(value):
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise SourceError("timestamp_invalid")
        return value.astimezone(timezone.utc)
    return _time(value)


def _schema(conn):
    # executescript would implicitly commit the caller's transaction.
    statements = (
        """CREATE TABLE IF NOT EXISTS source_approval_requests (
            request_id TEXT PRIMARY KEY, scope_json TEXT NOT NULL,
            review_payload_json TEXT NOT NULL, review_sha256 TEXT NOT NULL,
            revisions_json TEXT NOT NULL, generations_json TEXT NOT NULL,
            observations_json TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, approval_generation INTEGER NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS source_approval_decisions (
            request_id TEXT PRIMARY KEY REFERENCES source_approval_requests(request_id),
            decision TEXT NOT NULL CHECK(decision IN ('APPROVE','REJECT')),
            actor_id TEXT NOT NULL, authority_record_ref TEXT NOT NULL,
            reviewed_sha256 TEXT NOT NULL, decided_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, generations_json TEXT NOT NULL,
            approval_generation INTEGER NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS source_approval_revocations (
            request_id TEXT PRIMARY KEY REFERENCES source_approval_decisions(request_id),
            actor_id TEXT NOT NULL, reason TEXT NOT NULL, revoked_at TEXT NOT NULL,
            approval_generation INTEGER NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS source_approval_scope ON source_approval_requests(scope_json)",
    )
    for statement in statements:
        conn.execute(statement)
    for table in ("source_approval_requests", "source_approval_decisions", "source_approval_revocations"):
        for operation in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()} "
                         f"BEFORE {operation} ON {table} BEGIN "
                         "SELECT RAISE(ABORT, 'approval_records_immutable'); END")


def _load(conn, request_id):
    _token(request_id)
    row = conn.execute(
        "SELECT request_id, scope_json, review_payload_json, review_sha256, revisions_json, "
        "generations_json, observations_json, created_at, expires_at, approval_generation "
        "FROM source_approval_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    if row is None:
        raise SourceError("approval_request_not_found")
    request = {"request_id": row[0], "scope": json.loads(row[1]),
            "review_payload": json.loads(row[2]), "review_sha256": row[3],
            "component_revisions": json.loads(row[4]), "source_generations": json.loads(row[5]),
            "source_observations": json.loads(row[6]), "created_at": row[7],
            "expires_at": row[8], "approval_generation": row[9]}
    packet = request["review_payload"]
    if (not hmac.compare_digest(_digest(packet), request["review_sha256"])
            or packet.get("request_id") != request["request_id"]
            or packet.get("scope") != request["scope"]
            or packet.get("component_revisions") != request["component_revisions"]
            or packet.get("created_at") != request["created_at"]
            or packet.get("request_expires_at") != request["expires_at"]):
        raise SourceError("approval_request_integrity_failed")
    return request


def _decision(conn, request_id):
    row = conn.execute(
        "SELECT decision, actor_id, authority_record_ref, reviewed_sha256, decided_at, "
        "expires_at, generations_json, approval_generation FROM source_approval_decisions "
        "WHERE request_id=?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    return {"decision": row[0], "actor_id": row[1], "authority_record_ref": row[2],
            "reviewed_sha256": row[3], "decided_at": row[4], "expires_at": row[5],
            "source_generations": json.loads(row[6]), "approval_generation": row[7]}


def _view(store, conn, request):
    result = dict(request)
    decision = _decision(conn, request["request_id"])
    revoked = conn.execute(
        "SELECT actor_id, reason, revoked_at FROM source_approval_revocations WHERE request_id=?",
        (request["request_id"],)
    ).fetchone()
    current = store.read_scope(conn, request["scope"])
    is_current = current["sources"]["approval"].get("source_ref") == _reference(request["request_id"])
    material_matches, reason = None, None
    if revoked:
        state = "REVOKED"
    elif not is_current:
        state = "SUPERSEDED"
    elif decision:
        state = "APPROVED" if decision["decision"] == "APPROVE" else "REJECTED"
        if state == "APPROVED" and _time(decision["expires_at"]) <= conn.keel_now:
            state = "EXPIRED"
    elif _time(request["expires_at"]) <= conn.keel_now:
        state = "EXPIRED"
    else:
        try:
            _, revisions, material, _, attachments = _material(store, conn, request["scope"], current=current)
            packet = _packet(request["request_id"], request["scope"], request["created_at"], request["expires_at"],
                             revisions, material, attachments)
            material_matches = (revisions == request["component_revisions"] and
                                hmac.compare_digest(_digest(packet), request["review_sha256"]))
            state = "PENDING" if material_matches else "STALE"
            if not material_matches:
                reason = "approval_material_changed"
        except SourceError as error:
            state, reason = "BLOCKED", str(error)
    result.update({"state": state, "is_current": is_current, "decision": decision,
                   "request_actionable": state == "PENDING", "current_material_matches": material_matches,
                   "reason": reason,
                   "revocation": None if revoked is None else {
                       "actor_id": revoked[0], "reason": revoked[1], "revoked_at": revoked[2]},
                   **_FLAGS})
    return result


def _reference(request_id):
    return f"local-approval:{request_id}"


def _material(store, conn, scope, *, current=None):
    current = store.read_scope(conn, scope) if current is None else current
    sources = current["sources"]
    semantic = validate_semantics(sources, scope=current["scope"],
                                  attachment_root=store.attachment_root, now=conn.keel_now)
    if semantic.get("ready_for_approval") is not True:
        raise SourceError("approval_prerequisites_not_ready")
    revisions, material, observations = {}, {}, {}
    for component in PREAPPROVAL_COMPONENTS:
        descriptor = sources[component]
        result = _component(component, descriptor, current["scope"], revisions,
                            store.attachment_root, conn.keel_now)
        if result["status"] != "READY":
            raise SourceError("approval_prerequisites_not_ready")
        revisions[component] = result["revision"]
        # Only operational observation/expiry are excluded from material identity.
        # All other source metadata and all record fields remain visible and bound.
        material[component] = {key: value for key, value in descriptor.items()
                               if key not in ("observed_at", "expires_at")}
        observations[component] = {key: descriptor[key] for key in ("observed_at", "expires_at")}
    attachments = _material_record("attachments", sources["attachments"]["record"],
                                   current["scope"], store.attachment_root)
    if revision_digest(current["scope"], "attachments", sources["attachments"], attachments) != revisions["attachments"]:
        raise SourceError("approval_attachment_changed_during_review")
    return current, revisions, material, observations, attachments


def _packet(request_id, scope, created_at, expires_at, revisions, material, attachments):
    return {"schema": "keel.source_approval_review.v1", "request_id": request_id,
            "scope": scope, "created_at": created_at, "request_expires_at": expires_at,
            "component_revisions": revisions, "sources": material,
            "verified_attachments": attachments, **_FLAGS}


def _digest(packet):
    return hashlib.sha256(_canonical(packet)).hexdigest()


def prepare_request(store, scope, *, expires_at):
    """Explicitly create a request only when six current sources pass semantics.

    The returned review_payload contains complete material; source_observations
    shows the actual freshness metadata, separately from the stable challenge.
    """
    with store.transaction() as conn:
        _schema(conn)
        expiry = _expiry(expires_at)
        if not conn.keel_now < expiry <= conn.keel_now + timedelta(seconds=MAX_REQUEST_SECONDS):
            raise SourceError("approval_request_expiry_invalid")
        current, revisions, material, observations, attachments = _material(store, conn, scope)
        scope = current["scope"]
        previous = conn.execute(
            "SELECT request_id FROM source_approval_requests WHERE scope_json=? ORDER BY rowid DESC LIMIT 1",
            (_json(scope),)
        ).fetchone()
        if previous:
            old = _load(conn, previous[0])
            if old["component_revisions"] == revisions:
                decision = _decision(conn, old["request_id"])
                revoked = conn.execute("SELECT 1 FROM source_approval_revocations WHERE request_id=?",
                                       (old["request_id"],)).fetchone()
                if decision and (decision["decision"] == "REJECT" or
                                 (_time(decision["expires_at"]) > conn.keel_now and not revoked)):
                    raise SourceError("approval_material_already_decided")
                if decision is None and _time(old["expires_at"]) > conn.keel_now:
                    raise SourceError("approval_request_already_pending")
        request_id = "request-" + uuid.uuid4().hex
        created_at = conn.keel_now.isoformat()
        expiry_text = expiry.isoformat()
        packet = _packet(request_id, scope, created_at, expiry_text, revisions, material, attachments)
        challenge = _digest(packet)
        pending = {"source_ref": _reference(request_id), "source_version": "request",
                   "observed_at": created_at, "expires_at": expiry_text,
                   "absence": {"kind": "HUMAN_DECISION_REQUIRED", "decision_request_ref": request_id}}
        written = store.write_source(conn, scope, "approval", pending,
                                     expected_generation=current["generations"]["approval"], allow_approval=True)
        conn.execute(
            "INSERT INTO source_approval_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
            (request_id, _json(scope), _json(packet), challenge, _json(revisions),
             _json(current["generations"]), _json(observations), created_at, expiry_text, written["generation"])
        )
        store.audit_event(conn, "APPROVAL_REQUESTED", scope=scope, component="approval",
                          generation=written["generation"], details={"request_id": request_id,
                                                                     "reviewed_sha256": challenge})
        return _view(store, conn, _load(conn, request_id))


def decide_request(store, request_id, *, decision, actor_id, authority_record_ref,
                   reviewed_sha256, expires_at):
    """Record one explicit host decision; never infer permission from model output."""
    if decision not in ("APPROVE", "REJECT"):
        raise SourceError("approval_decision_invalid")
    _token(actor_id)
    _token(authority_record_ref)
    if not isinstance(reviewed_sha256, str) or not _SHA256.fullmatch(reviewed_sha256):
        raise SourceError("approval_review_digest_invalid")
    with store.transaction() as conn:
        _schema(conn)
        request = _load(conn, request_id)
        if _decision(conn, request_id) is not None:
            raise SourceError("approval_decision_immutable")
        if _time(request["expires_at"]) <= conn.keel_now:
            raise SourceError("approval_request_expired")
        if not hmac.compare_digest(request["review_sha256"], reviewed_sha256):
            raise SourceError("approval_review_digest_mismatch")
        current, revisions, material, observations, attachments = _material(store, conn, request["scope"])
        pending = current["sources"]["approval"]
        if (current["generations"]["approval"] != request["approval_generation"]
                or pending.get("source_ref") != _reference(request_id)
                or pending.get("absence", {}).get("decision_request_ref") != request_id):
            raise SourceError("approval_request_superseded")
        packet = _packet(request_id, request["scope"], request["created_at"], request["expires_at"],
                         revisions, material, attachments)
        if revisions != request["component_revisions"] or not hmac.compare_digest(_digest(packet), reviewed_sha256):
            raise SourceError("approval_material_changed")
        expiry = _expiry(expires_at)
        maximum = min([_time(request["expires_at"])] + [_time(x["expires_at"]) for x in observations.values()])
        if not conn.keel_now < expiry <= maximum:
            raise SourceError("approval_decision_expiry_invalid")
        decided_at = conn.keel_now.isoformat()
        expiry_text = expiry.isoformat()
        record = {"approval_id": request_id, "actor_id": actor_id,
                  "authority_record_ref": authority_record_ref, "decision": decision,
                  "scope": request["scope"], "component_revisions": revisions,
                  "approved_at": decided_at, "decided_at": decided_at,
                  "expires_at": expiry_text, "revoked": False,
                  "reviewed_sha256": reviewed_sha256}
        descriptor = {"source_ref": _reference(request_id), "source_version": "decision",
                      "observed_at": decided_at, "expires_at": expiry_text, "record": record}
        written = store.write_source(conn, request["scope"], "approval", descriptor,
                                     expected_generation=request["approval_generation"], allow_approval=True)
        conn.execute("INSERT INTO source_approval_decisions VALUES (?,?,?,?,?,?,?,?,?)",
                     (request_id, decision, actor_id, authority_record_ref, reviewed_sha256,
                      decided_at, expiry_text, _json(current["generations"]), written["generation"]))
        store.audit_event(conn, "APPROVAL_DECIDED", scope=request["scope"], component="approval",
                          generation=written["generation"], details={"request_id": request_id,
                          "decision": decision, "actor_id": actor_id, "reviewed_sha256": reviewed_sha256,
                          "authority_record_ref": authority_record_ref})
        return _view(store, conn, request)


def revoke_request(store, request_id, *, actor_id, reason):
    """Revoke the current approved request while retaining its immutable decision."""
    _token(actor_id)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 512 or "\x00" in reason:
        raise SourceError("approval_revocation_reason_invalid")
    with store.transaction() as conn:
        _schema(conn)
        request = _load(conn, request_id)
        decision = _decision(conn, request_id)
        if decision is None or decision["decision"] != "APPROVE":
            raise SourceError("approval_not_approved")
        if conn.execute("SELECT 1 FROM source_approval_revocations WHERE request_id=?", (request_id,)).fetchone():
            raise SourceError("approval_already_revoked")
        current = store.read_scope(conn, request["scope"])
        source = current["sources"]["approval"]
        if (source.get("source_ref") != _reference(request_id)
                or current["generations"]["approval"] != decision["approval_generation"]):
            raise SourceError("approval_request_superseded")
        revoked_at = conn.keel_now.isoformat()
        record = {**source["record"], "revoked": True, "revoked_at": revoked_at,
                  "revoked_by": actor_id, "revocation_reason": reason}
        descriptor = {"source_ref": _reference(request_id), "source_version": "revoked",
                      "observed_at": revoked_at,
                      "expires_at": (conn.keel_now + timedelta(days=1)).isoformat(), "record": record}
        written = store.write_source(conn, request["scope"], "approval", descriptor,
                                     expected_generation=decision["approval_generation"], allow_approval=True)
        conn.execute("INSERT INTO source_approval_revocations VALUES (?,?,?,?,?)",
                     (request_id, actor_id, reason, revoked_at, written["generation"]))
        store.audit_event(conn, "APPROVAL_REVOKED", scope=request["scope"], component="approval",
                          generation=written["generation"], details={"request_id": request_id,
                          "actor_id": actor_id, "reason": reason})
        return _view(store, conn, request)


def get_request(store, request_id):
    """Return the complete private review packet and immutable decision history."""
    with store.read_transaction() as conn:
        required = {"source_approval_requests", "source_approval_decisions", "source_approval_revocations"}
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= existing:
            raise SourceError("approval_request_not_found" if not required & existing else "approval_schema_incomplete")
        return _view(store, conn, _load(conn, request_id))
