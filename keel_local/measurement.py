"""Conservative measurement: missing meters and unverified receipts stay visible."""
from collections import Counter, defaultdict
from .contracts import ContractError, number, text, digest

DIMENSIONS = {"http_requests": "requests", "browser_taps": "taps",
              "browser_seconds": "seconds", "model_usd": "USD",
              "infrastructure_usd": "USD", "human_recovery_seconds": "seconds",
              "active_user_seconds": "seconds"}


def summarize_cost(attempt_ids, events):
    attempts = set(attempt_ids)
    for item in attempts:
        text(item)
    totals = {key: 0 for key in DIMENSIONS}
    coverage = {key: set() for key in DIMENSIONS}
    seen = {}
    for event in events:
        eid = text(event.get("event_id"))
        if eid in seen:
            if seen[eid] != digest(event):
                raise ContractError("conflicting meter replay")
            continue
        seen[eid] = digest(event)
        aid, dimension = event.get("attempt_id"), event.get("dimension")
        if aid not in attempts or dimension not in DIMENSIONS:
            raise ContractError("unknown attempt or meter dimension")
        if event.get("unit") != DIMENSIONS[dimension] or event.get("is_proxy") is not False:
            raise ContractError("proxy or incompatible unit cannot be counted")
        totals[dimension] += number(event.get("value"))
        # A final meter sample explicitly attests complete observation for that
        # dimension/attempt. One incidental tap does not mean complete coverage.
        if event.get("complete") is True:
            coverage[dimension].add(aid)
    missing = {key: sorted(attempts - coverage[key]) for key in DIMENSIONS}
    complete = not any(missing.values())
    return {"attempts_including_failures": len(attempts), "known_totals": totals,
            "missing_attempts_by_dimension": missing, "complete": complete,
            "known_usd": totals["model_usd"] + totals["infrastructure_usd"],
            "all_in_cost_usd": None, "cost_per_verified_completion_usd": None,
            "reason": "human-time valuation, provider costs and receipt-backed denominator need live integration"}


def receipt_diagnostic(raw, expected):
    """Negative checks only. NEVER returns VERIFIED or writes a success.

    A matching document can still be forged. Provider-specific trusted capture
    and independent validation are intentionally required outside this reducer.
    """
    reasons = []
    if type(raw) is not dict or type(expected) is not dict:
        return {"status": "UNKNOWN", "reasons": ["malformed_evidence"]}
    for key in ("attempt_id", "provider", "employer_id", "posting_id", "request_hash"):
        if not expected.get(key) or raw.get(key) != expected[key]:
            reasons.append("correlation_" + key)
    if not raw.get("provider_receipt_id"):
        reasons.append("provider_receipt_missing")
    reasons.append("trusted_provider_verifier_required")
    return {"status": "UNKNOWN", "reasons": reasons, "may_retry": False, "may_count_submitted": False}


def internal_telemetry(rows):
    """Allowlisted labels + per-candidate contribution bound; INTERNAL ONLY.

    This is not differential privacy or an anonymized dataset. No publication,
    pricing, sale, or federated-export path is exposed by this package.
    """
    categories = {"SUPPLY_LIMITED", "PROVIDER_LIMITED", "AWAITING_DECISION", "INTERNAL_FAILURE"}
    candidates = {}
    for row in rows:
        cid, label = row.get("candidate_id"), row.get("category")
        if type(cid) is not str or not cid or label not in categories:
            raise ContractError("invalid privacy unit or label")
        candidates.setdefault(cid, set()).add(label)
    # One contribution per candidate total, not one per lead or event.
    counts = Counter(sorted(values)[0] for values in candidates.values())
    return {"scope": "INTERNAL_ONLY", "privacy_unit": "candidate", "counts": dict(counts),
            "public_export_allowed": False, "anonymized": False}
