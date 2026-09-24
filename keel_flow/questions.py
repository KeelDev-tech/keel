"""Rank unanswered question groups by evidenced dependencies and deadlines."""
from datetime import timedelta
from keel_local.tray_audit import audit_tray
from .common import unique, text, number, boolean, timestamp, require, clock


def prioritize(tray, role_requirements, *, now):
    clock(now)
    report = audit_tray(tray)
    unique(role_requirements, "role_id")
    q_to_group = {qid: g["group_id"] for g in report["groups"] for qid in g["question_ids"]}
    groups = {g["group_id"]: {**g, "roles_progressed": [], "roles_unlocked_if_answered": [],
                             "earliest_deadline": None, "urgent": False} for g in report["groups"]}
    bundles, excluded = [], []
    for row in role_requirements:
        rid = row["role_id"]
        number(row.get("fit_score"), "fit_score", maximum=100)
        boolean(row.get("other_gates_clear"))
        required = row.get("open_question_ids")
        require(type(required) is list and required and len(required) <= 1000, "explicit open question IDs required")
        for qid in required: text(qid)
        require(len(required) == len(set(required)), "duplicate question dependency")
        require(set(required) <= q_to_group.keys(), "unknown question dependency")
        deadline = timestamp(row["deadline"]) if row.get("deadline") is not None else None
        if row["fit_score"] < 75 or row.get("action_band") != "APPLY" or not row["other_gates_clear"] or (deadline and deadline <= now):
            excluded.append(rid); continue
        needed = sorted({q_to_group[q] for q in required})
        bundles.append({"role_id": rid, "required_group_ids": needed, "release_authorized": False})
        for gid in needed:
            group = groups[gid]
            group["roles_progressed"].append(rid)
            if len(needed) == 1:
                group["roles_unlocked_if_answered"].append(rid)
            if deadline:
                old = group["earliest_deadline"]
                if old is None or deadline < timestamp(old): group["earliest_deadline"] = deadline.isoformat()
                group["urgent"] |= deadline <= now + timedelta(hours=24)
    ranked = sorted(groups.values(), key=lambda g: (
        not g["urgent"], -len(g["roles_unlocked_if_answered"]), -len(g["roles_progressed"]),
        timestamp(g["earliest_deadline"]).timestamp() if g["earliest_deadline"] else float("inf"), g["group_id"]))
    return {**report, "groups": ranked, "dependency_bundles": bundles,
            "excluded_roles": excluded, "ranking": "urgent; single-group unlocks; roles progressed; deadline",
            "limitation": "counts are conditional on current supplied gates and a valid operator reply; nothing is resolved"}
