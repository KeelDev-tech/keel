"""Exact-bound review projections and requests, never approval capabilities.

Input records are observations supplied by an adapter. Source hashes preserve
bindings but do not authenticate a person, host or source truth. The receiving
authoritative host must re-check current inputs and obtain a real human decision.
"""
from __future__ import annotations

import math

from keel_loki.common import (LokiError, canonical, clone, digest, require_dict, require_hash,
                              require_id, require_int)
from keel_loki.questions import plan_questions
from keel_loki.decision_sessions import plan_decision_session


class ReviewError(LokiError):
    pass


def _check(ok, code):
    if not ok:
        raise ReviewError(code)


def _text(value, maximum=8000, *, nullable=False, empty=False):
    if value is None and nullable:
        return
    _check(type(value) is str and len(value) <= maximum and (empty or bool(value.strip())), "invalid_text")


def _stamp(value, *, nullable=False):
    if value is None and nullable:
        return
    require_int(value, 0, 253402300799, "timestamp")


def _ids(value, maximum=256, *, nonempty=False):
    _check(type(value) is list and len(value) <= maximum and (not nonempty or value), "invalid_id_list")
    for item in value:
        require_id(item)
    _check(len(value) == len(set(value)), "duplicate_id")


# Presentation contract for keel-evidence-fit-v1, not a second scoring engine.
# Values are supplied observations; this module never scores or retrieves facts.
_FIT_MAXIMA = {"experience_alignment": 25, "transferable_skills": 15,
    "hard_requirements": 20, "career_upside": 10, "compensation": 10,
    "founder_advantage": 5, "industry_alignment": 5, "location_work_model": 5,
    "employer_quality": 5}
_FIT_SUBJECTIVE = {"career_upside", "founder_advantage", "industry_alignment", "employer_quality"}


def _fit_number(value, maximum=100, minimum=0):
    _check(type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value),
           "invalid_fit_number")


def _fit_texts(values, *, maximum=128, width=2048, nonempty=False):
    _check(type(values) is list and len(values) <= maximum and (values or not nonempty), "invalid_fit_list")
    for value in values:
        _text(value, width)
    _check(len(values) == len(set(values)), "duplicate_fit_text")


def _fit_criterion(row):
    _check(type(row) is dict, "invalid_fit_criterion")
    reason = row.get("reason")
    keys = {"id", "weight", "mandatory", "status", "fraction", "profile_evidence_refs",
            "posting_source_ref", "reason"}
    _check(reason in ("structured_criterion_missing", "applicant_fact_missing", "explicit_fact_comparison"),
           "invalid_fit_reason")
    if reason != "structured_criterion_missing":
        keys.update(("fact", "operator"))
    if reason == "explicit_fact_comparison":
        keys.update(("credit_numerator", "credit_denominator"))
    if "legacy_hold" in row:
        keys.add("legacy_hold")
        _check(row["legacy_hold"] in ("MISSING", "PARTIAL"), "invalid_fit_legacy_hold")
    require_dict(row, keys, "fit_criterion")
    _text(row["id"], 2048)
    _fit_number(row["weight"], minimum=.000001)
    _check(type(row["mandatory"]) is bool, "invalid_fit_mandatory")
    _fit_texts(row["profile_evidence_refs"], nonempty=reason == "explicit_fact_comparison")
    if reason == "structured_criterion_missing":
        _check(row["posting_source_ref"] is None, "invalid_fit_source")
    else:
        _text(row["fact"], 2048)
        _text(row["posting_source_ref"], 2048)
        _check(row["operator"] in ("eq", "gte", "lte", "contains", "all_of", "any_of"), "invalid_fit_operator")
    if reason == "explicit_fact_comparison":
        fraction = row["fraction"]
        _fit_number(fraction, maximum=1)
        require_int(row["credit_denominator"], 1, 128, "fit_denominator")
        require_int(row["credit_numerator"], 0, row["credit_denominator"], "fit_numerator")
        _check(fraction == row["credit_numerator"] / row["credit_denominator"], "invalid_fit_credit")
        expected = "VERIFIED" if fraction == 1 else "MISSING" if fraction == 0 else "PARTIAL"
        _check(row["status"] == expected, "invalid_fit_status")
    else:
        _check(row["status"] == "UNKNOWN" and row["fraction"] is None and not row["profile_evidence_refs"],
               "invalid_fit_unknown")


