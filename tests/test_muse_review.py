"""Exact review bindings, scoped answers and honest operational observations."""
from copy import deepcopy

import pytest

from keel_loki.common import digest
from keel_muse.review import (ReviewError, example_snapshot, make_review_request,
    packet_projection_digest, project_review, validate_projection, validate_review_request)


@pytest.fixture
def snapshot():
    return example_snapshot()


def project(snapshot, **kwargs):
    return project_review(snapshot, now=kwargs.pop("now", snapshot["captured_at"]), **kwargs)


def request(report, **kwargs):
    return make_review_request(report, application_id=kwargs.pop("application_id", "app-01"),
        question_id=kwargs.pop("question_id", "review-01"), response=kwargs.pop("response", "approve"),
        now=kwargs.pop("now", report["as_of"]), expected_report_sha256=digest(report), **kwargs)


def check(req, report, **kwargs):
    return validate_review_request(req, report, expected_current_report_sha256=digest(report),
                                   now=kwargs.pop("now", report["as_of"]), **kwargs)


def add_receipt(snapshot, *, binding=True, kind="receipt", state="verified"):
    app = snapshot["applications"][0]
    receipt = deepcopy(app["evidence"][0])
    receipt.update(evidence_id="receipt", source_id="receipt-source", source_revision_sha256="2" * 64,
                   kind=kind, status=state, permitted_uses=["review"],
                   binding={"application_revision_sha256": app["application_revision_sha256"],
                            "packet_revision_sha256": app["packet"]["revision_sha256"]} if binding else None)
    app["evidence"].append(receipt)
    snapshot["events"].append({"event_id": "submit-01", "application_id": "app-01", "kind": "submission_observed",
        "at": snapshot["captured_at"] - 60, "verification": "verified", "source_record_sha256": "2" * 64,
        "latency_ms": None, "detail": "Caller-supplied receipt observation; no live action."})
    return receipt


def test_projection_keeps_exact_question_changes_and_holds(snapshot):
    original = deepcopy(snapshot)
    report = project(snapshot, expected_snapshot_sha256=digest(snapshot))
    first, second = report["applications"]
    assert first["questions"][0]["prompt"] == snapshot["applications"][0]["questions"][0]["prompt"]
    assert first["questions"][0]["question_sha256"] == digest(snapshot["applications"][0]["questions"][0])
    assert first["changes"][0]["before"]["value"] == snapshot["applications"][0]["previous_packet"]["fields"][0]["value"]
    assert first["changes"][0]["after"]["value"] == snapshot["applications"][0]["packet"]["fields"][0]["value"]
    assert first["approval_state"] == "STALE"
    assert second["status"] == "BLOCKED"
    assert second["blocking_causes"] == ["DEDUPE-HOLD", "PACKET_MISSING"]
    assert snapshot == original
    assert report["execution_authorized"] is False
    assert report["source_truth_authenticated"] is False


@pytest.mark.parametrize("mutation", ["application", "packet", "projection", "other_application"])
def test_approval_is_stale_on_every_exact_binding_change(snapshot, mutation):
    app = snapshot["applications"][0]
    approval = app["approval_observation"]
    approval.update(application_id=app["application_id"], application_revision_sha256=app["application_revision_sha256"],
        packet_revision_sha256=app["packet"]["revision_sha256"], packet_projection_sha256=packet_projection_digest(app["packet"]))
    assert project(snapshot)["applications"][0]["approval_state"] == "APPROVAL_OBSERVED"
    if mutation == "application":
        app["application_revision_sha256"] = "8" * 64
    elif mutation == "packet":
        app["packet"]["revision_sha256"] = "8" * 64
    elif mutation == "projection":
        app["packet"]["fields"][0]["value"] += " Changed without an artifact revision."
    else:
        approval["application_id"] = "app-02"
    report = project(snapshot)
    assert report["applications"][0]["approval_state"] == "STALE"
    assert report["applications"][0]["execution_authorized"] is False


def test_same_revision_with_changed_packet_is_blocked(snapshot):
    app = snapshot["applications"][0]
    app["packet"]["revision_sha256"] = app["previous_packet"]["revision_sha256"]
    assert "PACKET_CONTENT_CHANGED_WITH_SAME_REVISION" in project(snapshot)["applications"][0]["blocking_causes"]


@pytest.mark.parametrize("source_change,cause", [
    ({"status": "unverified"}, "FIELD_EVIDENCE_UNVERIFIED"),
    ({"status": "rejected"}, "FIELD_EVIDENCE_REJECTED"),
    ({"scope_ids": ["app-02"]}, "FIELD_EVIDENCE_OUT_OF_SCOPE"),
    ({"permitted_uses": ["review"]}, "FIELD_EVIDENCE_OUT_OF_SCOPE"),
    ({"observed_at": None}, "FIELD_EVIDENCE_UNKNOWN_TIME"),
])
def test_fact_evidence_requires_verified_fresh_permitted_scope(snapshot, source_change, cause):
    snapshot["applications"][0]["evidence"][0].update(source_change)
    assert cause in project(snapshot)["applications"][0]["blocking_causes"]


