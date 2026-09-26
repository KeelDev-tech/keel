"""Bounded discovery allocation from measured yield; no HTTP or scheduling."""
import math
from .common import unique, text, integer, number, boolean, fresh, require, wilson, timestamp


def allocate(sources, *, budget_minutes, now, max_group_share=0.5, exploration_share=0.1):
    unique(sources, "source_id"); integer(budget_minutes, maximum=240)
    number(max_group_share, minimum=0.01, maximum=1); number(exploration_share, maximum=0.5)
    eligible, excluded, rate_hold = [], [], False
    for row in sources:
        sid = row["source_id"]; text(row.get("employer_group")); text(row.get("evidence_ref"))
        boolean(row.get("permitted")); boolean(row.get("rate_limited")); boolean(row.get("measurement_complete"))
        if row["rate_limited"] or (row.get("retry_after") is not None and timestamp(row["retry_after"]) > now):
            rate_hold = True; excluded.append({"source_id": sid, "reason": "HTTP_429_OR_ACTIVE_RATE_HOLD"}); continue
        if not row["permitted"]:
            excluded.append({"source_id": sid, "reason": "SOURCE_NOT_PERMITTED"}); continue
        if not row["measurement_complete"] or not fresh(row.get("observed_at"), now, max_age=86400):
            excluded.append({"source_id": sid, "reason": "MEASUREMENT_INCOMPLETE_OR_STALE"}); continue
        require(type(row.get("fit_floor")) is int and row["fit_floor"] == 75, "source measurement must preserve fit floor 75")
        checks = integer(row.get("checks"), maximum=1000000)
        success = integer(row.get("qualified_unique_live"), maximum=1000000)
        require(success <= checks, "qualified count exceeds checks")
        minutes = number(row.get("verification_minutes"), maximum=1000000)
        require((checks == 0 and success == 0 and minutes == 0) or (checks > 0 and minutes > 0), "inconsistent source measurement")
        interval = wilson(success, checks)
        score = interval[0] * checks / minutes if checks else 0.0
        eligible.append({"source_id": sid, "employer_group": row["employer_group"], "checks": checks,
                         "qualified_unique_live": success, "observed_yield_per_minute": success / minutes if minutes else None,
                         "conservative_yield_per_minute": score, "yield_fraction_interval_95": interval,
                         "allocated_minutes": 0, "exploration_minutes": 0})
    if rate_hold:
        return {"state": "RATE_LIMIT_RECONCILIATION_REQUIRED", "allocations": [], "excluded": excluded,
                "allocated_minutes": 0, "unallocated_minutes": budget_minutes, "http_requests": 0, "execution_authorized": False}
    cap = math.floor(budget_minutes * max_group_share)
    group_minutes = {}
    def available(row):
        return group_minutes.get(row["employer_group"], 0) < cap
    def assign(row, exploration=False):
        row["allocated_minutes"] += 1; row["exploration_minutes"] += int(exploration)
        group_minutes[row["employer_group"]] = group_minutes.get(row["employer_group"], 0) + 1
    for _ in range(math.floor(budget_minutes * exploration_share)):
        candidates = [r for r in eligible if available(r)]
        if not candidates: break
        assign(min(candidates, key=lambda r: (r["exploration_minutes"], r["checks"], r["source_id"])), True)
    for _ in range(budget_minutes - sum(r["allocated_minutes"] for r in eligible)):
        candidates = [r for r in eligible if available(r) and r["conservative_yield_per_minute"] > 0]
        if not candidates: break
        assign(min(candidates, key=lambda r: (-r["conservative_yield_per_minute"] / (r["allocated_minutes"] + 1), r["source_id"])))
    allocated = sum(r["allocated_minutes"] for r in eligible)
    return {"state": "PROPOSAL_ONLY", "allocations": sorted(eligible, key=lambda r: (-r["allocated_minutes"], r["source_id"])),
            "excluded": excluded, "allocated_minutes": allocated, "unallocated_minutes": budget_minutes - allocated,
            "employer_group_cap_minutes": cap, "http_requests": 0, "execution_authorized": False,
            "measurement_limit": "caller-supplied deduplication and comparable windows required; no outcome causality claimed"}