def _fit_evidence(evidence, component, role_id):
    _check(type(evidence) is dict, "invalid_fit_evidence")
    method = evidence.get("method")
    if method == "unknown":
        require_dict(evidence, ("method", "reason"), "fit_evidence")
        _check(evidence["reason"] == "no_supported_criteria_or_assessment", "invalid_fit_reason")
    elif method == "reviewed_no_requirements":
        require_dict(evidence, ("method", "posting_source_ref"), "fit_evidence")
        _check(component == "hard_requirements", "invalid_fit_component")
        _text(evidence["posting_source_ref"], 2048)
    elif method == "human_assessment":
        require_dict(evidence, ("method", "assessment"), "fit_evidence")
        _check(component in _FIT_SUBJECTIVE, "invalid_fit_component")
        row = evidence["assessment"]
        require_dict(row, ("role_id", "component", "fraction", "reviewer", "rationale", "evidence_refs"),
                     "fit_human_assessment")
        _check(row["role_id"] == role_id and row["component"] == component, "invalid_fit_binding")
        _fit_number(row["fraction"], maximum=1)
        require_dict(row["reviewer"], ("kind", "id"), "fit_reviewer")
        _check(row["reviewer"]["kind"] == "human", "invalid_fit_reviewer")
        _text(row["reviewer"]["id"], 2048)
        _text(row["rationale"], 2048)
        _fit_texts(row["evidence_refs"], nonempty=True)
    elif method == "explicit_fact_comparison":
        require_dict(evidence, ("method", "criteria"), "fit_evidence")
        rows = evidence["criteria"]
        _check(type(rows) is list and 0 < len(rows) <= 128, "invalid_fit_criteria")
        for row in rows:
            _fit_criterion(row)
        _check(len({row["id"] for row in rows}) == len(rows), "duplicate_fit_criterion")
    else:
        raise ReviewError("unsupported_fit_method")


def _fit_coherence(assessment):
    """Reject contradictory display metadata, allowing outward display rounding.

    This does not re-evaluate facts, weighted criteria, thresholds or eligibility.
    Component bounds round at six decimals; totals round at four.
    """
    bounds = assessment["score_bounds"]
    for component, maximum in _FIT_MAXIMA.items():
        bound, evidence = bounds[component], assessment["score_evidence"][component]
        low, high, known = (bound[key] for key in ("lower", "upper", "known_weight"))
        _check(low <= known and math.isclose(high - low + known, maximum, rel_tol=0, abs_tol=.000002000001),
               "inconsistent_fit_bounds")
        method = evidence["method"]
        if method == "unknown" or (method == "explicit_fact_comparison" and
                                   all(row["fraction"] is None for row in evidence["criteria"])):
            _check((low, high, known) == (0, maximum, 0), "inconsistent_fit_unknown")
        elif method == "reviewed_no_requirements":
            _check((low, high, known) == (maximum, maximum, maximum), "inconsistent_fit_reviewed")
        elif method == "human_assessment":
            points = evidence["assessment"]["fraction"] * maximum
            _check(known == maximum and all(math.isclose(value, points, rel_tol=0, abs_tol=.000001000001)
                                           for value in (low, high)), "inconsistent_fit_human")
        elif method == "explicit_fact_comparison" and all(row["fraction"] is not None for row in evidence["criteria"]):
            _check(known == maximum, "inconsistent_fit_coverage")
    for total, component_key in (("fit_score", "lower"), ("fit_score_upper", "upper"),
                                 ("score_coverage_percent", "known_weight")):
        _check(math.isclose(assessment[total], sum(bound[component_key] for bound in bounds.values()),
                            rel_tol=0, abs_tol=.000100000001), "inconsistent_fit_total")


