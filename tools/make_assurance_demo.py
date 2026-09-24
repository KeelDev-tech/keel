#!/usr/bin/env python3
"""Synthetic test fixture only. No real facts, approvals or model reviews."""
from datetime import timedelta
import argparse
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.make_flow_demo import NOW, make_snapshot
from keel_flow.common import digest
from keel_assurance.core import proposal_scope, review_subject


def make_envelope(snapshot=None):
    snapshot = make_snapshot() if snapshot is None else snapshot
    expiry = (NOW + timedelta(hours=1)).isoformat()
    envelope = {
        "schema_version": 1, "source_revision": snapshot["source_revision"],
        "export_sha256": digest(snapshot), "observed_at": NOW.isoformat(), "complete": True,
        "evidence": [{"id": "synthetic-profile-v1", "revision": "1", "expected_revision": "1",
                      "status": "CURRENT", "observed_at": NOW.isoformat(), "expires_at": expiry, "parents": []}],
        "facts": [{"fact_id": "synthetic-fact", "subject": "SYNTHETIC-PERSON",
                   "field": "demonstration_skill", "value_sha256": digest("SYNTHETIC-VALUE-NOT-A-QUALIFICATION"),
                   "purposes": ["application-preparation"],
                   "targets": [lead["role_id"] for lead in snapshot["leads"]],
                   "evidence_roots": ["synthetic-profile-v1"]}],
        "reviewers": [{"reviewer_id": "synthetic-source-check", "method": "source-comparison",
                       "family": "fixture-rule-check", "independence_group": "synthetic-a"},
                      {"reviewer_id": "synthetic-adversarial-check", "method": "counterexample-review",
                       "family": "fixture-adversarial-check", "independence_group": "synthetic-b"}],
        "actions": [],
    }
    for lead in snapshot["leads"]:
        action = {"role_id": lead["role_id"], "application_id": lead["identity"],
                  "lead_sha256": digest(lead), "packet_dependency_hash": lead["packet_dependency_hash"],
                  "proposal": {"subject": "SYNTHETIC-PERSON", "purpose": "application-preparation",
                               "target": lead["role_id"], "payload_sha256": digest({"synthetic_payload": lead["role_id"]}),
                               "claims": [{"claim_id": "claim-1", "field": "demonstration_skill",
                                           "value_sha256": envelope["facts"][0]["value_sha256"],
                                           "fact_id": "synthetic-fact", "evidence_roots": ["synthetic-profile-v1"]}]},
                  "risk": {"accountability": 3, "sensitivity": 2, "complexity": 2,
                           "irreversibility": 2, "external_impact": 3},
                  "authority": None, "human_review": None, "iteration": 0, "reviews": []}
        scope = proposal_scope(action)
        action["authority"] = {"status": "GRANTED", "receipt_ref": "SYNTHETIC-NOT-AUTHORITY",
                               "scope_sha256": scope, "observed_at": NOW.isoformat(), "expires_at": expiry, "revision": "1"}
        action["human_review"] = {**action["authority"], "status": "APPROVE", "receipt_ref": "SYNTHETIC-NOT-HUMAN-APPROVAL"}
        envelope["actions"].append(action)
    refresh_reviews(envelope)
    return envelope


def refresh_reviews(envelope):
    """Fixture builder only; NOT a production review generator or approval API."""
    for action in envelope["actions"]:
        action["reviews"] = [{"reviewer_id": reviewer["reviewer_id"],
                              "subject_sha256": review_subject(envelope, action),
                              "verdict": "PASS", "confidence": None,
                              "covered_claim_ids": [c["claim_id"] for c in action["proposal"]["claims"]],
                              "evidence_roots": ["synthetic-profile-v1"],
                              "observed_at": NOW.isoformat(), "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
                              "findings": []} for reviewer in envelope["reviewers"]]
    return envelope


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="new fixture file; existing files never overwritten")
    args = parser.parse_args()
    rendered = json.dumps(make_envelope(), indent=2, allow_nan=False) + "\n"
    if args.out:
        with Path(args.out).open("x", encoding="utf-8") as stream: stream.write(rendered)
    else:
        sys.stdout.write(rendered)
