"""Join the supplied readiness contract with fit, holds and attempt history."""
from collections import Counter
from keel_local.readiness import evaluate_readiness
from .common import clock, records, unique, number, require, hexdigest


def inventory(leads, hold_report, attempt_report, *, now):
    clock(now); unique(leads, "role_id")
    identities = Counter(row.get("identity") for row in leads if type(row.get("identity")) is str)
    held_roles = {row["role_id"] for row in hold_report["rows"]}
    held_apps = {row["application_id"] for row in attempt_report["applications"] if row["hold_required"]}
    results = []
    for row in leads:
        result = evaluate_readiness(row, now=now, maximum_age_seconds=90)
        reasons = list(result.reasons)
        number(row.get("fit_score"), "fit_score", maximum=100)
        hexdigest(row.get("identity"))
        require(type(row.get("holds")) is list and all(type(v) is str and v.strip() for v in row["holds"]),
                "explicit hold list required")
        if row["fit_score"] < 75: reasons.append("fit_below_75")
        if row.get("action_band") != "APPLY": reasons.append("action_band_not_apply")
        if row["holds"] or row["role_id"] in held_roles: reasons.append("canonical_hold_present")
        if row["identity"] in held_apps: reasons.append("unresolved_attempt_history")
        if not attempt_report["history_complete"]: reasons.append("attempt_history_incomplete")
        if identities[row["identity"]] != 1: reasons.append("duplicate_identity")
        results.append({"role_id": row["role_id"], "application_id": row["identity"],
                        "executable": not reasons, "reasons": sorted(set(reasons)), "route": result.route})
    nominal = sum(row.get("status") == "READY" for row in leads)
    actual = sum(row["executable"] for row in results)
    return {"nominal_ready": nominal, "executable_ready": actual, "nominal_to_actual_loss": nominal - actual,
            "rows": results, "blocked_reasons": dict(Counter(reason for row in results for reason in row["reasons"])),
            "execution_authorized": False, "basis": "supplied export; live lane must recheck all gates"}
