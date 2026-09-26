"""Integrated regressions; synthetic observations, actual supplied contracts."""
import contextlib
import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "engines"))
from tools.make_flow_demo import NOW, make_snapshot, lead, source, hold, capacity, release, application, question
from keel_flow.common import ContractError, strict_json, wilson
from keel_flow.forecast import forecast
from keel_flow.discovery import allocate
from keel_flow.holds import review_holds, hold_scope
from keel_flow.questions import prioritize
from keel_flow.attempts import intent, reconcile
from keel_flow.outcomes import cohorts
from keel_flow.bridge import compare_supply, inspect_attempts
from keel_flow.board import build, markdown
from keel_flow.journal import record_event, events_from_log
from keel_flow.__main__ import main
import log_event


def plan(executable=5, measurement=None, releases=None, **kw):
    return forecast(executable, capacity() if measurement is None else measurement,
                    [release()] if releases is None else releases, now=NOW, active=True, **kw)


def decision(h, **changes):
    return {"decision_id": "review-1", "hold_id": h["hold_id"], "evidence_revision": h["evidence_revision"],
            "hold_scope_sha256": hold_scope(h), "authority_ref": "synthetic-reviewer-record",
            "decision": "PROPOSE_RELEASE", "decided_at": NOW.isoformat(), "evidence_refs": ["synthetic-review-record"], **changes}


def events(): return make_snapshot()["attempt_events"]


def test_full_report_reduces_nominal_five_to_one_without_mutation():
    data = make_snapshot(); before = copy.deepcopy(data)
    report = build(data, now=NOW)
    assert data == before
    assert (report["readiness"]["nominal_ready"], report["readiness"]["executable_ready"]) == (5, 1)
    assert report["forecast"]["state"] == "STARVATION_RISK"
    assert report["integration"]["supply"]["contracts_agree"]
    assert report["outcomes"]["denominator"] == 2
    assert all(value == 0 for value in report["effects"].values())
    assert not report["execution_authorized"]


