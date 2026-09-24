"""Exercise real reducers together; no live accounts or model calls."""
import copy
from datetime import timedelta
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.make_workflow_demo import make_fixture, NOW
from keel_workflow.integration import evaluate_candidate, simulate_candidate, context_revisions
from tools.make_assurance_demo import refresh_reviews
from keel_workflow.reviews import ReviewStore, subject_digest
from keel_workflow.delivery import DeliveryStore, SyntheticAdapter, build_bundle, Bundle


def evaluate(fixture, now=NOW):
    bundle, flow, assurance, trust = fixture
    return evaluate_candidate(bundle, flow_export=flow, assurance_export=assurance, trust_export=trust, now=now)


def reviewed(tmp_path, fixture):
    candidate = evaluate(fixture)
    reviews = ReviewStore(tmp_path / "reviews.sqlite3")
    subject = candidate["subject"]
    sha = subject_digest(subject)
    reviews.create_round("round-1", subject, candidate["reviewer_ids"], NOW + timedelta(minutes=10), now=NOW)
    for reviewer in candidate["reviewer_ids"]:
        reviews.commit("round-1", reviewer, sha, verdict="PASS", covered_claim_ids=subject["required_claim_ids"], now=NOW)
    reviews.seal("round-1", sha, now=NOW)
    for reviewer in candidate["reviewer_ids"]:
        reviews.phase_b("round-1", reviewer, sha, now=NOW)
        reviews.audit("round-1", reviewer, sha, verdict="PASS", now=NOW)
    return reviews


def test_combines_both_branches_and_excludes_previous_opinions():
    fixture = make_fixture()
    before = copy.deepcopy(fixture[1:])
    result = evaluate(fixture)
    assert result["state"] == "READY_FOR_BLIND_REVIEW"
    assert result["flow_checks_passed"] and result["trust_checks_passed"]
    assert not result["execution_authorized"]
    assert "reviews" not in result["subject"] and "human_review" not in result["subject"]
    assert fixture[1:] == before


def test_mismatched_original_snapshot_fails_even_if_both_independently_valid():
    fixture = list(make_fixture())
    fixture[3] = copy.deepcopy(fixture[3])
    fixture[3]["flow_export"]["source_revision"] = "different"
    with pytest.raises(ValueError, match="snapshot mismatch"):
        evaluate(fixture)


def test_stale_exports_cannot_reach_blind_ready():
    result = evaluate(make_fixture(), NOW + timedelta(seconds=91))
    assert result["state"] == "BLOCKED"
    assert "FLOW_UNVERIFIED" in result["reasons"]


def alter_bundle(bundle, **changes):
    doc = bundle.document
    fields = {k: doc[k] for k in ("workspace_id", "role_id", "action", "destination", "account_id", "revisions", "content")}
    fields.update(changes)
    return build_bundle(**fields, attachments={"resume.txt": b"SYNTHETIC DEMONSTRATION ONLY. No real applicant or qualification.\n"})


@pytest.mark.parametrize("field,value", [("workspace_id", "another-workspace"),
                                           ("account_id", "another-account"),
                                           ("destination", "https://synthetic.invalid/other")])
def test_bundle_scope_changes_are_not_reapproved_implicitly(field, value):
    fixture = list(make_fixture())
    fixture[0] = alter_bundle(fixture[0], **{field: value})
    with pytest.raises(ValueError, match="mismatch"):
        evaluate(fixture)


def test_attachment_edits_cannot_reuse_assurance():
    fixture = list(make_fixture())
    doc = fixture[0].document
    fixture[0] = build_bundle(**{k: doc[k] for k in ("workspace_id", "role_id", "action", "destination", "account_id", "revisions", "content")},
                             attachments={"resume.txt": b"changed"})
    with pytest.raises(ValueError, match="attachment bytes changed"):
        evaluate(fixture)


def test_forged_bundle_object_with_replaced_bytes_is_rejected():
    fixture = list(make_fixture())
    original = fixture[0]
    fixture[0] = Bundle(original.canonical_json, (("resume.txt", b"forged bytes"),))
    with pytest.raises(ValueError):
        evaluate(fixture)


def test_different_claim_payload_fails_before_review():
    fixture = list(make_fixture())
    content = fixture[0].document["content"]
    content["application"]["claim_values"]["claim-1"] = "invented qualification"
    fixture[0] = alter_bundle(fixture[0], content=content)
    with pytest.raises(ValueError, match="payload differs"):
        evaluate(fixture)


