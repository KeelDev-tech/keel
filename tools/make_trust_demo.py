"""Synthetic trust export; examples are fixtures, never applicant facts."""
import json
from datetime import timedelta
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.make_flow_demo import NOW, make_snapshot
from keel_flow.board import build
from keel_trust.common import digest


def evidence():
    expires = (NOW + timedelta(hours=1)).isoformat()
    source = {"source_id": "source-example", "workspace_id": "keel-demo", "publisher_id": "synthetic-applicant",
              "source_ref": "fixture://applicant-assertion", "revision": "v1", "content_hash": digest("synthetic-experience"),
              "origin": "APPLICANT_RECORD", "observed_at": NOW.isoformat(), "expires_at": expires,
              "status": "ACTIVE", "verification_ref": None}
    claim = {"claim_id": "claim-example", "workspace_id": "keel-demo", "subject_id": "synthetic-applicant",
             "predicate": "experience-example", "value_hash": digest("synthetic-experience"), "revision": "v1",
             "kind": "EXPERIENCE", "basis": "SELF_ATTESTED", "review_state": "APPROVED",
             "approval_ref": "fixture://explicit-approval", "allowed_scopes": ["synthetic-keel-application"],
             "allowed_wording": ["Synthetic fixture experience; not a real qualification."],
             "evidence": [{k: source[k] for k in ("source_id", "revision", "content_hash")}],
             "expires_at": expires, "conflicts_with": []}
    def artifact(aid, parents, statements):
        return {"artifact_id": aid, "workspace_id": "keel-demo", "revision": "v1", "scope": "synthetic-keel-application",
                "statements": statements, "depends_on": parents}
    return {"schema_version": 1, "workspace_id": "keel-demo", "source_revision": "SYNTHETIC-EVIDENCE-v1",
            "observed_at": NOW.isoformat(), "complete": True, "sources": [source], "claims": [claim],
            "artifacts": [artifact("resume", [], [{"claim_id": claim["claim_id"], "claim_revision": claim["revision"],
                                                "wording": claim["allowed_wording"][0]}]),
                          artifact("packet", ["resume"], []), artifact("interview", ["packet"], [])]}


def research(check_id="check-example", **changes):
    return {"check_id": check_id, "uncertainty_key": "synthetic-posting-live", "role_ids": ["role-1"],
            "fit_score": 84, "permitted": True, "rate_limited": False, "affects_decision": True,
            "probability_of_change": .5, "decision_value": 20, "estimated_minutes": 2,
            "estimate_ref": "fixture://operator-estimate", "observed_at": NOW.isoformat(),
            "evidence_hash": digest("synthetic-research-evidence"), **changes}


def make_document():
    flow, facts = make_snapshot(), evidence()
    packet = facts["artifacts"][1]
    return {"schema_version": 1, "workspace_id": "keel-demo", "source_revision": "SYNTHETIC-TRUST-v1",
            "observed_at": NOW.isoformat(), "complete": True, "flow_export": flow, "evidence_export": facts,
            "artifact_bindings": [{"role_id": row["role_id"], "artifact_id": packet["artifact_id"],
                                   "artifact_revision": packet["revision"], "artifact_sha256": digest(packet),
                                   "packet_dependency_hash": row["packet_dependency_hash"]} for row in flow["leads"]],
            "question_costs": [{"group_id": row["group_id"], "estimated_minutes": 2, "owner": "synthetic-applicant",
                                "estimate_ref": "fixture://question-time-estimate"} for row in build(flow, now=NOW)["questions"]["groups"]],
            "research_checks": [research(), research("check-duplicate"), research("check-unknown", probability_of_change=None)],
            "decision_budget_minutes": 5, "research_budget_minutes": 5}


if __name__ == "__main__": print(json.dumps(make_document(), indent=2))
