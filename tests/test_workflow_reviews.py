"""Synthetic local review lifecycle tests; not evidence of model independence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3

import pytest

from keel_workflow.reviews import ReviewStore, subject_digest


NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
SUBJECT = {"task": "Review the exact application packet", "revision": "r1",
           "required_claim_ids": ["experience", "education"],
           "proposal": {"destination": "synthetic.example", "payload_sha256": "a" * 64}}
REVIEWERS = ["reviewer_a", "reviewer_b"]


@pytest.fixture
def store(tmp_path):
    value = ReviewStore(tmp_path / "reviews.sqlite")
    value.create_round("round-1", SUBJECT, REVIEWERS, NOW + timedelta(minutes=10), now=NOW)
    return value


def commit(store, reviewer="reviewer_a", verdict="PASS", claims=None, findings=None, now=NOW):
    return store.commit("round-1", reviewer, subject_digest(SUBJECT), verdict=verdict,
                        covered_claim_ids=SUBJECT["required_claim_ids"] if claims is None else claims,
                        findings=findings, now=now)


def seal(store, second_verdict="PASS", first_claims=None, first_findings=None):
    commit(store, claims=first_claims, findings=first_findings)
    commit(store, "reviewer_b", second_verdict)
    return store.seal("round-1", subject_digest(SUBJECT), now=NOW)


def complete_audits(store, verdict="PASS", findings=None):
    for reviewer in REVIEWERS:
        store.phase_b("round-1", reviewer, subject_digest(SUBJECT), now=NOW)
        store.audit("round-1", reviewer, subject_digest(SUBJECT), verdict=verdict,
                    findings=findings, now=NOW)


def evaluate(store, now=NOW):
    return store.evaluate("round-1", subject_digest(SUBJECT), now=now)


def test_phase_a_returns_only_subject_and_own_status_without_other_opinions(store):
    commit(store, verdict="FAIL", findings=["private phase A finding"])
    result = store.phase_a("round-1", "reviewer_b", subject_digest(SUBJECT), now=NOW)
    assert result["subject"] == SUBJECT and result["submitted"] is False
    assert set(result) == {"round_id", "reviewer_id", "subject_sha256", "subject", "submitted",
                           "own_commitment_sha256", "execution_authorized"}
    assert "private phase A finding" not in json.dumps(result)
    assert evaluate(store)["state"] == "WAITING_FOR_PHASE_A"
    assert "fail" not in json.dumps(evaluate(store)).lower()


def test_partial_commitment_cannot_seal_reveal_or_audit(store):
    commit(store)
    with pytest.raises(ValueError, match="all assigned"):
        store.seal("round-1", subject_digest(SUBJECT), now=NOW)
    with pytest.raises(ValueError, match="unavailable"):
        store.phase_b("round-1", "reviewer_a", subject_digest(SUBJECT), now=NOW)
    with pytest.raises(ValueError, match="retrieve"):
        store.audit("round-1", "reviewer_a", subject_digest(SUBJECT), verdict="PASS", now=NOW)


def test_full_phase_a_still_requires_explicit_seal(store):
    commit(store)
    commit(store, "reviewer_b")
    assert evaluate(store)["state"] == "WAITING_FOR_SEAL"
    with pytest.raises(ValueError, match="unavailable"):
        store.phase_b("round-1", "reviewer_a", subject_digest(SUBJECT), now=NOW)


def test_phase_b_each_reviewer_must_retrieve_commitments(store):
    seal(store)
    store.phase_b("round-1", "reviewer_a", subject_digest(SUBJECT), now=NOW)
    with pytest.raises(ValueError, match="retrieve"):
        store.audit("round-1", "reviewer_b", subject_digest(SUBJECT), verdict="PASS", now=NOW)
    assert evaluate(store)["state"] == "HOLD"
    assert "phase_b_incomplete" in evaluate(store)["reasons"]


def test_reopen_preserves_commitments_and_only_complete_clean_round_is_human_ready(store):
    first = commit(store)
    reopened = ReviewStore(store.path)
    own = reopened.phase_a("round-1", "reviewer_a", subject_digest(SUBJECT), now=NOW)
    assert own["submitted"] and own["own_commitment_sha256"] == first["commitment_sha256"]
    commit(reopened, "reviewer_b")
    reopened.seal("round-1", subject_digest(SUBJECT), now=NOW)
    complete_audits(reopened)
    result = evaluate(ReviewStore(store.path))
    assert result["state"] == "READY_FOR_HUMAN_REVIEW"
    assert result["phase_b_complete"] and result["reviewer_ids"] == REVIEWERS
    assert result["subject_sha256"] == subject_digest(SUBJECT)
    assert not result["execution_authorized"]
    assert not result["reviewer_identity_authenticated"] and not result["external_blindness_proven"]


@pytest.mark.parametrize("second,reason", [("FAIL", "phase_a_fail"), ("ABSTAIN", "phase_a_abstain")])
def test_phase_b_pass_cannot_erase_phase_a_disagreement_or_failure(store, second, reason):
    seal(store, second)
    complete_audits(store)
    result = evaluate(store)
    assert result["state"] == "HOLD"
    assert {reason, "phase_a_disagreement"} <= set(result["reasons"])
    assert result["phase_b_complete"] and not result["execution_authorized"]


@pytest.mark.parametrize("claims,findings,reason", [
    (["experience"], None, "phase_a_claim_coverage_incomplete"),
    ([], None, "phase_a_claim_coverage_incomplete"),
    (None, ["unverified diploma"], "phase_a_unresolved_findings"),
])
def test_phase_a_coverage_and_findings_survive_unanimous_phase_b(store, claims, findings, reason):
    seal(store, first_claims=claims, first_findings=findings)
    complete_audits(store)
    assert evaluate(store)["state"] == "HOLD"
    assert reason in evaluate(store)["reasons"]


@pytest.mark.parametrize("verdict,findings,reason", [
    ("FAIL", None, "phase_b_fail"), ("ABSTAIN", None, "phase_b_abstain"),
    ("PASS", ["primary review missed a contradiction"], "phase_b_unresolved_findings"),
])
def test_phase_b_has_its_own_hard_holds(store, verdict, findings, reason):
    seal(store)
    complete_audits(store, verdict, findings)
    assert evaluate(store)["state"] == "HOLD"
    assert reason in evaluate(store)["reasons"]


def test_commitment_replay_and_phase_b_replacement_are_rejected(store):
    commit(store, verdict="FAIL")
    with pytest.raises(ValueError, match="already committed"):
        commit(store, verdict="PASS")
    commit(store, "reviewer_b")
    store.seal("round-1", subject_digest(SUBJECT), now=NOW)
    with pytest.raises(ValueError, match="sealed"):
        commit(store)
    complete_audits(store)
    with pytest.raises(ValueError, match="already audited"):
        store.audit("round-1", "reviewer_a", subject_digest(SUBJECT), verdict="PASS", now=NOW)


def test_concurrent_same_reviewer_has_exactly_one_winning_commitment(store):
    def submit(verdict):
        try:
            return commit(ReviewStore(store.path), verdict=verdict)
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(submit, ["PASS", "FAIL"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum("already committed" in result for result in results if isinstance(result, str)) == 1
    commit(store, "reviewer_b")
    store.seal("round-1", subject_digest(SUBJECT), now=NOW)
    revealed = store.phase_b("round-1", "reviewer_b", subject_digest(SUBJECT), now=NOW)
    assert len(revealed["commitments"]) == 2


def test_concurrent_distinct_reviewers_are_both_durable(store):
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda who: commit(ReviewStore(store.path), who), REVIEWERS))
    assert len({result["commitment_sha256"] for result in results}) == 2
    reopened = ReviewStore(store.path)
    assert evaluate(reopened)["state"] == "WAITING_FOR_SEAL"
    reopened.seal("round-1", subject_digest(SUBJECT), now=NOW)


@pytest.mark.parametrize("field,value", [("revision", "r2"), ("required_claim_ids", ["education"]),
                                        ("proposal", {"destination": "changed.example"})])
def test_subject_revision_and_claim_inventory_edits_cannot_reuse_reviews(store, field, value):
    seal(store)
    complete_audits(store)
    changed = dict(SUBJECT, **{field: value})
    result = store.evaluate("round-1", subject_digest(changed), now=NOW)
    assert result["state"] == "HOLD" and "subject_changed" in result["reasons"][0]
    with pytest.raises(ValueError, match="subject_changed"):
        store.phase_b("round-1", "reviewer_a", subject_digest(changed), now=NOW)


def test_invalidated_round_cannot_be_resurrected_or_recreated(store):
    seal(store)
    complete_audits(store)
    store.invalidate("round-1", "upstream facts revised", now=NOW)
    assert "invalidated" in evaluate(store)["reasons"][0]
    with pytest.raises(ValueError, match="invalidated"):
        store.phase_a("round-1", "reviewer_a", subject_digest(SUBJECT), now=NOW)
    with pytest.raises(ValueError, match="already exists"):
        store.create_round("round-1", SUBJECT, REVIEWERS, NOW + timedelta(minutes=10), now=NOW)


def test_exact_expiry_and_clock_rollback_hold_completed_round(store):
    seal(store)
    complete_audits(store)
    result = evaluate(store, NOW + timedelta(minutes=10))
    assert result["state"] == "HOLD" and "expired" in result["reasons"][0]
    result = evaluate(store, NOW - timedelta(seconds=1))
    assert result["state"] == "HOLD" and "clock_before_latest_event" in result["reasons"]


def test_clock_cannot_move_before_a_later_commit(store):
    commit(store, now=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="clock_before_latest_event"):
        commit(store, "reviewer_b", now=NOW)


@pytest.mark.parametrize("reviewers", [[], ["same"], ["same", "same"], ["same", "Same"],
                                     ["same", "same "], ["same", " SAME"]])
def test_aliases_and_single_reviewer_are_not_independence(tmp_path, reviewers):
    with pytest.raises(ValueError):
        ReviewStore(tmp_path / "bad.sqlite").create_round(
            "round", SUBJECT, reviewers, NOW + timedelta(minutes=1), now=NOW)


def test_unassigned_reviewer_cannot_read_or_commit(store):
    with pytest.raises(ValueError, match="not assigned"):
        store.phase_a("round-1", "stranger", subject_digest(SUBJECT), now=NOW)
    with pytest.raises(ValueError, match="not assigned"):
        commit(store, "stranger")


@pytest.mark.parametrize("subject", [
    {}, {"required_claim_ids": []}, {"required_claim_ids": ["same", "same"]},
    dict(SUBJECT, confidence=float("nan")), dict(SUBJECT, payload="x" * 262145),
    dict(SUBJECT, unsupported={1, 2}), dict(SUBJECT, integer=1 << 300),
])
def test_malformed_or_unbounded_subject_fails_before_persistence(tmp_path, subject):
    with pytest.raises(ValueError):
        ReviewStore(tmp_path / "bad.sqlite").create_round(
            "round", subject, REVIEWERS, NOW + timedelta(minutes=1), now=NOW)


@pytest.mark.parametrize("expiry", [NOW, NOW - timedelta(seconds=1), NOW + timedelta(seconds=901), None])
def test_lifetime_is_explicit_positive_and_bounded(tmp_path, expiry):
    with pytest.raises(ValueError):
        ReviewStore(tmp_path / "bad.sqlite").create_round("round", SUBJECT, REVIEWERS, expiry, now=NOW)


@pytest.mark.parametrize("kwargs", [
    {"verdict": []}, {"covered_claim_ids": ["unbound"]},
    {"covered_claim_ids": ["experience", "experience"]}, {"findings": [""]},
    {"findings": ["x" * 2049]}, {"now": datetime(2026, 9, 18)},
])
def test_malformed_reviews_are_rejected(store, kwargs):
    with pytest.raises(ValueError):
        args = dict(verdict="PASS", covered_claim_ids=SUBJECT["required_claim_ids"], now=NOW)
        args.update(kwargs)
        store.commit("round-1", "reviewer_a", subject_digest(SUBJECT), **args)


def test_commitment_has_exact_round_subject_verdict_claim_and_time_binding(store):
    recorded = commit(store)
    commit(store, "reviewer_b")
    store.seal("round-1", subject_digest(SUBJECT), now=NOW)
    revealed = store.phase_b("round-1", "reviewer_b", subject_digest(SUBJECT), now=NOW)
    document = next(item for item in revealed["commitments"] if item["reviewer_id"] == "reviewer_a")
    digest = document.pop("commitment_sha256")
    assert digest == recorded["commitment_sha256"] == subject_digest(document)
    assert document["round_id"] == "round-1" and document["committed_at"] == NOW.isoformat()
    assert document["subject_sha256"] == subject_digest(SUBJECT)


def test_sql_ordinary_mutation_cannot_replace_or_delete_commitments(store):
    commit(store, verdict="FAIL")
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE review_commitments SET document='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM review_commitments")
    assert os.stat(store.path).st_mode & 0o777 == 0o600
