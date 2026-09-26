"""Deterministic dependency prioritization, reuse scope and offline statistics."""
import copy

import pytest

from keel_loki.questions import QuestionError, check_fact_reuse, demo, evaluate_outcomes, evaluate_policy, plan_questions


NOW = 10 * 86400


def spec():
    return {"schema": "keel.loki.question_input.v1", "tasks": [
        {"task_id": "role-a", "weight": 2, "blocker_ids": ["fact-a"], "created_at": NOW - 60, "deadline": None},
        {"task_id": "role-b", "weight": 1, "blocker_ids": ["fact-b", "approval-b"], "created_at": NOW - 60, "deadline": None}],
        "blockers": [
            {"blocker_id": "fact-a", "question_id": "preference", "kind": "fact", "scope_id": "scope-a", "fact_key": "geography", "depends_on": [], "state": "unresolved"},
            {"blocker_id": "fact-b", "question_id": "preference", "kind": "fact", "scope_id": "scope-b", "fact_key": "geography", "depends_on": [], "state": "unresolved"},
            {"blocker_id": "approval-b", "question_id": "approval", "kind": "approval", "scope_id": "scope-b", "fact_key": None, "depends_on": ["fact-b"], "state": "unresolved"}],
        "questions": [
            {"question_id": "preference", "kind": "fact", "scope_ids": ["scope-a", "scope-b"], "prompt": "Which locations?", "estimated_minutes": 1, "reuse_authorized": True},
            {"question_id": "approval", "kind": "approval", "scope_ids": ["scope-b"], "prompt": "Approve this exact application?", "estimated_minutes": 2, "reuse_authorized": False}]}


def answer(**changes):
    value = {"answer_id": "answer-1", "kind": "fact", "fact_key": "geography", "source_sha256": "a" * 64,
             "value_sha256": "b" * 64, "scope_ids": ["scope-a", "scope-b"], "permitted_uses": ["application_fact"],
             "reuse_authorized": True, "observed_at": NOW - 100, "expires_at": NOW + 100}
    value.update(changes)
    return value


def add_task(value, name, *, weight=1, age=60, deadline=None):
    value["tasks"].append({"task_id": name, "weight": weight, "blocker_ids": [f"blocker-{name}"],
                           "created_at": NOW - age, "deadline": deadline})
    value["blockers"].append({"blocker_id": f"blocker-{name}", "question_id": f"question-{name}", "kind": "fact",
                              "scope_id": name, "fact_key": "work-preference", "depends_on": [], "state": "unresolved"})
    value["questions"].append({"question_id": f"question-{name}", "kind": "fact", "scope_ids": [name],
                               "prompt": "What is your preference?", "estimated_minutes": 1, "reuse_authorized": False})


def test_shared_fact_counts_unique_blockers_and_preserves_separate_approval():
    report = plan_questions(spec(), NOW)
    assert report["unique_unresolved_blocker_count"] == 3
    assert report["unique_blocked_task_count"] == 2
    assert report["unique_ranked_question_count"] == 1
    row = report["ranking"][0]
    assert row["resolvable_blocker_ids"] == ["fact-a", "fact-b"]
    assert row["conditionally_unlocked_task_ids"] == ["role-a"]
    assert row["conditional_weight_per_minute"] == 2
    assert row["answer_probability"] is None
    assert report["deferred"] == [{"question_id": "approval", "reason": "PREREQUISITE_BLOCKED", "unresolved_prerequisite_ids": ["fact-b"]}]
    assert not row["approval_reused"]
    assert not report["execution_authorized"]


def test_resolving_facts_exposes_individual_human_approval():
    value = spec()
    for row in value["blockers"][:2]:
        row["state"] = "resolved"
    report = plan_questions(value, NOW)
    assert report["unique_unresolved_blocker_count"] == 1
    assert report["unique_blocked_task_count"] == 1
    assert report["ranking"][0]["question_id"] == "approval"
    assert report["ranking"][0]["human_only"]
    assert not report["ranking"][0]["reusable_fact"]
    assert report["answers_generated"] == 0


def test_transitive_dag_dependencies_are_counted_once():
    value = spec()
    value["tasks"][1]["blocker_ids"] = ["approval-b"]
    report = plan_questions(value, NOW)
    assert report["ranking"][0]["affected_task_ids"] == ["role-a", "role-b"]
    assert report["ranking"][0]["fractional_progress_per_minute"] == 2.5


