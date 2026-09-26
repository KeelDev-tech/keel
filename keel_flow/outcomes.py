"""Comparable fixed-window outcome cohorts from explicitly attributed exports."""
from collections import defaultdict, Counter
from datetime import timedelta
from .common import unique, number, integer, boolean, text, timestamp, require, clock, wilson


def cohorts(applications, *, now, horizon_days=14):
    clock(now); integer(horizon_days, maximum=180); require(horizon_days > 0, "positive follow-up horizon required")
    unique(applications, "application_id")
    references = Counter(row.get("submission_ref") for row in applications if type(row.get("submission_ref")) is str)
    groups, exclusions = defaultdict(list), []
    for row in applications:
        app = row["application_id"]
        text(row.get("source_id")); number(row.get("fit_score"), maximum=100)
        boolean(row.get("submission_confirmed_by_adapter"))
        if not row["submission_confirmed_by_adapter"]:
            exclusions.append({"application_id": app, "reason": "SUBMISSION_UNCONFIRMED"}); continue
        ref = text(row.get("submission_ref")); text(row.get("adapter_revision"))
        if references[ref] > 1:
            exclusions.append({"application_id": app, "reason": "SUBMISSION_REFERENCE_REUSED"}); continue
        submitted = timestamp(row.get("submitted_at"))
        require(submitted <= now, "future submission date")
        end = submitted + timedelta(days=horizon_days)
        if end > now:
            exclusions.append({"application_id": app, "reason": "COHORT_IMMATURE"}); continue
        through = timestamp(row.get("followup_observed_through"))
        require(through <= now and through >= submitted, "invalid follow-up observation window")
        if through < end:
            exclusions.append({"application_id": app, "reason": "FOLLOWUP_INCOMPLETE"}); continue
        outcomes = {}
        for name in ("interview", "offer", "rejection"):
            stamp = row.get(name + "_at")
            if stamp is None:
                outcomes[name] = False
            else:
                time = timestamp(stamp)
                require(submitted <= time <= through, "outcome outside observed history")
                text(row.get(name + "_evidence_ref"))
                outcomes[name] = time <= end
        band = "below_75" if row["fit_score"] < 75 else "75_to_84" if row["fit_score"] < 85 else "85_to_100"
        groups[(row["source_id"], band)].append(outcomes)
    result = []
    for (source, band), rows in sorted(groups.items()):
        n = len(rows)
        values = {name: sum(r[name] for r in rows) for name in ("interview", "offer", "rejection")}
        result.append({"source_id": source, "fit_band": band, "mature_observed_applications": n,
                       **values, "interview_rate": values["interview"] / n,
                       "interview_rate_interval_95": wilson(values["interview"], n),
                       "offer_rate": values["offer"] / n})
    return {"horizon_days": horizon_days, "cohorts": result, "excluded": exclusions,
            "denominator": sum(g["mature_observed_applications"] for g in result),
            "evidence_boundary": "caller exports claim trusted-adapter confirmation; this reducer does not authenticate receipts",
            "no_response_is_rejection": False, "causal_superiority_established": False}
