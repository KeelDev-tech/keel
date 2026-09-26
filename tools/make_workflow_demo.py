"""Build synthetic Keel workflow inputs; never generate real approvals/reviews."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.make_flow_demo import NOW
from tools.make_assurance_demo import make_envelope, refresh_reviews
from tools.make_trust_demo import make_document
from keel_assurance.core import proposal_scope
from keel_flow.common import digest
from keel_workflow.delivery import build_bundle
from keel_workflow.integration import context_revisions


def make_fixture():
    trust = make_document()
    flow = trust["flow_export"]
    assurance = make_envelope(flow)
    attachment = b"SYNTHETIC DEMONSTRATION ONLY. No real applicant or qualification.\n"
    artifact = trust["evidence_export"]["artifacts"][0]
    application = {
        "role_id": "role-1", "destination": "https://synthetic.invalid/apply/role-1",
        "account_id": "synthetic-account", "answers": {"name": "Synthetic Applicant"},
        "claim_values": {"claim-1": "SYNTHETIC-VALUE-NOT-A-QUALIFICATION"},
        "attachments": [{"name": "resume.txt", "sha256": hashlib.sha256(attachment).hexdigest(),
                         "artifact_id": artifact["artifact_id"], "artifact_sha256": digest(artifact)}],
    }
    action = assurance["actions"][0]
    action["proposal"]["payload_sha256"] = digest(application)
    for field in ("authority", "human_review"):
        action[field]["scope_sha256"] = proposal_scope(action)
    refresh_reviews(assurance)  # Synthetic fixture authoring, never a production reviewer.
    bundle = build_bundle(workspace_id=trust["workspace_id"], role_id="role-1",
                          action="SIMULATE_SUBMISSION", destination=application["destination"],
                          account_id=application["account_id"],
                          revisions=context_revisions(application, assurance, trust, "role-1"),
                          content={"application": application}, attachments={"resume.txt": attachment})
    return bundle, flow, assurance, trust


if __name__ == "__main__":
    bundle, flow, assurance, trust = make_fixture()
    print(json.dumps({"synthetic": True, "execution_authorized": False,
                      "bundle": bundle.to_dict(), "flow": flow, "assurance": assurance, "trust": trust}, indent=2))
