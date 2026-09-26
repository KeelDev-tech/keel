"""Frozen finite-threshold calibration of unsupported PASS recommendations.

This is Bonferroni-adjusted, one-sided exact binomial risk bounding, NOT
conformal risk control or an anytime-valid procedure. Scores and human label
provenance are caller observations, not authenticated probabilities or truth.
"""
import math

from keel_eval.evaluation import (
    EvaluationError, _snapshot, _sha, _keys, _token, _hash, validate_dataset,
)


class CalibrationError(ValueError):
    """Content-free validation failure."""


def _fail(code):
    raise CalibrationError(code)


def digest(value):
    try:
        return _sha(value)
    except EvaluationError as exc:
        raise CalibrationError(str(exc)) from None


def _number(value, code, *, lower=0.0, upper=1.0, exclusive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not lower <= value <= upper or exclusive and value in (lower, upper)):
        _fail(code)


def _visible_subject(subject):
    # A renamed claim/evidence ID does not create an independent observation.
    return {"claims": sorted(row["text"] for row in subject["claims"]),
            "evidence": sorted(row["text"] for row in subject["evidence"])}


def validate_data(data):
    try:
        data = _snapshot(data)
        _keys(data, ("schema", "dataset_id", "split", "synthetic", "label_source",
                     "config_sha256", "source_sha256", "rows"), "data_schema_invalid")
        if data["schema"] != "keel.loki.calibration-data.v1":
            _fail("data_version_invalid")
        _token(data["dataset_id"], "dataset_id_invalid")
        if data["split"] not in ("calibration", "test"):
            _fail("data_split_invalid")
        if type(data["synthetic"]) is not bool:
            _fail("synthetic_invalid")
        if data["label_source"] != ("synthetic_fixture" if data["synthetic"] else "human_supplied"):
            _fail("human_label_provenance_required")
        _hash(data["config_sha256"], "config_sha256_invalid")
        _hash(data["source_sha256"], "source_sha256_invalid")
        if type(data["rows"]) is not list or not 1 <= len(data["rows"]) <= 2000:
            _fail("row_count_invalid")
        cases, groups, subjects, observations = set(), set(), set(), set()
        for row in data["rows"]:
            _keys(row, ("case_id", "group_id", "subject", "ranking_score", "observed_verdict",
                        "expected_verdict", "observation_sha256", "label_revision_sha256",
                        "reviewer_id"), "row_schema_invalid")
            _token(row["case_id"], "case_id_invalid")
            _token(row["group_id"], "group_id_invalid")
            _token(row["reviewer_id"], "reviewer_id_invalid")
            for key in ("observation_sha256", "label_revision_sha256"):
                _hash(row[key], key + "_invalid")
            _number(row["ranking_score"], "ranking_score_invalid")
            if row["observed_verdict"] not in ("PASS", "FAIL", "ABSTAIN", "ERROR"):
                _fail("observed_verdict_invalid")
            # Reuse the existing bounded claim/evidence contract; no calls.
            validate_dataset({"schema": "keel.eval.dataset.v1", "dataset_id": "shape-check",
                "synthetic": True, "split": "development", "label_source": "synthetic_fixture",
                "cases": [{"case_id": row["case_id"], "tags": ["calibration"],
                    "expected_verdict": row["expected_verdict"], "label_rationale": "Shape check only.",
                    "subject": row["subject"]}]})
            subject = digest(_visible_subject(row["subject"]))
            if row["case_id"] in cases:
                _fail("duplicate_case")
            if row["group_id"] in groups:
                _fail("repeated_group_not_independent")
            if subject in subjects:
                _fail("duplicate_subject")
            if row["observation_sha256"] in observations:
                _fail("duplicate_observation")
            cases.add(row["case_id"])
            groups.add(row["group_id"])
            subjects.add(subject)
            observations.add(row["observation_sha256"])
        return data
    except EvaluationError as exc:
        raise CalibrationError(str(exc)) from None


def _pair(calibration, test, config, source_sha256):
    calibration, test = validate_data(calibration), validate_data(test)
    try:
        _hash(source_sha256, "source_sha256_invalid")
        _snapshot(config)
    except EvaluationError as exc:
        raise CalibrationError(str(exc)) from None
    if type(config) is not dict or not config:
        _fail("frozen_config_required")
    if calibration["split"] != "calibration" or test["split"] != "test":
        _fail("calibration_test_splits_required")
    if calibration["synthetic"] != test["synthetic"]:
        _fail("synthetic_real_mixing_forbidden")
    if calibration["dataset_id"] == test["dataset_id"]:
        _fail("dataset_ids_overlap")
    for data in (calibration, test):
        if data["config_sha256"] != digest(config):
            _fail("configuration_changed")
        if data["source_sha256"] != source_sha256:
            _fail("source_changed")
    for key in ("case_id", "group_id", "observation_sha256"):
        if {r[key] for r in calibration["rows"]} & {r[key] for r in test["rows"]}:
            _fail("calibration_test_overlap")
    if ({digest(_visible_subject(r["subject"])) for r in calibration["rows"]}
            & {digest(_visible_subject(r["subject"])) for r in test["rows"]}):
        _fail("calibration_test_subject_overlap")
    return calibration, test


