"""Exact-ID bridges to Keel's existing observations and conservative reducers."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import math

from .store import (LINEAGE, SCHEMA, MAX_EXPORT, ObservationError, event_digest,
                    require, sha, timestamp, validate_event)


def _envelope(*, event_id, account_id, producer_id, kind, occurred_at, lineage, revisions, payload, now):
    event = {"schema": SCHEMA, "event_id": event_id, "account_id": account_id, "producer_id": producer_id,
             "kind": kind, "occurred_at": occurred_at, "lineage": dict(lineage), "revisions": dict(revisions), "payload": payload}
    return validate_event(event, now=now.timestamp())


def session_effort_event(record, *, report, account_id, producer_id, lineage, now):
    """Validate the original review projection; copy actual effort without trust.

    Explicit lineage is required because one review question may help several
    applications. Shared time must be recorded once, not duplicated per role.
    """
    from keel_muse.review import record_session_effort
    require(type(record) is dict, "session_effort_object_required")
    expected = record_session_effort(report, event_id=record.get("event_id"), question_id=record.get("question_id"),
                                    human_minutes=record.get("human_minutes"), now=record.get("recorded_at"),
                                    expected_report_sha256=record.get("report_sha256"))
    require(record == expected, "session_effort_projection_mismatch")
    occurred_at = datetime.fromtimestamp(record["recorded_at"], timezone.utc).isoformat()
    require(timestamp(occurred_at) <= now, "future_effort")
    return _envelope(event_id=record["event_id"], account_id=account_id, producer_id=producer_id, kind="effort",
                     occurred_at=occurred_at, lineage=lineage,
                     revisions={"review": record["report_sha256"], "snapshot": record["snapshot_sha256"]},
                     payload={"human_minutes": record["human_minutes"], "measurement": "human_reported",
                              "task_ids": [record["question_id"]], "evidence_ref": "review:" + record["report_sha256"]}, now=now)


def receipt_event(receipt, *, claim, event_id, account_id, producer_id, lineage, adapter_revision, now,
                  provider_validators=None, receipt_conflicted=False):
    """Use existing receipt normalization/grading; never guess lineage or trust.

    AUTO_ACK becomes SUBMISSION_CONFIRMED only when an explicit existing provider
    validator establishes acceptance. Store authentication remains separately
    required and must establish provider_verified assurance for cohort use.
    """
    from engines.outcome_tracking.receipt_intake import normalize_receipt, grade_claim
    require(receipt_conflicted is False, "conflicted_receipt_cannot_be_exported")
    sha(adapter_revision)
    clean = normalize_receipt(receipt, now=now)
    mapping = {"role_id": "opportunity_id", "application_id": "application_id", "attempt_id": "attempt_id"}
    require(type(claim) is dict and all(claim.get(key) and claim.get(key) == lineage.get(target) == clean.get(key)
                                      for key, target in mapping.items()), "receipt_exact_lineage_required")
    grade = grade_claim(claim, [(event_digest(clean), clean, False)], now=now, validators=provider_validators)
    require(grade["observed"] and not grade["held"], "receipt_claim_not_observed")
    names = {"AUTO_ACK": "ACKNOWLEDGMENT", "INTERVIEW_INVITE": "INTERVIEW", "REJECTION": "REJECTION",
             "OFFER": "OFFER", "ASSESSMENT": "ASSESSMENT", "INFO_REQUEST": "INFO_REQUEST", "OTHER": "OTHER"}
    outcome = "SUBMISSION_CONFIRMED" if grade["provider_verified"] else names[clean["outcome"]]
    return _envelope(event_id=event_id, account_id=account_id, producer_id=producer_id, kind="outcome",
                     occurred_at=clean["received_at"], lineage=lineage,
                     revisions={"receipt": event_digest(clean), "claim": event_digest(claim), "adapter": adapter_revision},
                     payload={"outcome": outcome, "evidence_ref": "receipt:" + clean["source"] + ":" + clean["receipt_id"],
                              "provider_receipt_id": clean["source"] + ":" + clean["receipt_id"],
                              "receipt_sha256": clean["content_sha256"]}, now=now)


def source_feedback_snapshot(store, account_id, *, source_policies, window_start, window_end,
                             observed_at, now, objective="qualified_leads", horizon_days=14, receipt_store=None):
    """Build and validate a read-only input for engines.source_feedback.propose.

    Policies are explicit host configuration, never inferred from observations.
    Missing/unverified/conflicted measurements cause HOLD. No selection is made
    here: the existing proposal engine validates this snapshot at budget zero.
    """
    from engines.source_feedback import propose
    require(isinstance(now, datetime) and now.utcoffset() is not None, "aware_now_required")
    start, end, observed = (timestamp(value) for value in (window_start, window_end, observed_at))
    require(start < end <= observed <= now, "invalid_measurement_window")
    require(objective in ("qualified_leads", "interviews"), "invalid_objective")
    require(type(horizon_days) is int and 1 <= horizon_days <= 180, "invalid_horizon")
    require(type(source_policies) is list and 0 < len(source_policies) <= 128, "source_policies_required")
    rows = store.events(account_id, limit=MAX_EXPORT)
    if len(rows) == MAX_EXPORT:
        require(not store.events(account_id, after_sequence=rows[-1]["sequence"], limit=1), "snapshot_row_limit")
    holds = set()
    if receipt_store is not None:
        reconciliation = reconcile_receipt_observations(store, receipt_store, account_id=account_id,
                                                        producer_id="receipt-reconciliation", now=now)
        if reconciliation["requires_host_revocations"]:
            holds.add("RECEIPT_RECONCILIATION_REQUIRED")
    current = [row for row in rows if row["status"] == "CURRENT" and timestamp(row["event"]["occurred_at"]) <= observed]
    measurements = {row["event"]["payload"]["target_event_id"]: row for row in current
                    if row["event"]["kind"] == "measurement" and row.get("measurement_current")}
    current = deepcopy(current)
    for row in current:
        update = measurements.get(row["event"]["event_id"])
        if update:
            row["event"]["payload"].update(update["event"]["payload"]["values"])
            row["event"]["payload"]["evidence_ref"] = update["event"]["payload"]["evidence_ref"]
    relevant = {p.get("source_id") for p in source_policies if type(p) is dict}
    for row in rows:
        event = row["event"]
        if event["lineage"]["source_id"] in relevant and event["kind"] != "revocation" and start <= timestamp(event["occurred_at"]) <= observed and row["status"] != "CURRENT":
            holds.add("UNTRUSTED_OR_HELD_MEASUREMENT")
    def latest_window_candidates(candidates):
        if not candidates:
            return []
        latest = max(timestamp(w["event"]["occurred_at"]) for w in candidates)
        latest_rows = [w for w in candidates if timestamp(w["event"]["occurred_at"]) == latest]
        if any(not w["event"]["payload"]["complete"] for w in latest_rows):
            return []
        return latest_rows

    sources = []
    policy_fields = {"source_id", "permitted", "cap_minutes", "cooldown_until", "cooldown_verified", "evidence_ref"}
    windows = [row for row in current if row["event"]["kind"] == "window"]
    for policy in source_policies:
        require(type(policy) is dict and set(policy) == policy_fields, "invalid_source_policy_fields")
        sid = policy["source_id"]
        registered = any(r["event"]["kind"] == "source" and r["event"]["lineage"]["source_id"] == sid for r in current)
        covering_windows = latest_window_candidates([w for w in windows if
                            w["event"]["lineage"] == dict(zip(LINEAGE, (sid, None, None, None))) and
                            w["verification"]["assurance"] in ("host_measured", "provider_verified") and
                            timestamp(w["event"]["payload"]["window_start"]) <= start and
                            timestamp(w["event"]["payload"]["window_end"]) >= end])
        covered = bool(covering_windows) and all(timestamp(w["event"]["payload"]["observed_through"]) >= end for w in covering_windows)
        if not registered or not covered:
            holds.add("SOURCE_MEASUREMENT_INCOMPLETE")
        sources.append({**policy, "measurement_complete": bool(registered and covered)})
    opportunities = [row for row in current if row["event"]["kind"] == "opportunity" and
                     row["event"]["lineage"]["source_id"] in relevant and start <= timestamp(row["event"]["occurred_at"]) < end]
    efforts = [row for row in current if row["event"]["kind"] == "effort" and
               row["event"]["lineage"]["source_id"] in relevant and start <= timestamp(row["event"]["occurred_at"]) < end]
    opportunity_keys = {(r["event"]["lineage"]["source_id"], r["event"]["lineage"]["opportunity_id"]) for r in opportunities}
    if any((r["event"]["lineage"]["source_id"], r["event"]["lineage"]["opportunity_id"]) not in opportunity_keys for r in efforts):
        holds.add("EFFORT_ATTRIBUTION_INCOMPLETE")
    applications = [row for row in current if row["event"]["kind"] == "application"]
    observations, app_rows = [], []
    outcomes = [row for row in current if row["event"]["kind"] == "outcome" and row["verification"]["assurance"] == "provider_verified"]
    for row in opportunities:
        event = row["event"]
        sid, oid = event["lineage"]["source_id"], event["lineage"]["opportunity_id"]
        minutes = [r["event"]["payload"]["human_minutes"] for r in efforts if
                   (r["event"]["lineage"]["source_id"], r["event"]["lineage"]["opportunity_id"]) == (sid, oid) and
                   r["verification"]["assurance"] in ("human_reported", "host_measured")]
        matching = [r for r in applications if (r["event"]["lineage"]["source_id"], r["event"]["lineage"]["opportunity_id"]) == (sid, oid)]
        if len(matching) > 1:
            holds.add("APPLICATION_ATTRIBUTION_CONFLICT")
        app = matching[0] if len(matching) == 1 else None
        aid = app["event"]["lineage"]["application_id"] if app else None
        observations.append({"observation_id": event["event_id"], "source_id": sid, "opportunity_id": oid,
                             "observed_at": event["occurred_at"], "qualified": event["payload"]["qualified"],
                             "human_minutes": math.fsum(minutes) if minutes else None, "application_id": aid,
                             "evidence_ref": event["payload"]["evidence_ref"]})
        if objective != "interviews" or app is None:
            continue
        payload = app["event"]["payload"]
        if payload["submitted_at"] is None or payload["fit_score"] is None:
            holds.add("APPLICATION_MEASUREMENTS_INCOMPLETE")
            continue
        submitted = timestamp(payload["submitted_at"])
        if not start <= submitted < end:
            holds.add("APPLICATION_OUTSIDE_MEASUREMENT_WINDOW")
            continue
        app_outcomes = [r for r in outcomes if r["event"]["lineage"]["application_id"] == aid]
        confirmations = [r for r in app_outcomes if r["event"]["payload"]["outcome"] == "SUBMISSION_CONFIRMED" and
                         r["event"]["revisions"].get("adapter") and timestamp(r["event"]["occurred_at"]) >= submitted]
        followup_windows = latest_window_candidates([w for w in windows if
                           w["event"]["lineage"]["application_id"] == aid and w["verification"]["assurance"] in ("host_measured", "provider_verified") and
                           timestamp(w["event"]["payload"]["window_start"]) <= submitted and
                           timestamp(w["event"]["payload"]["window_end"]) >= submitted + timedelta(days=horizon_days)])
        throughs = [timestamp(w["event"]["payload"]["observed_through"]) for w in followup_windows]
        if not confirmations or not throughs:
            holds.add("OUTCOME_EVIDENCE_OR_WINDOW_INCOMPLETE")
            continue
        confirmation = min(confirmations, key=lambda r: timestamp(r["event"]["occurred_at"]))
        through = max(throughs)
        app_row = {"application_id": aid, "source_id": sid, "fit_score": payload["fit_score"], "submitted_at": payload["submitted_at"],
                   "submission_confirmed_by_adapter": True, "submission_ref": confirmation["event"]["payload"]["evidence_ref"],
                   "adapter_revision": confirmation["event"]["revisions"]["adapter"], "followup_observed_through": through.isoformat()}
        for name in ("interview", "offer", "rejection"):
            matches = [r for r in app_outcomes if r["event"]["payload"]["outcome"] == name.upper() and
                       submitted <= timestamp(r["event"]["occurred_at"]) <= through]
            earliest = min(matches, key=lambda r: timestamp(r["event"]["occurred_at"])) if matches else None
            app_row[name + "_at"] = earliest["event"]["occurred_at"] if earliest else None
            app_row[name + "_evidence_ref"] = earliest["event"]["payload"]["evidence_ref"] if earliest else None
        app_rows.append(app_row)
    document = {"schema": "keel.source_feedback.v1", "observed_at": observed_at, "window_start": window_start, "window_end": window_end,
                "telemetry_complete": not holds and all(s["measurement_complete"] for s in sources), "objective": objective,
                "horizon_days": horizon_days, "sources": sources, "observations": observations, "applications": app_rows}
    validation = propose(document, budget_minutes=0, now=now)
    return {"document": document, "validation": validation, "holds": sorted(holds), "read_only": True,
            "execution_authorized": False, "store_id": store.store_id, "account_id": account_id}


def reconcile_receipt_observations(store, receipt_store, *, account_id, producer_id, now, context_factory=None):
    """Recheck live ReceiptStore conflicts and propose/apply scoped revocations.

    With no context_factory this is read-only. Applying requires a trusted host
    callback producing authentication context for each exact revocation event;
    the ObservationStore verifier still authorizes every write. Call before
    analytics, or pass receipt_store to source_feedback_snapshot to force HOLD
    until host reconciliation has occurred. No source JSON can self-authorize.
    """
    require(isinstance(now, datetime) and now.utcoffset() is not None, "aware_now_required")
    require(context_factory is None or callable(context_factory), "host_context_factory_required")
    snapshot = receipt_store.snapshot()  # Existing validation fails closed on corrupt input.
    receipts = {clean["source"] + ":" + clean["receipt_id"]: (clean, conflict)
                for _, clean, conflict in snapshot}
    rows = store.events(account_id, limit=MAX_EXPORT)
    if len(rows) == MAX_EXPORT:
        require(not store.events(account_id, after_sequence=rows[-1]["sequence"], limit=1), "snapshot_row_limit")
    events, results = [], []
    for row in rows:
        original = row["event"]
        if row["status"] != "CURRENT" or original["kind"] != "outcome" or "receipt" not in original["revisions"]:
            continue
        key = original["payload"]["provider_receipt_id"]
        found = receipts.get(key)
        reason = None
        if found is None:
            reason = "Receipt missing from selected canonical receipt store"
        elif found[1]:
            reason = "Canonical receipt store reports a conflicting receipt"
        elif event_digest(found[0]) != original["revisions"]["receipt"]:
            reason = "Canonical receipt payload no longer matches observation revision"
        if reason is None:
            continue
        raw_id = {"account": account_id, "target": original["event_id"], "digest": row["digest"], "at": now.isoformat(), "reason": reason}
        value = _envelope(event_id="receipt-revoke:" + event_digest(raw_id)[:40], account_id=account_id, producer_id=producer_id,
                          kind="revocation", occurred_at=now.isoformat(), lineage={key: None for key in LINEAGE},
                          revisions={"receipt": original["revisions"]["receipt"]},
                          payload={"target_event_id": original["event_id"], "target_sha256": row["digest"], "reason": reason}, now=now)
        events.append(value)
        if context_factory is not None:
            results.append(store.ingest(value, authenticate=True, context=context_factory(value)))
    return {"revocations": events, "results": results, "requires_host_revocations": bool(events) and context_factory is None,
            "execution_authorized": False, "read_only": context_factory is None}
