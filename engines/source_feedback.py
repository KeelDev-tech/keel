"""Bounded local source-budget proposals from explicitly attributed observations.

Historical yield per recorded human minute is descriptive, not causal. This
module never changes schedules, spends money, or grants execution permission.
"""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import re

from keel_flow.common import wilson
from keel_flow.outcomes import cohorts


class FeedbackError(ValueError):
    """Invalid or contradictory measurement input; no partial allocation."""


def _require(ok, code):
    if not ok:
        raise FeedbackError(code)


def _fields(row, fields):
    _require(type(row) is dict and set(row) == set(fields.split()), "invalid_fields")


def _token(value):
    _require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value), "invalid_id")
    return value


def _stamp(value):
    _require(type(value) is str, "invalid_timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(stamp.utcoffset() is not None, "timezone_required")
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise FeedbackError("invalid_timestamp") from None


def _integer(value, maximum):
    _require(type(value) is int and 0 <= value <= maximum, "invalid_integer")


def _number(value):
    _require(type(value) in (int, float) and 0 <= value <= 1_000_000,
             "invalid_human_minutes")
    _require(math.isfinite(value), "invalid_human_minutes")


def _reference(value):
    _require(type(value) is str and 0 < len(value.strip()) <= 1024, "evidence_ref_required")


def _canonical(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        _require(len(encoded) <= 8 * 1024 * 1024, "input_too_large")
        return encoded
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise FeedbackError("invalid_json") from None


def propose(document, *, budget_minutes, now):
    """Allocate at most 240 whole human minutes, respecting declared caps/holds.

    Same observation IDs must have identical payloads. Repeated opportunities
    earn yield once, while distinct observation effort remains in the cost.
    Cross-source opportunity attribution is held for human reconciliation.
    Unknown effort or incomplete outcomes never silently become zero cost or
    negative outcomes. All qualified observations need complete attribution
    to an application before the interview objective can rank their source.
    """
    _integer(budget_minutes, 240)
    _require(isinstance(now, datetime) and now.utcoffset() is not None, "aware_now_required")
    now = now.astimezone(timezone.utc)
    raw = _canonical(document)
    document = json.loads(raw)
    _fields(document, "schema observed_at window_start window_end telemetry_complete objective horizon_days sources observations applications")
    _require(document["schema"] == "keel.source_feedback.v1", "unsupported_schema")
    _require(document["objective"] in ("qualified_leads", "interviews"), "invalid_objective")
    _integer(document["horizon_days"], 180)
    _require(document["horizon_days"] > 0, "positive_horizon_required")
    _require(type(document["telemetry_complete"]) is bool, "invalid_completeness")
    start, end, observed = (_stamp(document[key]) for key in ("window_start", "window_end", "observed_at"))
    _require(start < end <= observed <= now, "invalid_measurement_window")
    _require((end - start).days <= 180, "measurement_window_too_long")
    for name, bound in (("sources", 128), ("observations", 10000), ("applications", 10000)):
        _require(type(document[name]) is list and len(document[name]) <= bound, "collection_limit")
    _require(bool(document["sources"]), "sources_required")
    sources, holds = {}, defaultdict(set)
    for source in document["sources"]:
        _fields(source, "source_id permitted measurement_complete cap_minutes cooldown_until cooldown_verified evidence_ref")
        sid = _token(source["source_id"])
        _require(sid not in sources, "duplicate_source")
        sources[sid] = source
        _reference(source["evidence_ref"])
        _integer(source["cap_minutes"], 240)
        for field in ("permitted", "measurement_complete", "cooldown_verified"):
            _require(source[field] is None or type(source[field]) is bool, "invalid_source_flag")
        if source["permitted"] is not True:
            holds[sid].add("PERMISSION_UNKNOWN_OR_DENIED")
        if source["measurement_complete"] is not True:
            holds[sid].add("MEASUREMENT_INCOMPLETE")
        if source["cooldown_verified"] is not True:
            holds[sid].add("COOLDOWN_UNKNOWN")
        if source["cooldown_until"] is not None and _stamp(source["cooldown_until"]) > now:
            holds[sid].add("COOLDOWN_ACTIVE")
        if source["cap_minutes"] == 0:
            holds[sid].add("SOURCE_CAP_ZERO")
        if not document["telemetry_complete"]:
            holds[sid].add("TELEMETRY_INCOMPLETE")
        if (now - observed).total_seconds() > 86400:
            holds[sid].add("MEASUREMENT_STALE")

    unique, seen_ids, occurrences = {}, {}, {}
    opportunities, effort = defaultdict(list), defaultdict(list)
    repeated = 0
    for row in document["observations"]:
        _fields(row, "observation_id source_id opportunity_id observed_at qualified human_minutes application_id evidence_ref")
        oid, sid, opportunity = (_token(row[key]) for key in ("observation_id", "source_id", "opportunity_id"))
        _require(sid in sources, "unknown_source")
        stamp = _stamp(row["observed_at"])
        _require(start <= stamp < end, "observation_outside_window")
        _reference(row["evidence_ref"])
        _require(row["qualified"] is None or type(row["qualified"]) is bool, "invalid_qualification")
        if row["application_id"] is not None:
            _token(row["application_id"])
        if row["human_minutes"] is not None:
            _number(row["human_minutes"])
        if oid in seen_ids:
            _require(seen_ids[oid] == row, "conflicting_observation_id")
            repeated += 1
            continue
        seen_ids[oid] = row
        occurrence = (sid, opportunity, stamp, row["evidence_ref"])
        measured = {key: value for key, value in row.items() if key not in ("observation_id", "observed_at")}
        if occurrence in occurrences:
            _require(occurrences[occurrence] == measured, "conflicting_observation_occurrence")
            repeated += 1
            continue
        occurrences[occurrence] = measured
        unique[oid] = row
        opportunities[opportunity].append(row)
        if row["human_minutes"] is None:
            holds[sid].add("HUMAN_EFFORT_UNKNOWN")
        else:
            effort[sid].append(row["human_minutes"])
        if row["qualified"] is None:
            holds[sid].add("QUALIFICATION_UNKNOWN")

    counted = defaultdict(list)
    application_links = {}
    for opportunity, rows in opportunities.items():
        attribution = {row["source_id"] for row in rows}
        if len(attribution) != 1:
            for sid in attribution:
                holds[sid].add("OPPORTUNITY_ATTRIBUTION_CONFLICT")
            continue
        sid = rows[0]["source_id"]
        latest = max(rows, key=lambda row: (_stamp(row["observed_at"]), row["observation_id"]))
        # Conflicting observations at the same time cannot be ordered by ID.
        same_time = [row for row in rows if _stamp(row["observed_at"]) == _stamp(latest["observed_at"])]
        if len({row["qualified"] for row in same_time}) > 1:
            holds[sid].add("QUALIFICATION_CONFLICT")
        counted[sid].append(latest)
        links = {row["application_id"] for row in rows if row["application_id"] is not None}
        _require(len(links) <= 1, "opportunity_application_conflict")
        if links:
            aid = next(iter(links))
            _require(aid not in application_links, "application_reused_across_opportunities")
            application_links[aid] = (sid, opportunity)
        elif document["objective"] == "interviews" and latest["qualified"] is True:
            holds[sid].add("APPLICATION_ATTRIBUTION_UNKNOWN")

    outcome_report = None
    outcome_counts = defaultdict(lambda: [0, 0])
    if document["objective"] == "interviews":
        apps = document["applications"]
        by_id = {}
        for app in apps:
            _require(type(app) is dict, "invalid_application")
            aid, sid = _token(app.get("application_id")), _token(app.get("source_id"))
            _require(aid not in by_id, "duplicate_application")
            _require(sid in sources and aid in application_links and application_links[aid][0] == sid,
                     "application_source_binding_mismatch")
            _require(start <= _stamp(app.get("submitted_at")) < end, "application_outside_window")
            by_id[aid] = app
        for aid, (sid, _) in application_links.items():
            if aid not in by_id:
                holds[sid].add("APPLICATION_OUTCOME_MISSING")
        try:
            outcome_report = cohorts(apps, now=now, horizon_days=document["horizon_days"])
        except (ValueError, TypeError, KeyError, OverflowError):
            raise FeedbackError("invalid_outcome_cohort") from None
        for excluded in outcome_report["excluded"]:
            holds[by_id[excluded["application_id"]]["source_id"]].add("OUTCOME_WINDOW_INCOMPLETE_OR_UNCONFIRMED")
        for cohort in outcome_report["cohorts"]:
            outcome_counts[cohort["source_id"]][0] += cohort["mature_observed_applications"]
            outcome_counts[cohort["source_id"]][1] += cohort["interview"]
    else:
        _require(not document["applications"], "unused_outcomes_forbidden")

    metrics, eligible = [], []
    for sid, source in sorted(sources.items()):
        minutes = math.fsum(effort[sid])
        trials = len(counted[sid])
        qualified = sum(row["qualified"] is True for row in counted[sid])
        if not trials:
            holds[sid].add("NO_ATTRIBUTED_OBSERVATIONS")
        if minutes <= 0:
            holds[sid].add("NO_MEASURED_HUMAN_EFFORT")
        if document["objective"] == "interviews":
            trials, successes = outcome_counts[sid]
            if not trials:
                holds[sid].add("NO_MATURE_OUTCOMES")
        else:
            successes = qualified
        # Wilson floating-point roundoff at zero successes must not create
        # fictitious positive yield (e.g. the nominal endpoint for n=3).
        lower = wilson(successes, trials)[0] * trials / minutes if successes and trials and minutes else 0.0
        observed_rate = successes / minutes if minutes else None
        _require(math.isfinite(lower) and (observed_rate is None or math.isfinite(observed_rate)),
                 "unrepresentable_yield_rate")
        reasons = sorted(holds[sid])
        row = {"source_id": sid, "status": "HOLD" if reasons else "MEASURED",
               "hold_reasons": reasons, "unique_opportunities": len(counted[sid]),
               "qualified_opportunities": qualified, "recorded_human_minutes": minutes,
               "observation_count": sum(r["source_id"] == sid for r in unique.values()),
               "outcome_trials": trials, "observed_yield": successes,
               "observed_yield_per_human_minute": observed_rate if not reasons else None,
               "conservative_yield_per_human_minute": lower if not reasons else None,
               "cap_minutes": source["cap_minutes"], "proposed_minutes": 0,
               "evidence_ref": source["evidence_ref"]}
        metrics.append(row)
        if not reasons and lower > 0:
            eligible.append(row)
    remaining = budget_minutes
    for row in sorted(eligible, key=lambda r: (-r["conservative_yield_per_human_minute"], r["source_id"])):
        row["proposed_minutes"] = min(row["cap_minutes"], remaining)
        remaining -= row["proposed_minutes"]
    return {"schema": "keel.source_budget_proposal.v1", "input_sha256": hashlib.sha256(raw).hexdigest(),
            "as_of": now.isoformat(), "objective": document["objective"],
            "window_start": document["window_start"], "window_end": document["window_end"],
            "budget_minutes": budget_minutes, "allocated_minutes": budget_minutes - remaining,
            "unallocated_minutes": remaining, "sources": metrics, "outcome_report": outcome_report,
            "duplicate_observations_removed": repeated,
            "status": "PROPOSAL_ONLY" if budget_minutes != remaining else "HOLD",
            "method": "descending Wilson-formula lower yield per recorded human minute; fixed source caps",
            "human_review_required": True, "execution_authorized": False, "schedule_writes": 0,
            "causal_improvement_established": False, "off_policy_estimate": False,
            "boundary": "Explicit caller observations and complete declared windows; source truth, human time, "
                        "and receipt provenance are not authenticated here. Historical yield is not a future guarantee; "
                        "the Wilson ranking formula does not establish independence or a confidence guarantee."}
