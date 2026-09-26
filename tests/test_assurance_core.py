"""Synthetic ECV boundary tests; do not validate scientific efficacy."""
import copy
from datetime import timedelta
import pytest
from tools.make_assurance_demo import NOW, make_envelope, refresh_reviews
from keel_assurance.core import evaluate, proposal_scope, review_subject, delegation
from keel_flow.common import digest


def first(envelope): return envelope["actions"][0]
def reasons(envelope): return evaluate(envelope, now=NOW)["rows"][0]["reasons"]


def test_valid_fixture_is_read_only_and_never_grants_execution():
    value = make_envelope(); old = copy.deepcopy(value)
    result = evaluate(value, now=NOW)
    assert result["state"] == "CHECKS_PASSED" and value == old
    assert result["effects"] == {"network_requests": 0, "canonical_writes": 0, "model_calls": 0}
    assert not result["execution_authorized"]
    assert all(r["checks_passed"] and not r["execution_authorized"] and not r["confidence_calibrated"] for r in result["rows"])


@pytest.mark.parametrize("change,reason", [
    ("value", "identity_claim_drift"), ("subject", "identity_claim_drift"),
    ("purpose", "fact_scope_mismatch"), ("target", "fact_scope_mismatch"),
    ("missing_fact", "fact_missing"), ("evidence", "evidence_dependency_invalid"),
    ("revoked", "evidence_dependency_invalid"), ("revision", "evidence_dependency_invalid"),
    ("no_authority", "authority_missing"), ("unknown_authority", "authority_unknown"),
    ("denied", "authority_denied"), ("scope", "authority_scope_mismatch"),
    ("expired_authority", "authority_stale_or_future"),
    ("no_human", "human_review_missing"), ("human_reject", "human_rejected"),
    ("human_scope", "human_review_scope_mismatch"),
    ("iterations", "review_iteration_budget_exhausted"),
])
def test_hard_gates_survive_unanimous_fresh_reviews(change, reason):
    e = make_envelope(); a = first(e)
    if change == "value": a["proposal"]["claims"][0]["value_sha256"] = digest("invented")
    if change == "subject": a["proposal"]["subject"] = "other-person"
    if change == "purpose": a["proposal"]["purpose"] = "other-purpose"
    if change == "target": a["proposal"]["target"] = "other-target"
    if change == "missing_fact": a["proposal"]["claims"][0]["fact_id"] = "missing"
    if change == "evidence": e["evidence"][0]["expires_at"] = NOW.isoformat(); e["evidence"][0]["observed_at"] = (NOW-timedelta(minutes=2)).isoformat()
    if change == "revoked": e["evidence"][0]["status"] = "REVOKED"
    if change == "revision": e["evidence"][0]["revision"] = "2"
    if change == "no_authority": a["authority"] = None
    if change == "unknown_authority": a["authority"]["status"] = "UNKNOWN"
    if change == "denied": a["authority"]["status"] = "DENIED"
    if change == "scope": a["authority"]["scope_sha256"] = "f"*64
    if change == "expired_authority":
        a["authority"]["observed_at"] = (NOW-timedelta(minutes=2)).isoformat()
        a["authority"]["expires_at"] = NOW.isoformat()
    if change == "no_human": a["human_review"] = None
    if change == "human_reject": a["human_review"]["status"] = "REJECT"
    if change == "human_scope": a["human_review"]["scope_sha256"] = "f"*64
    if change == "iterations": a["iteration"] = 3
    refresh_reviews(e)
    assert reason in reasons(e)


@pytest.mark.parametrize("change", ["payload", "evidence", "fact", "risk", "registry", "authority", "iteration"])
def test_any_reviewed_subject_change_requires_new_review(change):
    e = make_envelope(); a = first(e)
    if change == "payload": a["proposal"]["payload_sha256"] = digest("new")
    if change == "evidence": e["evidence"][0]["revision"] = "2"
    if change == "fact": e["facts"][0]["value_sha256"] = digest("new")
    if change == "risk": a["risk"]["sensitivity"] = 3
    if change == "registry": e["reviewers"][0]["family"] = "new"
    if change == "authority": a["authority"]["revision"] = "2"
    if change == "iteration": a["iteration"] = 1
    assert "review_subject_changed" in reasons(e)