def _fit_view(assessment, revision):
    """Whitelist bounded, versioned evidence. Malformed optional data is unavailable.

    The original snapshot is still retained and pinned. This view does not
    authenticate assessments or recompute scoring, eligibility or canonical holds.
    """
    view = {"schema": "keel.muse.fit-assessment-view.v1", "availability": "UNAVAILABLE",
            "reason": "not_supplied" if assessment is None else "unsupported_or_malformed",
            "assessment_sha256": digest(assessment) if assessment is not None else None,
            "execution_authorized": False}
    if assessment is None:
        return view
    try:
        _check(len(canonical(assessment)) <= 262144, "fit_assessment_too_large")
        require_dict(assessment, ("schema", "scoring_version", "role_id", "application_revision_sha256",
            "fit_score", "fit_score_upper", "score_coverage_percent", "score_bounds", "score_evidence",
            "blocked_reasons"), "fit_assessment")
        _check(assessment["schema"] == "keel.muse.fit-assessment.v1" and
               assessment["scoring_version"] == "keel-evidence-fit-v1", "unsupported_fit_version")
        _check(assessment["application_revision_sha256"] == revision, "invalid_fit_binding")
        _text(assessment["role_id"], 2048)
        for key in ("fit_score", "fit_score_upper", "score_coverage_percent"):
            _fit_number(assessment[key])
        _check(assessment["fit_score"] <= assessment["fit_score_upper"], "invalid_fit_bounds")
        require_dict(assessment["score_bounds"], _FIT_MAXIMA, "fit_bounds")
        require_dict(assessment["score_evidence"], _FIT_MAXIMA, "fit_evidence")
        for component, maximum in _FIT_MAXIMA.items():
            bound = assessment["score_bounds"][component]
            require_dict(bound, ("lower", "upper", "known_weight"), "fit_bound")
            for value in bound.values():
                _fit_number(value, maximum=maximum)
            _check(bound["lower"] <= bound["upper"], "invalid_fit_bounds")
            _fit_evidence(assessment["score_evidence"][component], component, assessment["role_id"])
        _fit_texts(assessment["blocked_reasons"], maximum=131, width=2078)
        _fit_coherence(assessment)
    except (LokiError, ValueError, TypeError, KeyError):
        return view
    # Copy only the validated contract, never an entire scoring/discovery row.
    view.update({key: assessment[key] for key in ("scoring_version", "role_id", "application_revision_sha256",
        "fit_score", "fit_score_upper", "score_coverage_percent", "score_bounds", "score_evidence", "blocked_reasons")})
    view.update(availability="AVAILABLE" if assessment["score_coverage_percent"] else "UNAVAILABLE",
                reason="supplied_evidence" if assessment["score_coverage_percent"] else "no_supported_evidence")
    return view


def _packet(packet):
    if packet is None:
        return
    require_dict(packet, ("revision_sha256", "fields", "attachments"), "packet")
    require_hash(packet["revision_sha256"])
    for key, identity, keys, maximum in (
            ("fields", "field_id", ("field_id", "label", "value", "required", "evidence_ids"), 256),
            ("attachments", "attachment_id", ("attachment_id", "name", "sha256"), 32)):
        _check(type(packet[key]) is list and len(packet[key]) <= maximum, "packet_collection_invalid")
        seen = set()
        for item in packet[key]:
            require_dict(item, keys, key)
            require_id(item[identity])
            _check(item[identity] not in seen, "duplicate_packet_item")
            seen.add(item[identity])
            if key == "fields":
                _text(item["label"], 1000)
                _text(item["value"], 16000, nullable=True, empty=True)
                _check(type(item["required"]) is bool, "required_boolean")
                _ids(item["evidence_ids"])
            else:
                _text(item["name"], 512)
                require_hash(item["sha256"])


def packet_projection_digest(packet):
    """Digest the normalized displayed bytes, distinct from the source artifact."""
    _packet(packet)
    return digest(packet) if packet is not None else None


def _evidence(item):
    require_dict(item, ("evidence_id", "source_id", "source_revision_sha256", "label", "reference",
        "kind", "status", "scope_ids", "permitted_uses", "observed_at", "valid_until", "binding"), "evidence")
    require_id(item["evidence_id"])
    require_id(item["source_id"])
    require_hash(item["source_revision_sha256"])
    _text(item["label"], 1000)
    _text(item["reference"], 2048, nullable=True)
    _check(item["kind"] in ("source", "receipt", "review"), "evidence_kind_invalid")
    _check(item["status"] in ("verified", "unverified", "rejected"), "evidence_status_invalid")
    _ids(item["scope_ids"], nonempty=True)
    _check(type(item["permitted_uses"]) is list and item["permitted_uses"]
           and all(type(x) is str and x in ("application_fact", "planning", "review")
                   for x in item["permitted_uses"])
           and len(item["permitted_uses"]) == len(set(item["permitted_uses"])), "evidence_uses_invalid")
    _stamp(item["observed_at"], nullable=True)
    _stamp(item["valid_until"], nullable=True)
    if item["observed_at"] is not None and item["valid_until"] is not None:
        _check(item["valid_until"] > item["observed_at"], "evidence_validity_invalid")
    if item["binding"] is not None:
        require_dict(item["binding"], ("application_revision_sha256", "packet_revision_sha256"), "evidence_binding")
        for value in item["binding"].values():
            require_hash(value)


