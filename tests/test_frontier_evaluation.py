import copy
import json
import math

import pytest

from keel_eval.evaluation import EvaluationError, _sha, dataset_digest
from keel_eval.frontier import (audit_paired, cluster_interval, run_benchmark,
                                zero_failure_sample_size)


@pytest.fixture(scope="module")
def benchmark():
    return run_benchmark()


def test_benchmark_executes_existing_protocol_boundary_against_both_constants(benchmark):
    assert benchmark["status"] == "SMOKE_PASSED"
    assert benchmark["actual_callback_invocations"] == 432
    assert {item["baseline"] for item in benchmark["comparisons"]} == {"constant_pass", "constant_abstain"}
    for item in benchmark["comparisons"]:
        audit = item["audit"]
        assert audit["balanced_metrics"]["candidate"]["macro_recall"] == 1
        assert audit["balanced_metrics"]["baseline"]["macro_recall"] == pytest.approx(1 / 3)
        assert audit["declared_cluster_count"] == 6
        assert item["execution"]["metrics"]["candidate"]["total"] == 108
        assert audit["risk_intervals"]["candidate"]["unsafe_pass"]["upper"] > 0
        assert audit["production_qualified"] is False
    assert benchmark["execution_authorized"] is False


def test_related_variants_and_repetitions_never_multiply_clusters(benchmark):
    once = run_benchmark(repeats=1)
    left = once["comparisons"][0]["audit"]["cluster_intervals"]
    right = benchmark["comparisons"][0]["audit"]["cluster_intervals"]
    assert left == right
    assert left["overall"]["candidate"]["clusters"] == 6
    assert left["family:transport"]["candidate"]["clusters"] == 1


def test_all_abstain_cannot_hide_zero_decisive_class_recall(benchmark):
    item = benchmark["comparisons"][0]
    rows = copy.deepcopy(item["execution"]["trials"])
    for row in rows:
        if row["runner"] == "candidate":
            row["verdict"] = "ABSTAIN"
    result = audit_paired(item["plan"], benchmark["dataset"], rows)
    assert result["balanced_metrics"]["candidate"]["recall_by_expected_verdict"] == {"PASS": 0, "FAIL": 0, "ABSTAIN": 1}
    assert result["balanced_metrics"]["candidate"]["macro_recall"] == pytest.approx(1 / 3)
    assert result["risk_intervals"]["candidate"]["unnecessary_abstention"]["mean"] == 1


def test_errors_are_counted_not_dropped(benchmark):
    item = benchmark["comparisons"][0]
    rows = copy.deepcopy(item["execution"]["trials"])
    for row in rows:
        if row["runner"] == "candidate":
            row["verdict"] = "ERROR"
    result = audit_paired(item["plan"], benchmark["dataset"], rows)
    assert result["balanced_metrics"]["candidate"]["macro_recall"] == 0
    assert result["risk_intervals"]["candidate"]["error"]["mean"] == 1
    assert sum(matrix["ERROR"] for matrix in result["confusion_matrices"]["candidate"].values()) == 108


def test_missing_or_duplicated_trials_fail_before_statistics(benchmark):
    item = benchmark["comparisons"][0]
    rows = item["execution"]["trials"]
    for invalid in (rows[:-1], rows + rows[:1], rows[:-1] + rows[:1]):
        with pytest.raises(EvaluationError, match="trial_coverage_invalid"):
            audit_paired(item["plan"], benchmark["dataset"], invalid)


def test_frozen_family_assignment_cannot_be_changed_after_run(benchmark):
    item = benchmark["comparisons"][0]
    changed = copy.deepcopy(benchmark["dataset"])
    changed["cases"][0]["tags"] = ["easier_family"]
    with pytest.raises(EvaluationError, match="dataset_changed"):
        audit_paired(item["plan"], changed, item["execution"]["trials"])


def test_interval_matches_two_sided_hoeffding_and_spending_formula():
    result = cluster_interval([.5] * 100, alpha=.05, comparisons=8)
    delta = .05 / (8 * 100 * 101)
    radius = math.sqrt(math.log(2 / delta) / 200)
    assert result["alpha_at_n"] == delta
    assert result["lower"] == pytest.approx(.5 - radius)
    assert result["upper"] == pytest.approx(.5 + radius)
    difference = cluster_interval([0] * 100, alpha=.05, comparisons=8, lower=-1, upper=1)
    assert difference["upper"] == pytest.approx(2 * radius)


def test_zero_observations_produce_vacuous_bounds():
    result = cluster_interval([], alpha=.05, comparisons=1)
    assert result == {"clusters": 0, "mean": None, "lower": 0, "upper": 1, "alpha_at_n": None}


def test_familywise_and_all_time_spending_never_exceeds_budget():
    # Telescoping finite prefix is alpha * (1 - 1/(N+1)), for any M.
    alpha, metrics, maximum = .05, 36, 256
    allocated = math.fsum(metrics * alpha / (metrics * n * (n + 1)) for n in range(1, maximum + 1))
    assert allocated == pytest.approx(alpha * (1 - 1 / (maximum + 1)))
    assert allocated < alpha


