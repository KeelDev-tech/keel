"""Synthetic regressions for numerical and observation integrity, offline only."""
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import random
import sys

import pytest

import packet_dependency_adapter as packet
import seven_source_adapter as seven
from keel_agent.revisions import export_revisions

_spec = importlib.util.spec_from_file_location(
    "keel_math_audit", Path(__file__).resolve().parents[1] / "math" / "keel_math.py")
algorithms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(algorithms)
NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def export():
    return {"observed_at": NOW.isoformat(), "source_revision": "synthetic-v1",
            "leads": [{"role_id": "role-1", "identity": "application-1", "holds": ["human"]}]}


def revisions():
    return {name: name + "-v1" for name in packet.REVISIONS}


def lead():
    return {"role_id": "role-1", **{k + "_revision": v for k, v in revisions().items()}}


def question():
    return {"id": "question-1", "cost": 1, "resolves": ["requirement-1"]}


def candidate():
    return {"id": "packet-1", "value": 2, "needs": ["requirement-1"]}


def test_cvar_does_not_overflow_on_finite_mixed_sign_losses():
    assert algorithms.empirical_cvar([-1e308, 1e308], beta=0) == 0
    maximum = sys.float_info.max
    for beta in (0, .25, .9, math.nextafter(1.0, 0.0)):
        assert algorithms.empirical_cvar([maximum] * 7, beta) == pytest.approx(maximum)
        assert algorithms.empirical_cvar([-maximum] * 7, beta) == pytest.approx(-maximum)


def test_cvar_matches_independent_exact_rational_tail_integral():
    rng = random.Random(20260924)
    for _ in range(100):
        values = [rng.randrange(-1000, 1000) for _ in range(rng.randrange(1, 30))]
        beta = Fraction(rng.randrange(0, 8), 8)
        remaining = Fraction(len(values)) * (1 - beta)
        mass = remaining
        integral = Fraction(0)
        for value in sorted(values, reverse=True):
            portion = min(remaining, 1)
            integral += portion * value
            remaining -= portion
            if remaining == 0:
                break
        assert algorithms.empirical_cvar(values, float(beta)) == pytest.approx(float(integral / mass))


def test_extreme_integer_is_controlled_validation_error():
    with pytest.raises(ValueError):
        algorithms.finite(10 ** 10000)


@pytest.mark.parametrize("replacement", ["requirement-1", {"requirement-1": True}, None])
def test_requirement_collections_are_not_coerced_from_other_types(replacement):
    for field, row, qs, packets in (
        ("resolves", question(), None, [candidate()]),
        ("needs", candidate(), [question()], None),
    ):
        row[field] = replacement
        with pytest.raises(ValueError):
            algorithms.best_question_bundle(qs or [row], packets or [row])


def test_duplicate_packet_cannot_double_count_unlock_value():
    with pytest.raises(ValueError):
        algorithms.best_question_bundle([question()], [candidate(), candidate()])


@pytest.mark.parametrize("target", ["cost", "value"])
def test_negative_cost_or_unlock_value_rejected(target):
    q, p = question(), candidate()
    (q if target == "cost" else p)[target] = -1
    with pytest.raises(ValueError):
        algorithms.best_question_bundle([q], [p])


def test_unrepresentable_aggregate_rejected_before_optimization():
    rows = [{"id": str(i), "value": 1e308, "needs": ["requirement-1"]} for i in range(2)]
    with pytest.raises(ValueError):
        algorithms.best_question_bundle([question()], rows)


@pytest.mark.parametrize("limit", [True, 0, -1, 21, 1e300])
def test_exponential_search_limit_cannot_be_disabled(limit):
    with pytest.raises(ValueError):
        algorithms.best_question_bundle([question()], [candidate()], max_questions=limit)


def test_total_enumeration_work_is_bounded():
    questions = [{**question(), "id": str(i)} for i in range(20)]
    packets = [{**candidate(), "id": str(i)} for i in range(100)]
    with pytest.raises(ValueError):
        algorithms.best_question_bundle(questions, packets, max_questions=20)


@pytest.mark.parametrize("parents", [None, {"fact": True}, "fact"])
def test_missing_or_malformed_graph_is_never_axiomatic_support(parents):
    with pytest.raises(ValueError):
        algorithms.invalidate({"claim": parents, "fact": []}, [])


def test_revocation_identifier_is_not_split_into_characters():
    with pytest.raises(ValueError):
        algorithms.invalidate({"a": [], "b": []}, "ab")


@pytest.mark.parametrize("malformed", [{}, {"leads": None}, {"leads": {}}, {"leads": [lead(), lead()]}])
def test_packet_observation_rejects_missing_array_or_ambiguous_identity(malformed):
    with pytest.raises(ValueError):
        packet.observe_snapshot(malformed)


def test_packet_hash_never_trims_a_revision_identity():
    original = lead()
    original["policy_revision"] += " "
    result = packet.derive_lead(original)
    assert result["packet_dependency_hash"] is None
    assert result["execution_authorized"] is False