def test_evidence_expiry_and_deadline_boundaries_block(snapshot):
    app = snapshot["applications"][0]
    app["evidence"][0]["valid_until"] = snapshot["captured_at"] + 10
    assert "FIELD_EVIDENCE_EXPIRED" not in project(snapshot, now=snapshot["captured_at"] + 9)["applications"][0]["blocking_causes"]
    assert "FIELD_EVIDENCE_EXPIRED" in project(snapshot, now=snapshot["captured_at"] + 10)["applications"][0]["blocking_causes"]
    assert "DEADLINE_PASSED" in project(snapshot, now=app["deadline"])["applications"][0]["blocking_causes"]


def test_absent_required_values_and_missing_sources_stay_missing(snapshot):
    fields = snapshot["applications"][0]["packet"]["fields"]
    fields[0]["value"] = " "
    assert "REQUIRED_FIELD_MISSING" in project(snapshot)["applications"][0]["blocking_causes"]
    fields[0]["value"] = "Claim"
    fields[0]["evidence_ids"] = []
    assert "FIELD_EVIDENCE_MISSING" in project(snapshot)["applications"][0]["blocking_causes"]
    fields[0]["evidence_ids"] = ["missing-record"]
    assert "FIELD_EVIDENCE_MISSING" in project(snapshot)["applications"][0]["blocking_causes"]


def test_fact_reuse_is_only_the_declared_shared_scope(snapshot):
    shared = {"question_id": "shared-location", "kind": "fact", "scope_ids": ["app-01", "app-02"],
              "prompt": "Which locations are acceptable?", "estimated_minutes": 1, "reuse_authorized": True}
    for app in snapshot["applications"]:
        app["questions"] = [deepcopy(shared)]
    report = project(snapshot)
    req = request(report, question_id="shared-location", response="London")
    assert req["scope_ids"] == ["app-01", "app-02"]
    assert report["metrics"]["pending_questions"] == 1
    assert report["question_plan"]["ranking"][0]["approval_reused"] is False
    assert check(req, report)["execution_authorized"] is False


@pytest.mark.parametrize("kind", ["approval", "attestation", "unaided"])
def test_human_only_answers_cannot_be_reused_across_applications(snapshot, kind):
    shared = {"question_id": "bad-shared", "kind": kind, "scope_ids": ["app-01", "app-02"],
              "prompt": "Answer for both?", "estimated_minutes": 1, "reuse_authorized": True}
    for app in snapshot["applications"]:
        app["questions"] = [deepcopy(shared)]
    with pytest.raises(ReviewError):
        project(snapshot)


def test_reused_question_requires_identical_prompt_and_explicit_projection(snapshot):
    first = snapshot["applications"][0]["questions"][0]
    second = snapshot["applications"][1]["questions"][0]
    second["question_id"] = first["question_id"]
    with pytest.raises(ReviewError, match="inconsistent_reused_question"):
        project(snapshot)


@pytest.mark.parametrize("binding,kind,state", [(False, "receipt", "verified"), (True, "source", "verified"),
                                              (True, "receipt", "unverified")])
def test_verified_word_is_not_a_verified_submission_without_bound_receipt(snapshot, binding, kind, state):
    add_receipt(snapshot, binding=binding, kind=kind, state=state)
    metrics = project(snapshot)["metrics"]["submissions"]
    assert metrics["verified_observations"] == 0
    assert metrics["unverified_observations"] == 1
    assert metrics["success_rate"] is None


def test_receipt_is_bound_to_current_application_and_packet_and_holds_dominate(snapshot):
    receipt = add_receipt(snapshot)
    first = project(snapshot)["applications"][0]
    assert first["submission_status"] == "VERIFIED_OBSERVATION"
    assert first["status"] == "SUBMISSION_OBSERVED"
    snapshot["applications"][0]["holds"] = [{"code": "CONSENT-HOLD", "since": None}]
    assert project(snapshot)["applications"][0]["status"] == "BLOCKED"
    receipt["binding"]["packet_revision_sha256"] = "7" * 64
    assert project(snapshot)["applications"][0]["submission_status"] == "UNVERIFIED_OBSERVATION"


def test_missing_telemetry_never_becomes_zero_cost_or_a_success_rate(snapshot):
    metrics = project(snapshot)["metrics"]
    assert metrics["model_calls"]["value"] is None
    assert metrics["human_interventions"]["value"] is None
    assert metrics["model_latency_ms"]["p50"] is None
    assert metrics["model_latency_ms"]["missing_or_unverified"] == 1
    assert metrics["submissions"]["unknown"] == 2
    assert metrics["submissions"]["success_rate"] is None


