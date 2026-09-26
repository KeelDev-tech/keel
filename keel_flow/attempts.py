"""Attempt identity, replay checks and intent construction for the canonical log.

No transport, provider verification, retry authorization or alternative store.
"""
from collections import defaultdict, Counter
from .common import (records, text, integer, timestamp, require, digest, hexdigest,
                     clock, boolean, version, keys)
from keel_local.contracts import application_identity

STATES = {"INTENT", "DISPATCHED", "UNKNOWN", "SUBMISSION_CLAIMED",
          "CANCELLED_BEFORE_DISPATCH", "RECONCILIATION_REPORTED", "CONFIRMATION_REPORTED"}
EDGES = {
    "INTENT": {"DISPATCHED", "CANCELLED_BEFORE_DISPATCH"},
    "DISPATCHED": {"UNKNOWN", "SUBMISSION_CLAIMED", "CONFIRMATION_REPORTED"},
    "UNKNOWN": {"RECONCILIATION_REPORTED", "CONFIRMATION_REPORTED"},
    "SUBMISSION_CLAIMED": {"UNKNOWN", "CONFIRMATION_REPORTED"},
    "CANCELLED_BEFORE_DISPATCH": set(), "RECONCILIATION_REPORTED": set(), "CONFIRMATION_REPORTED": set(),
}


def intent(candidate_id, provider, employer_id, posting_id, request, *, nonce, observed_at):
    """Stable application/attempt/event IDs bound to exact proposed content.

    The caller persists this through the EXISTING canonical log before any
    separately authorized dispatch. Constructing an intent grants no authority.
    """
    aid = application_identity(candidate_id, provider, employer_id, posting_id)
    text(nonce); timestamp(observed_at)
    require(type(request) is dict and request, "nonempty request content required")
    content_hash = digest(request)
    attempt_id = digest({"application_id": aid, "nonce": nonce})
    return {"schema_version": 1, "event_id": "flow-" + digest({"attempt_id": attempt_id, "sequence": 0}),
            "application_id": aid, "attempt_id": attempt_id, "sequence": 0, "state": "INTENT",
            "content_hash": content_hash, "observed_at": observed_at,
            "source_ref": "constructed-intent", "execution_authorized": False}


def reconcile(events, *, now, history_complete):
    clock(now); boolean(history_complete)
    seen, attempts, duplicates = {}, defaultdict(list), 0
    for event in records(events, "attempt_events"):
        version(event)
        for key in ("event_id", "attempt_id", "source_ref"):
            text(event.get(key), key)
        hexdigest(event.get("application_id")); hexdigest(event.get("content_hash"))
        integer(event.get("sequence"), maximum=100000)
        require(type(event.get("state")) is str and event["state"] in STATES, "unknown attempt state")
        require(timestamp(event.get("observed_at")) <= now, "future attempt event")
        eid = event["event_id"]
        if eid in seen:
            require(seen[eid] == digest(event), "conflicting replay event_id")
            duplicates += 1; continue
        seen[eid] = digest(event); attempts[event["attempt_id"]].append(event)
    result, findings, by_application = [], [], defaultdict(list)
    for aid, rows in sorted(attempts.items()):
        rows.sort(key=lambda r: r["sequence"])
        require(len({r["sequence"] for r in rows}) == len(rows), "duplicate attempt sequence")
        require(len({r["application_id"] for r in rows}) == 1, "attempt identity changed")
        require(len({r["content_hash"] for r in rows}) == 1, "attempt content changed")
        reasons = []
        if rows[0]["sequence"] != 0 or rows[0]["state"] != "INTENT": reasons.append("MISSING_INITIAL_INTENT")
        for left, right in zip(rows, rows[1:]):
            if right["sequence"] != left["sequence"] + 1: reasons.append("SEQUENCE_GAP")
            if timestamp(right["observed_at"]) < timestamp(left["observed_at"]): reasons.append("CLOCK_ORDER_CONFLICT")
            if right["state"] not in EDGES[left["state"]]: reasons.append("INVALID_TRANSITION")
        last = rows[-1]
        if not history_complete: reasons.append("HISTORY_INCOMPLETE")
        if last["state"] in {"CONFIRMATION_REPORTED", "RECONCILIATION_REPORTED"}:
            reasons.append("TRUSTED_PROVIDER_RECONCILIATION_REQUIRED")
        hold = bool(reasons) or last["state"] != "CANCELLED_BEFORE_DISPATCH"
        row = {"attempt_id": aid, "application_id": last["application_id"], "reported_state": last["state"],
               "last_sequence": last["sequence"], "content_hash": last["content_hash"],
               "hold_required": hold, "reasons": sorted(set(reasons)), "retry_authorized": False,
               "provider_acceptance_verified": False}
        result.append(row); by_application[row["application_id"]].append(row)
        findings.extend({"attempt_id": aid, "reason": reason} for reason in row["reasons"])
    applications = []
    for app, rows in sorted(by_application.items()):
        multiple = sum(bool(r["hold_required"]) for r in rows) > 1
        if multiple: findings.append({"application_id": app, "reason": "MULTIPLE_UNRESOLVED_ATTEMPTS"})
        applications.append({"application_id": app, "attempt_ids": [r["attempt_id"] for r in rows],
                             "hold_required": not history_complete or any(r["hold_required"] for r in rows),
                             "multiple_unresolved_attempts": multiple, "retry_authorized": False})
    return {"attempts": result, "applications": applications, "findings": findings,
            "states": dict(Counter(r["reported_state"] for r in result)),
            "duplicate_events_ignored": duplicates, "history_complete": history_complete,
            "provider_acceptance_verified": False, "queue_writes": 0}
