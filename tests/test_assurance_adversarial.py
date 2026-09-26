"""Independent synthetic adversarial probes, not efficacy or security proofs.

Fixtures explicitly manufacture observations for tests. They are not issuers,
authenticated review providers, real approvals, or a live execution adapter.
"""
import copy
from datetime import timedelta

import pytest

from keel_assurance.core import evaluate, proposal_scope
import keel_assurance.core as assurance_core
from keel_assurance.metrics import evaluate_challenges, evaluate_predictions
from keel_flow.board import build
from keel_flow.common import digest
from tools.make_assurance_demo import NOW, make_envelope, refresh_reviews
from tools.make_flow_demo import make_snapshot


def first_result(envelope):
    return evaluate(envelope, now=NOW)["rows"][0]


def renew_fixture_scope(action):
    """Test data only, never issuance of real authorization."""
    for field in ("authority", "human_review"):
        if action[field] is not None:
            action[field]["scope_sha256"] = proposal_scope(action)


@pytest.mark.parametrize("status", ["REVOKED", "UNKNOWN"])
def test_ancestor_invalidity_reaches_claim_and_combined_inventory(status):
    snapshot = make_snapshot()
    envelope = make_envelope(snapshot)
    source = copy.deepcopy(envelope["evidence"][0])
    source.update(id="ancestor", status=status)
    envelope["evidence"].append(source)
    envelope["evidence"][0]["parents"] = [source["id"]]
    refresh_reviews(envelope)
    report = build(snapshot, now=NOW, assurance=envelope)
    assert report["readiness"]["base_executable_ready"] == 1
    assert report["readiness"]["assurance_qualified_ready"] == 0
    assert report["assurance"]["invalid_evidence_count"] == 2
    assert all("evidence_dependency_invalid" in row["reasons"]
               for row in report["assurance"]["rows"])
    assert report["execution_authorized"] is False


def test_unanimous_maximum_confidence_cannot_clear_denied_authority():
    envelope = make_envelope()
    envelope["actions"][0]["authority"]["status"] = "DENIED"
    refresh_reviews(envelope)
    for review in envelope["actions"][0]["reviews"]:
        review["confidence"] = 1.0
    result = first_result(envelope)
    assert not result["checks_passed"]
    assert "authority_denied" in result["reasons"]
    assert result["review_confidence_spread"] == 0
    assert result["confidence_calibrated"] is False


def test_two_valid_reviews_do_not_mask_additional_stale_review():
    envelope = make_envelope()
    extra = {**envelope["reviewers"][0], "reviewer_id": "stale-extra"}
    envelope["reviewers"].append(extra)
    refresh_reviews(envelope)
    stale = envelope["actions"][0]["reviews"][-1]
    stale["observed_at"] = (NOW - timedelta(seconds=2)).isoformat()
    stale["expires_at"] = NOW.isoformat()
    result = first_result(envelope)
    assert result["reviewer_diversity_constraint_met"]
    assert "review_stale_or_future" in result["reasons"]
    assert not result["checks_passed"]


def test_stale_human_rejection_cannot_be_replaced_by_low_risk_route():
    envelope = make_envelope()
    action = envelope["actions"][0]
    action["risk"] = dict.fromkeys(action["risk"], 1)
    renew_fixture_scope(action)
    action["human_review"].update(
        status="REJECT",
        observed_at=(NOW - timedelta(seconds=5)).isoformat(),
        expires_at=NOW.isoformat(),
    )
    refresh_reviews(envelope)
    result = first_result(envelope)
    assert result["delegation"] == "BOUNDED_LOCAL_CANDIDATE"
    assert "human_rejected" in result["reasons"]
    assert not result["checks_passed"]


def test_additional_current_conflicting_fact_cannot_be_cherry_picked_away():
    envelope = make_envelope()
    conflicting = copy.deepcopy(envelope["facts"][0])
    conflicting.update(fact_id="counter-evidence", value_sha256=digest("different-value"))
    envelope["facts"].append(conflicting)
    refresh_reviews(envelope)
    result = first_result(envelope)
    assert "canonical_fact_conflict" in result["reasons"]
    assert result["source_grounding_index"] == 0
    assert not result["checks_passed"]


def test_conflicting_fact_for_different_subject_does_not_poison_subject():
    envelope = make_envelope()
    other = copy.deepcopy(envelope["facts"][0])
    other.update(fact_id="other-person", subject="DIFFERENT-SYNTHETIC-PERSON",
                 value_sha256=digest("different-value"))
    envelope["facts"].append(other)
    refresh_reviews(envelope)
    assert first_result(envelope)["checks_passed"]


