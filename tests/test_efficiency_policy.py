"""Quality, numerical, budget and mandatory-review invariants without inference."""
from dataclasses import replace
from itertools import product
import math

import pytest

from keel_efficiency.policy import (
    ContextualBandit, QualityPolicy, RouteEvidence, allocate_batch, plan_review, select_route,
)


HASH = "a" * 64


def route(name="small", cost=2, **changes):
    return replace(RouteEvidence(name, "code", HASH, HASH, HASH, True, cost,
                                 1000, 990, 1000, "high", True, True), **changes)


def select(routes, **changes):
    options = dict(context_key="code", risk="medium", budget_units=20,
                   current_bindings={r.route_id: r.bindings() for r in routes})
    options.update(changes)
    return select_route(routes, **options)


def test_select_cheapest_qualified_local_route_and_lexical_tie():
    rows = [route("z"), route("remote", 0, local=False), route("bad", 1, successes=500), route("a")]
    result = select(rows)
    assert result["selected_route_id"] == "a"
    assert result["execution_authorized"] is False
    assert result["budget_reserved"] is False
    reports = {item["route_id"]: item for item in result["routes"]}
    assert reports["remote"]["reasons"] == ["paid_or_remote_route_disabled"]
    assert "success_floor_not_met" in reports["bad"]["reasons"]


@pytest.mark.parametrize("changes,reason", [
    ({"trials": 20, "covered": 20, "successes": 20}, "insufficient_trials"),
    ({"covered": 100, "successes": 100}, "coverage_floor_not_met"),
    ({"successes": 600}, "success_floor_not_met"),
    ({"evidence_certified": False}, "certified_heldout_evidence_required"),
    ({"heldout": False}, "certified_heldout_evidence_required"),
    ({"context_key": "other"}, "context_mismatch"),
    ({"max_risk": "low"}, "risk_outside_validated_scope"),
    ({"cost_units": 30}, "budget_exceeded"),
])
def test_disqualifiers_hold(changes, reason):
    result = select([route(**changes)])
    assert result["status"] == "HOLD"
    assert result["selected_route_id"] is None
    assert reason in result["routes"][0]["reasons"]


def test_stale_current_binding_and_missing_binding_hold():
    item = route()
    for bindings in ({}, {item.route_id: {**item.bindings(), "model_hash": "b" * 64}}):
        result = select([item], current_bindings=bindings)
        assert result["status"] == "HOLD"
        assert "current_binding_mismatch" in result["routes"][0]["reasons"]


def test_more_routes_require_predeclared_family_and_lower_bound_accounts_for_it():
    rows = [route("a"), route("b")]
    with pytest.raises(ValueError, match="family"):
        select(rows, policy=QualityPolicy(family_size=1))
    assert select([rows[0]], policy=QualityPolicy(family_size=1))["routes"][0]["success_lcb"] > select(rows)["routes"][0]["success_lcb"]


@pytest.mark.parametrize("changes", [
    {"cost_units": float("nan")}, {"cost_units": float("inf")}, {"cost_units": -1},
    {"cost_units": True}, {"covered": 1001}, {"successes": 1001}, {"trials": True},
    {"model_hash": "not-a-hash"}, {"local": 1}, {"max_risk": "unknown"},
])
def test_invalid_route_metrics_rejected(changes):
    with pytest.raises(ValueError):
        route(**changes)


@pytest.mark.parametrize("changes", [
    {"delta": float("nan")}, {"min_success_lcb": float("inf")},
    {"min_coverage_lcb": -.1}, {"min_trials": True}, {"family_size": 0},
])
def test_invalid_quality_policy_rejected(changes):
    with pytest.raises(ValueError):
        QualityPolicy(**changes)


def test_zero_budget_deterministic_route_and_empty_set():
    assert select([route(cost=0)], budget_units=0)["selected_route_id"] == "small"
    assert select([])["status"] == "HOLD"
    with pytest.raises(ValueError, match="duplicate"):
        select([route(), route()])


