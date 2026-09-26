"""Explicit synthetic v0.7 inputs; no actual model, browser or approvals.

Underlying demonstration facts and receipts are conspicuously synthetic. Legacy
fixture reviewer opinions are removed: a v0.7 review must produce its own actual
commitments. This helper never creates a real snapshot or refreshes real records.
"""
import json
from keel_agent.scope import material_context_revisions
from keel_workflow.delivery import build_bundle
from tools.make_workflow_demo import NOW, make_fixture as make_v06_fixture


def make_fixture():
    """Return (bundle, flow, assurance, trust) at the exported synthetic NOW."""
    old_bundle, flow, assurance, trust = make_v06_fixture()
    for action in assurance["actions"]:
        action["reviews"] = []
    document = old_bundle.document
    application = document["content"]["application"]
    bundle = build_bundle(**{key: document[key] for key in
                            ("workspace_id", "role_id", "action", "destination", "account_id")},
                          revisions=material_context_revisions(application, assurance, trust, document["role_id"]),
                          content={"application": application}, attachments=dict(old_bundle.attachments))
    return bundle, flow, assurance, trust


if __name__ == "__main__":
    bundle, flow, assurance, trust = make_fixture()
    print(json.dumps({"synthetic": True, "fixture_time": NOW.isoformat(),
                      "actual_model_calls": 0, "browser_actions": 0, "execution_authorized": False,
                      "bundle": bundle.to_dict(), "flow": flow, "assurance": assurance, "trust": trust}, indent=2))
