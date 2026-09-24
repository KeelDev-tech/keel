#!/usr/bin/env python3
"""Reproducible synthetic boundary replay. Not a real-world benchmark."""
import argparse
import copy
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.make_flow_demo import make_snapshot, NOW
from tools.make_assurance_demo import make_envelope, refresh_reviews
from keel_flow.board import build
from keel_assurance.core import evaluate


def replay():
    snapshot = make_snapshot()
    baseline = build(snapshot, now=NOW)
    cases = []
    for name in ("valid", "revoked_source", "unknown_authority", "invented_claim", "dissent",
                 "human_rejection", "correlated_reviewers", "budget_exhausted", "partial", "removed_assurance"):
        envelope = make_envelope(snapshot)
        action = envelope["actions"][0]
        if name == "revoked_source": envelope["evidence"][0]["status"] = "REVOKED"
        if name == "unknown_authority": action["authority"]["status"] = "UNKNOWN"
        if name == "invented_claim": action["proposal"]["claims"][0]["value_sha256"] = "0" * 64
        if name == "human_rejection": action["human_review"]["status"] = "REJECT"
        if name == "correlated_reviewers": envelope["reviewers"][1]["family"] = envelope["reviewers"][0]["family"]
        if name == "budget_exhausted": action["iteration"] = 3
        if name == "partial": envelope["complete"] = False
        refresh_reviews(envelope)
        if name == "dissent": action["reviews"][0]["verdict"] = "FAIL"
        before = copy.deepcopy((snapshot, envelope))
        start = perf_counter()
        report = build(snapshot, now=NOW, assurance=None if name == "removed_assurance" else envelope)
        elapsed = perf_counter() - start
        qualified = report["readiness"]["assurance_qualified_ready"]
        expected = None if name == "removed_assurance" else 1 if name == "valid" else 0
        matched = qualified == expected and before == (snapshot, envelope) and not report["execution_authorized"]
        cases.append({"case": name, "expected_assurance_qualified": expected,
                      "actual_assurance_qualified": qualified,
                      "assurance_state": report["assurance"]["state"], "elapsed_seconds": elapsed,
                      "expectations_met": matched, "execution_authorized": report["execution_authorized"]})
    return {"schema_version": 1, "scope": "10 synthetic contract scenarios; not independently sampled real-world cases",
            "baseline_legacy_executable": baseline["readiness"]["executable_ready"],
            "baseline_assurance_state": baseline["assurance"]["state"],
            "all_expectations_met": all(c["expectations_met"] for c in cases), "cases": cases,
            "production_deployed": False,
            "limitations": "Elapsed times are one local run, not a capacity SLO. No real reviews or model efficacy measured."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="new JSON report")
    args = parser.parse_args()
    report = replay()
    with Path(args.out).open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False); stream.write("\n")
    print(json.dumps({"all_expectations_met": report["all_expectations_met"], "cases": len(report["cases"])}))
    return 0 if report["all_expectations_met"] else 1


if __name__ == "__main__": raise SystemExit(main())