def test_metadata_checks_do_not_claim_payload_authentication():
    envelope = make_envelope()
    action = envelope["actions"][0]
    # Arbitrary caller-supplied payload digests are allowed by this metadata
    # adapter. Actual bytes, completeness, and signatures are an external duty.
    action["proposal"]["payload_sha256"] = digest("not available as source bytes")
    renew_fixture_scope(action)
    refresh_reviews(envelope)
    report = evaluate(envelope, now=NOW)
    assert report["rows"][0]["checks_passed"]
    assert "payload extraction external" in report["trust_boundary"]
    assert report["execution_authorized"] is False
    assert all(row["execution_authorized"] is False for row in report["rows"])


def test_abstention_does_not_hide_labeled_overconfident_error():
    report = evaluate_predictions([
        {"id": "answered", "probability": 1.0, "outcome": True},
        {"id": "abstained-error", "probability": 1.0, "outcome": False, "abstained": True},
        {"id": "unknown", "probability": 1.0, "outcome": None},
    ])
    assert report["selective_accuracy"] == 1
    assert report["accuracy_all_labeled"] == 0.5
    assert report["brier_loss"] == 0.5
    assert report["counts"]["unknown_outcomes"] == 1
    assert report["coverage"] == pytest.approx(2 / 3)
    assert report["labeled_coverage"] == 0.5


def test_correcting_one_case_does_not_conceal_harm_or_missing_references():
    def challenge(identity, before, after, reference):
        return dict(id=identity, challenged=True, revised=True,
                    before_correct=before, after_correct=after,
                    ground_truth_ref=reference)
    report = evaluate_challenges([
        challenge("corrected", False, True, "fixture:corrected"),
        challenge("harmed", True, False, "fixture:harmed"),
        challenge("unreferenced", False, True, None),
    ])
    assert report["acr"] == 1
    assert report["harmful_revision_rate"] == 1
    assert report["correction_precision"] == 0.5
    assert report["counts"]["excluded_union"] == 1
    assert report["execution_authority"] is False


def test_reviewed_other_role_target_cannot_qualify_current_role():
    snapshot = make_snapshot()
    envelope = make_envelope(snapshot)
    action = envelope["actions"][0]
    action["proposal"]["target"] = "role-2"
    renew_fixture_scope(action)
    refresh_reviews(envelope)
    report = build(snapshot, now=NOW, assurance=envelope)
    result = report["assurance"]["rows"][0]
    assert result["role_id"] == "role-1"
    assert result["authority_observation_matched"]
    assert result["human_review_observation_matched"]
    assert "proposal_target_mismatch" in result["reasons"]
    assert not result["checks_passed"]
    assert report["readiness"]["base_executable_ready"] == 1
    assert report["readiness"]["assurance_qualified_ready"] == 0


def test_crossed_reviewers_cannot_fake_a_fully_diverse_pair():
    envelope = make_envelope()
    envelope["reviewers"] = [
        {"reviewer_id": str(i), "method": method, "family": family,
         "independence_group": group}
        for i, (method, family, group) in enumerate([
            ("M1", "F1", "G1"), ("M1", "F2", "G2"), ("M2", "F1", "G2"),
        ])
    ]
    refresh_reviews(envelope)
    result = first_result(envelope)
    assert "reviewer_diversity_unestablished" in result["reasons"]
    assert not result["reviewer_diversity_constraint_met"]
    assert not result["checks_passed"]


@pytest.mark.parametrize("location", ["authority", "human_review", "verdict"])
@pytest.mark.parametrize("invalid", [[], {}, True, None])
def test_malformed_status_contract_raises_value_error(location, invalid):
    envelope = make_envelope()
    action = envelope["actions"][0]
    if location == "verdict":
        action["reviews"][0]["verdict"] = invalid
    else:
        action[location]["status"] = invalid
    with pytest.raises(ValueError):
        evaluate(envelope, now=NOW)


def claim_budget_envelope(action_count):
    envelope = make_envelope()
    action = envelope["actions"][0]
    action["proposal"]["claims"] = [
        {**copy.deepcopy(action["proposal"]["claims"][0]), "claim_id": str(index)}
        for index in range(256)
    ]
    action["authority"] = action["human_review"] = None
    action["reviews"] = []
    envelope["actions"] = [
        {**copy.deepcopy(action), "role_id": "budget-role-" + str(index),
         "application_id": digest(index)}
        for index in range(action_count)
    ]
    return envelope


def test_total_claim_budget_rejects_before_review_processing(monkeypatch):
    envelope = claim_budget_envelope(17)

    def unexpected_review(*args, **kwargs):
        pytest.fail("Exceeded claim budget reached review processing")

    monkeypatch.setattr(assurance_core, "review_subject", unexpected_review)
    with pytest.raises(ValueError, match="total claim budget exceeded"):
        evaluate(envelope, now=NOW)


def test_total_claim_budget_accepts_boundary_without_authorizing():
    report = evaluate(claim_budget_envelope(16), now=NOW)
    assert sum(row["claim_count"] for row in report["rows"]) == 4096
    assert all(not row["checks_passed"] for row in report["rows"])
    assert not report["execution_authorized"]