@pytest.mark.parametrize("values,alpha,comparisons", [
    ([True], .05, 1), ([float("nan")], .05, 1), ([1.1], .05, 1),
    ([0], True, 1), ([0], 0, 1), ([0], .05, True), ([0], .05, 0),
])
def test_invalid_statistical_inputs_are_rejected(values, alpha, comparisons):
    with pytest.raises(EvaluationError):
        cluster_interval(values, alpha=alpha, comparisons=comparisons)


def test_zero_failure_sample_planner_has_exact_known_values():
    assert zero_failure_sample_size(maximum_rate=.05)["independent_opportunities_required"] == 59
    assert zero_failure_sample_size(maximum_rate=.01)["independent_opportunities_required"] == 299
    result = zero_failure_sample_size(maximum_rate=.01)
    assert result["upper_failure_rate"] <= .01
    assert -math.expm1(math.log(.05) / 298) > .01
    assert result["execution_authorized"] is False


def test_synthetic_benchmark_reports_do_not_claim_independence_or_qualification(benchmark):
    for item in benchmark["comparisons"]:
        audit = item["audit"]
        assert audit["status"] == "DIAGNOSTIC_ONLY"
        assert "cluster_independence_not_authenticated" in audit["reasons"]
        assert "synthetic_fixture_only" in audit["reasons"]
        assert "original_dataset_missing_outcome_class" not in audit["reasons"]
        assert item["execution"]["status"] != "QUALIFIED"
        assert audit["cluster_intervals"]["overall"]["paired_gain"]["lower"] < 0
    assert sum(item["audit"]["alpha"] for item in benchmark["comparisons"]) == .05


def test_trial_budget_is_enforced_before_benchmark_execution():
    with pytest.raises(EvaluationError, match="repeats_invalid"):
        run_benchmark(repeats=100000)


def _rebind_dataset(item, dataset):
    # Simulates a separately frozen experiment, not mutation of an existing one.
    plan = copy.deepcopy(item["plan"])
    plan["dataset_sha256"] = dataset_digest(dataset)
    plan.pop("plan_sha256")
    plan["plan_sha256"] = _sha(plan)
    return plan


def test_byte_identical_subjects_cannot_be_renamed_to_inflate_clusters(benchmark):
    item = benchmark["comparisons"][0]
    dataset = copy.deepcopy(benchmark["dataset"])
    dataset["cases"][3]["subject"] = copy.deepcopy(dataset["cases"][0]["subject"])
    plan = _rebind_dataset(item, dataset)
    with pytest.raises(EvaluationError, match="duplicate_subject_cross_cluster"):
        audit_paired(plan, dataset, item["execution"]["trials"])


def test_missing_outcome_label_is_reported_as_missing_not_perfect_recall(benchmark):
    item = benchmark["comparisons"][0]
    dataset = copy.deepcopy(benchmark["dataset"])
    for case in dataset["cases"]:
        if case["expected_verdict"] == "PASS":
            case["expected_verdict"] = "ABSTAIN"
    audit = audit_paired(_rebind_dataset(item, dataset), dataset, item["execution"]["trials"])
    assert "original_dataset_missing_outcome_class" in audit["reasons"]
    assert audit["balanced_metrics"]["candidate"]["macro_recall"] is None
    assert audit["cluster_intervals"]["label:PASS"]["candidate"]["clusters"] == 0
    assert audit["cluster_intervals"]["label:PASS"]["candidate"]["lower"] == 0


def test_all_family_slots_are_reserved_even_when_some_are_absent(benchmark):
    audit = benchmark["comparisons"][0]["audit"]
    assert audit["simultaneous_comparisons"] == 66
    assert audit["reported_intervals"] == 36


def test_each_case_requires_one_unambiguous_frozen_family(benchmark):
    item = benchmark["comparisons"][0]
    dataset = copy.deepcopy(benchmark["dataset"])
    dataset["cases"][0]["tags"].append("posthoc-selection")
    with pytest.raises(EvaluationError, match="one_frozen_family_per_case"):
        audit_paired(_rebind_dataset(item, dataset), dataset, item["execution"]["trials"])


def test_more_than_reserved_family_slots_are_rejected(benchmark):
    item = benchmark["comparisons"][0]
    dataset = copy.deepcopy(benchmark["dataset"])
    for index, case in enumerate(dataset["cases"]):
        case["tags"] = ["family-" + str(index)]
    with pytest.raises(EvaluationError, match="family_limit"):
        audit_paired(_rebind_dataset(item, dataset), dataset, item["execution"]["trials"])


def test_protocol_acceptance_exercises_all_model_verdicts(benchmark):
    verdicts = set()
    for case in benchmark["dataset"]["cases"]:
        if case["expected_verdict"] == "PASS":
            response = json.loads(case["subject"]["evidence"][0]["text"])
            envelope = json.loads(response["body"])
            verdicts.add(json.loads(envelope["message"]["content"])["verdict"])
    assert verdicts == {"PASS", "FAIL", "ABSTAIN"}