def _validate(snapshot, now):
    snapshot = clone(snapshot)
    require_dict(snapshot, ("schema", "snapshot_id", "source_sha256", "captured_at", "telemetry_complete",
                            "applications", "events"), "snapshot")
    _check(snapshot["schema"] in ("keel.muse.review-snapshot.v1", "keel.muse.review-snapshot.v2"),
           "snapshot_version_invalid")
    require_id(snapshot["snapshot_id"])
    require_hash(snapshot["source_sha256"])
    _stamp(snapshot["captured_at"])
    _stamp(now)
    _check(snapshot["captured_at"] <= now, "future_snapshot")
    _check(type(snapshot["telemetry_complete"]) is bool, "telemetry_complete_boolean")
    _check(type(snapshot["applications"]) is list and len(snapshot["applications"]) <= 512,
           "applications_count_invalid")
    applications, question_definitions = {}, {}
    for app in snapshot["applications"]:
        fit_keys = ("fit_assessment",) if (snapshot["schema"] == "keel.muse.review-snapshot.v2" and
                                         type(app) is dict and "fit_assessment" in app) else ()
        require_dict(app, ("application_id", "role", "employer", "created_at", "deadline",
            "application_revision_sha256", "packet", "previous_packet", "holds", "questions", "evidence",
            "approval_observation") + fit_keys, "application")
        identity = require_id(app["application_id"])
        _check(identity not in applications, "duplicate_application")
        applications[identity] = app
        _text(app["role"], 1000)
        _text(app["employer"], 1000)
        _stamp(app["created_at"])
        _stamp(app["deadline"], nullable=True)
        _check(app["created_at"] <= snapshot["captured_at"], "future_application")
        if app["deadline"] is not None:
            _check(app["deadline"] >= app["created_at"], "deadline_before_creation")
        require_hash(app["application_revision_sha256"])
        _packet(app["packet"])
        _packet(app["previous_packet"])
        _check(type(app["holds"]) is list and len(app["holds"]) <= 64, "holds_invalid")
        seen = set()
        for hold in app["holds"]:
            require_dict(hold, ("code", "since"), "hold")
            require_id(hold["code"])
            _check(hold["code"] not in seen, "duplicate_hold")
            seen.add(hold["code"])
            _stamp(hold["since"], nullable=True)
            _check(hold["since"] is None or hold["since"] <= snapshot["captured_at"], "future_hold")
        _check(type(app["questions"]) is list and len(app["questions"]) <= 64, "questions_invalid")
        seen = set()
        for q in app["questions"]:
            require_dict(q, ("question_id", "kind", "scope_ids", "prompt", "estimated_minutes", "reuse_authorized"),
                         "question")
            require_id(q["question_id"])
            _ids(q["scope_ids"], nonempty=True)
            _check(identity in q["scope_ids"], "question_application_scope_mismatch")
            _check(q["question_id"] not in seen, "duplicate_question")
            seen.add(q["question_id"])
            existing = question_definitions.setdefault(q["question_id"], q)
            _check(existing == q, "inconsistent_reused_question")
        _check(type(app["evidence"]) is list and len(app["evidence"]) <= 256, "evidence_count_invalid")
        seen = set()
        for evidence in app["evidence"]:
            _evidence(evidence)
            _check(evidence["evidence_id"] not in seen, "duplicate_evidence")
            seen.add(evidence["evidence_id"])
            _check(evidence["observed_at"] is None or evidence["observed_at"] <= snapshot["captured_at"],
                   "future_evidence")
        approval = app["approval_observation"]
        if approval is not None:
            require_dict(approval, ("application_id", "application_revision_sha256", "packet_revision_sha256",
                "packet_projection_sha256", "observed_at", "decision", "source_record_sha256"), "approval_observation")
            require_id(approval["application_id"])
            for key in ("application_revision_sha256", "packet_revision_sha256", "packet_projection_sha256",
                        "source_record_sha256"):
                require_hash(approval[key])
            _stamp(approval["observed_at"])
            _check(approval["observed_at"] <= snapshot["captured_at"], "future_approval")
            _check(approval["decision"] in ("approved", "rejected"), "approval_decision_invalid")
    for q in question_definitions.values():
        _check(set(q["scope_ids"]) <= set(applications), "unknown_question_scope")
        for scope in q["scope_ids"]:
            _check(any(item["question_id"] == q["question_id"] for item in applications[scope]["questions"]),
                   "question_scope_not_projected")
    _check(type(snapshot["events"]) is list and len(snapshot["events"]) <= 4096, "events_count_invalid")
    seen = set()
    for event in snapshot["events"]:
        require_dict(event, ("event_id", "application_id", "at", "kind", "verification",
                            "source_record_sha256", "latency_ms", "detail"), "event")
        require_id(event["event_id"])
        _check(event["event_id"] not in seen, "duplicate_event")
        seen.add(event["event_id"])
        _check(event["application_id"] in applications, "unknown_event_application")
        _stamp(event["at"], nullable=True)
        _check(event["at"] is None or event["at"] <= snapshot["captured_at"], "future_event")
        _check(event["kind"] in ("discovered", "prepared", "model_call", "human_intervention",
                                 "submission_observed", "blocked", "note"), "event_kind_invalid")
        _check(event["verification"] in ("verified", "unverified", "unknown"), "event_verification_invalid")
        if event["source_record_sha256"] is not None:
            require_hash(event["source_record_sha256"])
        latency = event["latency_ms"]
        _check(latency is None or type(latency) in (int, float) and math.isfinite(latency)
               and 0 <= latency <= 86400000, "latency_invalid")
        _text(event["detail"], 2000, empty=True)
    return snapshot, applications, question_definitions


