"""Measurement integrity and budget bounds for proposal-only source feedback."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from engines.source_feedback import FeedbackError, propose
from engines import source_yield_proposals as cli

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


def fixture():
    data = {"schema": "keel.source_feedback.v1", "observed_at": NOW.isoformat(),
            "window_start": (NOW - timedelta(days=1)).isoformat(), "window_end": NOW.isoformat(),
            "telemetry_complete": True, "objective": "qualified_leads", "horizon_days": 14,
            "sources": [], "observations": [], "applications": []}
    for sid, minutes, cap in (("a", 1, 3), ("b", 2, 5)):
        data["sources"].append({"source_id": sid, "permitted": True, "measurement_complete": True,
            "cap_minutes": cap, "cooldown_until": None, "cooldown_verified": True,
            "evidence_ref": "local:source-" + sid})
        for index in range(4):
            data["observations"].append({"observation_id": sid + str(index), "source_id": sid,
                "opportunity_id": sid + str(index), "observed_at": (NOW - timedelta(hours=2)).isoformat(),
                "qualified": True, "human_minutes": minutes, "application_id": None,
                "evidence_ref": "local:observation-" + sid + str(index)})
    return data


def run(data=None, budget=6):
    return propose(data or fixture(), budget_minutes=budget, now=NOW)


def source(report, sid):
    return next(row for row in report["sources"] if row["source_id"] == sid)


def interview_fixture():
    data = fixture()
    data["objective"] = "interviews"
    data["window_start"] = (NOW - timedelta(days=21)).isoformat()
    data["window_end"] = (NOW - timedelta(days=20)).isoformat()
    submitted = NOW - timedelta(days=20, hours=12)
    for row in data["observations"]:
        aid = "app-" + row["opportunity_id"]
        row.update(observed_at=submitted.isoformat(), application_id=aid)
        data["applications"].append({"application_id": aid, "source_id": row["source_id"],
            "fit_score": 85, "submission_confirmed_by_adapter": True, "submission_ref": "receipt:" + aid,
            "adapter_revision": "adapter-v1", "submitted_at": submitted.isoformat(),
            "followup_observed_through": NOW.isoformat(), "interview_at": (submitted + timedelta(days=2)).isoformat(),
            "interview_evidence_ref": "local:interview-" + aid, "offer_at": None, "rejection_at": None})
    return data


def test_measured_budget_has_caps_determinism_and_no_authority():
    result = run()
    assert result == run()
    assert source(result, "a")["proposed_minutes"] == 3
    assert source(result, "b")["proposed_minutes"] == 3
    assert result["allocated_minutes"] == 6 and result["unallocated_minutes"] == 0
    assert result["execution_authorized"] is False and result["human_review_required"] is True
    assert result["schedule_writes"] == 0 and result["causal_improvement_established"] is False
    capped = run(budget=20)
    assert capped["allocated_minutes"] == 8 and capped["unallocated_minutes"] == 12


def test_reordered_sources_and_observations_do_not_change_allocation():
    data = fixture()
    data["sources"].reverse()
    data["observations"].reverse()
    assert run(data)["sources"] == run()["sources"]


def test_exact_observation_duplicates_dont_double_yield_or_effort():
    data = fixture()
    data["observations"].append(deepcopy(data["observations"][0]))
    result = run(data)
    assert result["duplicate_observations_removed"] == 1
    assert source(result, "a")["recorded_human_minutes"] == 4
    assert source(result, "a")["qualified_opportunities"] == 4
    data["observations"][-1]["human_minutes"] = 50
    with pytest.raises(FeedbackError, match="conflicting_observation_id"):
        run(data)


def test_rechecks_count_effort_but_not_extra_yield():
    data = fixture()
    data["observations"].append(dict(data["observations"][0], observation_id="repeat", human_minutes=5, evidence_ref="local:recheck"))
    row = source(run(data), "a")
    assert row["unique_opportunities"] == 4 and row["recorded_human_minutes"] == 9
    assert row["qualified_opportunities"] == 4


def test_latest_opportunity_observation_controls_current_quality():
    data = fixture()
    data["observations"].append(dict(data["observations"][0], observation_id="repeat", qualified=False,
        observed_at=(NOW - timedelta(hours=1)).isoformat()))
    assert source(run(data), "a")["qualified_opportunities"] == 3


@pytest.mark.parametrize("field,value,reason", [("permitted", None, "PERMISSION_UNKNOWN_OR_DENIED"),
    ("cooldown_verified", None, "COOLDOWN_UNKNOWN"), ("measurement_complete", False, "MEASUREMENT_INCOMPLETE"),
    ("cooldown_until", (NOW + timedelta(minutes=1)).isoformat(), "COOLDOWN_ACTIVE"),
    ("cap_minutes", 0, "SOURCE_CAP_ZERO")])
def test_source_holds_do_not_spend_budget(field, value, reason):
    data = fixture()
    data["sources"][0][field] = value
    row = source(run(data), "a")
    assert reason in row["hold_reasons"] and row["proposed_minutes"] == 0
    assert row["conservative_yield_per_human_minute"] is None


@pytest.mark.parametrize("field,reason", [("human_minutes", "HUMAN_EFFORT_UNKNOWN"), ("qualified", "QUALIFICATION_UNKNOWN")])
def test_unknown_measurement_is_not_zero(field, reason):
    data = fixture()
    data["observations"][0][field] = None
    row = source(run(data), "a")
    assert reason in row["hold_reasons"] and row["proposed_minutes"] == 0


def test_cross_source_opportunity_conflict_holds_both_sources():
    data = fixture()
    data["observations"][4]["opportunity_id"] = data["observations"][0]["opportunity_id"]
    result = run(data)
    assert result["status"] == "HOLD" and result["allocated_minutes"] == 0
    assert all("OPPORTUNITY_ATTRIBUTION_CONFLICT" in row["hold_reasons"] for row in result["sources"])


def test_incomplete_or_stale_snapshot_never_allocates():
    data = fixture()
    data["telemetry_complete"] = False
    assert run(data)["allocated_minutes"] == 0
    old_now = NOW + timedelta(days=2)
    assert propose(fixture(), budget_minutes=6, now=old_now)["allocated_minutes"] == 0


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf")])
def test_invalid_effort_is_rejected(value):
    data = fixture()
    data["observations"][0]["human_minutes"] = value
    with pytest.raises(FeedbackError):
        run(data)


@pytest.mark.parametrize("stamp", ["2026-09-24T11:00:00", (NOW + timedelta(seconds=1)).isoformat(), "yesterday"])
def test_invalid_or_future_timestamps_are_rejected(stamp):
    data = fixture()
    data["observations"][0]["observed_at"] = stamp
    with pytest.raises(FeedbackError):
        run(data)


def test_mature_observed_outcomes_are_accepted_without_causal_claims():
    report = run(interview_fixture())
    assert report["outcome_report"]["denominator"] == 8
    assert report["allocated_minutes"] == 6
    assert source(report, "a")["observed_yield"] == 4
    assert report["off_policy_estimate"] is False


@pytest.mark.parametrize("change", ["incomplete", "unconfirmed", "missing", "immature"])
def test_incomplete_outcome_cohort_is_held_not_counted_as_failure(change):
    data = interview_fixture()
    if change == "incomplete":
        data["applications"][0]["followup_observed_through"] = data["applications"][0]["submitted_at"]
    elif change == "unconfirmed":
        data["applications"][0]["submission_confirmed_by_adapter"] = False
    elif change == "missing":
        data["applications"].pop(0)
    else:
        data["horizon_days"] = 30
    row = source(run(data), "a")
    assert row["status"] == "HOLD" and row["proposed_minutes"] == 0
    assert row["observed_yield_per_human_minute"] is None


def test_outcome_attribution_must_match_source():
    data = interview_fixture()
    data["applications"][0]["source_id"] = "b"
    with pytest.raises(FeedbackError, match="application_source_binding_mismatch"):
        run(data)


def test_receipt_reuse_holds_affected_cohorts():
    data = interview_fixture()
    data["applications"][4]["submission_ref"] = data["applications"][0]["submission_ref"]
    assert run(data)["allocated_minutes"] == 0


def test_zero_yield_and_zero_budget_remain_unallocated():
    data = fixture()
    for row in data["observations"]:
        row["qualified"] = False
    assert run(data)["allocated_minutes"] == 0
    assert run(budget=0)["allocated_minutes"] == 0
    with pytest.raises(FeedbackError):
        run(budget=241)


def test_cli_feedback_uses_local_input_without_legacy_reads(tmp_path, monkeypatch, capsys):
    path = tmp_path / "feedback.json"
    path.write_text(json.dumps(fixture()))
    monkeypatch.setattr(cli, "load_events", lambda *a, **k: pytest.fail("legacy read was attempted"))
    assert cli.main(["--feedback-input", str(path), "--budget-minutes", "6", "--now", NOW.isoformat()]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["allocated_minutes"] == 6
    assert cli.main(["--feedback-input", str(tmp_path / "missing"), "--budget-minutes", "6"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "HOLD"


def test_renamed_duplicate_observation_is_not_new_effort():
    data = fixture()
    data["observations"].append(dict(data["observations"][0], observation_id="renamed"))
    report = run(data)
    assert report["duplicate_observations_removed"] == 1
    assert source(report, "a")["recorded_human_minutes"] == 4


def test_zero_success_roundoff_never_creates_positive_budget():
    data = fixture()
    data["observations"] = [row for row in data["observations"] if not row["observation_id"].endswith("3")]
    for row in data["observations"]:
        row["qualified"] = False
    result = run(data)
    assert result["allocated_minutes"] == 0
    assert all(row["conservative_yield_per_human_minute"] == 0 for row in result["sources"])


def test_unrepresentable_derived_rate_is_rejected_by_public_api():
    data = fixture()
    data["observations"] = data["observations"][:1]
    data["observations"][0]["human_minutes"] = 1e-323
    with pytest.raises(FeedbackError, match="unrepresentable_yield_rate"):
        run(data)
    data["observations"][0]["human_minutes"] = 10 ** 309
    with pytest.raises(FeedbackError, match="invalid_human_minutes"):
        run(data)