def _plan(value):
    try:
        value = _snapshot(value)
        _keys(value, ("schema", "calibration_id", "config_sha256", "source_sha256",
                      "calibration_sha256", "test_sha256", "thresholds", "risk_limit", "delta"),
              "plan_schema_invalid")
        if value["schema"] != "keel.loki.calibration-plan.v1":
            _fail("plan_version_invalid")
        _token(value["calibration_id"], "calibration_id_invalid")
        for key in ("config_sha256", "source_sha256", "calibration_sha256", "test_sha256"):
            _hash(value[key], key + "_invalid")
        thresholds = value["thresholds"]
        if type(thresholds) is not list or not 1 <= len(thresholds) <= 64:
            _fail("threshold_count_invalid")
        for threshold in thresholds:
            _number(threshold, "threshold_invalid")
        if thresholds != sorted(set(thresholds)):
            _fail("thresholds_must_be_sorted_unique")
        _number(value["risk_limit"], "risk_limit_invalid", exclusive=True)
        _number(value["delta"], "delta_invalid", lower=0.000000001, upper=.5)
        return value
    except EvaluationError as exc:
        raise CalibrationError(str(exc)) from None


def make_plan(config, *, source_sha256, thresholds, risk_limit, delta,
              calibration_id, calibration_sha256, test_sha256):
    """Declare thresholds before fitting; scores are rankings, not probabilities."""
    if type(config) is not dict or not config:
        _fail("frozen_config_required")
    return _plan({"schema": "keel.loki.calibration-plan.v1", "calibration_id": calibration_id,
        "config_sha256": digest(config), "source_sha256": source_sha256,
        "calibration_sha256": calibration_sha256, "test_sha256": test_sha256,
        "thresholds": thresholds, "risk_limit": risk_limit, "delta": delta})


def binomial_upper(errors, selected, delta):
    """One-sided Clopper-Pearson upper bound P_p[Bin(n,p)<=errors]=delta.

    Log-sum-exp avoids underflow. The returned endpoint is on the conservative
    side of the numerical bisection; n=0 and all-errors cases give upper=1.
    """
    if (type(errors) is not int or type(selected) is not int
            or not 0 <= errors <= selected <= 2000):
        _fail("binomial_counts_invalid")
    _number(delta, "binomial_delta_invalid", exclusive=True)
    if selected == 0 or errors == selected:
        return 1.0
    if errors == 0:
        return min(1.0, math.nextafter(-math.expm1(math.log(delta) / selected), 1.0))
    log_coefficients = [math.lgamma(selected + 1) - math.lgamma(i + 1)
                        - math.lgamma(selected - i + 1) for i in range(errors + 1)]
    def log_cdf(p):
        terms = [c + i * math.log(p) + (selected - i) * math.log1p(-p)
                 for i, c in enumerate(log_coefficients)]
        maximum = max(terms)
        return maximum + math.log(math.fsum(math.exp(t - maximum) for t in terms))
    low, high = 0.0, 1.0
    for _ in range(64):
        middle = (low + high) / 2
        if middle in (low, high):
            break
        if log_cdf(middle) > math.log(delta):
            low = middle
        else:
            high = middle
    return min(1.0, math.nextafter(high, 1.0))


def _statistics(data, threshold, delta=None):
    eligible = [r for r in data["rows"] if r["observed_verdict"] == "PASS"
                and threshold is not None and r["ranking_score"] >= threshold]
    errors = sum(r["expected_verdict"] != "PASS" for r in eligible)
    n, total = len(eligible), len(data["rows"])
    return {"threshold": threshold, "selected": n, "total": total, "unsupported_pass": errors,
        "coverage": n / total, "empirical_risk_among_selected": errors / n if n else None,
        "unsupported_pass_per_all_cases": errors / total,
        "binomial_upper": binomial_upper(errors, n, delta) if delta is not None else None}