def _state(evidence, app, now, use="review"):
    if app["application_id"] not in evidence["scope_ids"] or use not in evidence["permitted_uses"]:
        return "OUT_OF_SCOPE"
    if evidence["observed_at"] is None:
        return "UNKNOWN_TIME"
    if evidence["valid_until"] is not None and now >= evidence["valid_until"]:
        return "EXPIRED"
    return evidence["status"].upper()


def _deltas(before, after):
    result = []
    for family, identity in (("fields", "field_id"), ("attachments", "attachment_id")):
        old = {r[identity]: r for r in before[family]} if before else {}
        new = {r[identity]: r for r in after[family]} if after else {}
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                result.append({"kind": family, "item_id": key, "change": "added" if key not in old
                    else "removed" if key not in new else "changed", "before": old.get(key), "after": new.get(key)})
    return result


def _percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def project_review(snapshot, *, now, expected_snapshot_sha256=None, session_minutes=None):
    """Project exact questions, packet deltas, live blockers and observed metrics."""
    try:
        snapshot, apps, questions = _validate(snapshot, now)
        snapshot_hash = digest(snapshot)
        if expected_snapshot_sha256 is not None:
            require_hash(expected_snapshot_sha256)
            _check(expected_snapshot_sha256 == snapshot_hash, "snapshot_pin_mismatch")
        specs = {"schema": "keel.loki.question_input.v1", "tasks": [], "blockers": [],
                 "questions": list(questions.values())}
        projected, causes, observed_events = [], {}, []
        for app in apps.values():
            aid, packet = app["application_id"], app["packet"]
            blocked, blocker_ids = [], []
            def block(code):
                if code not in blocked:
                    blocked.append(code)
            for hold in app["holds"]:
                block(hold["code"])
            if app["deadline"] is not None and now >= app["deadline"]:
                block("DEADLINE_PASSED")
            evidence_table = {e["evidence_id"]: e for e in app["evidence"]}
            evidence = [dict(e, review_state=_state(e, app, now)) for e in app["evidence"]]
            if packet is None:
                block("PACKET_MISSING")
            else:
                for field in packet["fields"]:
                    if field["required"] and (field["value"] is None or not field["value"].strip()):
                        block("REQUIRED_FIELD_MISSING")
                    if field["value"] is not None and field["value"].strip():
                        if not field["evidence_ids"]:
                            block("FIELD_EVIDENCE_MISSING")
                        for eid in field["evidence_ids"]:
                            source = evidence_table.get(eid)
                            state = _state(source, app, now, "application_fact") if source else "MISSING"
                            if state != "VERIFIED":
                                block("FIELD_EVIDENCE_" + state)
            changes = _deltas(app["previous_packet"], packet) if app["previous_packet"] is not None else []
            if (changes and packet is not None and app["previous_packet"]["revision_sha256"] == packet["revision_sha256"]):
                block("PACKET_CONTENT_CHANGED_WITH_SAME_REVISION")
            for code in blocked:
                causes[code] = causes.get(code, 0) + 1
                bid = "system-" + digest([aid, code])[:24]
                blocker_ids.append(bid)
                specs["blockers"].append({"blocker_id": bid, "question_id": None, "kind": "system",
                    "scope_id": aid, "fact_key": None, "depends_on": [], "state": "unresolved"})
            for q in app["questions"]:
                bid = "question-" + digest([aid, q["question_id"]])[:24]
                blocker_ids.append(bid)
                specs["blockers"].append({"blocker_id": bid, "question_id": q["question_id"], "kind": q["kind"],
                    "scope_id": aid, "fact_key": q["question_id"] if q["kind"] == "fact" else None,
                    "depends_on": [], "state": "unresolved"})
            specs["tasks"].append({"task_id": aid, "weight": 1, "blocker_ids": blocker_ids,
                                   "created_at": app["created_at"], "deadline": app["deadline"]})
            approval = app["approval_observation"]
            approval_state, stale_reasons = "ABSENT", []
            if approval is not None:
                checks = {"application_changed": approval["application_id"] == aid and
                    approval["application_revision_sha256"] == app["application_revision_sha256"],
                    "packet_changed": packet is not None and approval["packet_revision_sha256"] == packet["revision_sha256"],
                    "displayed_content_changed": approval["packet_projection_sha256"] == packet_projection_digest(packet)}
                stale_reasons = [key for key, ok in checks.items() if not ok]
                approval_state = ("STALE" if stale_reasons else "APPROVAL_OBSERVED" if approval["decision"] == "approved"
                                  else "REJECTION_OBSERVED")
            timeline, receipts = [], []
            for event in snapshot["events"]:
                if event["application_id"] != aid:
                    continue
                matches = [e for e in app["evidence"] if e["source_revision_sha256"] == event["source_record_sha256"]
                           and _state(e, app, now) == "VERIFIED"]
                backed = event["verification"] == "verified" and bool(matches)
                if event["kind"] == "submission_observed":
                    backed = backed and packet is not None and any(e["kind"] == "receipt" and e["binding"] == {
                        "application_revision_sha256": app["application_revision_sha256"],
                        "packet_revision_sha256": packet["revision_sha256"]} for e in matches)
                    if backed:
                        receipts.append(event["event_id"])
                row = dict(event, evidence_backed=backed)
                observed_events.append(row)
                timeline.append(row)
            timeline.sort(key=lambda row: (row["at"] is None, row["at"] if row["at"] is not None else 0, row["event_id"]))
            submission = "VERIFIED_OBSERVATION" if receipts else "UNVERIFIED_OBSERVATION" if any(
                e["kind"] == "submission_observed" for e in timeline) else "UNKNOWN"
            status = ("BLOCKED" if blocked else "SUBMISSION_OBSERVED" if receipts else
                      "NEEDS_REVIEW" if app["questions"] else "PACKET_CHANGED" if changes else "PREPARED")
            projected.append({"application_id": aid, "role": app["role"], "employer": app["employer"],
                "application_revision_sha256": app["application_revision_sha256"], "packet": packet,
                "packet_projection_sha256": packet_projection_digest(packet), "previous_packet": app["previous_packet"],
                "status": status, "created_at": app["created_at"], "queue_age_seconds": now - app["created_at"],
                "deadline": app["deadline"], "blocking_causes": blocked, "holds": app["holds"],
                "questions": [dict(q, question_sha256=digest(q)) for q in app["questions"]],
                "evidence": evidence, "changes": changes, "timeline": timeline,
                "approval_state": approval_state, "approval_stale_reasons": stale_reasons,
                "submission_status": submission, "verified_receipt_event_ids": receipts,
                "execution_authorized": False})
            if snapshot["schema"] == "keel.muse.review-snapshot.v2":
                projected[-1]["fit_assessment"] = _fit_view(app.get("fit_assessment"), app["application_revision_sha256"])
        question_plan = plan_questions(specs, now)
        latencies = [e["latency_ms"] for e in observed_events if e["kind"] == "model_call"
                     and e["evidence_backed"] and e["latency_ms"] is not None]
        model = [e for e in observed_events if e["kind"] == "model_call"]
        interventions = [e for e in observed_events if e["kind"] == "human_intervention"]
        verified_successes = sum(a["submission_status"] == "VERIFIED_OBSERVATION" for a in projected)
        ages = [a["queue_age_seconds"] for a in projected if a["submission_status"] != "VERIFIED_OBSERVATION"]
        def metric(events):
            backed = sum(e["evidence_backed"] for e in events)
            complete = snapshot["telemetry_complete"] and backed == len(events)
            return {"value": backed if complete else None, "known_count": backed, "reported_count": len(events),
                    "unverified_count": len(events) - backed, "complete": complete}
        metrics = {"applications": len(projected), "blocked": sum(bool(a["blocking_causes"]) for a in projected),
            "pending_questions": len(questions), "queue_age_seconds": {"count": len(ages), "p50": _percentile(ages, .5),
                "p95": _percentile(ages, .95), "max": max(ages) if ages else None},
            "blocked_causes": [{"code": key, "applications": value} for key, value in sorted(causes.items())],
            "model_calls": metric(model), "human_interventions": metric(interventions),
            "model_latency_ms": {"count": len(latencies), "missing_or_unverified": len(model) - len(latencies),
                "p50": _percentile(latencies, .5), "p95": _percentile(latencies, .95),
                "complete": snapshot["telemetry_complete"] and len(latencies) == len(model)},
            "submissions": {"verified_observations": verified_successes,
                "unverified_observations": sum(a["submission_status"] == "UNVERIFIED_OBSERVATION" for a in projected),
                "unknown": sum(a["submission_status"] == "UNKNOWN" for a in projected),
                "success_rate": None, "reason": "No verified attempt denominator or complete outcome window."}}
        version = snapshot["schema"].rsplit(".", 1)[1]
        report = {"schema": "keel.muse.review-projection." + version, "snapshot_sha256": snapshot_hash,
            "source_sha256": snapshot["source_sha256"], "snapshot_id": snapshot["snapshot_id"], "as_of": now,
            "captured_at": snapshot["captured_at"], "snapshot_age_seconds": now - snapshot["captured_at"],
            "applications": projected, "question_plan": question_plan, "metrics": metrics, "snapshot": snapshot,
            "source_truth_authenticated": False, "human_identity_authenticated": False,
            "execution_authorized": False, "canonical_writes": 0, "network_requests": 0,
            "scope": "Private review projection from supplied observations; requests require authenticated host ingestion."}
        if session_minutes is not None:
            report["decision_session"] = plan_decision_session(specs, now, minute_budget=session_minutes)
        return report
    except (LokiError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, ReviewError):
            raise
        raise ReviewError("review_snapshot_invalid") from None