@pytest.mark.parametrize("change,reason", [
    ("empty", "reviewer_diversity_unestablished"), ("same_group", "reviewer_diversity_unestablished"),
    ("same_family", "reviewer_diversity_unestablished"), ("same_method", "reviewer_diversity_unestablished"),
    ("abstain", "review_abstain"), ("fail", "reviewer_disagreement"),
    ("coverage", "review_coverage_incomplete"), ("roots", "review_coverage_incomplete"),
    ("findings", "unresolved_review_findings"), ("future", "review_stale_or_future"),
    ("expired", "review_stale_or_future"), ("confidence_only", "review_abstain"),
])
def test_review_failure_modes(change, reason):
    e = make_envelope(); a = first(e)
    for key in ("group", "family", "method"):
        if change == "same_" + key:
            name = "independence_group" if key == "group" else key
            e["reviewers"][1][name] = e["reviewers"][0][name]
    refresh_reviews(e)
    if change == "empty": a["reviews"] = []
    else:
        r = a["reviews"][0]
        if change in ("abstain", "confidence_only"): r["verdict"] = "ABSTAIN"; r["confidence"] = 1.0
        if change == "fail": r["verdict"] = "FAIL"
        if change == "coverage": r["covered_claim_ids"] = []
        if change == "roots": r["evidence_roots"] = []
        if change == "findings": r["findings"] = ["unresolved-risk"]
        if change == "future": r["observed_at"] = (NOW+timedelta(seconds=1)).isoformat()
        if change == "expired": r["observed_at"] = (NOW-timedelta(seconds=1)).isoformat(); r["expires_at"] = NOW.isoformat()
    assert reason in reasons(e)


@pytest.mark.parametrize("seconds", [-1, 91])
def test_freshness_never_renews_itself(seconds):
    e = make_envelope(); e["observed_at"] = (NOW-timedelta(seconds=seconds)).isoformat()
    refresh_reviews(e)
    assert evaluate(e, now=NOW)["state"] == "UNVERIFIED"


def test_partial_envelope_cannot_pass():
    e = make_envelope(); e["complete"] = False; refresh_reviews(e)
    assert all(not r["checks_passed"] for r in evaluate(e, now=NOW)["rows"])


@pytest.mark.parametrize("change", ["bool_risk", "nan_confidence", "duplicate_review", "duplicate_fact", "extra_field", "empty_claims", "duplicate_identity", "missing_root", "wildcard_scope"])
def test_strict_contracts(change):
    e = make_envelope(); a = first(e)
    if change == "bool_risk": a["risk"]["sensitivity"] = True
    if change == "nan_confidence": a["reviews"][0]["confidence"] = float("nan")
    if change == "duplicate_review": a["reviews"].append(copy.deepcopy(a["reviews"][0]))
    if change == "duplicate_fact": e["facts"].append(copy.deepcopy(e["facts"][0]))
    if change == "extra_field": a["execute"] = True
    if change == "empty_claims": a["proposal"]["claims"] = []
    if change == "duplicate_identity": e["actions"][1]["application_id"] = a["application_id"]
    if change == "missing_root": a["proposal"]["claims"][0]["evidence_roots"] = ["absent"]
    if change == "wildcard_scope": e["facts"][0]["targets"] = "*"
    with pytest.raises(ValueError): evaluate(e, now=NOW)


def test_low_risk_still_respects_rejection_and_authority():
    e = make_envelope(); a = first(e); a["risk"] = {key: 1 for key in a["risk"]}
    for field in ("authority", "human_review"): a[field]["scope_sha256"] = proposal_scope(a)
    a["human_review"]["status"] = "REJECT"; refresh_reviews(e)
    assert "human_rejected" in reasons(e)


def test_conflicting_values_in_same_proposal_block_even_when_facts_match():
    e = make_envelope(); a = first(e)
    fact = copy.deepcopy(e["facts"][0]); fact.update(fact_id="other", value_sha256=digest("contradiction"))
    e["facts"].append(fact)
    claim = copy.deepcopy(a["proposal"]["claims"][0]); claim.update(claim_id="other", fact_id="other", value_sha256=fact["value_sha256"])
    a["proposal"]["claims"].append(claim)
    refresh_reviews(e)
    assert "conflicting_claim_values" in reasons(e)


def test_source_roots_cannot_be_replaced_by_unrelated_valid_evidence():
    e = make_envelope(); a = first(e)
    other = copy.deepcopy(e["evidence"][0]); other["id"] = "unrelated"
    e["evidence"].append(other); a["proposal"]["claims"][0]["evidence_roots"] = ["unrelated"]
    refresh_reviews(e)
    assert "fact_evidence_mismatch" in reasons(e)


def test_delegation_maximum_dimension_cannot_be_averaged_away():
    base = {key: 1 for key in first(make_envelope())["risk"]}
    assert delegation(base) == "BOUNDED_LOCAL_CANDIDATE"
    assert delegation({**base, "external_impact": 2}) == "HUMAN_REVIEW_REQUIRED"
    assert delegation({**base, "sensitivity": 5}) == "HUMAN_LED"


def test_cosmetic_registry_labels_cannot_establish_diversity():
    e = make_envelope()
    e["reviewers"][0]["family"] = "Same Family"
    e["reviewers"][1]["family"] = "same   family"
    refresh_reviews(e)
    assert "reviewer_diversity_unestablished" in reasons(e)
