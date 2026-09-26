"""Adapter into Keel's existing append-only event logger, not a second store.

Import the installed canonical log_event.log at the receiver and inject it.
This adapter records reports only. It neither dispatches nor authorizes retries.
All producers must preserve the original event on replay. A new nonce creates
a new attempt, not permission to retry an unresolved application.
"""
from .attempts import reconcile
from .common import keys, require, text, digest


def record_event(event, *, logger, role_id, now):
    keys(event, {"schema_version", "event_id", "application_id", "attempt_id", "sequence", "state",
                 "content_hash", "observed_at", "source_ref", "execution_authorized"})
    require(event["execution_authorized"] is False, "attempt event cannot grant execution authority")
    text(role_id)
    reconcile([event], now=now, history_complete=False)
    receipt = logger("flow_attempt", role_id=role_id, source="keel-flow", details=event,
                     event_id=event["event_id"])
    require(type(receipt) is dict and receipt.get("event_type") == "flow_attempt"
            and receipt.get("event_id") == event["event_id"]
            and digest(receipt.get("details")) == digest(event), "canonical logger did not acknowledge exact event")
    return {"event_id": event["event_id"], "recorded": True, "execution_authorized": False,
            "retry_authorized": False, "provider_acceptance_verified": False}


def events_from_log(rows):
    """Extract only this adapter's reports; caller must establish log completeness."""
    from .common import records
    events = []
    for row in records(rows, "canonical_log_rows"):
        if row.get("event_type") == "flow_attempt":
            require(row.get("source") == "keel-flow" and type(row.get("details")) is dict,
                    "unmapped flow event producer")
            require(row.get("event_id") == row["details"].get("event_id"), "event envelope identity mismatch")
            events.append(row["details"])
    return events