def validate_projection(report):
    _check(type(report) is dict and report.get("schema") in
           ("keel.muse.review-projection.v1", "keel.muse.review-projection.v2"), "projection_schema_invalid")
    rebuilt = project_review(report.get("snapshot"), now=report.get("as_of"),
                             expected_snapshot_sha256=report.get("snapshot_sha256"),
                             session_minutes=report.get("decision_session", {}).get("minute_budget")
                             if type(report.get("decision_session", {})) is dict else None)
    _check(digest(rebuilt) == digest(report), "projection_changed")
    return rebuilt


def record_session_effort(report, *, event_id, question_id, human_minutes, now,
                          expected_report_sha256):
    """Create a declared effort observation, never infer elapsed time or results.

The host must durably deduplicate event IDs and authenticate the reporting
human. Values may exceed the planned budget: real effort must not be clamped.
This pure function performs no write and is not an execution/approval request.
"""
    report = validate_projection(report)
    _check(digest(report) == expected_report_sha256, "report_pin_mismatch")
    require_id(event_id)
    require_id(question_id)
    _stamp(now)
    _check(now >= report["as_of"], "effort_before_projection")
    _check(type(human_minutes) in (int, float) and 0 < human_minutes <= 1440
           and math.isfinite(human_minutes), "invalid_observed_human_minutes")
    session = report.get("decision_session")
    _check(session is not None and question_id in session["selected_question_ids"],
           "question_not_in_session")
    return {"schema": "keel.muse.session-effort.v1", "event_id": event_id, "question_id": question_id,
            "report_sha256": digest(report), "snapshot_sha256": report["snapshot_sha256"],
            "recorded_at": now, "human_minutes": human_minutes, "measurement_source": "human_reported",
            "exceeds_planned_session_minutes": human_minutes > session["minute_budget"],
            "completed_task_ids": [], "interview_event_ids": [], "completion_inferred": False,
            "requires_authenticated_host_ingestion": True, "requires_event_id_deduplication": True,
            "human_identity_authenticated": False, "execution_authorized": False,
            "canonical_writes": 0, "network_requests": 0}


