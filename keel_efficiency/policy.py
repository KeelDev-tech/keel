"""Replayable local routing, shadow LinUCB, and bounded review/batch plans.

These are proposals, never approval or dispatch authority. Budget units are a
host-defined integer resource measure, not an invented token/credit conversion.
Quality bounds assume a fixed, IID held-out cohort and honest operator-certified
labels; this module cannot establish those assumptions from counts alone.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Mapping, Sequence


_RISKS = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_HASH_FIELDS = ("model_hash", "source_hash", "policy_hash")
_MAX_UNITS = 10**12


def _int(value, name, low=0, high=_MAX_UNITS):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def _number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be finite and in [{low}, {high}]")
    return float(value)


def _name(value, name):
    if type(value) is not str or not 1 <= len(value) <= 256 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must be a bounded nonempty identifier")
    return value


def _hash(value, name):
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be lowercase sha256")
    return value


def _risk(value):
    if type(value) is not str or value not in _RISKS:
        raise ValueError("risk must be low, medium, high, or critical")
    return value


@dataclass(frozen=True)
class RouteEvidence:
    route_id: str
    context_key: str
    model_hash: str
    source_hash: str
    policy_hash: str
    local: bool
    cost_units: int
    trials: int
    successes: int
    covered: int
    max_risk: str
    evidence_certified: bool = False
    heldout: bool = False

    def __post_init__(self):
        _name(self.route_id, "route_id")
        _name(self.context_key, "context_key")
        for field in _HASH_FIELDS:
            _hash(getattr(self, field), field)
        for field in ("local", "evidence_certified", "heldout"):
            if type(getattr(self, field)) is not bool:
                raise ValueError(f"{field} must be boolean")
        _int(self.cost_units, "cost_units")
        _int(self.trials, "trials", high=10**8)
        _int(self.covered, "covered", high=self.trials)
        _int(self.successes, "successes", high=self.covered)
        _risk(self.max_risk)

    def bindings(self):
        return {key: getattr(self, key) for key in _HASH_FIELDS}


@dataclass(frozen=True)
class QualityPolicy:
    min_trials: int = 100
    min_success_lcb: float = .8
    min_coverage_lcb: float = .5
    delta: float = .05
    family_size: int = 16

    def __post_init__(self):
        _int(self.min_trials, "min_trials", 1, 10**8)
        _number(self.min_success_lcb, "min_success_lcb", 0, 1)
        _number(self.min_coverage_lcb, "min_coverage_lcb", 0, 1)
        _number(self.delta, "delta", 1e-12, .25)
        _int(self.family_size, "family_size", 1, 256)


def _bounds(route, policy):
    # Union bound for the two one-sided quantities across a fixed route family.
    log_term = math.log(2 * policy.family_size / policy.delta)
    def lower(wins, count):
        return max(0., wins / count - math.sqrt(log_term / (2 * count))) if count else 0.
    success = lower(route.successes, route.covered)
    return {"success_lcb": success, "error_ucb": 1 - success,
            "coverage_lcb": lower(route.covered, route.trials),
            "bound_method": "finite_family_hoeffding", "delta": policy.delta,
            "family_size": policy.family_size}


def _routes(routes, policy):
    if not isinstance(policy, QualityPolicy):
        raise ValueError("QualityPolicy required")
    if not isinstance(routes, (list, tuple)) or len(routes) > policy.family_size:
        raise ValueError("routes must fit the predeclared quality family")
    if any(not isinstance(r, RouteEvidence) for r in routes):
        raise ValueError("RouteEvidence required")
    if len({r.route_id for r in routes}) != len(routes):
        raise ValueError("duplicate route_id")
    return sorted(routes, key=lambda r: r.route_id)


def select_route(routes: Sequence[RouteEvidence], *, context_key: str, risk: str,
                 budget_units: int, current_bindings: Mapping,
                 policy: QualityPolicy = QualityPolicy()):
    """Select the cheapest qualified local route under a declared cost ceiling.

    Caller must reserve the ceiling in the actual governor before dispatch.
    Bounds are fixed-cohort bounds, not sequentially reusable certification.
    A new model/source/policy revision requires new evidence. Certification is
    an operator attestation, not a model-generated confidence score.
    """
    routes = _routes(routes, policy)
    _name(context_key, "context_key")
    _risk(risk)
    _int(budget_units, "budget_units")
    if not isinstance(current_bindings, Mapping):
        raise ValueError("current_bindings required")
    eligible, reports = [], []
    for route in routes:
        reasons = []
        bounds = _bounds(route, policy)
        if not route.local:
            reasons.append("paid_or_remote_route_disabled")
        if route.context_key != context_key:
            reasons.append("context_mismatch")
        if not route.evidence_certified or not route.heldout:
            reasons.append("certified_heldout_evidence_required")
        if current_bindings.get(route.route_id) != route.bindings():
            reasons.append("current_binding_mismatch")
        if _RISKS[route.max_risk] < _RISKS[risk]:
            reasons.append("risk_outside_validated_scope")
        if route.trials < policy.min_trials:
            reasons.append("insufficient_trials")
        if bounds["success_lcb"] < policy.min_success_lcb:
            reasons.append("success_floor_not_met")
        if bounds["coverage_lcb"] < policy.min_coverage_lcb:
            reasons.append("coverage_floor_not_met")
        if route.cost_units > budget_units:
            reasons.append("budget_exceeded")
        reports.append({"route_id": route.route_id, "qualified": not reasons,
                        "reasons": reasons, "cost_units": route.cost_units, **bounds})
        if not reasons:
            eligible.append(route)
    chosen = min(eligible, key=lambda r: (r.cost_units, r.route_id)) if eligible else None
    return {"schema": "keel.efficiency.route.v1", "status": "PROPOSAL" if chosen else "HOLD",
            "selected_route_id": chosen.route_id if chosen else None,
            "reserved_units_required": chosen.cost_units if chosen else 0,
            "routes": reports, "context_key": context_key, "risk": risk,
            "execution_authorized": False, "paid_execution_enabled": False,
            "budget_reserved": False, "quality_policy": asdict(policy)}


def _solve(matrix, vector):
    """Cholesky solve of a positive-definite ridge matrix; no matrix inversion."""
    n = len(vector)
    lower = [[0.] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            value = matrix[i][j] - sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("ridge matrix is not positive definite")
                lower[i][j] = math.sqrt(value)
            else:
                lower[i][j] = value / lower[j][j]
    y = [0.] * n
    for i in range(n):
        y[i] = (vector[i] - sum(lower[i][k] * y[k] for k in range(i))) / lower[i][i]
    x = [0.] * n
    for i in range(n - 1, -1, -1):
        x[i] = (y[i] - sum(lower[k][i] * x[k] for k in range(i + 1, n))) / lower[i][i]
    return x


class ContextualBandit:
    """Disjoint LinUCB with quality-gated recommendations in shadow mode.

    Training outcomes never replace held-out qualification. Rewards are verified
    binary outcomes; scores per declared unit are experimental ranking signals,
    not calibrated probabilities or evidence authorizing a task. Observations
    export as plain JSON-compatible records for deterministic replay.
    """
    def __init__(self, feature_count: int, *, ridge: float = 1., alpha: float = 1.,
                 policy: QualityPolicy = QualityPolicy(), max_observations: int = 10000):
        self.feature_count = _int(feature_count, "feature_count", 1, 32)
        self.ridge = _number(ridge, "ridge", .001, 1000)
        self.alpha = _number(alpha, "alpha", 0, 100)
        if not isinstance(policy, QualityPolicy):
            raise ValueError("QualityPolicy required")
        self.policy = policy
        self.max_observations = _int(max_observations, "max_observations", 1, 100000)
        self._arms, self._seen, self._observations = {}, set(), []

    def _features(self, features):
        if not isinstance(features, (list, tuple)) or len(features) != self.feature_count:
            raise ValueError("feature_count mismatch")
        return [_number(x, "feature", -1, 1) for x in features]

    def _key(self, route):
        return (route.route_id, route.context_key, route.model_hash, route.source_hash, route.policy_hash)

    def _arm(self, route):
        key = self._key(route)
        if key not in self._arms:
            if len(self._arms) >= 256:
                raise ValueError("bandit arm limit")
            self._arms[key] = ([[self.ridge if i == j else 0. for j in range(self.feature_count)]
                                for i in range(self.feature_count)], [0.] * self.feature_count)
        return self._arms[key]

    def observe(self, route: RouteEvidence, features, *, observation_id: str,
                verified_success: bool, measured_cost_units: int,
                evidence_certified: bool, current_bindings: Mapping):
        _name(observation_id, "observation_id")
        x = self._features(features)
        if type(verified_success) is not bool or evidence_certified is not True:
            raise ValueError("operator-certified verified outcome required")
        _int(measured_cost_units, "measured_cost_units")
        if observation_id in self._seen:
            raise ValueError("duplicate observation")
        if len(self._observations) >= self.max_observations:
            raise ValueError("observation limit")
        if not isinstance(route, RouteEvidence):
            raise ValueError("RouteEvidence required")
        result = select_route([route], context_key=route.context_key, risk="low",
                              budget_units=_MAX_UNITS, current_bindings=current_bindings, policy=self.policy)
        if result["status"] != "PROPOSAL":
            raise ValueError("unqualified training route")
        if measured_cost_units > route.cost_units:
            # A cost ceiling violation invalidates this training observation;
            # real usage still belongs in the governor's ledger.
            raise ValueError("measured cost exceeded declared route ceiling")
        matrix, reward_vector = self._arm(route)
        reward = int(verified_success)
        for i in range(self.feature_count):
            reward_vector[i] += reward * x[i]
            for j in range(self.feature_count):
                matrix[i][j] += x[i] * x[j]
        self._seen.add(observation_id)
        self._observations.append({"route": asdict(route), "features": x,
                                   "observation_id": observation_id,
                                   "verified_success": verified_success,
                                   "measured_cost_units": measured_cost_units,
                                   "evidence_certified": True,
                                   "current_bindings": {route.route_id: route.bindings()}})

    def propose(self, routes, features, **selection):
        x = self._features(features)
        result = select_route(routes, policy=self.policy, **selection)
        allowed = {r["route_id"] for r in result["routes"] if r["qualified"]}
        scores = []
        for route in sorted(routes, key=lambda r: r.route_id):
            if route.route_id not in allowed:
                continue
            matrix, rewards = self._arm(route)
            theta, ax = _solve(matrix, rewards), _solve(matrix, x)
            mean = sum(a * b for a, b in zip(theta, x))
            uncertainty = math.sqrt(max(0., sum(a * b for a, b in zip(x, ax))))
            ucb = mean + self.alpha * uncertainty
            # A zero-cost deterministic route is allowed without division by zero.
            score = ucb / max(1, route.cost_units)
            scores.append({"route_id": route.route_id, "score": score,
                           "predicted_reward": mean, "exploration_radius": self.alpha * uncertainty})
        best = min(scores, key=lambda s: (-s["score"], s["route_id"])) if scores else None
        return {**result, "learned_route_id": best["route_id"] if best else None,
                "shadow_only": True, "scores": scores,
                "learning_changes_execution": False,
                "training_observation_count": len(self._observations)}

    def export_observations(self):
        return json.loads(json.dumps(self._observations, allow_nan=False))

    @classmethod
    def replay(cls, records, *, feature_count, **kwargs):
        learner = cls(feature_count, **kwargs)
        if not isinstance(records, (list, tuple)) or len(records) > learner.max_observations:
            raise ValueError("replay record limit")
        fields = {"route", "features", "observation_id", "verified_success", "measured_cost_units", "evidence_certified", "current_bindings"}
        for record in records:
            if type(record) is not dict or set(record) != fields:
                raise ValueError("invalid observation record")
            values = dict(record)
            values["route"] = RouteEvidence(**values["route"])
            learner.observe(**values)
        return learner


def plan_review(*, mandatory_reviewers, optional_reviewers=(), budget_units: int,
                per_call_units: int, disagreements: int = 0, disagreement_trials: int = 0,
                evidence_certified: bool = False, evidence_hash: str | None = None,
                disagreement_threshold: float = .25, min_trials: int = 30,
                max_additional: int = 2):
    """Preserve the mandatory blind-review roster and budget both review phases.

    Additional reviewers require a certified fixed-cohort disagreement signal.
    Missing/unqualified empirical input never removes mandatory reviewers.
    High empirical disagreement triggers a budget-limited optional expansion;
    the plan is not a statistical guarantee that extra reviewers improve quality.
    """
    _int(budget_units, "budget_units")
    _int(per_call_units, "per_call_units", 1)
    _int(disagreement_trials, "disagreement_trials", high=10**8)
    _int(disagreements, "disagreements", high=disagreement_trials)
    _int(min_trials, "min_trials", 1, 10**8)
    _int(max_additional, "max_additional", 0, 14)
    _number(disagreement_threshold, "disagreement_threshold", 0, 1)
    if type(evidence_certified) is not bool:
        raise ValueError("evidence_certified must be boolean")
    if not isinstance(mandatory_reviewers, (list, tuple)) or not isinstance(optional_reviewers, (list, tuple)):
        raise ValueError("reviewer sequences required")
    mandatory, optional = list(mandatory_reviewers), list(optional_reviewers)
    if not 2 <= len(mandatory) <= 16 or len(mandatory) + len(optional) > 16:
        raise ValueError("blind review requires 2..16 distinct reviewers")
    for reviewer in mandatory + optional:
        _name(reviewer, "reviewer_id")
    if len(set(mandatory + optional)) != len(mandatory + optional):
        raise ValueError("duplicate reviewer")
    if evidence_certified:
        _hash(evidence_hash, "evidence_hash")
    mandatory_cost = 2 * len(mandatory) * per_call_units
    if mandatory_cost > budget_units:
        return {"status": "HOLD", "reason": "mandatory_review_budget_insufficient",
                "mandatory_reviewers": mandatory, "reviewers": [], "planned_calls": 0,
                "required_calls": 2 * len(mandatory), "required_units": mandatory_cost,
                "execution_authorized": False, "budget_reserved": False}
    qualified_signal = evidence_certified and disagreement_trials >= min_trials
    disagreement_rate = disagreements / disagreement_trials if disagreement_trials else None
    # Conservative upper bound retains an uncertainty term. Fixed .05 one-sided
    # Hoeffding bound; multiple rounds require a separately planned error budget.
    disagreement_ucb = min(1., disagreement_rate + math.sqrt(math.log(20) / (2 * disagreement_trials))) if qualified_signal else None
    expand = qualified_signal and disagreement_ucb > disagreement_threshold
    available = (budget_units - mandatory_cost) // (2 * per_call_units)
    count = min(max_additional, len(optional), available) if expand else 0
    roster = mandatory + optional[:count]
    return {"status": "PROPOSAL", "mandatory_reviewers": mandatory, "reviewers": roster,
            "planned_calls": 2 * len(roster), "required_units": 2 * len(roster) * per_call_units,
            "additional_reviewers": count, "signal_qualified": qualified_signal,
            "disagreement_rate": disagreement_rate, "disagreement_ucb": disagreement_ucb,
            "additional_budget_limited": bool(expand and count < min(max_additional, len(optional))),
            "evidence_hash": evidence_hash if evidence_certified else None,
            "execution_authorized": False, "budget_reserved": False}


def allocate_batch(tasks, *, budget_units: int, max_operations: int = 100000):
    """Exact bounded multiple-choice knapsack over previously qualified proposals.

    Tasks: [{task_id, choices: [{route_id, cost_units, value_units}]}]. Value is
    operator-assigned integer utility, never model confidence. This allocation
    has no quality authority: callers must revalidate routes and reserve actual
    resources at dispatch. One route per task or explicit deferral is allowed.
    Hard operation limits produce HOLD without a misleading partial optimum.
    """
    _int(budget_units, "budget_units")
    _int(max_operations, "max_operations", 1, 1000000)
    if not isinstance(tasks, (list, tuple)) or len(tasks) > 128:
        raise ValueError("at most 128 batch tasks")
    normalized, seen = [], set()
    for task in tasks:
        if type(task) is not dict or set(task) != {"task_id", "choices"}:
            raise ValueError("invalid batch task fields")
        tid = _name(task["task_id"], "task_id")
        if tid in seen:
            raise ValueError("duplicate batch task")
        seen.add(tid)
        choices, route_ids = task["choices"], set()
        if not isinstance(choices, (list, tuple)) or len(choices) > 32:
            raise ValueError("at most 32 choices per task")
        for choice in choices:
            if type(choice) is not dict or set(choice) != {"route_id", "cost_units", "value_units"}:
                raise ValueError("invalid batch choice fields")
            rid = _name(choice["route_id"], "route_id")
            if rid in route_ids:
                raise ValueError("duplicate task route")
            route_ids.add(rid)
            _int(choice["cost_units"], "cost_units")
            _int(choice["value_units"], "value_units")
        normalized.append((tid, sorted(choices, key=lambda c: c["route_id"])))
    states = {0: (0, ())}
    operations = 0
    for task_id, choices in sorted(normalized):
        candidates = dict(states)  # Deferral is a valid option.
        for spent, (value, picks) in states.items():
            for choice in choices:
                operations += 1
                if operations > max_operations:
                    return {"status": "HOLD", "reason": "allocation_complexity_limit",
                            "allocations": [], "execution_authorized": False, "budget_reserved": False}
                cost = spent + choice["cost_units"]
                if cost > budget_units:
                    continue
                candidate = (value + choice["value_units"], picks + ((task_id, choice["route_id"], choice["cost_units"]),))
                existing = candidates.get(cost)
                if existing is None or candidate[0] > existing[0] or (candidate[0] == existing[0] and candidate[1] < existing[1]):
                    candidates[cost] = candidate
        # Drop states whose value is already achieved at lower cost.
        states, best_value = {}, -1
        for cost in sorted(candidates):
            if candidates[cost][0] > best_value:
                states[cost] = candidates[cost]
                best_value = candidates[cost][0]
    spent, (value, chosen) = min(states.items(), key=lambda item: (-item[1][0], item[0], item[1][1]))
    selected = {item[0] for item in chosen}
    return {"status": "PROPOSAL", "allocations": [{"task_id": t, "route_id": r, "cost_units": c} for t, r, c in chosen],
            "deferred_tasks": sorted(seen - selected), "total_units": spent, "total_value_units": value,
            "operations": operations, "execution_authorized": False, "budget_reserved": False,
            "quality_revalidation_required": True}
