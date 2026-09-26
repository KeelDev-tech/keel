"""Operational question bundles, fair turns, revision pins and real effort."""
from copy import deepcopy
import importlib.util
import itertools
from pathlib import Path
import random

import pytest

from keel_loki.common import digest
from keel_loki.decision_sessions import best_question_bundle, plan_decision_session
from keel_loki.questions import QuestionError
from keel_muse.review import (ReviewError, example_snapshot, make_review_request,
                              project_review, record_session_effort,
                              validate_projection, validate_review_request)

NOW = 10 * 86400


def empty_spec():
    return {"schema": "keel.loki.question_input.v1", "tasks": [], "blockers": [], "questions": []}


def add_task(spec, tid, qids, *, weight=1, age=60, deadline=None, minutes=1, kind="fact"):
    bids = []
    for qid in qids:
        bid = tid + ":" + qid
        bids.append(bid)
        existing = next((q for q in spec["questions"] if q["question_id"] == qid), None)
        if existing:
            existing["scope_ids"].append(tid)
            existing["reuse_authorized"] = True
        else:
            spec["questions"].append({"question_id": qid, "kind": kind, "scope_ids": [tid],
                "prompt": "Exact question " + qid, "estimated_minutes": minutes, "reuse_authorized": False})
        spec["blockers"].append({"blocker_id": bid, "question_id": qid, "kind": kind,
            "scope_id": tid, "fact_key": qid if kind == "fact" else None, "depends_on": [], "state": "unresolved"})
    spec["tasks"].append({"task_id": tid, "weight": weight, "blocker_ids": bids,
                         "created_at": NOW - age, "deadline": deadline})


def test_complementary_bundle_beats_single_question_and_respects_budget():
    spec = empty_spec()
    add_task(spec, "complement", ["a", "b"], weight=10)
    add_task(spec, "single", ["c"], weight=2)
    result = plan_decision_session(spec, NOW, minute_budget=2)
    assert set(result["selected_question_ids"]) == {"a", "b"}
    assert result["conditionally_unlocked_task_ids"] == ["complement"]
    assert result["conditionally_unlocked_weight"] == 10
    assert result["estimated_minutes"] == 2
    assert not result["execution_authorized"]
    assert not result["all_authorizations_cleared"]
    assert result["actual_human_minutes"] is None
    assert plan_decision_session(spec, NOW, minute_budget=1)["selected_question_ids"] == ["c"]


def test_decimal_budget_has_no_binary_float_overrun_or_false_rejection():
    spec = empty_spec()
    add_task(spec, "pair", ["a", "b"])
    spec["questions"][0]["estimated_minutes"] = .1
    spec["questions"][1]["estimated_minutes"] = .2
    result = plan_decision_session(spec, NOW, minute_budget=.3)
    assert result["selected_question_ids"] == ["a", "b"]
    assert result["estimated_minutes"] == .3
    assert not plan_decision_session(spec, NOW, minute_budget=.29)["conditionally_unlocked_task_ids"]


def test_shared_fact_counts_each_task_once_and_approvals_remain_individual():
    spec = empty_spec()
    add_task(spec, "t1", ["shared", "a"])
    add_task(spec, "t2", ["shared", "b"])
    for q in spec["questions"]:
        if q["question_id"] != "shared":
            q["kind"] = "approval"
    for b in spec["blockers"]:
        if b["question_id"] != "shared":
            b.update(kind="approval", fact_key=None)
    result = plan_decision_session(spec, NOW, minute_budget=3)
    assert result["candidate_question_count"] == 3
    assert result["conditionally_unlocked_task_ids"] == ["t1", "t2"]
    assert result["conditionally_unlocked_weight"] == 2
    assert sum(q["human_only"] for q in result["questions"]) == 2
    assert all(not q["approval_reused"] for q in result["questions"])


@pytest.mark.parametrize("kind", ["approval", "attestation", "unaided"])
def test_nonreusable_human_decisions_cannot_be_deduplicated(kind):
    spec = empty_spec()
    add_task(spec, "t1", ["same"], kind=kind)
    add_task(spec, "t2", ["same"], kind=kind)
    with pytest.raises(QuestionError, match="human_decision_not_reusable"):
        plan_decision_session(spec, NOW, minute_budget=2)


