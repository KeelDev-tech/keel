"""Finite-family risk selection, isolation and source/config pin regressions."""
from copy import deepcopy
import math

import pytest

from keel_loki import calibration as c


SOURCE, CONFIG = "a" * 64, {"model": "fixture", "score_function": "frozen-v1"}


def data(split="calibration", count=40):
    return {"schema": "keel.loki.calibration-data.v1", "dataset_id": split, "split": split,
        "synthetic": True, "label_source": "synthetic_fixture", "config_sha256": c.digest(CONFIG),
        "source_sha256": SOURCE, "rows": [{"case_id": f"{split}-{i}", "group_id": f"{split}-{i}",
            "subject": {"required_claim_ids": ["claim"], "claims": [{"claim_id": "claim", "text": f"{split} {i}"}],
                "evidence": [{"evidence_id": "record", "text": f"record {split} {i}"}]},
            "ranking_score": .9, "observed_verdict": "PASS", "expected_verdict": "PASS",
            "observation_sha256": c.digest([split, i]), "label_revision_sha256": c.digest(["label", split, i]),
            "reviewer_id": "fixture-reviewer"} for i in range(count)]}


def plan(cal=None, test=None, **kwargs):
    return c.make_plan(CONFIG, source_sha256=SOURCE, thresholds=kwargs.pop("thresholds", [.5, .8]),
        risk_limit=kwargs.pop("risk_limit", .2), delta=kwargs.pop("delta", .05), calibration_id="experiment",
        calibration_sha256=c.digest(cal or data()), test_sha256=c.digest(test or data("test")), **kwargs)


def fit(cal=None, test=None, frozen=None):
    cal, test = cal or data(), test or data("test")
    frozen = frozen or plan(cal, test)
    return c.fit(cal, test, frozen, expected_plan_sha256=c.digest(frozen), config=CONFIG, source_sha256=SOURCE)


def test_binomial_zero_error_matches_closed_form():
    assert c.binomial_upper(0, 30, .025) == pytest.approx(1 - .025 ** (1 / 30))
    assert c.binomial_upper(0, 0, .05) == 1
    assert c.binomial_upper(5, 5, .05) == 1


@pytest.mark.parametrize("n,k,delta", [(10, 1, .05), (20, 5, .025), (100, 60, .001), (500, 1, .05)])
def test_binomial_bound_solves_tail(n, k, delta):
    upper = c.binomial_upper(k, n, delta)
    tail = math.fsum(math.comb(n, i) * upper ** i * (1 - upper) ** (n - i) for i in range(k + 1))
    assert tail == pytest.approx(delta, rel=1e-8, abs=1e-12)


@pytest.mark.parametrize("counts", [(True, 2, .05), (1, 0, .05), (0, 2001, .05), (0, 2, 0), (0, 2, float("nan"))])
def test_invalid_binomial_counts(counts):
    with pytest.raises(c.CalibrationError):
        c.binomial_upper(*counts)


def test_fixed_family_is_bonferroni_adjusted():
    report = fit()
    assert report["per_threshold_delta"] == .025
    assert report["selected_threshold"] == .5
    assert report["table"][0]["binomial_upper"] == pytest.approx(c.binomial_upper(0, 40, .025))
    assert not report["ranking_scores_are_probabilities"]
    assert not report["execution_authorized"]


def test_small_samples_do_not_manufacture_certainty():
    report = fit(data(count=1))
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert report["selected_threshold"] is None


def test_can_select_lower_risk_high_threshold():
    cal = data(count=100)
    for row in cal["rows"][:30]:
        row.update(ranking_score=.6, expected_verdict="FAIL")
    report = fit(cal)
    assert report["selected_threshold"] == .8
    assert report["table"][0]["unsupported_pass"] == 30
    assert report["table"][1]["selected"] == 70


def test_errors_and_abstentions_stay_in_coverage_denominator():
    cal = data(count=100)
    for row in cal["rows"][:50]:
        row["observed_verdict"] = "ERROR"
    report = fit(cal)
    assert report["table"][0]["coverage"] == .5
    assert report["table"][0]["total"] == 100


