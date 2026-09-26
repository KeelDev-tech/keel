from copy import deepcopy
import pytest
from keel_loki.common import digest
from keel_loki import lab
from keel_loki.research import propose, record_result, demo


def hypothesis(report):
    return propose(research_id="test", paper_reference="Research proposition only",
        proposition="Grounded critique can improve supported decisions.",
        hypothesis="Review reduces false PASS on frozen fixtures.",
        comparator_stage="drafter_blind", experimental_stage="reviewer_revision",
        metric="false_pass", direction="decrease", dataset_sha256=report["dataset_sha256"],
        experiment_plan_sha256=report["plan_sha256"])


def test_actual_run_metrics_are_bound_without_proving_research():
    report = lab.demo()
    h = hypothesis(report)
    result = record_result(h, report, expected_hypothesis_sha256=digest(h), expected_plan_sha256=report["plan_sha256"])
    assert result["run_sha256"] == digest(report)
    assert result["mode"] == "INJECTED" and result["synthetic"] is True
    assert result["observed_delta"] == 0 and result["direction_observed"] is False
    assert result["research_proposition_proven"] is False
    assert result["broad_cognitive_benefit_established"] is False


@pytest.mark.parametrize("kind", ["hypothesis", "plan", "dataset", "metrics", "provenance"])
def test_changed_bindings_or_computed_result_rejected(kind):
    report = lab.demo()
    h = hypothesis(report)
    hp, pp = digest(h), report["plan_sha256"]
    if kind == "hypothesis": h["hypothesis"] = "Different prediction"
    elif kind == "plan": pp = "e" * 64
    elif kind == "dataset": h["dataset_sha256"] = "e" * 64; hp = digest(h)
    elif kind == "metrics": report["metrics"]["stages"]["drafter_blind"]["false_pass"]["value"] = 1
    else: report["mode"] = "LOCAL_LOOPBACK"
    with pytest.raises(ValueError):
        record_result(h, report, expected_hypothesis_sha256=hp, expected_plan_sha256=pp)


def test_missing_model_calls_remain_incomplete():
    data = lab.mutation_dataset()
    report = lab.run_lab(data, lab.fixture_configs(), expected_dataset_sha256=digest(data))
    h = hypothesis(report)
    result = record_result(h, report, expected_hypothesis_sha256=digest(h), expected_plan_sha256=report["plan_sha256"])
    assert result["status"] == "INCOMPLETE" and result["comparison_errors"] == 16


def test_research_demo_has_no_inference_or_causal_claim():
    result = demo()
    assert result["mode"] == "INJECTED"
    assert result["statistical_significance_established"] is False
    assert result["causal_effect_established"] is False