def test_measured_latency_percentiles_and_counts_use_only_backed_observations(snapshot):
    snapshot["events"] = []
    snapshot["telemetry_complete"] = True
    for index, latency in enumerate([100, 200, 400]):
        snapshot["events"].append({"event_id": "call-" + str(index), "application_id": "app-01", "at": None,
            "kind": "model_call", "verification": "verified", "source_record_sha256": "c" * 64,
            "latency_ms": latency, "detail": "Measured fixture observation."})
    report = project(snapshot)
    assert report["metrics"]["model_calls"]["value"] == 3
    assert report["metrics"]["human_interventions"]["value"] == 0
    assert report["metrics"]["model_latency_ms"]["p50"] == 200
    assert report["metrics"]["model_latency_ms"]["p95"] == pytest.approx(380)
    snapshot["events"][0]["source_record_sha256"] = "9" * 64
    report = project(snapshot)
    assert report["metrics"]["model_calls"]["value"] is None
    assert report["metrics"]["model_calls"]["known_count"] == 2
    assert report["metrics"]["model_latency_ms"]["missing_or_unverified"] == 1


def test_timeline_keeps_unknown_time_explicit_and_sorted_last(snapshot):
    timeline = project(snapshot)["applications"][0]["timeline"]
    assert timeline[0]["event_id"] == "prepared-01"
    assert timeline[-1]["at"] is None


def test_exact_bound_review_request_is_only_unauthenticated_intent(snapshot):
    report = project(snapshot)
    req = request(report)
    assert req["response"] == "approve"
    assert req["scope_ids"] == ["app-01"]
    assert req["packet_revision_sha256"] == report["applications"][0]["packet"]["revision_sha256"]
    assert req["packet_projection_sha256"] == report["applications"][0]["packet_projection_sha256"]
    assert req["report_sha256"] == digest(report)
    assert req["requires_authenticated_host_ingestion"] is True
    assert req["execution_authorized"] is False
    assert req["human_identity_authenticated"] is False
    assert check(req, report)["status"] == "BINDINGS_MATCH"
    blocked = request(report, application_id="app-02", question_id="fact-02", response="London")
    assert blocked["blocking_causes"] == ["DEDUPE-HOLD", "PACKET_MISSING"]


@pytest.mark.parametrize("change", ["extra_authority", "packet", "question", "scope", "response", "hold"])
def test_request_rejects_forged_or_stale_bindings(snapshot, change):
    report = project(snapshot)
    req = request(report)
    if change == "extra_authority":
        req["approved"] = True
    elif change == "packet":
        snapshot["applications"][0]["packet"]["fields"][0]["value"] += " New."
        report = project(snapshot)
    elif change == "question":
        snapshot["applications"][0]["questions"][0]["prompt"] += " Exact new question."
        report = project(snapshot)
    elif change == "scope":
        req["scope_ids"] = ["app-01", "app-02"]
    elif change == "response":
        req["response"] = "approved=true"
    else:
        snapshot["applications"][0]["holds"] = [{"code": "CONSENT-HOLD", "since": None}]
        report = project(snapshot)
    with pytest.raises(ReviewError):
        check(req, report)


def test_request_rechecks_expiry_at_ingestion_without_rejecting_elapsed_seconds(snapshot):
    snapshot["applications"][0]["evidence"][0]["valid_until"] = snapshot["captured_at"] + 10
    report = project(snapshot)
    req = request(report)
    assert check(req, report, now=report["as_of"] + 9)["status"] == "BINDINGS_MATCH"
    with pytest.raises(ReviewError):
        check(req, report, now=report["as_of"] + 10)


def test_future_request_and_wrong_snapshot_pin_rejected(snapshot):
    report = project(snapshot)
    req = request(report, now=report["as_of"] + 1)
    with pytest.raises(ReviewError):
        check(req, report)
    with pytest.raises(ReviewError, match="snapshot_pin_mismatch"):
        project(snapshot, expected_snapshot_sha256="0" * 64)


def test_forged_projection_metrics_or_authority_rejected(snapshot):
    report = project(snapshot)
    report["metrics"]["submissions"]["success_rate"] = 1.0
    with pytest.raises(ReviewError, match="projection_changed"):
        validate_projection(report)
    report = project(snapshot)
    report["execution_authorized"] = True
    with pytest.raises(ReviewError, match="projection_changed"):
        validate_projection(report)


@pytest.mark.parametrize("field,value", [("verification", "PASS"), ("latency_ms", True),
                                        ("latency_ms", float("inf")), ("at", True)])
def test_fake_pass_and_invalid_measurements_are_rejected(snapshot, field, value):
    snapshot["events"][0][field] = value
    with pytest.raises(ReviewError):
        project(snapshot)


def test_duplicates_unknown_fields_and_future_snapshots_rejected(snapshot):
    duplicate = deepcopy(snapshot)
    duplicate["applications"].append(deepcopy(duplicate["applications"][0]))
    with pytest.raises(ReviewError):
        project(duplicate)
    snapshot["approved"] = True
    with pytest.raises(ReviewError):
        project(snapshot)
    snapshot.pop("approved")
    with pytest.raises(ReviewError, match="future_snapshot"):
        project(snapshot, now=snapshot["captured_at"] - 1)


def test_empty_queue_has_null_age_and_success_denominators(snapshot):
    snapshot.update(applications=[], events=[])
    report = project(snapshot)
    assert report["metrics"]["applications"] == 0
    assert report["metrics"]["queue_age_seconds"] == {"count": 0, "p50": None, "p95": None, "max": None}
    assert report["metrics"]["submissions"]["success_rate"] is None