def test_trust_revocation_blocks_even_with_passed_assurance():
    fixture = list(make_fixture())
    fixture[3]["evidence_export"]["sources"][0]["status"] = "REVOKED"
    application = fixture[0].document["content"]["application"]
    fixture[0] = alter_bundle(fixture[0], revisions=context_revisions(application, fixture[2], fixture[3], "role-1"))
    result = evaluate(fixture)
    assert result["flow_checks_passed"]
    assert not result["trust_checks_passed"]
    assert result["state"] == "BLOCKED"


def test_blind_task_excludes_unrelated_fact_values():
    fixture = list(make_fixture())
    unrelated = copy.deepcopy(fixture[2]["facts"][0])
    unrelated.update(fact_id="unrelated-private-fact", subject="another-person", field="another-field")
    fixture[2]["facts"].append(unrelated)
    refresh_reviews(fixture[2])
    application = fixture[0].document["content"]["application"]
    fixture[0] = alter_bundle(fixture[0], revisions=context_revisions(application, fixture[2], fixture[3], "role-1"))
    result = evaluate(fixture)
    assert result["state"] == "READY_FOR_BLIND_REVIEW"
    assert [f["fact_id"] for f in result["subject"]["facts"]] == ["synthetic-fact"]


def test_simulation_requires_current_blind_review_and_exact_approval(tmp_path):
    fixture = make_fixture()
    bundle, flow, assurance, trust = fixture
    reviews = reviewed(tmp_path, fixture)
    delivery = DeliveryStore(tmp_path / "delivery.sqlite3")
    approval = delivery.approve(bundle, approver_id="synthetic-owner", authority_ref="fixture:authority",
                                expires_at=NOW + timedelta(minutes=10), now=NOW)
    result = simulate_candidate(bundle, flow_export=flow, assurance_export=assurance, trust_export=trust,
                                reviews=reviews, round_id="round-1", delivery=delivery,
                                approval_id=approval["approval_id"], idempotency_key="simulation-1",
                                adapter=SyntheticAdapter(mode="confirmed"), current_account_id="synthetic-account",
                                current_authority_ref="fixture:authority", now=NOW)
    assert result["blind_review"]["state"] == "READY_FOR_HUMAN_REVIEW"
    assert not result["execution_authorized"]
    assert not result["simulation"]["execution_authorized"]


def test_incomplete_review_cannot_reach_simulator(tmp_path):
    fixture = make_fixture()
    bundle, flow, assurance, trust = fixture
    reviews = ReviewStore(tmp_path / "reviews.sqlite3")
    candidate = evaluate(fixture)
    reviews.create_round("round-1", candidate["subject"], candidate["reviewer_ids"], NOW + timedelta(minutes=10), now=NOW)
    delivery = DeliveryStore(tmp_path / "delivery.sqlite3")
    approval = delivery.approve(bundle, approver_id="synthetic-owner", authority_ref="fixture:authority",
                                expires_at=NOW + timedelta(minutes=10), now=NOW)
    with pytest.raises(ValueError, match="review incomplete"):
        simulate_candidate(bundle, flow_export=flow, assurance_export=assurance, trust_export=trust,
                           reviews=reviews, round_id="round-1", delivery=delivery,
                           approval_id=approval["approval_id"], idempotency_key="simulation-1",
                           adapter=SyntheticAdapter(mode="confirmed"), current_account_id="synthetic-account",
                           current_authority_ref="fixture:authority", now=NOW)
    assert delivery.events() == [] or all("attempt" not in str(row.get("kind", "")) for row in delivery.events())


def test_changed_context_cannot_reuse_blind_commitments(tmp_path):
    fixture = make_fixture()
    reviews = reviewed(tmp_path, fixture)
    altered = copy.deepcopy(fixture[3])
    # A new research observation changes the exact context without affecting the
    # already validated material. Old blind commitments must still be rejected.
    altered["research_checks"][0]["estimated_minutes"] = 3
    candidate = evaluate_candidate(fixture[0], flow_export=fixture[1], assurance_export=fixture[2], trust_export=altered, now=NOW)
    assert candidate["state"] == "READY_FOR_BLIND_REVIEW"
    result = reviews.evaluate("round-1", subject_digest(candidate["subject"]), now=NOW)
    assert result["state"] == "HOLD"
    assert result["reasons"]
