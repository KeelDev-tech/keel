"""Pure shadow readiness and bounded adaptive refill proposals.

Adapters must obtain observations from the existing canonical queue, holds,
approval issuer and provider evidence. Output never authorizes a launch.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import timedelta
import math
from .contracts import ContractError, digest, number, integer, timestamp, versioned, public_url_shape

DEPENDENCIES = ("policy", "form", "answers", "attachments", "target", "approval", "route")


@dataclass(frozen=True)
class Readiness:
    executable: bool
    state: str
    reasons: tuple[str, ...]
    route: str
    dependency_hash: str | None

    def to_dict(self):
        return asdict(self)


def dependency_hash(dependencies):
    if type(dependencies) is not dict or set(dependencies) != set(DEPENDENCIES):
        raise ContractError("incomplete dependency set")
    if any(type(v) is not str or not v.strip() for v in dependencies.values()):
        raise ContractError("empty dependency revision")
    return digest(dependencies)


def evaluate_readiness(lead, *, now, maximum_age_seconds=900):
    reasons = []
    route, dep = "unknown", None
    try:
        versioned(lead)
        number(maximum_age_seconds, minimum=1)
        observed = timestamp(lead["observed_at"])
        if observed > now or now - observed > timedelta(seconds=maximum_age_seconds):
            reasons.append("observation_stale")
        if lead.get("status") != "READY":
            reasons.append("queue_not_ready")
        if not public_url_shape(lead.get("posting_url")):
            reasons.append("posting_url_missing_or_invalid")
        if not lead.get("identity") or type(lead["identity"]) is not str:
            reasons.append("canonical_identity_missing")
        if lead.get("history_reconciled") is not True:
            reasons.append("legacy_history_unreconciled")
        attempt = lead.get("attempt_state")
        if attempt not in {"NONE", "AUTHORITATIVE_NOT_SUBMITTED"}:
            reasons.append("attempt_hold" if attempt in {"INTENT", "UNKNOWN", "DISPATCHED", "SUBMITTED"}
                           else "attempt_state_unverified")
        # False explicitly means no lease, not lack of evidence about a lease.
        if lead.get("launch_lock_held") is not False:
            reasons.append("launch_lock_held_or_unknown")
        if lead.get("policy_pass") is not True:
            reasons.append("policy_not_passed")
        if lead.get("posting_verified") is not True:
            reasons.append("posting_unverified")
        if lead.get("answers_resolved") is not True:
            reasons.append("answers_unresolved")
        if lead.get("approval_valid") is not True:
            reasons.append("approval_unverified")
        if lead.get("packet_present") is not True:
            reasons.append("packet_missing")
        route = lead.get("route", "unknown")
        if route not in {"browser", "api"} or lead.get("route_supported") is not True:
            reasons.append("provider_route_unverified")
        if route == "api" and lead.get("provider_contract_validated") is not True:
            reasons.append("provider_contract_unvalidated")
        dep = dependency_hash(lead["dependencies"])
        if lead.get("packet_dependency_hash") != dep:
            reasons.append("packet_dependencies_changed")
        if timestamp(lead["approval_expires_at"]) <= now:
            reasons.append("approval_expired")
    except (ContractError, TypeError, ValueError, KeyError):
        reasons.append("input_contract_invalid")
    state = ("EXECUTABLE" if not reasons else
             "AWAITING_DECISION" if {"answers_unresolved", "approval_unverified", "approval_expired"} & set(reasons) else
             "PROVIDER_LIMITED" if {"provider_route_unverified", "provider_contract_unvalidated"} & set(reasons) else
             "INTERNAL_FAILURE")
    return Readiness(not reasons, state, tuple(dict.fromkeys(reasons)), route, dep)


def inventory(leads, *, now):
    observations = list(leads)
    identities = Counter(x["identity"] for x in observations
                         if type(x) is dict and type(x.get("identity")) is str and x["identity"])
    states, losses, routes, rows = Counter(), Counter(), Counter(), []
    for lead in observations:
        result = evaluate_readiness(lead, now=now)
        if (type(lead) is dict and type(lead.get("identity")) is str and
                identities[lead["identity"]] > 1):
            result = Readiness(False, "INTERNAL_FAILURE", result.reasons + ("duplicate_identity",),
                               result.route, result.dependency_hash)
        states[result.state] += 1
        losses.update(result.reasons)
        if result.executable:
            routes[result.route] += 1
        rows.append(result.to_dict())
    nominal = sum(type(x) is dict and x.get("status") == "READY" for x in observations)
    actual = states["EXECUTABLE"]
    return {"mode": "SHADOW_ONLY", "nominal_ready": nominal, "executable_ready": actual,
            "nominal_to_actual_loss": nominal - actual, "states": dict(states),
            "blocked_reasons": dict(losses), "executable_by_route": dict(routes), "rows": rows}


def refill_plan(*, active, permitted_slots, capacity_per_hour, p95_prepare_seconds,
                burst, executable, preparation_wip, verified_supply, yield_rate,
                max_checks, minimum_yield=0.01):
    if type(active) is not bool:
        raise ContractError("active must be a bool")
    for v in (permitted_slots, burst, executable, preparation_wip, verified_supply, max_checks):
        integer(v)
    number(capacity_per_hour); number(p95_prepare_seconds)
    number(yield_rate, maximum=1); number(minimum_yield, minimum=0.000001, maximum=1)
    if not active or not permitted_slots:
        return {"state": "SCHEDULED_PAUSE", "target": 0, "prepare": 0, "checks": 0}
    if capacity_per_hour <= 0:
        return {"state": "CAPACITY_UNMEASURED", "target": permitted_slots + burst,
                "prepare": 0, "checks": 0, "reason": "measure permitted non-starved capacity"}
    # capacity is measured while supplied, never the recent starved launch rate.
    target = max(permitted_slots, math.ceil(capacity_per_hour * p95_prepare_seconds / 3600)) + burst
    shortfall = max(0, target - executable - preparation_wip)
    prepare = min(shortfall, verified_supply)
    remaining = shortfall - prepare
    infeasible = remaining > 0 and (yield_rate < minimum_yield or max_checks == 0)
    checks = 0 if infeasible else min(max_checks, math.ceil(remaining / yield_rate)) if remaining else 0
    unmet = max(0, remaining - math.floor(checks * yield_rate))
    runway = executable / capacity_per_hour * 3600
    return {"state": "SUPPLY_LIMITED" if infeasible or unmet else "REFILL" if shortfall else "COVERED",
            "target": target, "prepare": prepare, "checks": checks,
            "unfunded_promotions": unmet, "runway_seconds": runway,
            "bounded_diagnostic_checks": min(max_checks, 5) if infeasible else 0,
            "replenishment_eta_seconds": p95_prepare_seconds if prepare else None}