def make_review_request(report, *, application_id, question_id, response, now,
                        expected_report_sha256):
    """Export user intent only. Holds are retained; no approval is issued."""
    report = validate_projection(report)
    _check(digest(report) == expected_report_sha256, "report_pin_mismatch")
    _stamp(now)
    _check(now >= report["as_of"], "request_before_projection")
    app = next((a for a in report["applications"] if a["application_id"] == application_id), None)
    _check(app is not None, "application_not_found")
    question = next((q for q in app["questions"] if q["question_id"] == question_id), None)
    _check(question is not None, "question_not_found")
    _text(response, 16000)
    if question["kind"] == "approval":
        _check(response in ("approve", "reject", "defer"), "approval_intent_invalid")
    return {"schema": "keel.muse.review-request.v1", "kind": "review_request", "created_at": now,
        "report_sha256": digest(report), "snapshot_sha256": report["snapshot_sha256"],
        "application_id": application_id, "application_revision_sha256": app["application_revision_sha256"],
        "packet_revision_sha256": app["packet"]["revision_sha256"] if app["packet"] else None,
        "packet_projection_sha256": app["packet_projection_sha256"], "question_id": question_id,
        "question_sha256": question["question_sha256"], "question_kind": question["kind"],
        "scope_ids": question["scope_ids"] if question["kind"] == "fact" and question["reuse_authorized"] else [application_id],
        "response": response, "blocking_causes": app["blocking_causes"],
        "requires_authenticated_host_ingestion": True, "human_identity_authenticated": False,
        "execution_authorized": False, "canonical_writes": 0, "network_requests": 0}


