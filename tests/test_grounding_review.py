"""Independent review regressions for question-to-claim identity.

These use synthetic evidence and prove relation binding, never factual truth.
"""
import pytest

from keel_grounding.answers import resolve_grounded_answer
from keel_grounding.claims import verify_grounding
from keel_grounding.demo import make_fixture
from keel_trust.common import digest


def _answer(fixture, root):
    return resolve_grounded_answer(
        fixture["records"], fixture["context"], fixture["answer_bindings"],
        fixture["document"], fixture["bindings"], root=root / "evidence",
        now=fixture["now"],
    )


def test_same_value_for_unrelated_predicate_cannot_ground_an_answer(tmp_path):
    fixture = make_fixture(tmp_path)
    fixture["document"]["claims"][0]["predicate"] = "different-question-with-the-same-answer"
    fixture["bindings"]["trust_snapshot_sha256"] = digest(fixture["document"])
    # The source and approved graph still match their exact bytes and values.
    # The trusted form context names a different relation, so answer reuse fails.
    evidence = verify_grounding(fixture["document"], fixture["bindings"],
                                root=tmp_path / "evidence", now=fixture["now"])
    assert evidence["status"] == "VERIFIED"
    result = _answer(fixture, tmp_path)
    assert result["status"] == "NEEDS_USER"
    assert result["reason"] == "answer_claim_binding_mismatch"
    assert result["execution_authorized"] is False


@pytest.mark.parametrize("predicate", [None, "", "different-predicate", [], {}])
def test_missing_or_malformed_trusted_predicate_cannot_be_inferred(tmp_path, predicate):
    fixture = make_fixture(tmp_path)
    if predicate is None:
        fixture["context"].pop("claim_predicate")
    else:
        fixture["context"]["claim_predicate"] = predicate
    result = _answer(fixture, tmp_path)
    assert result["status"] == "NEEDS_USER"
    assert result["execution_authorized"] is False


def test_matching_relation_remains_traceability_not_truth_or_authorization(tmp_path):
    fixture = make_fixture(tmp_path)
    result = _answer(fixture, tmp_path)
    assert result["status"] == "RESOLVED"
    assert result["record_content_verified"] is True
    assert result["truth_independently_verified"] is False
    assert result["reviewer_authentication_verified"] is False
    assert result["execution_authorized"] is False


@pytest.mark.parametrize("field,value", [("context", []), ("context", None),
                                        ("records", {}), ("records", [None])])
def test_malformed_answer_containers_return_structured_failure(tmp_path, field, value):
    fixture = make_fixture(tmp_path)
    fixture[field] = value
    assert _answer(fixture, tmp_path) == {
        "status": "NEEDS_USER", "reason": "invalid_answer_grounding",
        "execution_authorized": False,
    }
