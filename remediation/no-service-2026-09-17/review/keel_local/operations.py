"""Deterministic helpers for current engine adapters; none submit applications."""
from __future__ import annotations
import json
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo
from .contracts import ContractError, strict_json, text, timestamp, digest


def pacific_now():
    return datetime.now(ZoneInfo("America/Los_Angeles"))


def parse_guard_verdict(output):
    try:
        info = strict_json(output)
        if type(info) is not dict:
            raise ContractError("guard object required")
        status, verdict = info.get("status"), info.get("verdict")
        allowed = status == "ACQUIRED" and verdict == "GO"
        holder = info.get("lock") or {}
        if type(holder) is not dict:
            raise ContractError("guard holder invalid")
        reason = f"{status or 'UNKNOWN'}: {verdict or 'UNKNOWN'}"
        if holder.get("task_id"):
            reason += f" holder={holder['task_id']}"
        if info.get("note"):
            reason += f"; {info['note']}"
        return allowed, reason[:500]
    except (ValueError, TypeError):
        # A word GO embedded in a traceback is never authorization.
        return False, "guard_invalid_json"


def queue_entries(document):
    if type(document) is list:
        rows = document
    elif type(document) is dict:
        keys = [k for k in ("entries", "items", "leads") if k in document]
        if len(keys) != 1:
            raise ContractError("ambiguous or missing queue collection")
        rows = document[keys[0]]
    else:
        raise ContractError("invalid queue document")
    if type(rows) is not list or any(type(x) is not dict for x in rows):
        raise ContractError("queue rows must be objects")
    return rows


def buffer_plan(queues, packets):
    """Read-only plan, using packet content identity rather than its filename.

    Any unreadable queue aborts the sweep. Conflicting duplicate rows are held.
    READY is eligible for further checks; IN-FLIGHT is retained, never relaunched.
    """
    by_role = {}
    for name, document in queues.items():
        for entry in queue_entries(document):
            rid = text(entry.get("role_id"), "role_id")
            by_role.setdefault(rid, []).append((name, entry))
    result = []
    for packet in packets:
        rid = packet.get("role_id")
        matches = by_role.get(rid, [])
        reason, action = "eligible_for_readiness_check", "CHECK_READINESS"
        if not matches:
            reason, action = "orphan_packet", "SHELVE"
        elif len(matches) > 1:
            reason, action = "ambiguous_queue_identity", "HOLD"
        else:
            _, entry = matches[0]
            status = entry.get("status")
            if status in {"IN-FLIGHT", "IN_FLIGHT", "INFLIGHT", "LAUNCHING", "STAGED"}:
                reason, action = "already_in_flight", "KEEP_NO_LAUNCH"
            elif status != "READY":
                reason, action = "queue_not_ready", "SHELVE"
            elif (not entry.get("dependency_hash") or
                  packet.get("dependency_hash") != entry.get("dependency_hash")):
                reason, action = "dependencies_stale_or_missing", "REBUILD"
        result.append({"role_id": rid, "action": action, "reason": reason})
    return result


def posting_url(entry):
    for key in ("application_url", "apply_url", "posting_url"):
        value = entry.get(key)
        if type(value) is str and value.strip():
            return value
    return None


def scheduler_drift(expected, observed):
    """Compare authoritative scheduler export, not just files on disk.

    None means the scheduler read failed; it never implies every job vanished.
    """
    if observed is None:
        return {"status": "UNVERIFIED", "missing": [], "changed": []}
    exp, obs = {}, {}
    for rows, target in ((expected, exp), (observed, obs)):
        for row in rows:
            key = text(row.get("id"))
            if key in target:
                raise ContractError("duplicate scheduler ID")
            target[key] = digest({k: row.get(k) for k in ("schedule", "enabled", "command_hash")})
    missing = sorted(set(exp) - set(obs))
    changed = sorted(k for k in exp.keys() & obs.keys() if exp[k] != obs[k])
    return {"status": "DRIFT" if missing or changed else "MATCH", "missing": missing, "changed": changed}


def parked_session_plan(sessions, *, now, age_seconds=900):
    result = []
    for session in sessions:
        action = "KEEP"
        if session.get("state") == "needs_user":
            try:
                age = (now - timestamp(session["updated_at"])).total_seconds()
                if (age >= age_seconds and session.get("lane_routable") is True and
                    session.get("user_takeover") is False and session.get("same_day_deadline") is False and
                    session.get("attempt_state") == "NONE"):
                    action = "PROPOSE_CLOSE"
            except (KeyError, ValueError, TypeError):
                action = "INSPECT_UNKNOWN_STATE"
        result.append({"task_id": session.get("task_id"), "action": action})
    return result


def classify_without_model(reason):
    """No-API fallback for routing only; not an LLM red-team or essay writer."""
    text(reason)
    exact = {"no_packet": "PACKET_BUILD", "fastlane_browser": "BROWSER_HANDOFF",
             "insufficient_quota": "MODEL_UNAVAILABLE", "429": "RATE_LIMITED",
             "no_ai": "NEEDS_USER", "unaided_work": "NEEDS_USER"}
    return {"classification": exact.get(reason, "UNCLASSIFIED"),
            "source": "deterministic_rule", "model_used": False}
