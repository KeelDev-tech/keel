#!/usr/bin/env python3
"""Genuine human decision capture (Workstream F, Keel review lane).

MECHANISM ONLY. No genuine decision exists yet, and nothing in this module
manufactures one: :func:`record_decision` requires an OPEN decision request
issued by :func:`review.packet.issue_decision_request`, a human actor, an
explicit authority reference, and a reviewed SHA-256 that binds EXACTLY to
the sealed packet.

A recorded decision carries: request ID, decision (APPROVE/REJECT), actor,
authority reference, reviewed SHA-256, timestamp, expiration.

Querying for a decision that does not exist returns HUMAN_DECISION_REQUIRED
-- never an inferred or defaulted answer. There is currently no genuine
decision on file.

Stdlib only. Append-only decision log; no rewrites, no deletes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .packet import (
    PacketError,
    REQUESTS_FILE,
    REVIEW_DIR,
    load_requests,
    read_packet,
)

DECISION_SCHEMA = "keel.review_decision.v1"
DECISIONS_FILE = REVIEW_DIR / "decisions.jsonl"

DECISION_APPROVE = "APPROVE"
DECISION_REJECT = "REJECT"
DECISIONS = (DECISION_APPROVE, DECISION_REJECT)

# Sentinel returned when no genuine decision exists. It is a plain string, not
# an exception, so callers must handle it explicitly -- it can never be
# mistaken for a decision record.
HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"


class DecisionError(ValueError):
    """A decision-capture contract violation. Never silently defaulted."""


class DecisionBindingError(DecisionError):
    """The reviewed SHA-256 does not bind to the sealed packet."""


class HumanDecisionRequired(DecisionError):
    """Raised by require_* helpers when no genuine decision exists."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value, *, field):
    if not isinstance(value, str) or not value:
        raise DecisionError(f"timestamp_invalid: {field}")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DecisionError(f"timestamp_invalid: {field}") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise DecisionError(f"timestamp_invalid: {field}")
    return result.astimezone(timezone.utc)


def _load_decisions(decisions_file: str | Path | None = None) -> list[dict]:
    target = Path(decisions_file) if decisions_file else DECISIONS_FILE
    if not target.exists():
        return []
    decisions = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            decisions.append(json.loads(line))
    return decisions


def _find_request(request_id: str, requests_file=None) -> dict:
    for request in load_requests(requests_file):
        if request.get("request_id") == request_id:
            return request
    raise DecisionError(f"decision_request_not_found: {request_id}")


def record_decision(request_id: str, decision: str, *, actor: str,
                    authority_ref: str, reviewed_sha256: str,
                    expires_at: str,
                    packet_path: str | Path | None = None,
                    requests_file: str | Path | None = None,
                    decisions_file: str | Path | None = None,
                    now: datetime | None = None) -> dict:
    """Record Trent's genuine decision on an OPEN decision request.

    Binding chain (all must hold):
      1. the request exists, is OPEN, and has not expired;
      2. the sealed packet for the request reads back intact
         (tamper check via review.packet.read_packet);
      3. ``reviewed_sha256`` equals BOTH the packet's embedded
         ``packet_sha256`` AND the request's ``packet_sha256``.

    ``authority_ref`` is the explicit reference to Trent's own decision
    (e.g. his message / approval record) -- it is never inferred, and an
    empty authority is rejected. ``expires_at`` bounds the decision's
    validity and must be in the future.

    On success the decision is appended to decisions.jsonl and the request
    is marked DECIDED (rewritten atomically via the requests file).
    """
    if decision not in DECISIONS:
        raise DecisionError(f"decision_must_be_APPROVE_or_REJECT: {decision!r}")
    if not isinstance(actor, str) or not actor.strip():
        raise DecisionError("actor_required: a human actor must be named")
    if not isinstance(authority_ref, str) or not authority_ref.strip():
        raise DecisionError(
            "authority_ref_required: the decision must cite Trent's explicit "
            "authority; approval is never inferred")
    if not isinstance(reviewed_sha256, str) or not reviewed_sha256.strip():
        raise DecisionError("reviewed_sha256_required")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expiry = _parse_time(expires_at, field="expires_at")
    if expiry <= now:
        raise DecisionError("decision_expires_at_must_be_future")

    request = _find_request(request_id, requests_file)
    if request.get("status") != "OPEN":
        raise DecisionError(
            f"decision_request_not_open: {request_id} ({request.get('status')})")
    request_expiry = _parse_time(request["expires_at"], field="request.expires_at")
    if request_expiry <= now:
        raise DecisionError(f"decision_request_expired: {request_id}")

    # Re-read the sealed packet from disk and verify the binding chain.
    packet_id = request.get("packet_id")
    default_path = REVIEW_DIR / f"{packet_id}-review-packet.json"
    packet = read_packet(packet_path or default_path)
    sealed_sha = packet.get("packet_sha256")
    if (reviewed_sha256 != sealed_sha
            or reviewed_sha256 != request.get("packet_sha256")):
        raise DecisionBindingError(
            "reviewed_sha256 does not bind to the sealed packet: "
            "the reviewer did not review THESE exact contents")

    record = {
        "schema": DECISION_SCHEMA,
        "decision_id": f"keel-decision-{uuid.uuid4().hex[:12]}",
        "request_id": request_id,
        "packet_id": packet_id,
        "decision": decision,
        "actor": actor.strip(),
        "authority_ref": authority_ref.strip(),
        "reviewed_sha256": reviewed_sha256,
        "decided_at": now.isoformat(),
        "expires_at": expiry.isoformat(),
    }
    target = Path(decisions_file) if decisions_file else DECISIONS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")

    _close_request(request_id, requests_file)
    return record


def _close_request(request_id: str, requests_file=None) -> None:
    """Mark a request DECIDED by rewriting the requests file atomically."""
    target = Path(requests_file) if requests_file else REQUESTS_FILE
    requests = load_requests(requests_file)
    updated = []
    for item in requests:
        if item.get("request_id") == request_id:
            item = {**item, "status": "DECIDED",
                    "decided_at": _iso_now()}
        updated.append(item)
    tmp = target.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(item, sort_keys=True) + "\n"
                           for item in updated), encoding="utf-8")
    tmp.replace(target)


def list_genuine_decisions(decisions_file: str | Path | None = None) -> list[dict]:
    """All genuine decisions on file. Currently: none."""
    return _load_decisions(decisions_file)


def decision_status(request_id: str, *, requests_file=None,
                    decisions_file=None) -> str:
    """HUMAN_DECISION_REQUIRED while no genuine decision exists for the
    request; otherwise DECIDED_APPROVE / DECIDED_REJECT."""
    request = _find_request(request_id, requests_file)
    if request.get("status") == "OPEN":
        decided = [d for d in _load_decisions(decisions_file)
                   if d.get("request_id") == request_id]
        if not decided:
            return HUMAN_DECISION_REQUIRED
    for record in _load_decisions(decisions_file):
        if record.get("request_id") == request_id:
            return f"DECIDED_{record['decision']}"
    return HUMAN_DECISION_REQUIRED


def require_genuine_decision(request_id: str, *, requests_file=None,
                             decisions_file=None) -> dict:
    """Return the genuine decision record, or raise HumanDecisionRequired."""
    for record in _load_decisions(decisions_file):
        if record.get("request_id") == request_id:
            return record
    raise HumanDecisionRequired(
        f"{HUMAN_DECISION_REQUIRED}: no genuine decision on file for "
        f"{request_id}; the decision belongs to Trent only")
