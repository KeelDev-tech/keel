"""Conditional supply runway model; no retry/spawn schedule changes."""
import math
from .common import clock, fresh, integer, number, records, require, text, timestamp, boolean


def forecast(executable, measurement, releases, *, now, active, safety_seconds=300):
    clock(now); integer(executable, maximum=100000); boolean(active)
    number(safety_seconds, maximum=86400)
    result = {"state": "UNKNOWN", "runway_seconds": None, "first_refill_opportunity_seconds": None,
              "earliest_exhaustion_seconds": None, "expected_deficit_at_horizon": None,
              "discovery_recommended": False, "execution_authorized": False,
              "ready_floor": 5, "check_seconds": 30, "spawn_throttle_seconds": 600,
              "assumption": "conditional estimates; future conversions are not guaranteed"}
    if not active:
        return {**result, "state": "SCHEDULED_PAUSE"}
    if measurement is None:
        return {**result, "reason": "capacity measurement unavailable"}
    require(type(measurement) is dict, "measurement object required")
    for key in ("evidence_ref", "source_revision"):
        text(measurement.get(key), key)
    if not fresh(measurement.get("observed_at"), now, max_age=86400) or measurement.get("measured_while_supplied") is not True:
        return {**result, "reason": "capacity is stale or was measured while starved"}
    require(measurement.get("unit") == "applications/hour", "capacity unit must be applications/hour")
    rate = number(measurement.get("value"), "capacity", maximum=10000)
    if rate == 0:
        return {**result, "reason": "positive supplied capacity has not been measured"}
    horizon = number(measurement.get("horizon_seconds"), "horizon_seconds", minimum=1, maximum=604800)
    runway = executable / rate * 3600
    result["runway_seconds"] = runway
    timeline, release_ids, role_ids = [], set(), set()
    for release in records(releases, "releases"):
        rid = text(release.get("release_id")); require(rid not in release_ids, "duplicate release_id")
        release_ids.add(rid)
        text(release.get("evidence_ref")); integer(release.get("count"), maximum=100000)
        members = release.get("role_ids")
        require(type(members) is list and len(members) == release["count"], "release count must match explicit role IDs")
        for member in members: text(member)
        require(len(members) == len(set(members)) and not role_ids.intersection(members), "role appears in multiple release opportunities")
        role_ids.update(members)
        require(release.get("stage") in {"VERIFICATION_ELIGIBLE", "VERIFIED_PREPARATION_READY"}, "unknown supply stage")
        available = timestamp(release.get("available_at"))
        prepare = number(release.get("p95_prepare_seconds"), maximum=604800)
        verify = number(release.get("p95_verify_seconds"), maximum=604800) if release["stage"] == "VERIFICATION_ELIGIBLE" else 0
        conversion = number(release.get("estimated_conversion"), maximum=1)
        text(release.get("conversion_evidence_ref"))
        # Fractional expectation stays in the model: flooring each cohort's
        # mean erases combined small-cohort expectation (five cohorts at
        # ~0.031 each floor to 0 but imply ~0.154 expected promotions).
        # The conservative whole-item reserve is reported separately per
        # release; it is not a confidence bound on the expectation.
        expected_fractional = release["count"] * conversion
        expected_reserve = math.floor(expected_fractional)
        delay = max(0.0, (available - now).total_seconds()) + verify + prepare
        if expected_fractional > 0:
            timeline.append((delay, expected_fractional, expected_reserve, rid))
    timeline.sort()
    result["first_refill_opportunity_seconds"] = timeline[0][0] if timeline else None
    inventory, previous, exhaustion = float(executable), 0.0, None
    for delay, fractional, _reserve, _rid in timeline:
        if delay > horizon:
            break
        depletion = previous + max(0, inventory) / rate * 3600
        if depletion <= delay and exhaustion is None:
            exhaustion = depletion
        inventory = max(0, inventory - rate * (delay - previous) / 3600) + fractional
        previous = delay
    depletion = previous + inventory / rate * 3600
    if exhaustion is None and depletion <= horizon:
        exhaustion = depletion
    result["earliest_exhaustion_seconds"] = exhaustion
    result["expected_deficit_at_horizon"] = max(0.0, rate * horizon / 3600 - executable - sum(n for d, n, _r, _i in timeline if d <= horizon))
    result["horizon_seconds"] = horizon
    result["release_opportunities"] = [{"after_seconds": d, "estimated_leads": rsv,
                                        "expected_leads_fractional": frac, "release_id": rid}
                                       for d, frac, rsv, rid in timeline]
    if not timeline:
        return {**result, "state": "REFILL_UNMEASURED", "discovery_recommended": True,
                "reason": "no evidenced replacement opportunity; inspect supply and permitted discovery"}
    risk = exhaustion is not None or timeline[0][0] + safety_seconds >= runway
    return {**result, "state": "STARVATION_RISK" if risk else "COVERED_UNDER_ASSUMPTIONS", "discovery_recommended": risk}