def test_packet_direct_hash_api_enforces_all_seven_revision_types():
    supplied = revisions()
    supplied["approval"] = True
    with pytest.raises(ValueError):
        packet.derive_packet_dependency_hash(supplied)


@pytest.mark.parametrize("raw", [b'{"leads": [], "leads": []}', b'{"leads": [], "bad": NaN}'])
def test_snapshot_file_rejects_ambiguous_or_nonfinite_json(tmp_path, raw):
    path = tmp_path / "snapshot.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        packet.read_snapshot(path)


def test_snapshot_digest_binds_exact_interpreted_bytes(tmp_path):
    raw = json.dumps(export(), indent=3).encode()
    path = tmp_path / "snapshot.json"
    path.write_bytes(raw)
    observed, actual = packet.read_snapshot(path)
    assert observed == export()
    assert actual == hashlib.sha256(raw).hexdigest()
    assert path.read_bytes() == raw


def test_snapshot_symlink_refused(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps(export()))
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        packet.read_snapshot(alias)


def test_missing_snapshot_revision_is_not_fabricated():
    source = export()
    del source["source_revision"]
    with pytest.raises(ValueError):
        seven.build_normalized_snapshot(source, now=NOW)


@pytest.mark.parametrize("duration", [True, 0, -1, math.nan, math.inf, 86401])
def test_snapshot_validity_is_explicit_bounded_integer(duration):
    with pytest.raises(ValueError):
        seven.build_normalized_snapshot(export(), now=NOW, snapshot_validity_s=duration)


def test_summary_preserves_human_ownership_and_does_not_claim_live_absence(tmp_path):
    source = export()
    decision = {"source_ref": "synthetic:decision-request", "source_version": "v1",
                "observed_at": NOW.isoformat(), "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
                "absence": {"kind": "HUMAN_DECISION_REQUIRED", "decision_request_ref": "synthetic:request"}}
    normalized = seven.build_normalized_snapshot(
        source, now=NOW, approval_store={("role-1", "application-1", "submit"): decision})
    report = export_revisions(normalized, attachment_root=tmp_path, now=NOW)
    summary = seven.summarize(report, export_path="synthetic.json", export_sha256="a" * 64,
                              export_observed_at=source["observed_at"], snapshot=source, shared_reads={})
    blocker = next(row for row in summary["named_blockers_per_category"] if row["component"] == "approval")
    assert blocker["responsibility"] == "human"
    assert blocker["owner"] == "human_reviewer"
    assert summary["genuinely_absent_verified"] == []
    assert summary["source_authenticity_verified"] is False


def test_snapshot_freshness_failure_remains_visible_in_summary(tmp_path):
    source = export()
    source["observed_at"] = (NOW - timedelta(hours=1)).isoformat()
    normalized = seven.build_normalized_snapshot(source, now=NOW)
    report = export_revisions(normalized, attachment_root=tmp_path, now=NOW)
    summary = seven.summarize(report, export_path="synthetic.json", export_sha256="a" * 64,
                              export_observed_at=source["observed_at"], snapshot=source, shared_reads={})
    assert any(row["component"] == "snapshot" and row["reason"] == "source_expired"
               for row in summary["named_blockers_per_category"])


@pytest.mark.parametrize("broken", ["hash", "selector"])
def test_failed_sources_still_consume_cumulative_grounding_budget(tmp_path, monkeypatch, broken):
    from copy import deepcopy
    from keel_grounding import claims
    from keel_grounding.demo import make_fixture
    from keel_trust.common import digest
    import keel_grounding.evidence as evidence

    fixture = make_fixture(tmp_path)
    document, bindings = fixture["document"], fixture["bindings"]
    raw = (tmp_path / "evidence" / "profile.json").read_bytes()
    original_source = document["sources"][0]
    original_binding = bindings["sources"][0]
    duplicate_source = {**deepcopy(original_source), "source_id": "extra-source"}
    duplicate_binding = {**deepcopy(original_binding), "source_id": "extra-source"}
    document["sources"].append(duplicate_source)
    bindings["sources"].append(duplicate_binding)
    if broken == "hash":
        original_source["content_hash"] = "0" * 64
    else:
        original_binding["selectors"][0]["pointer"] = "/nonexistent"
    bindings["trust_snapshot_sha256"] = digest(document)
    monkeypatch.setattr(claims, "MAX_TOTAL_EVIDENCE_BYTES", len(raw))
    read_bytes = 0
    actual_read = evidence.os.read

    def counted(fd, size):
        nonlocal read_bytes
        result = actual_read(fd, size)
        read_bytes += len(result)
        return result

    monkeypatch.setattr(evidence.os, "read", counted)
    report = claims.verify_grounding(document, bindings, root=tmp_path / "evidence", now=fixture["now"])
    assert report["status"] == "BLOCKED"
    assert read_bytes == len(raw)
    extra = next(row for row in report["sources"] if row["source_id"] == "extra-source")
    assert "total_evidence_byte_limit" in extra["reasons"]
