"""Read-only audit of explicit exports from the CURRENT live verifier.

Never imports/replaces a private verifier, performs HTTP, clears a hold or
changes a queue. Eligibility here means eligible for VERIFICATION, not READY.
The export schema is documented in docs/SUPPLY_RECOVERY.md.
"""
from collections import Counter
from datetime import timedelta, timezone
import math
from .contracts import ContractError, digest, number, text, timestamp, versioned
from .supply import aware, pool_health

HOLDS = {"consent_quarantine", "operator_input", "policy", "rate_limit", "dedupe", "unknown_attempt"}
PENDING = "PARKED-PENDING-VERIFICATION"


def _cooldown(row, now):
    """Reconcile exported policy outputs; do not guess a private hash algorithm."""
    cooldown = row.get("cooldown")
    if type(cooldown) is not dict:
        raise ContractError("cooldown policy output missing")
    text(cooldown.get("policy_revision"), "cooldown.policy_revision")
    base = number(cooldown.get("base_hours"), "base_hours", minimum=0.000001)
    delta = number(cooldown.get("jitter_delta_hours"), "jitter_delta_hours", minimum=-float("inf"))
    effective = number(cooldown.get("effective_hours"), "effective_hours", minimum=0.000001)
    # The supplied report specifies +/-25% jitter and half-hour quantization.
    # Half a quantum permits rounding at either edge of the jitter interval.
    if not math.isclose(effective, base + delta, abs_tol=1e-8, rel_tol=0):
        raise ContractError("effective cooldown differs from base plus exported jitter")
    if abs(delta) > base * 0.25 + 0.25 + 1e-8:
        raise ContractError("cooldown exceeds reported jitter envelope")
    if not math.isclose(effective / 0.5, round(effective / 0.5), abs_tol=1e-8, rel_tol=0):
        raise ContractError("cooldown is not quantized to 0.5h")
    if "last_verify_attempt" not in row:
        raise ContractError("last_verify_attempt missing; null must be explicit for never attempted")
    attempt = row["last_verify_attempt"]
    if attempt is None:
        due = now
    else:
        last = timestamp(attempt)
        if last > now:
            raise ContractError("last_verify_attempt is future-dated")
        due = last + timedelta(hours=effective)
    calculated = due > now
    reported = cooldown.get("reported_in_cooldown")
    if type(reported) is not bool:
        raise ContractError("reported_in_cooldown must come from the live verifier")
    if reported is not calculated:
        raise ContractError("live cooldown decision disagrees with calculated deadline")
    if cooldown.get("reported_eligible_at") is not None:
        exported = timestamp(cooldown["reported_eligible_at"])
        if abs((due - exported).total_seconds()) > 1:
            raise ContractError("reported eligible time disagrees with calculated deadline")
    return due, effective