def test_dependency_cycles_and_inconsistent_resolved_state_rejected():
    value = spec()
    value["blockers"][1]["depends_on"] = ["approval-b"]
    with pytest.raises(QuestionError, match="dependency_cycle"):
        plan_questions(value, NOW)
    value = spec()
    value["blockers"][2]["state"] = "resolved"
    with pytest.raises(QuestionError, match="resolved_dependency_inconsistent"):
        plan_questions(value, NOW)


@pytest.mark.parametrize("kind", ["approval", "attestation", "unaided"])
def test_human_decisions_never_reuse_across_blockers_or_scopes(kind):
    value = spec()
    value["questions"][1]["kind"] = kind
    value["blockers"][2]["kind"] = kind
    value["questions"][1]["reuse_authorized"] = True
    with pytest.raises(QuestionError, match="human_decision_not_reusable"):
        plan_questions(value, NOW)
    value["questions"][1]["reuse_authorized"] = False
    duplicate = {**value["blockers"][2], "blocker_id": "another"}
    value["blockers"].append(duplicate)
    with pytest.raises(QuestionError, match="human_decision_not_reusable"):
        plan_questions(value, NOW)


def test_reuse_requires_same_fact_and_explicit_permitted_scope():
    value = spec()
    value["blockers"][1]["fact_key"] = "salary"
    with pytest.raises(QuestionError, match="fact_reuse_mismatch"):
        plan_questions(value, NOW)
    value = spec()
    value["questions"][0]["scope_ids"] = ["scope-a"]
    with pytest.raises(QuestionError, match="question_scope_mismatch"):
        plan_questions(value, NOW)
    value = spec()
    value["questions"][0]["reuse_authorized"] = False
    with pytest.raises(QuestionError, match="reuse_not_authorized"):
        plan_questions(value, NOW)


def test_age_and_deadline_fairness_prevent_small_old_task_starvation():
    value = spec()
    add_task(value, "old-small", weight=0.1, age=8 * 86400)
    add_task(value, "new-large", weight=1000)
    report = plan_questions(value, NOW)
    assert report["ranking"][0]["question_id"] == "question-old-small"
    add_task(value, "overdue-small", weight=0.01, age=60, deadline=NOW - 1)
    report = plan_questions(value, NOW)
    assert report["ranking"][0]["question_id"] == "question-overdue-small"
    assert report["ranking"][0]["fairness_tier"] == 2


def test_order_determinism_and_input_immutability():
    value = spec()
    add_task(value, "same-score")
    before = copy.deepcopy(value)
    first = plan_questions(value, NOW)
    assert value == before
    for table in ("tasks", "questions", "blockers"):
        value[table].reverse()
    second = plan_questions(value, NOW)
    assert first["ranking"] == second["ranking"]
    assert first["deferred"] == second["deferred"]


def test_system_blocker_never_becomes_manufactured_human_request():
    value = spec()
    value["blockers"].append({"blocker_id": "missing-export", "question_id": None, "kind": "system",
                              "scope_id": "scope-a", "fact_key": None, "depends_on": [], "state": "unresolved"})
    value["tasks"][0]["blocker_ids"].append("missing-export")
    report = plan_questions(value, NOW)
    assert report["unique_unresolved_blocker_count"] == 4
    assert report["ranking"][0]["conditionally_unlocked_task_ids"] == []
    assert report["unique_ranked_question_count"] == 1


@pytest.mark.parametrize("minutes", [0, -1, float("nan"), float("inf"), True, 1e-300, 1441])
def test_invalid_effort_never_creates_infinite_priority(minutes):
    value = spec()
    value["questions"][0]["estimated_minutes"] = minutes
    with pytest.raises(QuestionError):
        plan_questions(value, NOW)


def test_duplicate_ids_and_edges_do_not_inflate_counts():
    value = spec()
    value["tasks"].append(copy.deepcopy(value["tasks"][0]))
    with pytest.raises(QuestionError, match="duplicate_record"):
        plan_questions(value, NOW)
    value = spec()
    value["tasks"][0]["blocker_ids"] = ["fact-a", "fact-a"]
    with pytest.raises(QuestionError, match="duplicate_id"):
        plan_questions(value, NOW)