def test_no_selected_cases_has_no_empirical_risk_claim():
    cal = data()
    for row in cal["rows"]:
        row["observed_verdict"] = "ABSTAIN"
    report = fit(cal)
    assert report["table"][0]["empirical_risk_among_selected"] is None
    assert report["table"][0]["binomial_upper"] == 1


@pytest.mark.parametrize("variant", ["case", "group", "content", "renamed-content", "observation"])
def test_duplicate_rows_cannot_inflate_sample_size(variant):
    cal = data()
    if variant == "case":
        cal["rows"][1]["case_id"] = cal["rows"][0]["case_id"]
    elif variant == "group":
        cal["rows"][1]["group_id"] = cal["rows"][0]["group_id"]
    elif variant == "observation":
        cal["rows"][1]["observation_sha256"] = cal["rows"][0]["observation_sha256"]
    else:
        cal["rows"][1]["subject"] = deepcopy(cal["rows"][0]["subject"])
        if variant == "renamed-content":
            s = cal["rows"][1]["subject"]
            s["required_claim_ids"] = ["other"]
            s["claims"][0]["claim_id"] = "other"
            s["evidence"][0]["evidence_id"] = "renamed"
    with pytest.raises(c.CalibrationError):
        fit(cal)


@pytest.mark.parametrize("key", ["case_id", "group_id", "subject", "observation_sha256"])
def test_cross_partition_overlap_rejected(key):
    cal, test = data(), data("test")
    test["rows"][0][key] = deepcopy(cal["rows"][0][key])
    with pytest.raises(c.CalibrationError, match="overlap"):
        fit(cal, test)


def test_test_labels_do_not_choose_threshold():
    cal, test = data(), data("test")
    first = fit(cal, test)
    for row in test["rows"]:
        row["expected_verdict"] = "FAIL"
    second = fit(cal, test)
    assert first["selected_threshold"] == second["selected_threshold"]
    assert first["table"] == second["table"]


def test_evaluation_exposes_bad_generalization_not_quality_pass():
    cal, test = data(), data("test")
    for row in test["rows"]:
        row["expected_verdict"] = "FAIL"
    frozen = plan(cal, test)
    fitted = fit(cal, test, frozen)
    result = c.evaluate(cal, test, frozen, fitted, expected_plan_sha256=c.digest(frozen),
        expected_fit_sha256=c.digest(fitted), config=CONFIG, source_sha256=SOURCE)
    assert result["risk_coverage"]["empirical_risk_among_selected"] == 1
    assert not result["deployment_validated"]


def test_forged_fit_rejected_even_with_recomputed_digest():
    cal, test, frozen = data(), data("test"), plan()
    fitted = fit()
    fitted["selected_threshold"] = .99
    with pytest.raises(c.CalibrationError, match="fit_report_changed"):
        c.evaluate(cal, test, frozen, fitted, expected_plan_sha256=c.digest(frozen),
            expected_fit_sha256=c.digest(fitted), config=CONFIG, source_sha256=SOURCE)


@pytest.mark.parametrize("variant", ["source", "config", "confidence", "label-source", "synthetic", "nan"])
def test_untrusted_or_changed_inputs_rejected(variant):
    cal = data()
    if variant == "source":
        cal["source_sha256"] = "c" * 64
    elif variant == "config":
        cal["config_sha256"] = "c" * 64
    elif variant == "confidence":
        cal["rows"][0]["confidence"] = .99
    elif variant == "label-source":
        cal["label_source"] = "model-self-judge"
    elif variant == "synthetic":
        cal.update(synthetic=False, label_source="human_supplied")
    else:
        cal["rows"][0]["ranking_score"] = float("nan")
    with pytest.raises(c.CalibrationError):
        fit(cal)


def test_configuration_and_source_invalidate():
    fitted = fit()
    assert c.invalidate(fitted, config=CONFIG, source_sha256=SOURCE)["status"] == "PINS_MATCH"
    changed = c.invalidate(fitted, config={"model": "other"}, source_sha256="c" * 64)
    assert changed["status"] == "INVALIDATED"
    assert changed["reasons"] == ["configuration_changed", "source_changed"]


def test_demo_is_explicit_synthetic():
    report = c.demo()
    assert report["synthetic"]
    assert report["evaluation"]["risk_coverage"]["selected"] == 5
    assert report["invalidation"]["status"] == "INVALIDATED"