def _row_audit(row, *, at):
    result = {"role_id": row.get("role_id"), "state": "UNKNOWN", "diagnostics": [],
              "eligible_at": None, "row_sha256": digest(row), "queue_mutation_authorized": False}
    rules = row.get("structural_rules")
    if row.get("structurally_blocked") is True:
        result["structural_rules"] = rules if type(rules) is list else []
        if not rules or type(rules) is not list or any(
                type(r) is not dict or any(type(r.get(k)) is not str or not r[k].strip()
                                          for k in ("rule_id", "field", "evidence_ref")) for r in rules):
            result["diagnostics"].append("structural block lacks rule-level evidence: inspect source and raw fields")
        if type(rules) is list and any(type(r) is dict and r.get("contradiction_ref") for r in rules):
            result["diagnostics"].append("reported evidence conflict: review each cited artifact; retain hold")
    try:
        text(row.get("role_id"), "role_id")
        text(row.get("status"), "status")
        if row["status"] != PENDING:
            result["state"] = "NOT_PENDING_VERIFICATION"
            return result
        holds = row.get("holds")
        if type(holds) is not list or any(type(h) is not str or h not in HOLDS for h in holds):
            raise ContractError("explicit supported holds list required; empty means checked and none found")
        if holds:
            result["state"] = ("CONSENT_QUARANTINED" if "consent_quarantine" in holds else
                               "OPERATOR_INPUT" if "operator_input" in holds else "POLICY_OR_SAFETY_HOLD")
            result["holds"] = sorted(set(holds))
            return result
        fit = number(row.get("fit_score"), "fit_score", minimum=0, maximum=100)
        if fit < 75:
            result["state"] = "LOW_FIT"
            return result
        if row.get("action_band") != "APPLY":
            raise ContractError("APPLY band is not explicitly established")
        blocked = row.get("structurally_blocked")
        if type(blocked) is not bool:
            raise ContractError("structurally_blocked must come from the current live function")
        if blocked:
            result["state"] = "STRUCTURALLY_BLOCKED"
            return result
        due, effective = _cooldown(row, at)
        result.update(state="COOLDOWN" if due > at else "ACTIONABLE_FOR_VERIFICATION",
                      eligible_at=due.astimezone(timezone.utc).isoformat(),
                      effective_cooldown_hours=effective)
    except (ValueError, TypeError, OverflowError) as exc:
        result["diagnostics"].append(str(exc))
    return result


def audit_supply(document, *, now):
    versioned(document); aware(now)
    text(document.get("source_revision"), "source_revision")
    observed = timestamp(document.get("observed_at"))
    data = document.get("leads")
    if type(data) is not list or len(data) > 50000 or any(type(row) is not dict for row in data):
        raise ContractError("leads must be an array of at most 50000 objects")
    fresh = 0 <= (now - observed).total_seconds() <= 90
    identities = Counter(row.get("role_id") for row in data if type(row.get("role_id")) is str)
    audited = []
    for row in data:
        # Reconcile cooldown predicates at the SAME instant the live export used.
        result = _row_audit(row, at=observed)
        rid = row.get("role_id")
        if type(rid) is str and identities[rid] > 1:
            result.update(state="UNKNOWN", eligible_at=None)
            result["diagnostics"].append("duplicate role_id in export; reconcile identities")
        if not fresh:
            result["state_at_observation"] = result["state"]
            result.update(state="UNKNOWN", eligible_at=None)
            result["diagnostics"].append("export stale or future-dated; capture current observations")
        audited.append(result)
    counts = Counter(r["state"] for r in audited)
    release = Counter(r["eligible_at"] for r in audited if r["state"] == "COOLDOWN")
    schedule, cumulative = [], 0
    for due, count in sorted(release.items()):
        cumulative += count
        schedule.append({"eligible_at": due, "leads": count, "cumulative": cumulative})
    known_actionable = counts["ACTIONABLE_FOR_VERIFICATION"]
    # An incomplete export or even one unknown record cannot establish exact counts.
    complete = document.get("complete") is True and fresh and counts["UNKNOWN"] == 0
    health = pool_health(document.get("ready"), known_actionable if complete else None,
                         observed_at=document["observed_at"], now=now)
    return {"schema_version": 1, "mode": "READ_ONLY_AUDIT", "source_revision": document["source_revision"],
            "source_export_sha256": digest(document), "observed_at": document["observed_at"],
            "complete_and_current": complete, "counts": dict(counts), "total_records": len(audited),
            "known_actionable_for_verification": known_actionable,
            "reported_structural_count": sum(r.get("structurally_blocked") is True for r in data),
            "next_eligible_at": schedule[0]["eligible_at"] if schedule else None,
            "cooldown_release_schedule": schedule, "pool_health": health,
            "review_candidates": [r["role_id"] for r in audited if r["diagnostics"]],
            "rows": audited, "queue_writes": 0, "http_requests": 0,
            "liveness_established": False, "ready_promotions": 0}
