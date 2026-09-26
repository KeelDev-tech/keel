"""Contract checks over real DurableHostAdapter SQLite histories.

The finite Loki model describes related rules but is not substituted for the
implementation. These checks bind to the durable/boundary/model source hashes,
read committed database state atomically, and compare real event histories to it.
The database owner can tamper with this evidence: this is not remote attestation.
"""
import hashlib
import json
from pathlib import Path

from security.execution.durable import SQLiteAuthority
from security.execution.host import Denied


MAX_EVENTS = 100000
_DETAIL_FIELDS = frozenset({"digest", "status", "evidence_id", "maximum", "kind", "state",
                           "dispatched_at", "handler_id", "envelope_digest"})


def _hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _pins():
    root = Path(__file__).resolve().parent.parent
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("security/execution/durable.py", "security/execution/boundary.py",
                         "keel_loki/modelcheck.py", "keel_eval/trace_conformance.py")}


class TracedHandler:
    """Trusted-host wrapper recording entry before its fixed handler is called.

    Instrument every handler path if invocation-count coverage is required. The
    probe itself changes no approvals/attempts/budgets. It refuses an uncommitted,
    duplicate, or mismatched dispatch and stores no payload or destination.
    """
    def __init__(self, authority, handler, *, handler_id):
        if type(authority) is not SQLiteAuthority or not callable(handler):
            raise ValueError("canonical_authority_and_handler_required")
        from security.execution.envelope import identifier
        identifier(handler_id)
        self.authority, self.handler, self.handler_id = authority, handler, handler_id

    def __call__(self, envelope):
        with self.authority.transaction() as (db, now):
            row = db.execute("SELECT state,dispatched_at,envelope_digest FROM authority_attempts WHERE attempt_id=?",
                             (envelope.attempt_id,)).fetchone()
            if (row is None or row["state"] != "UNKNOWN" or row["dispatched_at"] is None
                    or row["envelope_digest"] != envelope.digest):
                raise Denied("handler_without_durable_dispatch_commit")
            if db.execute("SELECT 1 FROM authority_events WHERE kind='trace_handler_entered' AND subject_id=?",
                          (envelope.attempt_id,)).fetchone():
                raise Denied("handler_entry_already_recorded")
            self.authority.event(db, "trace_handler_entered", envelope.attempt_id, now,
                                 {"state": row["state"], "dispatched_at": row["dispatched_at"],
                                  "envelope_digest": envelope.digest, "handler_id": self.handler_id})
        # Entry probe is committed first. A crash in the tiny following gap is
        # conservatively counted as an attempted invocation, not a side effect.
        return self.handler(envelope)


def capture_trace(authority):
    """Read a consistent real store snapshot; return pseudonymous, closed data.

    This uses authority.transaction(), which updates its clock watermark. It
    cannot consume an approval or dispatch. Stable identifier hashes permit
    joins but are pseudonyms, not guaranteed anonymization of low-entropy IDs.
    """
    if type(authority) is not SQLiteAuthority:
        raise ValueError("canonical_authority_required")
    with authority.transaction() as (db, now):
        count = db.execute("SELECT count(*) FROM authority_events").fetchone()[0]
        if count > MAX_EVENTS:
            raise ValueError("trace_event_limit_exceeded")
        events = []
        for row in db.execute("SELECT * FROM authority_events ORDER BY sequence"):
            detail = json.loads(row["detail"])
            if type(detail) is not dict:
                raise ValueError("invalid_authority_event_detail")
            # No operator names, cancellation reasons, payloads, context or URLs.
            detail = {key: value for key, value in detail.items() if key in _DETAIL_FIELDS}
            for key in ("evidence_id", "handler_id"):
                if key in detail:
                    detail[key] = _hash(detail[key])
            events.append({"sequence": row["sequence"], "kind": row["kind"],
                           "subject": _hash(row["subject_id"]), "at": row["recorded_at"], "detail": detail})
        attempts = []
        for row in db.execute("SELECT * FROM authority_attempts"):
            attempts.append({"attempt": _hash(row["attempt_id"]), "role": _hash(row["role_id"]),
                             "approval": _hash(row["approval_id"]), "nonce": _hash(row["nonce"]),
                             "digest": row["envelope_digest"], "state": row["state"],
                             "created_at": row["created_at"], "dispatched_at": row["dispatched_at"],
                             "evidence": _hash(row["evidence_id"]) if row["evidence_id"] else None,
                             "budget": _hash(row["budget_scope"])})
        approvals = []
        for row in db.execute("SELECT * FROM authority_approvals"):
            context = json.loads(row["context"])
            subjects = context.get("subjects", []) if row["purpose"] == "envelope" else []
            approvals.append({"approval": _hash(row["approval_id"]), "purpose": row["purpose"],
                              "digest": row["binding"], "used_at": row["used_at"],
                              "issued_at": row["issued_at"], "expires_at": row["expires_at"],
                              "authority_subjects": [{"kind": kind, "subject": _hash(subject)}
                                                     for kind, subject in subjects]})
        budgets = [{"budget": _hash(row["scope"]), "dispatched": row["dispatched"],
                    "maximum": row["max_dispatches"]}
                   for row in db.execute("SELECT * FROM authority_budgets")]
        rate_limited = bool(db.execute("SELECT rate_limited FROM authority_meta WHERE singleton=1").fetchone()[0])
    return {"schema": "keel.implementation.trace.v1", "source_pins": _pins(), "captured_at": now,
            "events": events, "attempts": attempts, "approvals": approvals, "budgets": budgets,
            "rate_limited": rate_limited, "evidence_kind": "LOCAL_CANONICAL_SQLITE_SNAPSHOT",
            "execution_authorized": False}