def fit(calibration, test, plan, *, expected_plan_sha256, config, source_sha256):
    """Fit a fixed family; test labels never influence threshold selection."""
    plan = _plan(plan)
    if digest(plan) != expected_plan_sha256:
        _fail("plan_pin_mismatch")
    calibration, test = _pair(calibration, test, config, source_sha256)
    if plan["config_sha256"] != digest(config) or plan["source_sha256"] != source_sha256:
        _fail("plan_configuration_changed")
    if (plan["calibration_sha256"] != digest(calibration)
            or plan["test_sha256"] != digest(test)):
        _fail("frozen_dataset_changed")
    adjusted = plan["delta"] / len(plan["thresholds"])
    table = [_statistics(calibration, threshold, adjusted) for threshold in plan["thresholds"]]
    feasible = [row for row in table if row["selected"] and row["binomial_upper"] <= plan["risk_limit"]]
    chosen = max(feasible, key=lambda row: (row["coverage"], -row["threshold"])) if feasible else None
    return {"schema": "keel.loki.calibration-fit.v1", "plan_sha256": digest(plan),
        "config_sha256": digest(config), "source_sha256": source_sha256,
        "calibration_sha256": digest(calibration), "test_sha256": digest(test),
        "synthetic": calibration["synthetic"], "method": "finite_family_bonferroni_binomial_upper",
        "status": "THRESHOLD_SELECTED" if chosen else "INSUFFICIENT_EVIDENCE",
        "per_threshold_delta": adjusted, "table": table,
        "selected_threshold": chosen["threshold"] if chosen else None,
        "label_provenance_authenticated": False, "ranking_scores_are_probabilities": False,
        "exchangeability_or_independence_verified": False,
        "assumptions": ["Frozen score function and threshold family before observing calibration labels.",
            "One independent IID case per group from the target workload; truthful human labels.",
            "Conditionally selected errors are Bernoulli observations at each fixed threshold.",
            "Bonferroni handles threshold selection only; no anytime or arbitrary-shift guarantee."],
        "execution_authorized": False, "production_deployed": False,
        "deployment_validated": False}


def evaluate(calibration, test, plan, fitted, *, expected_plan_sha256,
             expected_fit_sha256, config, source_sha256):
    """Verify/recompute the fit, then evaluate the untouched pinned test set."""
    if digest(fitted) != expected_fit_sha256:
        _fail("fit_pin_mismatch")
    rebuilt = fit(calibration, test, plan, expected_plan_sha256=expected_plan_sha256,
                  config=config, source_sha256=source_sha256)
    if digest(fitted) != digest(rebuilt):
        _fail("fit_report_changed")
    test = validate_data(test)
    return {"schema": "keel.loki.calibration-evaluation.v1", "fit_sha256": digest(fitted),
        "plan_sha256": digest(plan), "test_sha256": digest(test), "synthetic": test["synthetic"],
        "status": fitted["status"], "risk_coverage": _statistics(test, fitted["selected_threshold"]),
        "test_used_for_selection": False, "label_provenance_authenticated": False,
        "calibrated_probability_claim": False, "execution_authorized": False,
        "deployment_validated": False, "production_deployed": False}


def invalidate(fitted, *, config, source_sha256):
    """Report stale bindings. Matching pins are consistency, not certification."""
    if type(fitted) is not dict or fitted.get("schema") != "keel.loki.calibration-fit.v1":
        _fail("fit_schema_invalid")
    try:
        fitted = _snapshot(fitted)
        _hash(source_sha256, "source_sha256_invalid")
    except EvaluationError as exc:
        raise CalibrationError(str(exc)) from None
    reasons = []
    if fitted.get("config_sha256") != digest(config):
        reasons.append("configuration_changed")
    if fitted.get("source_sha256") != source_sha256:
        reasons.append("source_changed")
    return {"schema": "keel.loki.calibration-binding.v1", "status": "INVALIDATED" if reasons else "PINS_MATCH",
            "reasons": reasons, "requires_fresh_calibration": bool(reasons),
            "execution_authorized": False, "deployment_validated": False}


def demo():
    """Deterministic synthetic arithmetic demonstration; no model or file I/O."""
    config, source = {"model": "synthetic-observer", "score_function": "fixture-v1"}, "a" * 64
    def data(split, count):
        return {"schema": "keel.loki.calibration-data.v1", "dataset_id": split, "split": split,
            "synthetic": True, "label_source": "synthetic_fixture", "config_sha256": digest(config),
            "source_sha256": source, "rows": [{"case_id": f"{split}-{i}", "group_id": f"{split}-{i}",
                "subject": {"required_claim_ids": ["claim"],
                    "claims": [{"claim_id": "claim", "text": f"{split} value {i}"}],
                    "evidence": [{"evidence_id": "evidence", "text": f"{split} record {i}"}]},
                "ranking_score": .9, "observed_verdict": "PASS", "expected_verdict": "PASS",
                "observation_sha256": digest([split, i]), "label_revision_sha256": digest(["label", split, i]),
                "reviewer_id": "synthetic-reviewer"} for i in range(count)]}
    calibration, test = data("calibration", 30), data("test", 5)
    plan = make_plan(config, source_sha256=source, thresholds=[.5, .8], risk_limit=.2,
        delta=.05, calibration_id="demo", calibration_sha256=digest(calibration), test_sha256=digest(test))
    fitted = fit(calibration, test, plan, expected_plan_sha256=digest(plan), config=config, source_sha256=source)
    return {"fit": fitted, "evaluation": evaluate(calibration, test, plan, fitted,
        expected_plan_sha256=digest(plan), expected_fit_sha256=digest(fitted), config=config, source_sha256=source),
        "invalidation": invalidate(fitted, config={"model": "changed"}, source_sha256=source),
        "synthetic": True, "execution_authorized": False}