@pytest.mark.parametrize("change", ["stale", "future", "partial", "history", "ready_conflict"])
def test_bad_export_cannot_propose_discovery_or_ask_questions(change):
    data = make_snapshot()
    if change == "stale": data["observed_at"] = (NOW - timedelta(seconds=91)).isoformat()
    if change == "future": data["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    if change == "partial": data["complete"] = False
    if change == "history": data["attempt_history_complete"] = False
    if change == "ready_conflict": data["pool"]["ready"] = 6
    report = build(data, now=NOW)
    assert report["status"] == "UNVERIFIED"
    assert report["discovery"]["allocated_minutes"] == 0
    assert report["forecast"]["state"] == "UNKNOWN"
    assert not report["readiness"]["current_estimate_verified"]
    assert "ASK_PRIORITIZED_QUESTIONS" not in [a["action"] for a in report["actions"]]


@pytest.mark.parametrize("change", ["fit", "duplicate", "consent", "stale", "packet", "unmapped_attempt"])
def test_ready_gates_join_across_contracts(change):
    data = make_snapshot(); row = data["leads"][0]
    if change == "fit": row["fit_score"] = 74
    if change == "duplicate": data["leads"][1]["identity"] = row["identity"]
    if change == "consent": row["holds"] = ["consent"]
    if change == "stale": row["observed_at"] = (NOW - timedelta(seconds=91)).isoformat()
    if change == "packet": row["dependencies"]["answers"] = "changed"
    if change == "unmapped_attempt": row["attempt_state"] = "UNRECOGNIZED"
    report = build(data, now=NOW)
    assert report["readiness"]["executable_ready"] == 0


def test_question_gate_conflicts_reject_export():
    data = make_snapshot(); data["question_dependencies"][0]["fit_score"] = 99
    with pytest.raises(ContractError): build(data, now=NOW)


@pytest.mark.parametrize("seconds,state", [(0, "HEALTHY"), (90, "HEALTHY"), (91, "UNVERIFIED"), (-1, "UNVERIFIED")])
def test_bridge_freshness_uses_actual_both_implementations(seconds, state):
    result = compare_supply({"schema_version": 1, "ready": 5, "actionable": 1,
                             "observed_at": (NOW - timedelta(seconds=seconds)).isoformat()}, now=NOW)
    assert result["state"] == state and result["contracts_agree"]


def test_bridge_full_count_grid_and_missing_observations():
    for ready in (None, 0, 4, 5, 10):
        for actionable in (None, 0, 1, 200):
            result = compare_supply({"schema_version": 1, "ready": ready, "actionable": actionable, "observed_at": NOW.isoformat()}, now=NOW)
            assert result["contracts_agree"]


def test_prediction_accounts_for_cooldown_verification_and_preparation():
    result = plan(executable=1)
    assert result["runway_seconds"] == 300
    assert result["first_refill_opportunity_seconds"] == 1200
    assert result["earliest_exhaustion_seconds"] == 300
    assert result["expected_deficit_at_horizon"] == 9


def test_late_second_refill_detects_intermittent_starvation():
    items = [release(release_id="early", available_at=NOW.isoformat(), p95_verify_seconds=0, p95_prepare_seconds=0, count=1, estimated_conversion=1),
             release(release_id="late", available_at=(NOW + timedelta(seconds=2000)).isoformat(), count=100, estimated_conversion=1)]
    result = plan(executable=1, releases=items)
    assert result["state"] == "STARVATION_RISK" and result["earliest_exhaustion_seconds"] == 600
    assert result["expected_deficit_at_horizon"] == 0  # end totals alone hide the gap


def test_sufficient_early_refill_is_conditional_coverage():
    result = plan(executable=5, releases=[release(available_at=NOW.isoformat(), p95_verify_seconds=0,
                                                p95_prepare_seconds=30, count=50, estimated_conversion=.5)])
    assert result["state"] == "COVERED_UNDER_ASSUMPTIONS"
    assert result["earliest_exhaustion_seconds"] is None


def test_small_cohort_expectations_are_not_erased_by_flooring():
    # Five single-lead cohorts at pooled conversion 76/2462 each floor to 0
    # whole-item reserves; the combined ~0.154 expected promotions must still
    # enter the model instead of vanishing into REFILL_UNMEASURED.
    conv = 76 / 2462
    items = [release(release_id=f"cohort-{i}", available_at=NOW.isoformat(),
                     p95_verify_seconds=0, p95_prepare_seconds=0,
                     count=1, estimated_conversion=conv) for i in range(5)]
    result = plan(executable=5, releases=items)
    opps = result["release_opportunities"]
    assert len(opps) == 5
    assert all(o["estimated_leads"] == 0 for o in opps)  # conservative reserve
    assert sum(o["expected_leads_fractional"] for o in opps) == pytest.approx(5 * conv, rel=1e-9)
    assert result["state"] != "REFILL_UNMEASURED"
    # Deficit uses the fractional expectation: 12 - 5 - 5*conv, not 12 - 5.
    assert result["expected_deficit_at_horizon"] == pytest.approx(12 - 5 - 5 * conv, rel=1e-9)


@pytest.mark.parametrize("measurement", [capacity(value=0), capacity(measured_while_supplied=False),
                                         capacity(observed_at=(NOW - timedelta(days=2)).isoformat())])
def test_unmeasured_capacity_never_borrows_starved_recent_rate(measurement):
    assert plan(measurement=measurement)["state"] == "UNKNOWN"


def test_missing_replacements_and_pause_are_distinct():
    assert plan(releases=[])["state"] == "REFILL_UNMEASURED"
    assert forecast(5, None, [], now=NOW, active=True)["state"] == "UNKNOWN"
    assert forecast(5, None, [], now=NOW, active=False)["state"] == "SCHEDULED_PAUSE"


def test_duplicate_refill_batches_rejected():
    with pytest.raises(ContractError): plan(releases=[release(), release()])


def test_release_roles_cannot_be_counted_under_two_different_batches():
    one = release(); two = release(release_id="different", role_ids=one["role_ids"])
    with pytest.raises(ContractError): plan(releases=[one, two])


def test_release_count_and_members_must_agree():
    with pytest.raises(ContractError): plan(releases=[release(role_ids=[])])


def test_ready_inventory_not_also_counted_as_future_supply():
    data = make_snapshot(); data["releases"][0]["role_ids"][0] = "role-1"
    with pytest.raises(ContractError): build(data, now=NOW)


@pytest.mark.parametrize("change", [{"fit_score": 74}, {"holds": ["consent"]}, {"policy_pass": False}])
def test_future_supply_must_preserve_held_and_unqualified_roles(change):
    data = make_snapshot(); data["leads"][5].update(change)
    with pytest.raises(ContractError): build(data, now=NOW)


def test_unknown_forecast_is_an_actionable_measurement_gap():
    data = make_snapshot(); data["capacity"] = None
    result = build(data, now=NOW)
    assert "MEASURE_SUPPLIED_CAPACITY" in [row["action"] for row in result["actions"]]


def test_hold_decision_is_bound_and_never_releases():
    h = hold(); d = decision(h)
    result = review_holds([h], [d], now=NOW)["rows"][0]
    assert result["review_reusable_for_inspection"] and result["action"] == "VALIDATE_CANONICAL_RELEASE"
    assert not result["release_authorized"]
    for field, value in (("evidence_revision", "new"), ("role_id", "other"), ("release_condition", "different"), ("family", "policy")):
        changed = {**h, field: value}
        assert not review_holds([changed], [d], now=NOW)["rows"][0]["review_reusable_for_inspection"]


def test_consent_hold_requires_reply_even_after_release_proposal():
    h = hold(family="consent")
    assert review_holds([h], [decision(h)], now=NOW)["rows"][0]["action"] == "AWAIT_OPERATOR_REPLY"


def test_unowned_and_repeated_holds_get_concrete_actions():
    assert review_holds([hold(owner=None)], [], now=NOW)["rows"][0]["action"] == "ASSIGN_OWNER"
    assert review_holds([hold(review_after=(NOW + timedelta(days=1)).isoformat())], [], now=NOW)["rows"][0]["action"] == "ESCALATE_REVIEW"


def test_conflicting_reviews_never_choose_a_winner():
    h = hold(); d = decision(h)
    row = review_holds([h], [d, {**d, "decision_id": "other", "decision": "KEEP"}], now=NOW)["rows"][0]
    assert row["action"] == "REVIEW_CONFLICTING_DECISIONS" and row["review_record_id"] is None


def test_missing_owner_is_not_silently_defaulted():
    h = hold(); del h["owner"]
    with pytest.raises(ContractError): review_holds([h], [], now=NOW)


@pytest.mark.parametrize("budget", [0, 1, 3, 30, 239, 240])
def test_discovery_budget_and_employer_group_cap_are_conserved(budget):
    rows = [source("a", employer_group="same"), source("b", employer_group="same"), source("c")]
    result = allocate(rows, budget_minutes=budget, now=NOW)
    assert result["allocated_minutes"] + result["unallocated_minutes"] == budget
    group = sum(r["allocated_minutes"] for r in result["allocations"] if r["employer_group"] == "same")
    assert group <= budget // 2
    assert not result["execution_authorized"] and result["http_requests"] == 0


def test_discovery_exploration_bounded_and_zero_yield_not_invented():
    result = allocate([source("a", checks=0, qualified_unique_live=0, verification_minutes=0)], budget_minutes=30, now=NOW)
    assert result["allocated_minutes"] == 3 and result["unallocated_minutes"] == 27


@pytest.mark.parametrize("change", [{"rate_limited": True}, {"retry_after": (NOW + timedelta(seconds=1)).isoformat()}])
def test_rate_hold_stops_all_discovery_proposals(change):
    result = allocate([source("a"), source("b", **change)], budget_minutes=30, now=NOW)
    assert result["state"] == "RATE_LIMIT_RECONCILIATION_REQUIRED" and result["allocated_minutes"] == 0


def test_discovery_excludes_stale_incomplete_and_unpermitted_sources():
    result = allocate([source("a", observed_at=(NOW - timedelta(days=2)).isoformat()),
                       source("b", measurement_complete=False), source("c", permitted=False)], budget_minutes=30, now=NOW)
    assert not result["allocations"] and len(result["excluded"]) == 3


def test_discovery_requires_exact_fit_floor():
    with pytest.raises(ContractError): allocate([source("a", fit_floor=74)], budget_minutes=30, now=NOW)


def test_low_sample_yield_is_not_overconfident():
    small, large = wilson(1, 1), wilson(100, 100)
    assert small[0] < large[0] and wilson(0, 0) is None


def test_questions_do_not_double_count_multi_question_unlocks():
    data = make_snapshot(); result = prioritize(data["tray"], data["question_dependencies"], now=NOW)
    travel = next(g for g in result["groups"] if "travel" in g["question_ids"])
    assert travel["roles_unlocked_if_answered"] == ["role-4"]
    assert travel["roles_progressed"] == ["role-4", "role-5"]
    assert all(not g["resolved"] for g in result["groups"])


def test_consent_quarantine_never_surfaces_disputed_yes():
    data = make_snapshot(); data["tray"]["questions"][1]["answer_bank_key"] = "consent"
    data["tray"]["answer_bank"] = {"answers": {"consent": "YES"}}
    result = prioritize(data["tray"], data["question_dependencies"], now=NOW)
    q = next(g for g in result["groups"] if "consent" in g["question_ids"])
    assert q["classification"] == "CONSENT_QUARANTINED" and q["answer_bank_candidates"] == []


def test_question_grouping_preserves_employer_scope():
    tray = {"schema_version": 1, "questions": [question("a"), question("b", employer="Different")]}
    assert prioritize(tray, [], now=NOW)["display_groups"] == 2


def test_question_deadline_and_other_gates_remove_false_unlocks():
    data = make_snapshot()
    data["question_dependencies"][0]["deadline"] = NOW.isoformat()
    data["question_dependencies"][1]["other_gates_clear"] = False
    result = prioritize(data["tray"], data["question_dependencies"], now=NOW)
    assert all(not g["roles_progressed"] for g in result["groups"])


def test_attempt_identity_and_content_binding():
    kwargs = dict(nonce="n", observed_at=NOW.isoformat())
    a = intent("candidate", "provider", "employer", "posting", {"packet": 1}, **kwargs)
    b = intent("candidate", "provider", "employer", "posting", {"packet": 2}, **kwargs)
    assert a["attempt_id"] == b["attempt_id"] and a["content_hash"] != b["content_hash"]
    with pytest.raises(ContractError): reconcile([a, b], now=NOW, history_complete=True)


def test_replay_identical_event_ignored_and_unknown_stays_held():
    rows = events(); result = reconcile(rows + [rows[-1]], now=NOW, history_complete=True)
    assert result["duplicate_events_ignored"] == 1
    assert result["applications"][0]["hold_required"] and not result["applications"][0]["retry_authorized"]


def test_cancelled_before_dispatch_not_counted_as_unresolved():
    first = events()[0]; cancelled = {**first, "event_id": "cancel", "sequence": 1, "state": "CANCELLED_BEFORE_DISPATCH"}
    result = reconcile([first, cancelled], now=NOW, history_complete=True)
    assert not result["applications"][0]["hold_required"]
    assert not result["applications"][0]["retry_authorized"]


def test_cancelled_after_unknown_cannot_clear_hold():
    rows = events(); rows.append({**rows[-1], "event_id": "bad-cancel", "sequence": 3, "state": "CANCELLED_BEFORE_DISPATCH"})
    result = reconcile(rows, now=NOW, history_complete=True)
    assert result["applications"][0]["hold_required"]
    assert "INVALID_TRANSITION" in result["attempts"][0]["reasons"]


def test_confirmation_report_is_not_provider_acceptance():
    rows = events(); rows.append({**rows[-1], "event_id": "claimed-receipt", "sequence": 3, "state": "CONFIRMATION_REPORTED"})
    result = reconcile(rows, now=NOW, history_complete=True)
    assert result["applications"][0]["hold_required"] and not result["provider_acceptance_verified"]
    bridge = inspect_attempts(rows)
    assert not bridge["confirmation_reports_mapped_to_completed"]


def test_invalid_retry_after_unknown_detected_by_both_packages():
    rows = events(); rows.append({**rows[-1], "event_id": "bad-retry", "sequence": 3, "state": "DISPATCHED"})
    assert "INVALID_TRANSITION" in reconcile(rows, now=NOW, history_complete=True)["attempts"][0]["reasons"]
    assert inspect_attempts(rows)["findings"][0]["kind"] == "RETRY_AFTER_UNKNOWN_REQUIRES_CANONICAL_RECONCILIATION"


def test_multiple_unresolved_attempts_flag_application():
    rows = events(); first = rows[0]
    rows.append({**first, "event_id": "different-attempt", "attempt_id": "attempt-2"})
    assert reconcile(rows, now=NOW, history_complete=True)["applications"][0]["multiple_unresolved_attempts"]


@pytest.mark.parametrize("change,reason", [("gap", "SEQUENCE_GAP"), ("clock", "CLOCK_ORDER_CONFLICT"), ("missing", "MISSING_INITIAL_INTENT")])
def test_attempt_export_gaps_are_visible(change, reason):
    rows = events()
    if change == "gap": rows.pop(1)
    if change == "clock": rows[2]["observed_at"] = (NOW - timedelta(days=1)).isoformat()
    if change == "missing": rows.pop(0)
    assert reason in reconcile(rows, now=NOW, history_complete=True)["attempts"][0]["reasons"]


def test_intent_durably_uses_real_existing_logger_and_replay_rules(tmp_path):
    target = tmp_path / "events.jsonl"; event = events()[0]
    with patch.object(log_event, "EVENTS", str(target)):
        result = record_event(event, logger=log_event.log, role_id="role-3", now=NOW)
        record_event(event, logger=log_event.log, role_id="role-3", now=NOW)
        assert result["recorded"] and not result["execution_authorized"]
        with pytest.raises(ValueError):
            record_event({**event, "content_hash": "a" * 64}, logger=log_event.log, role_id="role-3", now=NOW)
    rows = [json.loads(line) for line in target.read_text().splitlines()]
    assert len(rows) == 1 and events_from_log(rows) == [event]
    assert "packet_revision" not in target.read_text()  # hashed request, no request body


def test_logger_acknowledgment_and_write_failure_propagate():
    with pytest.raises(ContractError): record_event(events()[0], logger=lambda *a, **k: {}, role_id="role-3", now=NOW)
    def failing(*args, **kwargs): raise OSError("synthetic disk full")
    with pytest.raises(OSError): record_event(events()[0], logger=failing, role_id="role-3", now=NOW)


def test_cohorts_exclude_immature_unconfirmed_and_incomplete_followup():
    rows = make_snapshot()["applications"]
    rows.append(application("partial", followup_observed_through=(NOW - timedelta(days=10)).isoformat()))
    result = cohorts(rows, now=NOW)
    assert result["denominator"] == 2
    assert {r["reason"] for r in result["excluded"]} == {"COHORT_IMMATURE", "SUBMISSION_UNCONFIRMED", "FOLLOWUP_INCOMPLETE"}
    assert result["cohorts"][0]["interview_rate"] == .5 and result["cohorts"][0]["rejection"] == 0


def test_outcomes_after_fixed_window_not_credited_to_short_horizon():
    rows = [application("a", interview_at=(NOW - timedelta(days=1)).isoformat(), interview_evidence_ref="late")]
    assert cohorts(rows, now=NOW)["cohorts"][0]["interview"] == 0


def test_reused_submission_evidence_not_counted_twice():
    rows = [application("a"), application("b", submission_ref="synthetic-receipt-a")]
    result = cohorts(rows, now=NOW)
    assert result["denominator"] == 0 and len(result["excluded"]) == 2


def test_source_and_fit_cohorts_separated():
    rows = [application("a"), application("b", source_id="other"), application("c", fit_score=90)]
    result = cohorts(rows, now=NOW)
    assert len(result["cohorts"]) == 3 and not result["causal_superiority_established"]


def test_outcomes_require_evidence_and_valid_observation_window():
    with pytest.raises(ContractError): cohorts([application("a", interview_at=NOW.isoformat())], now=NOW)
    with pytest.raises(ContractError): cohorts([application("a", followup_observed_through=(NOW + timedelta(days=1)).isoformat())], now=NOW)


def test_cli_json_markdown_and_exclusive_output(tmp_path):
    src = tmp_path / "export.json"; out = tmp_path / "report.json"
    src.write_text(json.dumps(make_snapshot()))
    argv = ["board", str(src), "--now", NOW.isoformat(), "--out", str(out)]
    assert main(argv) == 0 and json.loads(out.read_text())["readiness"]["executable_ready"] == 1
    original = out.read_bytes(); assert main(argv) == 2 and out.read_bytes() == original
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        assert main(["board", str(src), "--now", NOW.isoformat(), "--format", "markdown"]) == 0
    assert "Executable estimate | 1" in stream.getvalue() and "SYNTHETIC-DEMO" in stream.getvalue()


@pytest.mark.parametrize("raw", ['{"schema_version":1,"schema_version":1}', '{"x":NaN}', '{"x":1e999}', '[]'])
def test_cli_invalid_input_creates_no_report(tmp_path, raw):
    src = tmp_path / "bad.json"; out = tmp_path / "report.json"; src.write_text(raw)
    assert main(["board", str(src), "--now", NOW.isoformat(), "--out", str(out)]) == 2
    assert not out.exists()