def validate_review_request(request, current_report, *, expected_current_report_sha256, now):
    """Validate current bindings only; the result never grants authority."""
    _check(type(request) is dict, "request_schema_invalid")
    try:
        expected = make_review_request(current_report, application_id=request["application_id"],
            question_id=request["question_id"], response=request["response"], now=request["created_at"],
            expected_report_sha256=expected_current_report_sha256)
        _stamp(now)
        _check(request["created_at"] <= now, "future_request")
        _check(digest(expected) == digest(request), "stale_or_changed_review_request")
        # A correctly pinned old file can still age past an evidence expiry or
        # deadline. Re-evaluate every allowed response scope at ingestion time.
        fresh = project_review(current_report["snapshot"], now=now)
        for identity in expected["scope_ids"]:
            old_app = next(a for a in current_report["applications"] if a["application_id"] == identity)
            new_app = next(a for a in fresh["applications"] if a["application_id"] == identity)
            ignored = {"queue_age_seconds"}
            _check({k: v for k, v in old_app.items() if k not in ignored} ==
                   {k: v for k, v in new_app.items() if k not in ignored},
                   "review_scope_expired")
    except (KeyError, TypeError, LokiError):
        raise ReviewError("stale_or_changed_review_request") from None
    return {"schema": "keel.muse.review-request-check.v1", "status": "BINDINGS_MATCH",
            "requires_authenticated_host_ingestion": True, "human_identity_authenticated": False,
            "execution_authorized": False}


def example_snapshot():
    """Synthetic adapter example, deliberately includes missing/stale evidence."""
    now = 1789776000
    source = {"evidence_id": "employment", "source_id": "fixture-record", "source_revision_sha256": "c" * 64,
        "label": "Employment history · verified fixture", "reference": "fixture://employment-history",
        "kind": "source", "status": "verified", "scope_ids": ["app-01"],
        "permitted_uses": ["application_fact", "review"], "observed_at": now - 3600,
        "valid_until": None, "binding": None}
    packet = {"revision_sha256": "d" * 64, "fields": [{"field_id": "motivation", "label": "Why this role?",
        "value": "I would bring my verified operations experience to this role.", "required": True,
        "evidence_ids": ["employment"]}], "attachments": [{"attachment_id": "resume", "name": "Resume.pdf", "sha256": "e" * 64}]}
    previous = clone(packet)
    previous["revision_sha256"] = "f" * 64
    previous["fields"][0]["value"] = "I would bring my verified experience to this role."
    app = {"application_id": "app-01", "role": "Operations Manager", "employer": "Northline · fixture",
        "created_at": now - 86400 * 2, "deadline": now + 86400, "application_revision_sha256": "b" * 64,
        "packet": packet, "previous_packet": previous, "holds": [], "evidence": [source],
        "questions": [{"question_id": "review-01", "kind": "approval", "scope_ids": ["app-01"],
            "prompt": "Review this exact application and attached packet. What decision should the authenticated host request?",
            "estimated_minutes": 2, "reuse_authorized": False}],
        "approval_observation": {"application_id": "app-01", "application_revision_sha256": "b" * 64,
            "packet_revision_sha256": previous["revision_sha256"], "packet_projection_sha256": packet_projection_digest(previous),
            "observed_at": now - 7200, "decision": "approved", "source_record_sha256": "1" * 64}}
    blocked = clone(app)
    blocked.update(application_id="app-02", role="Customer Success Lead", employer="Harbor Works · fixture",
        packet=None, previous_packet=None, approval_observation=None, evidence=[],
        holds=[{"code": "DEDUPE-HOLD", "since": now - 7200}],
        questions=[{"question_id": "fact-02", "kind": "fact", "scope_ids": ["app-02"],
            "prompt": "What location preferences should be used for this role?", "estimated_minutes": 1,
            "reuse_authorized": False}])
    return {"schema": "keel.muse.review-snapshot.v1", "snapshot_id": "synthetic-review-desk",
        "source_sha256": "a" * 64, "captured_at": now, "telemetry_complete": False,
        "applications": [app, blocked], "events": [{"event_id": "prepared-01", "application_id": "app-01",
            "at": now - 1800, "kind": "prepared", "verification": "unverified",
            "source_record_sha256": None, "latency_ms": None, "detail": "Packet revised; fresh review required."},
            {"event_id": "call-01", "application_id": "app-01", "at": None, "kind": "model_call",
            "verification": "unknown", "source_record_sha256": None, "latency_ms": None,
            "detail": "Example telemetry is incomplete. No timing is inferred."}]}
