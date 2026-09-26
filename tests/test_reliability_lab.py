import copy
import json

import pytest

from keel_eval.evaluation import EvaluationError, _sha
from keel_eval.reliability import (Runner, evaluate_trials, freeze_plan, import_regressions,
                                   run_paired, synthetic_demo)


def baseline(subject, config, seed):
    return "PASS"


def candidate(subject, config, seed):
    assert set(subject) == {"required_claim_ids", "claims", "evidence"}
    return "ABSTAIN" if not subject["evidence"] else "FAIL"


def broken(subject, config, seed):
    raise RuntimeError("SECRET EXCEPTION CONTENT")


def config_mutator(subject, config, seed):
    config["nested"]["a"] = 42
    return "ABSTAIN"


def inconsistent(subject, config, seed):
    return "FAIL" if seed % 2 else "PASS"


def inputs(count=24, *, synthetic=False, clustered=False):
    dataset = {"schema": "keel.eval.dataset.v1", "dataset_id": "test-holdout",
               "synthetic": synthetic, "split": "held_out",
               "label_source": "synthetic_fixture" if synthetic else "operator_supplied", "cases": []}
    adjudications = {}
    for n in range(count):
        case_id = "case-" + str(n)
        dataset["cases"].append({"case_id": case_id, "tags": ["regression"], "expected_verdict": "FAIL",
                                 "label_rationale": "Adjudicated contradictory evidence.",
                                 "subject": {"required_claim_ids": ["c"],
                                             "claims": [{"claim_id": "c", "text": "Flag is true."}],
                                             "evidence": [{"evidence_id": "e", "text": "Flag is false."}]}})
        adjudications[case_id] = {"cluster_id": "one-cluster" if clustered else case_id,
                                 "adjudicator_id": "test-host", "record_sha256": "a" * 64}
    return dataset, adjudications


def experiment(*, count=24, repeats=2, faults=("none",), synthetic=False, clustered=False, right=candidate):
    dataset, adj = inputs(count, synthetic=synthetic, clustered=clustered)
    left, right = Runner(baseline, {}), Runner(right, {})
    plan = freeze_plan(dataset, baseline=left, candidate=right, adjudications=adj, repeats=repeats, faults=faults)
    return dataset, plan, left, right


def test_actual_paired_trials_require_host_attestation_to_qualify():
    ds, plan, left, right = experiment()
    report = run_paired(plan, ds, baseline=left, candidate=right)
    assert report["status"] == "PROPOSED"
    report = run_paired(plan, ds, baseline=left, candidate=right, adjudication_validator=lambda p, d: True)
    assert report["status"] == "QUALIFIED"
    assert report["metrics"]["candidate"]["total"] == 48
    assert report["metrics"]["baseline"]["false_pass"] == 48
    assert report["execution_authorized"] is False


def test_repetitions_do_not_inflate_independent_sample_size():
    ds, plan, left, right = experiment(repeats=32, clustered=True)
    result = run_paired(plan, ds, baseline=left, candidate=right, adjudication_validator=lambda p, d: True)
    assert result["independent_cluster_count"] == 1
    assert result["status"] == "BLOCKED"
    assert result["gain_lower_bound"] == -1


def test_repeat_count_does_not_shrink_cluster_bound():
    bounds = []
    for repeats in (1, 10):
        ds, plan, left, right = experiment(repeats=repeats)
        bounds.append(run_paired(plan, ds, baseline=left, candidate=right)["gain_lower_bound"])
    assert bounds[0] == bounds[1]


def test_instability_is_measured_within_case_and_fault():
    ds, plan, left, right = experiment(right=inconsistent)
    report = run_paired(plan, ds, baseline=left, candidate=right)
    assert report["metrics"]["candidate"]["instability_rate"] == 1
    assert report["metrics"]["candidate"]["all_repeats_correct_rate"] == 0
    assert "instability_rate_threshold_exceeded" in report["reasons"]


def test_stateful_closure_must_use_explicit_pinned_config():
    private_state = {"verdict": "PASS"}
    def hidden_state(subject, config, seed):
        return private_state["verdict"]
    with pytest.raises(EvaluationError, match="closure_not_pinned"):
        Runner(hidden_state, {}).pin()


def test_errors_and_abstentions_stay_in_denominator_and_no_exception_leak():
    ds, plan, left, right = experiment(right=broken)
    report = run_paired(plan, ds, baseline=left, candidate=right)
    assert report["metrics"]["candidate"]["errors"] == 48
    assert report["metrics"]["candidate"]["total"] == 48
    assert "SECRET" not in json.dumps(report)
    ds, plan, left, right = experiment(faults=("none", "missing_evidence", "reversed_evidence"))
    report = run_paired(plan, ds, baseline=left, candidate=right)
    assert report["metrics"]["candidate"]["abstentions"] == 48
    assert report["metrics"]["candidate"]["correct"] == 144


def test_frozen_plan_dataset_and_configuration_mutation_fail():
    ds, plan, left, right = experiment()
    changed = copy.deepcopy(plan)
    changed["thresholds"]["min_clusters"] = 1
    with pytest.raises(EvaluationError, match="frozen_plan_changed"):
        run_paired(changed, ds, baseline=left, candidate=right)
    right.config["changed"] = True
    with pytest.raises(EvaluationError, match="runner_pin_mismatch"):
        run_paired(plan, ds, baseline=left, candidate=right)
    right.config.clear()
    ds["cases"][0]["expected_verdict"] = "PASS"
    with pytest.raises(EvaluationError, match="dataset_changed"):
        run_paired(plan, ds, baseline=left, candidate=right)


