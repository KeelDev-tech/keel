"""Blocker ownership, deadlines and revision-bound review; never clears holds."""
from collections import Counter
from .common import clock, unique, text, timestamp, require, integer, digest, hexdigest

FAMILIES = {"dedupe", "verification", "cooldown", "consent", "operator_input", "policy", "unknown_attempt"}


def hold_scope(hold):
    """Bind a review to the role, blocker, evidence and exact release condition."""
    return digest({key: hold.get(key) for key in
                   ("hold_id", "role_id", "family", "evidence_revision", "release_condition", "opened_at")})


def review_holds(holds, decisions, *, now):
    clock(now); unique(holds, "hold_id"); unique(decisions, "decision_id")
    by_hold = {}
    for decision in decisions:
        text(decision.get("hold_id")); text(decision.get("evidence_revision")); text(decision.get("authority_ref"))
        hexdigest(decision.get("hold_scope_sha256"))
        require(decision.get("decision") in {"KEEP", "PROPOSE_RELEASE", "PROPOSE_REJECT"}, "unsupported review decision")
        require(timestamp(decision.get("decided_at")) <= now, "future review decision")
        refs = decision.get("evidence_refs")
        require(type(refs) is list and 0 < len(refs) <= 100, "review evidence references required")
        for ref in refs: text(ref)
        by_hold.setdefault(decision["hold_id"], []).append(decision)
    require(set(by_hold) <= {h["hold_id"] for h in holds}, "review references an unknown hold")
    results = []
    for hold in holds:
        for key in ("role_id", "evidence_revision", "release_condition"):
            text(hold.get(key), key)
        require(hold.get("family") in FAMILIES, "unknown hold family")
        integer(hold.get("occurrences"), maximum=1000000)
        opened, due = timestamp(hold.get("opened_at")), timestamp(hold.get("review_after"))
        require(opened <= now and due >= opened, "invalid hold dates")
        require("owner" in hold, "owner must be present, using null when unassigned")
        owner = hold.get("owner")
        require(owner is None or type(owner) is str, "owner must be a string or explicit null")
        candidates = [d for d in by_hold.get(hold["hold_id"], []) if d["evidence_revision"] == hold["evidence_revision"]
                      and d["hold_scope_sha256"] == hold_scope(hold) and timestamp(d["decided_at"]) >= opened]
        latest = max(candidates, key=lambda d: (timestamp(d["decided_at"]), d["decision_id"])) if candidates else None
        conflicts = bool(latest and len({d["decision"] for d in candidates if timestamp(d["decided_at"]) == timestamp(latest["decided_at"])}) > 1)
        if conflicts:
            action = "REVIEW_CONFLICTING_DECISIONS"
        elif not owner or not owner.strip():
            action = "ASSIGN_OWNER"
        elif hold["family"] in {"consent", "operator_input"}:
            action = "AWAIT_OPERATOR_REPLY"
        elif latest:
            action = {"KEEP": "RETAIN_REVIEWED_HOLD", "PROPOSE_RELEASE": "VALIDATE_CANONICAL_RELEASE", "PROPOSE_REJECT": "VALIDATE_CANONICAL_REJECTION"}[latest["decision"]]
        elif due <= now or hold["occurrences"] >= 3:
            action = "ESCALATE_REVIEW"
        else:
            action = "WAIT_UNTIL_REVIEW"
        results.append({"hold_id": hold["hold_id"], "role_id": hold["role_id"], "family": hold["family"],
                        "owner": owner, "age_seconds": (now - opened).total_seconds(), "overdue": due <= now,
                        "repeated_unchanged_evidence": hold["occurrences"] >= 3,
                        "evidence_revision": hold["evidence_revision"], "hold_sha256": digest(hold),
                        "hold_scope_sha256": hold_scope(hold),
                        "action": action, "review_record_id": latest["decision_id"] if latest and not conflicts else None,
                        "review_reusable_for_inspection": latest is not None and not conflicts,
                        "release_condition": hold["release_condition"], "release_authorized": False})
    return {"rows": results, "actions": dict(Counter(r["action"] for r in results)),
            "overdue_count": sum(r["overdue"] for r in results), "queue_writes": 0}