def observe(learner, item, number, success, features=(1., 0.)):
    learner.observe(item, features, observation_id=f"o{number}", verified_success=success,
                    measured_cost_units=item.cost_units, evidence_certified=True,
                    current_bindings={item.route_id: item.bindings()})


def propose(learner, rows, features=(1., 0.), **changes):
    options = dict(context_key="code", risk="medium", budget_units=20,
                   current_bindings={r.route_id: r.bindings() for r in rows})
    options.update(changes)
    return learner.propose(rows, features, **options)


def test_real_ridge_learning_recommends_successful_arm_but_only_in_shadow():
    small, large = route("small", 1), route("large", 2)
    learner = ContextualBandit(2, alpha=0.)
    for i in range(20):
        observe(learner, small, 2 * i, False)
        observe(learner, large, 2 * i + 1, True)
    result = propose(learner, [small, large])
    assert result["selected_route_id"] == "small"
    assert result["learned_route_id"] == "large"
    assert result["shadow_only"] is True
    assert result["learning_changes_execution"] is False
    assert result["execution_authorized"] is False
    large_score = next(x for x in result["scores"] if x["route_id"] == "large")
    assert large_score["predicted_reward"] == pytest.approx(20 / 21)


def test_contextual_learning_responds_to_different_features():
    a, b = route("a"), route("b")
    learner = ContextualBandit(2, alpha=.2)
    for i in range(20):
        observe(learner, a, 4 * i, True, (1, 0))
        observe(learner, b, 4 * i + 1, False, (1, 0))
        observe(learner, a, 4 * i + 2, False, (0, 1))
        observe(learner, b, 4 * i + 3, True, (0, 1))
    assert propose(learner, [a, b], (1, 0))["learned_route_id"] == "a"
    assert propose(learner, [a, b], (0, 1))["learned_route_id"] == "b"
    replayed = ContextualBandit.replay(learner.export_observations(), feature_count=2, alpha=.2)
    assert propose(replayed, [a, b], (.4, .8)) == propose(learner, [a, b], (.4, .8))


def test_learning_never_rescues_unqualified_or_unaffordable_routes():
    a, b = route("a", 1), route("b", 10)
    learner = ContextualBandit(2)
    observe(learner, b, 1, True)
    assert propose(learner, [a, b], budget_units=2)["learned_route_id"] == "a"
    assert propose(learner, [replace(b, successes=0)])["learned_route_id"] is None


def test_model_revision_does_not_inherit_learned_rewards():
    a = route()
    learner = ContextualBandit(2, alpha=0.)
    observe(learner, a, 0, True)
    revised = replace(a, model_hash="b" * 64)
    assert propose(learner, [a])["scores"][0]["predicted_reward"] > 0
    assert propose(learner, [revised])["scores"][0]["predicted_reward"] == 0


@pytest.mark.parametrize("changes", [
    {"evidence_certified": False}, {"verified_success": .9},
    {"measured_cost_units": float("nan")}, {"measured_cost_units": 3},
    {"features": [float("nan"), 0.]}, {"features": [2., 0.]}, {"features": [1.]},
    {"current_bindings": {}},
])
def test_invalid_observation_never_updates_learner(changes):
    a = route()
    learner = ContextualBandit(2)
    args = dict(route=a, features=[1., 0.], observation_id="o", verified_success=True,
                measured_cost_units=2, evidence_certified=True, current_bindings={a.route_id: a.bindings()})
    args.update(changes)
    with pytest.raises(ValueError):
        learner.observe(**args)
    assert learner.export_observations() == []


def test_duplicate_observations_and_bounded_replay():
    a = route()
    learner = ContextualBandit(2, max_observations=1)
    observe(learner, a, 0, True)
    with pytest.raises(ValueError, match="duplicate"):
        observe(learner, a, 0, False)
    with pytest.raises(ValueError, match="limit"):
        observe(learner, a, 1, True)
    exported = learner.export_observations()
    exported[0]["features"][0] = -1
    assert learner.export_observations()[0]["features"][0] == 1