def test_config_given_to_callbacks_is_a_private_snapshot():
    ds, adj = inputs()
    left, right = Runner(config_mutator, {"nested": {"a": 0}}), Runner(candidate, {})
    plan = freeze_plan(ds, baseline=left, candidate=right, adjudications=adj)
    run_paired(plan, ds, baseline=left, candidate=right)
    assert left.config == {"nested": {"a": 0}}


def test_replay_cannot_qualify_and_rejects_missing_duplicate_or_extra_trials():
    ds, plan, left, right = experiment()
    trials = run_paired(plan, ds, baseline=left, candidate=right)["trials"]
    assert evaluate_trials(plan, ds, trials)["status"] == "PROPOSED"
    for invalid in (trials[:-1], trials + trials[:1], trials[:-1] + trials[:1]):
        with pytest.raises(EvaluationError, match="trial_coverage_invalid"):
            evaluate_trials(plan, ds, invalid)


def test_synthetic_results_are_never_qualified():
    result = synthetic_demo()
    assert result["report"]["synthetic"] is True
    assert result["report"]["status"] == "PROPOSED"


def test_missing_nonpass_denominator_is_blocked():
    ds, adj = inputs()
    for case in ds["cases"]:
        case["expected_verdict"] = "PASS"
    left, right = Runner(candidate, {}), Runner(baseline, {})
    plan = freeze_plan(ds, baseline=left, candidate=right, adjudications=adj)
    result = run_paired(plan, ds, baseline=left, candidate=right)
    assert "no_nonpass_labels" in result["reasons"]


def test_failure_import_requires_explicit_provenance_and_privacy_review():
    ds, _ = inputs(1)
    row = {"case_id": "failure1", "failure_kind": "false_pass", "expected_verdict": "FAIL",
           "sanitized_subject": ds["cases"][0]["subject"], "adjudicator_id": "operator",
           "adjudication_sha256": "a" * 64, "source_event_sha256": "b" * 64, "synthetic": True}
    with pytest.raises(EvaluationError, match="unverified"):
        import_regressions([row], dataset_id="failures", adjudication_validator=lambda r: False)
    out = import_regressions([row], dataset_id="failures", adjudication_validator=lambda r: True)
    assert out["dataset"]["split"] == "development"
    assert out["held_out_eligible"] is False
    assert out["dataset"]["synthetic"] is True
    assert out["independently_authenticated"] is False
    for forbidden in ("raw_log", "email", "attachments", "actor"):
        with pytest.raises(EvaluationError, match="whitelist"):
            import_regressions([{**row, forbidden: "private"}], dataset_id="failures", adjudication_validator=lambda r: True)


def test_freeze_rejects_development_or_uncovered_adjudication_or_over_budget():
    ds, adj = inputs()
    kwargs = {"baseline": Runner(baseline, {}), "candidate": Runner(candidate, {}), "adjudications": adj}
    ds["split"] = "development"
    with pytest.raises(EvaluationError, match="held_out"):
        freeze_plan(ds, **kwargs)
    ds["split"] = "held_out"
    adj.pop("case-0")
    with pytest.raises(EvaluationError, match="coverage"):
        freeze_plan(ds, **kwargs)
    ds, adj = inputs(256)
    with pytest.raises(EvaluationError, match="budget"):
        freeze_plan(ds, baseline=Runner(baseline, {}), candidate=Runner(candidate, {}),
                    adjudications=adj, repeats=32, faults=("none", "missing_evidence"))


def test_cli_demo_is_explicit_synthetic(capsys):
    from keel_eval.__main__ import main
    assert main(["reliability-demo"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["report"]["mode"] == "LOCAL_CALLBACKS"


@pytest.mark.parametrize("field,value", [
    ("repeats", 10**12), ("repeats", True), ("faults", ["unknown-transformation"]),
    ("faults", "none"), ("seed", -1), ("adjudications", {}),
    ("thresholds", {}), ("schema", "forged-schema"), ("execution_authorized", True),
])
def test_self_rehashed_imported_plan_cannot_bypass_validation(field, value):
    ds, plan, left, right = experiment()
    plan[field] = value
    plan.pop("plan_sha256")
    plan["plan_sha256"] = _sha(plan)
    with pytest.raises(EvaluationError):
        evaluate_trials(plan, ds, [])
    with pytest.raises(EvaluationError):
        run_paired(plan, ds, baseline=left, candidate=right)


def test_self_rehashed_plan_cannot_overrun_trial_budget_or_weaken_threshold_schema():
    ds, plan, left, right = experiment(count=256, repeats=1)
    for mutation in ({"repeats": 32}, {"thresholds": {**plan["thresholds"], "alpha": float("inf")}},
                     {"baseline": {**plan["baseline"], "source_sha256": "bad"}}, {"extra": True}):
        changed = {**plan, **mutation}
        changed.pop("plan_sha256")
        if mutation.get("thresholds"):
            # Nonfinite JSON is rejected even before digest checking.
            changed["plan_sha256"] = "0" * 64
        else:
            changed["plan_sha256"] = _sha(changed)
        with pytest.raises(EvaluationError):
            evaluate_trials(changed, ds, [])