def check_trace(trace):
    """Check dispatch admission, terminal transitions, binds and budget accounting.

    No result authenticates an external receipt, proves the absence of hidden
    handlers, or establishes exactly-once external side effects.
    """
    if type(trace) is not dict or trace.get("schema") != "keel.implementation.trace.v1":
        raise ValueError("trace_schema_invalid")
    if any(type(trace.get(k)) is not list or len(trace[k]) > MAX_EVENTS
           for k in ("events", "attempts", "approvals", "budgets")):
        raise ValueError("trace_collection_invalid")
    violations = []
    violation_keys = set()
    def bad(code, subject=None):
        item = {"code": code}
        if subject is not None:
            item["subject"] = subject
        key = (code, subject)
        if key not in violation_keys:
            violations.append(item)
            violation_keys.add(key)
    if trace.get("source_pins") != _pins():
        bad("implementation_source_pin_mismatch")
    events, attempts = trace["events"], trace["attempts"]
    sequences = [event["sequence"] for event in events]
    if any(type(n) is not int or n <= 0 for n in sequences) or sequences != sorted(set(sequences)):
        bad("event_sequence_invalid")
    if not events or events[0]["kind"] != "initialized":
        bad("initialization_event_missing")
    timestamps = [e["at"] for e in events]
    if any(type(n) is not int or n < 0 for n in timestamps) or timestamps != sorted(timestamps):
        bad("event_clock_regressed")
    approvals = {row["approval"]: row for row in trace["approvals"]}
    if len(approvals) != len(trace["approvals"]):
        bad("duplicate_approval")
    seen = {"attempt": set(), "approval": set(), "nonce": set()}
    by_subject = {}
    approved_by_subject = {}
    first_429 = None
    revocations = {}
    for event in events:
        by_subject.setdefault(event["subject"], []).append(event)
        if event["kind"] == "envelope_approved":
            approved_by_subject.setdefault(event["subject"], []).append(event)
        if event["kind"] == "rate_429" and first_429 is None:
            first_429 = event["sequence"]
        if event["kind"] == "revoked":
            revocations.setdefault((event["detail"].get("kind"), event["subject"]), event["sequence"])
    dispatches_by_budget = {}
    observed_probes, dispatched = 0, 0
    attempts_by_id = {row["attempt"]: row for row in attempts}
    for row in attempts:
        aid = row["attempt"]
        for key in seen:
            if row[key] in seen[key]:
                bad("reused_" + key, aid)
            seen[key].add(row[key])
        history = by_subject.get(aid, [])
        approval = approvals.get(row["approval"])
        if (approval is None or approval["purpose"] != "envelope" or approval["digest"] != row["digest"]
                or approval["used_at"] is None or approval["used_at"] != row["created_at"]):
            bad("attempt_approval_binding_invalid", aid)
        approved = approved_by_subject.get(row["approval"], [])
        if len(approved) != 1 or approved[0]["detail"].get("digest") != row["digest"]:
            bad("approval_history_invalid", aid)
        state, dispatch_at, evidence = None, None, None
        probe_count, dispatch_count, reserve_count = 0, 0, 0
        for event in history:
            kind, detail = event["kind"], event["detail"]
            if kind in ("attempt_reserved", "dispatch_unknown") and approval:
                if any(revocations.get((s["kind"], s["subject"]), float("inf")) < event["sequence"]
                       for s in approval.get("authority_subjects", [])):
                    bad("admission_after_authority_revocation", aid)
            if kind == "attempt_reserved":
                reserve_count += 1
                if state is not None or detail.get("digest") != row["digest"]:
                    bad("reservation_history_invalid", aid)
                if not approved or approved[0]["sequence"] >= event["sequence"]:
                    bad("reserve_before_approval", aid)
                if approval and not approval["issued_at"] <= event["at"] < approval["expires_at"]:
                    bad("reserve_outside_approval_lifetime", aid)
                state = "RESERVED"
            elif kind == "dispatch_unknown":
                dispatch_count += 1
                if state != "RESERVED":
                    bad("dispatch_from_nonreserved_state", aid)
                if approval and not approval["issued_at"] <= event["at"] < approval["expires_at"]:
                    bad("dispatch_outside_approval_lifetime", aid)
                if first_429 is not None and first_429 < event["sequence"]:
                    bad("dispatch_after_absolute_429", aid)
                state, dispatch_at = "UNKNOWN", event["at"]
            elif kind == "uncertain_commit":
                if state != "RESERVED":
                    bad("uncertain_commit_from_invalid_state", aid)
                state = "UNKNOWN"
            elif kind == "trace_handler_entered":
                probe_count += 1
                if (state != "UNKNOWN" or dispatch_at is None or detail.get("state") != "UNKNOWN"
                        or detail.get("dispatched_at") != dispatch_at or detail.get("envelope_digest") != row["digest"]):
                    bad("handler_before_committed_unknown", aid)
            elif kind == "outcome_recorded":
                if state != "UNKNOWN" or dispatch_at is None or detail.get("status") not in ("SUBMITTED", "NOT_SUBMITTED"):
                    bad("outcome_without_unknown_dispatch", aid)
                state, evidence = detail.get("status"), detail.get("evidence_id")
            elif kind == "predispatch_cancelled":
                if state != "RESERVED" or dispatch_at is not None:
                    bad("cancellation_after_dispatch", aid)
                state, evidence = "NOT_SUBMITTED", detail.get("evidence_id")
            elif kind == "pending_proven_not_dispatched":
                if state != "UNKNOWN" or dispatch_at is not None:
                    bad("invalid_predispatch_recovery", aid)
                state, evidence = "NOT_SUBMITTED", detail.get("evidence_id")
        if reserve_count != 1:
            bad("reservation_count_invalid", aid)
        if dispatch_count > 1:
            bad("duplicate_dispatch", aid)
        if probe_count > 1:
            bad("duplicate_handler_entry", aid)
        if (state != row["state"] or dispatch_at != row["dispatched_at"] or evidence != row["evidence"]):
            bad("event_state_disagrees_with_canonical_attempt", aid)
        if dispatch_at is not None:
            dispatched += 1
            dispatches_by_budget[row["budget"]] = dispatches_by_budget.get(row["budget"], 0) + 1
        observed_probes += probe_count
    attempt_kinds = {"attempt_reserved", "dispatch_unknown", "uncertain_commit", "trace_handler_entered",
                     "outcome_recorded", "predispatch_cancelled", "pending_proven_not_dispatched"}
    for event in events:
        if event["kind"] in attempt_kinds and event["subject"] not in attempts_by_id:
            bad("orphan_attempt_event", event["subject"])
    active_roles, budget_limits, budget_used = {}, {}, {}
    for event in events:
        kind, aid = event["kind"], event["subject"]
        row = attempts_by_id.get(aid)
        if kind == "budget_changed":
            budget_limits[aid] = event["detail"].get("maximum")
        elif row is not None and kind == "attempt_reserved":
            if row["role"] in active_roles:
                bad("overlapping_or_already_submitted_role", aid)
            active_roles[row["role"]] = aid
        elif row is not None and kind == "dispatch_unknown":
            budget = row["budget"]
            maximum = budget_limits.get(budget)
            used = budget_used.get(budget, 0)
            if type(maximum) is not int or used >= maximum:
                bad("dispatch_over_historical_budget", aid)
            budget_used[budget] = used + 1
        elif row is not None and (kind in ("predispatch_cancelled", "pending_proven_not_dispatched")
                                  or kind == "outcome_recorded" and event["detail"].get("status") == "NOT_SUBMITTED"):
            if active_roles.get(row["role"]) == aid:
                del active_roles[row["role"]]
    for budget in trace["budgets"]:
        if budget["dispatched"] != dispatches_by_budget.pop(budget["budget"], 0):
            bad("budget_dispatch_accounting_mismatch", budget["budget"])
        if budget_limits.get(budget["budget"]) != budget["maximum"]:
            bad("budget_limit_history_mismatch", budget["budget"])
        # Current maximum may legitimately be reduced below previous consumption.
    if dispatches_by_budget:
        bad("dispatch_budget_missing")
    if any(e["kind"] == "rate_429" for e in events) and trace.get("rate_limited") is not True:
        bad("absolute_429_forgotten")
    unknown = sum(row["state"] == "UNKNOWN" for row in attempts)
    return {"schema": "keel.implementation.conformance.v1", "status": "BLOCKED" if violations else "CONFORMANT",
            "violations": violations, "attempts": len(attempts), "dispatch_commits": dispatched,
            "handler_entry_probes": observed_probes, "unresolved_unknown": unknown,
            "missing_handler_probe_count": max(0, dispatched - observed_probes),
            "external_side_effects_verified": False, "hidden_handler_paths_excluded": False,
            "source_pins": trace["source_pins"], "execution_authorized": False,
            "scope": "Observed canonical history only; crash gaps are UNKNOWN and cannot authorize retry."}


def inspect_authority(authority):
    return check_trace(capture_trace(authority))
