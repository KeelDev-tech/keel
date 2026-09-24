"""Bounded operator briefs and research proposals; no messages or HTTP calls."""
from .common import (keys, unique, strings, integer, number, boolean, require,
                     text, clock, fresh, hexdigest)


def operator_brief(flow, costs, *, budget_minutes, now):
    clock(now); integer(budget_minutes, maximum=240); unique(costs, "group_id")
    questions = flow["questions"]; groups = {g["group_id"]: g for g in questions["groups"]}
    require({c["group_id"] for c in costs} <= groups.keys(), "cost references unknown question group")
    cost_map = {}
    for row in costs:
        keys(row, {"group_id", "estimated_minutes", "owner", "estimate_ref"})
        integer(row["estimated_minutes"], maximum=240); require(row["estimated_minutes"] > 0, "positive decision time required")
        text(row["owner"]); text(row["estimate_ref"]); cost_map[row["group_id"]] = row
    if (questions.get("current_dependencies_verified") is not True
            or not fresh(flow["observed_at"], now) or not fresh(flow["evaluated_at"], now)):
        return {"status": "REFRESH_EXPORT", "selected": [], "minutes": 0, "remaining_minutes": budget_minutes,
                "messages_sent": 0, "answers_generated": 0, "execution_authorized": False}
    bundles = {r["role_id"]: set(r["required_group_ids"]) for r in questions["dependency_bundles"]}
    useful = {gid for gid, g in groups.items() if g["roles_progressed"]}
    eligible = useful & cost_map.keys(); selected = set(); remaining = budget_minutes
    def unlocked(chosen): return {rid for rid, needed in bundles.items() if needed and needed <= chosen}
    while True:
        candidates = {frozenset({gid}) for gid in eligible - selected}
        candidates |= {frozenset(needed - selected) for needed in bundles.values() if needed <= eligible and needed - selected}
        choices = []
        before = unlocked(selected)
        for group_set in candidates:
            minutes = sum(cost_map[g]["estimated_minutes"] for g in group_set)
            if minutes > remaining: continue
            urgent = any(groups[g]["urgent"] for g in group_set)
            gain = len(unlocked(selected | group_set) - before)
            progressed = len({rid for g in group_set for rid in groups[g]["roles_progressed"]})
            choices.append(((not urgent, -gain / minutes, -progressed / minutes, minutes, tuple(sorted(group_set))), group_set, minutes))
        if not choices: break
        _, chosen, minutes = min(choices, key=lambda item: item[0]); selected.update(chosen); remaining -= minutes
    rows = []
    for gid in sorted(selected, key=lambda g: (not groups[g]["urgent"], g)):
        group = groups[gid]
        rows.append({"group_id": gid, "question": group["question"], "options": group["options"],
                     "classification": group["classification"], "owner": cost_map[gid]["owner"],
                     "estimated_minutes": cost_map[gid]["estimated_minutes"], "estimate_ref": cost_map[gid]["estimate_ref"],
                     "deadline": group["earliest_deadline"], "question_ids": group["question_ids"],
                     "roles_progressed": group["roles_progressed"], "operator_reply_required": True,
                     "resolved": False})
    deferred = [{"group_id": gid, "reason": "TIME_ESTIMATE_MISSING" if gid not in cost_map else "BUDGET_EXHAUSTED",
                 "urgent": groups[gid]["urgent"]} for gid in sorted(useful - selected)]
    return {"status": "PROPOSAL_ONLY", "selected": rows, "deferred": deferred,
            "minutes": budget_minutes - remaining, "remaining_minutes": remaining,
            "roles_unlocked_if_all_selected_answered": sorted(unlocked(selected)),
            "urgent_deferred_count": sum(r["urgent"] for r in deferred),
            "messages_sent": 0, "answers_generated": 0, "execution_authorized": False,
            "method": "bounded greedy bundle selection; conditional dependencies, not a globally optimal schedule"}


def research_plan(checks, *, budget_minutes, now):
    clock(now); unique(checks, "check_id"); integer(budget_minutes, maximum=240)
    eligible, excluded, rate_hold, seen_uncertainty = [], [], False, set()
    for row in checks:
        keys(row, {"check_id", "uncertainty_key", "role_ids", "fit_score", "permitted", "rate_limited",
                   "affects_decision", "probability_of_change", "decision_value", "estimated_minutes",
                   "estimate_ref", "observed_at", "evidence_hash"})
        text(row["uncertainty_key"]); strings(row["role_ids"], nonempty=True); hexdigest(row["evidence_hash"])
        number(row["fit_score"], maximum=100)
        for field in ("permitted", "rate_limited", "affects_decision"): boolean(row[field], field)
        minutes = integer(row["estimated_minutes"], maximum=240); require(minutes > 0, "positive research time required")
        value = number(row["decision_value"], maximum=100)
        if row["probability_of_change"] is not None: number(row["probability_of_change"], maximum=1)
        if row["estimate_ref"] is not None: text(row["estimate_ref"])
        if row["rate_limited"]: rate_hold = True
        reason = ("RATE_LIMIT_HOLD" if row["rate_limited"] else "NOT_PERMITTED" if not row["permitted"]
                  else "FIT_BELOW_75" if row["fit_score"] < 75 else "NO_DECISION_IMPACT" if not row["affects_decision"]
                  else "STALE_OBSERVATION" if not fresh(row["observed_at"], now) else
                  "ESTIMATE_MISSING" if row["probability_of_change"] is None or row["estimate_ref"] is None else None)
        if reason: excluded.append({"check_id": row["check_id"], "reason": reason}); continue
        eligible.append({"check_id": row["check_id"], "uncertainty_key": row["uncertainty_key"], "role_ids": row["role_ids"],
                         "estimated_minutes": minutes, "score": row["probability_of_change"] * value / minutes,
                         "estimate_ref": row["estimate_ref"], "evidence_hash": row["evidence_hash"]})
    if rate_hold:
        return {"status": "RATE_LIMIT_RECONCILIATION_REQUIRED", "selected": [], "excluded": excluded,
                "minutes": 0, "remaining_minutes": budget_minutes, "http_requests": 0, "execution_authorized": False}
    selected, remaining = [], budget_minutes
    for row in sorted(eligible, key=lambda r: (-r["score"], r["estimated_minutes"], r["check_id"])):
        reason = ("DUPLICATE_UNCERTAINTY" if row["uncertainty_key"] in seen_uncertainty else
                  "ZERO_ESTIMATED_VALUE" if row["score"] == 0 else "BUDGET_EXHAUSTED" if row["estimated_minutes"] > remaining else None)
        if reason: excluded.append({"check_id": row["check_id"], "reason": reason}); continue
        selected.append(row); remaining -= row["estimated_minutes"]; seen_uncertainty.add(row["uncertainty_key"])
    return {"status": "PROPOSAL_ONLY", "selected": selected, "excluded": excluded, "minutes": budget_minutes - remaining,
            "remaining_minutes": remaining, "http_requests": 0, "execution_authorized": False,
            "method": "caller-estimated probability of a useful decision change times decision value per minute; uncalibrated heuristic"}