@pytest.mark.parametrize("changes", [
    {"kind": "approval"}, {"kind": "attestation"}, {"kind": "unaided"},
    {"scope_ids": ["scope-other"]}, {"fact_key": "other"}, {"reuse_authorized": False},
    {"permitted_uses": ["submit"]}, {"permitted_uses": ["application_fact", "approval"]},
    {"observed_at": NOW + 1}, {"expires_at": NOW},
])
def test_answer_reuse_fails_closed_for_wrong_scope_use_time_or_decisions(changes):
    result = check_fact_reuse(answer(**changes), scope_id="scope-a", fact_key="geography", now=NOW)
    assert result["status"] == "BLOCKED"
    assert not result["execution_authorized"]


def test_answer_reuse_checks_metadata_without_claiming_verified_truth():
    result = check_fact_reuse(answer(), scope_id="scope-a", fact_key="geography", now=NOW)
    assert result["metadata_compatible"]
    assert not result["answer_truth_verified"]
    assert not result["source_bytes_verified"]
    assert not result["approval_reusable"]


def test_outcomes_deduplicate_tasks_and_do_not_infer_interviews():
    log = [{"event_id": "e1", "question_id": "q1", "human_minutes": 2,
            "completed_task_ids": ["t1", "t2"], "interview_event_ids": []},
           {"event_id": "e2", "question_id": "q2", "human_minutes": 3,
            "completed_task_ids": ["t1"], "interview_event_ids": []}]
    report = evaluate_outcomes(log)
    assert report["unique_completed_tasks"] == 2
    assert report["tasks_per_recorded_human_minute"] == 0.4
    assert report["reported_unique_interview_events"] == 0
    assert not report["causal_improvement_established"]
    assert evaluate_outcomes([])["tasks_per_recorded_human_minute"] is None
    with pytest.raises(QuestionError, match="duplicate_event"):
        evaluate_outcomes(log + [log[0]])


def policy_log():
    return [{"decision_id": "d1", "selected_action": "a", "logging_probabilities": {"a": .5, "b": .5},
             "propensity": .5, "reward": 1, "outcome_observed": True},
            {"decision_id": "d2", "selected_action": "b", "logging_probabilities": {"a": .5, "b": .5},
             "propensity": .5, "reward": 0, "outcome_observed": True}]


def candidate():
    return {"d1": {"a": .75, "b": .25}, "d2": {"a": .75, "b": .25}}


def test_offline_policy_estimate_uses_actual_propensities_and_exposes_limits():
    report = evaluate_policy(policy_log(), candidate())
    assert report["ips_mean"] == .75
    assert report["snips_mean"] == .75
    assert report["effective_sample_size"] == 1.6
    assert report["maximum_importance_weight"] == 1.5
    assert not report["learned_policy"]
    assert not report["deployment_recommended"]
    assert report["confidence_interval"] is None
    assert not report["randomization_verified"]


def test_unsupported_candidate_policy_fails_instead_of_faking_learning():
    log = policy_log()
    log[0]["logging_probabilities"] = {"a": 1, "b": 0}
    log[0]["propensity"] = 1
    with pytest.raises(QuestionError, match="insufficient_policy_support"):
        evaluate_policy(log, candidate())


@pytest.mark.parametrize("changes", [
    {"propensity": .2}, {"propensity": 0}, {"reward": 2}, {"outcome_observed": False},
    {"selected_action": "absent"}, {"logging_probabilities": {"a": .5, "b": .1}},
])
def test_incomplete_or_inconsistent_logged_policy_data_is_rejected(changes):
    log = policy_log()
    log[0].update(changes)
    with pytest.raises(QuestionError):
        evaluate_policy(log, candidate())


def test_empty_support_in_sample_is_visible_not_zero_variance_success():
    log = policy_log()[:1]
    report = evaluate_policy(log, {"d1": {"a": 0, "b": 1}})
    assert report["status"] == "NO_MATCHING_LOGGED_ACTIONS"
    assert report["snips_mean"] is None
    assert report["effective_sample_size"] == 0


def test_demo_is_honest_and_preserves_human_boundary():
    report = demo()
    assert report["synthetic"]
    assert report["shared_fact_question_ranked_first"]
    assert report["individual_approval_deferred"]
    assert report["human_decisions_manufactured"] == 0