def review(**changes):
    args = dict(mandatory_reviewers=["safety", "independent"], optional_reviewers=["specialist", "challenger"],
                budget_units=20, per_call_units=2, disagreements=60, disagreement_trials=100,
                evidence_certified=True, evidence_hash=HASH)
    args.update(changes)
    return plan_review(**args)


def test_review_preserves_mandatory_and_counts_both_phases():
    result = review(budget_units=12)
    assert result["reviewers"] == ["safety", "independent", "specialist"]
    assert result["planned_calls"] == 6
    assert result["required_units"] == 12
    assert result["additional_budget_limited"] is True
    assert result["execution_authorized"] is False
    assert result["budget_reserved"] is False


def test_insufficient_mandatory_budget_holds_entire_review():
    result = review(budget_units=7)
    assert result["status"] == "HOLD"
    assert result["required_units"] == 8
    assert result["reviewers"] == []
    assert result["planned_calls"] == 0
    assert result["mandatory_reviewers"] == ["safety", "independent"]


@pytest.mark.parametrize("changes", [
    {"evidence_certified": False}, {"disagreements": 1, "disagreement_trials": 2},
    {"disagreements": 0, "disagreement_trials": 1000},
])
def test_no_qualified_expansion_keeps_mandatory(changes):
    result = review(**changes)
    assert result["reviewers"] == ["safety", "independent"]
    assert result["planned_calls"] == 4


@pytest.mark.parametrize("changes", [
    {"disagreements": 101}, {"evidence_hash": None}, {"disagreement_threshold": float("nan")},
    {"mandatory_reviewers": ["solo"]}, {"optional_reviewers": ["safety"]},
    {"per_call_units": 0}, {"budget_units": True},
])
def test_invalid_adaptive_review_inputs_rejected(changes):
    with pytest.raises(ValueError):
        review(**changes)


def test_exact_batch_knapsack_matches_exhaustive_oracle_and_one_per_task():
    tasks = [
        {"task_id": "a", "choices": [{"route_id": "cheap", "cost_units": 2, "value_units": 4}, {"route_id": "deep", "cost_units": 4, "value_units": 7}]},
        {"task_id": "b", "choices": [{"route_id": "cheap", "cost_units": 3, "value_units": 6}, {"route_id": "deep", "cost_units": 5, "value_units": 10}]},
        {"task_id": "c", "choices": [{"route_id": "free", "cost_units": 0, "value_units": 1}]},
    ]
    for budget in range(12):
        result = allocate_batch(tasks, budget_units=budget)
        brute = []
        for choices in product(*([[None] + task["choices"] for task in tasks])):
            cost = sum(c["cost_units"] for c in choices if c)
            if cost <= budget:
                brute.append((sum(c["value_units"] for c in choices if c), -cost))
        value, neg_cost = max(brute)
        assert (result["total_value_units"], result["total_units"]) == (value, -neg_cost)
        assert len({r["task_id"] for r in result["allocations"]}) == len(result["allocations"])
        assert result == allocate_batch(list(reversed(tasks)), budget_units=budget)
        assert result["execution_authorized"] is False
        assert result["quality_revalidation_required"] is True


def test_knapsack_complexity_limit_fails_closed():
    tasks = [{"task_id": "a", "choices": [{"route_id": "one", "cost_units": 1, "value_units": 1}, {"route_id": "two", "cost_units": 2, "value_units": 2}]}]
    result = allocate_batch(tasks, budget_units=3, max_operations=1)
    assert result["status"] == "HOLD"
    assert result["allocations"] == []


def test_invalid_knapsack_cost_rejected():
    with pytest.raises(ValueError):
        allocate_batch([{"task_id": "a", "choices": [{"route_id": "x", "cost_units": math.nan, "value_units": 1}]}], budget_units=5)