def test_transitive_prerequisites_are_planned_as_conditional_followups():
    spec = empty_spec()
    add_task(spec, "t1", ["a", "b", "c"])
    spec["blockers"][1]["depends_on"] = ["t1:a"]
    spec["blockers"][2]["depends_on"] = ["t1:b"]
    spec["tasks"][0]["blocker_ids"] = ["t1:c"]
    result = plan_decision_session(spec, NOW, minute_budget=3)
    assert result["selected_question_ids"] == ["a", "b", "c"]
    assert result["conditionally_unlocked_task_ids"] == ["t1"]
    assert [q["conditional_followup"] for q in result["questions"]] == [False, True, True]
    assert result["requires_fresh_projection_after_each_response"]


def test_unresolved_system_hold_cannot_be_converted_into_human_unlock():
    spec = empty_spec()
    add_task(spec, "t1", ["a", "b"])
    spec["blockers"].append({"blocker_id": "system", "question_id": None, "kind": "system", "scope_id": "t1",
        "fact_key": None, "depends_on": [], "state": "unresolved"})
    spec["tasks"][0]["blocker_ids"].append("system")
    result = plan_decision_session(spec, NOW, minute_budget=2)
    assert result["status"] == "PROGRESS_ONLY"
    assert result["conditionally_unlocked_task_ids"] == []
    assert result["remaining_blocked_task_ids"] == ["t1"]


def test_overdue_then_old_tasks_receive_reserved_turn_within_budget():
    spec = empty_spec()
    add_task(spec, "recent", ["a"], weight=1000)
    add_task(spec, "old", ["b"], weight=.1, age=8 * 86400)
    assert plan_decision_session(spec, NOW, minute_budget=1)["selected_question_ids"] == ["b"]
    add_task(spec, "overdue", ["c"], weight=.01, deadline=NOW - 1)
    result = plan_decision_session(spec, NOW, minute_budget=1)
    assert result["selected_question_ids"] == ["c"]
    assert result["fairness_reserved_question_id"] == "c"
    assert plan_decision_session(spec, NOW, minute_budget=2)["selected_question_ids"][0] == "c"
    spec["questions"][-1]["estimated_minutes"] = 2
    assert plan_decision_session(spec, NOW, minute_budget=1)["selected_question_ids"] == ["b"]


def test_too_large_fails_explicitly_without_truncating_or_approximating():
    spec = empty_spec()
    for i in range(17):
        add_task(spec, f"t{i}", [f"q{i}"])
    result = plan_decision_session(spec, NOW, minute_budget=30)
    assert result["status"] == "SESSION_TOO_LARGE"
    assert result["selected_question_ids"] == []
    assert result["candidate_question_count"] == 17
    assert result["optimal_for"] is None


@pytest.mark.parametrize("value", [False, 0, -1, float("nan"), float("inf"), 1441, "2", 10**1000])
def test_budget_invalid_input_is_rejected(value):
    with pytest.raises(QuestionError, match="invalid_session_minutes"):
        plan_decision_session(empty_spec(), NOW, minute_budget=value)


def test_empty_and_unaffordable_are_not_successful_sessions():
    spec = empty_spec()
    assert plan_decision_session(spec, NOW, minute_budget=1)["status"] == "NO_PENDING_QUESTIONS"
    add_task(spec, "t1", ["q"], minutes=2)
    assert plan_decision_session(spec, NOW, minute_budget=1)["status"] == "NO_AFFORDABLE_ACTIONABLE_QUESTION"


def test_deterministic_model_and_input_unchanged():
    spec = empty_spec()
    add_task(spec, "t1", ["a", "b"])
    add_task(spec, "t2", ["b", "c"])
    original = deepcopy(spec)
    first = plan_decision_session(spec, NOW, minute_budget=2)
    assert spec == original
    for family in ("tasks", "questions", "blockers"):
        spec[family].reverse()
    second = plan_decision_session(spec, NOW, minute_budget=2)
    assert first["selected_question_ids"] == second["selected_question_ids"]
    assert first["conditionally_unlocked_task_ids"] == second["conditionally_unlocked_task_ids"]


