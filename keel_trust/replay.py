"""Bounded incident replay against actual flow reducers; no dynamic code loading."""
from keel_flow.board import build
from .common import keys, version, text, unique, require, timestamp, digest, clone
from .twin import metrics


def run(cases, *, evaluator=build):
    unique(cases, "case_id")
    if not cases: return {"status": "NOT_RUN", "cases_run": 0, "results": [], "production_deployed": False}
    results = []
    for case in cases:
        keys(case, {"schema_version", "case_id", "incident_ref", "as_of", "flow_export", "expected"}); version(case)
        text(case["incident_ref"]); now = timestamp(case["as_of"])
        require(type(case["expected"]) is dict and case["expected"], "replay requires explicit expected observations")
        row = {"case_id": case["case_id"], "incident_ref": case["incident_ref"], "input_sha256": digest(case["flow_export"])}
        try:
            report = evaluator(clone(case["flow_export"]), now=now)
            actual = metrics(report)
            require(set(case["expected"]) <= actual.keys(), "unknown replay metric")
            failures = {key: {"expected": value, "actual": actual[key]} for key, value in case["expected"].items()
                        if type(value) is not type(actual[key]) or value != actual[key]}
            require(report.get("execution_authorized") is False and all(v == 0 for v in report["effects"].values()), "evaluator reported side effects")
            row.update(status="FAIL" if failures else "PASS", failures=failures, observed=actual)
        except (ValueError, TypeError, KeyError) as exc:
            row.update(status="ERROR", error_type=type(exc).__name__)
        results.append(row)
    return {"status": "PASS" if all(r["status"] == "PASS" for r in results) else "FAIL", "cases_run": len(results),
            "results": results, "production_deployed": False, "scope": "fixture replay, not real-provider end-to-end verification"}


def compare(cases, *, baseline, candidate):
    """Trusted callables supplied by the host; never loaded from an export path."""
    before, after = run(cases, evaluator=baseline), run(cases, evaluator=candidate)
    old = {r["case_id"]: r for r in before["results"]}
    regressions = [r["case_id"] for r in after["results"] if old[r["case_id"]]["status"] == "PASS" and r["status"] != "PASS"]
    return {"baseline": before, "candidate": after, "regressions": regressions,
            "status": "NOT_RUN" if not cases else "REVIEW_REQUIRED" if regressions or after["status"] != "PASS" else "NO_REGRESSION_IN_FIXTURES",
            "performance_improvement_proven": False, "deployment_authorized": False}