def test_solver_matches_independent_small_subset_reference():
    rng = random.Random(123)
    for _ in range(30):
        qs = [{"id": str(i), "minutes": rng.randint(1, 3), "cost": 0, "resolves": [str(i)]} for i in range(5)]
        packets = [{"id": str(i), "value": rng.randint(1, 10),
                    "needs": rng.sample([str(i) for i in range(5)], rng.randint(1, 3))} for i in range(4)]
        result = best_question_bundle(qs, packets, minute_budget=4)
        reference = max(sum(p["value"] for p in packets if set(p["needs"]) <= set(ids))
                        for size in range(6) for ids in itertools.combinations([str(i) for i in range(5)], size)
                        if sum(q["minutes"] for q in qs if q["id"] in ids) <= 4)
        assert result["unlocked_value"] == reference
        assert result["estimated_minutes"] <= 4


def test_existing_math_api_keeps_exact_result_fields():
    location = Path(__file__).resolve().parents[1] / "math" / "keel_math.py"
    module_spec = importlib.util.spec_from_file_location("session_math_compat", location)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    result = module.best_question_bundle([{"id": "q", "cost": 1, "resolves": ["r"]}],
                                          [{"id": "p", "value": 2, "needs": ["r"]}])
    assert set(result) == {"selected", "cost", "unlocked_value", "objective", "status"}
    assert result["selected"] == ["q"]


def test_optional_muse_path_preserves_old_api_and_validates_bundle():
    snapshot = example_snapshot()
    old = project_review(snapshot, now=snapshot["captured_at"])
    assert "decision_session" not in old
    assert validate_projection(old) == old
    report = project_review(snapshot, now=snapshot["captured_at"], session_minutes=2)
    assert report["decision_session"]["selected_question_ids"] == ["review-01"]
    assert validate_projection(report) == report
    report["decision_session"]["conditionally_unlocked_task_ids"].append("made-up")
    with pytest.raises(ReviewError, match="projection_changed"):
        validate_projection(report)


@pytest.mark.parametrize("field", ["application", "packet", "question", "budget"])
def test_session_response_stale_after_any_bound_revision_or_budget_change(field):
    snapshot = example_snapshot()
    report = project_review(snapshot, now=snapshot["captured_at"], session_minutes=2)
    request = make_review_request(report, application_id="app-01", question_id="review-01", response="approve",
                                  now=report["as_of"], expected_report_sha256=digest(report))
    assert not request["execution_authorized"]
    if field == "application":
        snapshot["applications"][0]["application_revision_sha256"] = "9" * 64
    elif field == "packet":
        snapshot["applications"][0]["packet"]["fields"][0]["value"] += " changed"
    elif field == "question":
        snapshot["applications"][0]["questions"][0]["prompt"] += " changed"
    current = project_review(snapshot, now=snapshot["captured_at"], session_minutes=3 if field == "budget" else 2)
    with pytest.raises(ReviewError):
        validate_review_request(request, current, expected_current_report_sha256=digest(current), now=current["as_of"])


def test_actual_effort_is_supplied_separately_and_never_inferred_or_clamped():
    snapshot = example_snapshot()
    report = project_review(snapshot, now=snapshot["captured_at"], session_minutes=2)
    event = record_session_effort(report, event_id="effort1", question_id="review-01", human_minutes=3.5,
                                  now=report["as_of"], expected_report_sha256=digest(report))
    assert event["human_minutes"] == 3.5
    assert event["exceeds_planned_session_minutes"]
    assert event["completed_task_ids"] == []
    assert not event["completion_inferred"]
    assert event["requires_event_id_deduplication"]
    assert report["decision_session"]["actual_human_minutes"] is None
    for invalid in (True, -1, 0, "3", float("nan"), float("inf"), 1441, 10**1000):
        with pytest.raises(ReviewError, match="invalid_observed_human_minutes"):
            record_session_effort(report, event_id="effort1", question_id="review-01", human_minutes=invalid,
                                   now=report["as_of"], expected_report_sha256=digest(report))
    with pytest.raises(ReviewError, match="question_not_in_session"):
        record_session_effort(report, event_id="effort2", question_id="fact-02", human_minutes=1,
                               now=report["as_of"], expected_report_sha256=digest(report))
