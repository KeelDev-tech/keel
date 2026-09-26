"""Offline experiment safety, coverage and pinning regressions."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import json
from unittest.mock import patch

import pytest

from keel_agent.models import HTTPResult, ReviewerConfig
from keel_bench.experiment import (
    ALL_ERROR_CODES, NO_CALL_ERROR_CODES,
    BenchmarkError, _schedule, digest, make_plan, plan_digest, run_experiment, schedule,
    validate_plan,
)


SOURCE = "a" * 64


def dataset():
    return {"schema": "keel.eval.dataset.v1", "dataset_id": "private-gold-set",
            "synthetic": True, "split": "held_out", "label_source": "synthetic_fixture",
            "cases": [{"case_id": "case-" + str(i), "tags": ["private-gold-tag"],
                       "expected_verdict": verdict, "label_rationale": "PRIVATE GOLD RATIONALE",
                       "subject": {"required_claim_ids": ["claim"],
                                   "claims": [{"claim_id": "claim", "text": "VALUE-" + str(i)}],
                                   "evidence": [{"evidence_id": "record", "text": "Evidence " + str(i)}]}}
                      for i, verdict in enumerate(("PASS", "FAIL", "ABSTAIN"))]}


def systems(extra=False):
    result = [{"system_id": "baseline", "kind": "abstain_baseline",
               "model_config": None, "declared_weights_sha256": None},
              {"system_id": "local-a", "kind": "local_model", "declared_weights_sha256": "b" * 64,
               "model_config": asdict(ReviewerConfig("a", "ollama", "http://127.0.0.1:11434/api/chat",
                                                       "fixture-a", timeout_seconds=.05))}]
    if extra:
        result.append(deepcopy(result[1]))
        result[-1]["system_id"] = "local-b"
        result[-1]["model_config"].update(reviewer_id="b", model="fixture-b")
    return result


def plan(data=None, roster=None, **kwargs):
    return make_plan(data or dataset(), roster or systems(), experiment_id="experiment",
                     source_sha256=SOURCE, **kwargs)


def run(data=None, frozen=None, **kwargs):
    data = data or dataset()
    frozen = frozen or plan(data)
    return run_experiment(data, frozen, expected_plan_sha256=plan_digest(frozen),
                          source_sha256=SOURCE, **kwargs)


def response(config, verdict="PASS", *, claims=None, findings=None):
    assessment = {"verdict": verdict, "covered_claim_ids": claims or ["claim"],
                  "findings": ([] if verdict == "PASS" else ["Deliberate fixture finding"])
                  if findings is None else findings}
    return HTTPResult(200, {"content-type": "application/json"}, json.dumps({
        "model": config.model, "done": True, "created_at": "fixture-response",
        "message": {"role": "assistant", "content": json.dumps(assessment)}}).encode())


def test_plan_has_exact_scope_and_is_detached():
    data, roster = dataset(), systems()
    frozen = plan(data, roster)
    pin = plan_digest(frozen)
    roster[1]["model_config"]["model"] = "changed"
    data["cases"][0]["subject"]["claims"][0]["text"] = "changed"
    assert frozen["systems"][1]["model_config"]["model"] == "fixture-a"
    assert plan_digest(frozen) == pin
    assert len(pin) == 64


@pytest.mark.parametrize("key,value,code", [
    ("trials", 0, "trials_invalid"), ("trials", 21, "trials_invalid"),
    ("trials", True, "trials_invalid"), ("seed", -1, "seed_invalid"),
    ("seed", 2**32, "seed_invalid"), ("seed", False, "seed_invalid"),
    ("schema", "other", "plan_version_invalid"), ("extra", 1, "plan_schema_invalid"),
    ("experiment_id", "unsafe\nlabel", "experiment_id_invalid"),
    ("source_sha256", "invalid", "source_sha256_invalid"),
])
def test_plan_rejects_malformed_fields(key, value, code):
    frozen = plan()
    frozen[key] = value
    with pytest.raises(BenchmarkError, match=code):
        validate_plan(frozen, dataset(), source_sha256=SOURCE)


@pytest.mark.parametrize("key,value", [("max_model_calls", 0), ("max_model_calls", 4097),
    ("max_model_calls", True), ("max_wall_seconds", .99), ("max_wall_seconds", 86401),
    ("max_wall_seconds", True), ("max_wall_seconds", float("inf"))])
def test_bad_limits(key, value):
    frozen = plan()
    frozen["limits"][key] = value
    with pytest.raises(BenchmarkError):
        validate_plan(frozen, dataset(), source_sha256=SOURCE)


def test_source_dataset_and_plan_pins_are_independent():
    data, frozen = dataset(), plan()
    with pytest.raises(BenchmarkError, match="plan_source_mismatch"):
        validate_plan(frozen, data, source_sha256="c" * 64)
    data["cases"][0]["expected_verdict"] = "FAIL"
    with pytest.raises(BenchmarkError, match="plan_dataset_mismatch"):
        validate_plan(frozen, data, source_sha256=SOURCE)
    frozen["seed"] += 1
    with pytest.raises(BenchmarkError, match="plan_pin_mismatch"):
        run_experiment(dataset(), frozen, expected_plan_sha256=plan_digest(plan()),
                       source_sha256=SOURCE)


@pytest.mark.parametrize("variant", ["id", "config", "baseline", "cloud", "extra", "weights", "short"])
def test_roster_validation(variant):
    roster = systems(extra=True)
    if variant == "id":
        roster[2]["system_id"] = roster[1]["system_id"]
    elif variant == "config":
        roster[2]["model_config"] = deepcopy(roster[1]["model_config"])
        roster[2]["model_config"]["reviewer_id"] = "alias"
    elif variant == "baseline":
        roster[2] = dict(roster[0], system_id="duplicate-baseline")
    elif variant == "cloud":
        roster[1]["model_config"]["endpoint"] = "https://example.invalid/api/chat"
    elif variant == "extra":
        roster[1]["private"] = True
    elif variant == "weights":
        roster[1]["declared_weights_sha256"] = "not-a-digest"
    else:
        roster = roster[:1]
    with pytest.raises(BenchmarkError):
        plan(roster=roster)


def test_all_planned_rows_are_counted_with_models_disabled():
    result = run(transport=lambda *args: pytest.fail("unexpected model call"))
    assert result["mode"] == "BASELINE_ONLY"
    assert result["model_calls_attempted"] == 0
    assert len(result["records"]) == 18
    baseline = [r for r in result["records"] if r["system_id"] == "baseline"]
    blocked = [r for r in result["records"] if r["system_id"] == "local-a"]
    assert {r["observed_verdict"] for r in baseline} == {"ABSTAIN"}
    assert {r["error_code"] for r in blocked} == {"local_model_not_enabled"}
    assert all(r["assessment_sha256"] is None for r in blocked)
    assert all(r["request_sha256"] is None and r["response_sha256"] is None for r in baseline)
    # A supported case is withheld by the baseline; abstention is not PASS.
    assert all(r["observed_verdict"] != "PASS" for r in baseline if r["expected_verdict"] == "PASS")
    assert result["execution_authorized"] is False
    assert result["model_quality_validated"] is False
    assert result["production_deployed"] is False


def test_schedule_is_reproducible_balanced_and_covers_all_cells():
    data, frozen = dataset(), plan(roster=systems(extra=True), trials=4, seed=37)
    schedule = list(_schedule(data, frozen))
    assert schedule == list(_schedule(data, frozen))
    assert len(schedule) == len(set(schedule)) == 36
    assert set(schedule) == {(trial, case, system) for trial in range(4)
                            for case in range(3) for system in range(3)}
    first_positions = Counter(schedule[index][2] for index in range(0, len(schedule), 3))
    assert len(set(first_positions.values())) == 1
    changed = dict(frozen, seed=38)
    assert schedule != list(_schedule(data, changed))


def test_injected_requests_never_contain_gold_metadata():
    captured = []
    def send(config, payload, timeout):
        captured.append(payload)
        assert timeout == config.timeout_seconds
        rendered = json.dumps(payload)
        for forbidden in ("PRIVATE GOLD RATIONALE", "private-gold-set", "private-gold-tag",
                          "label_rationale", "expected_verdict", "case_id", "dataset_sha256"):
            assert forbidden not in rendered
        view = json.loads(payload["messages"][1]["content"])
        assert set(view) == {"phase", "subject", "subject_sha256"}
        return response(config)
    result = run(allow_model_calls=True, transport=send)
    assert result["mode"] == "INJECTED"
    assert result["model_calls_attempted"] == len(captured) == 9
    model_rows = [r for r in result["records"] if r["system_id"] == "local-a"]
    assert all(r["observed_verdict"] == "PASS" and r["model_calls_attempted"] == 1 for r in model_rows)
    assert all(len(r[key]) == 64 for r in model_rows
               for key in ("request_sha256", "response_sha256", "assessment_sha256"))


def test_global_429_stops_other_systems_and_all_trials():
    calls = []
    def limited(config, payload, timeout):
        calls.append(config.model)
        return HTTPResult(429, {}, b"private rate-limit body")
    result = run(frozen=plan(roster=systems(extra=True), trials=3),
                 allow_model_calls=True, transport=limited)
    models = [r for r in result["records"] if r["system_id"] != "baseline"]
    assert len(calls) == result["model_calls_attempted"] == 1
    assert result["stop_reason"] == "model_http_429"
    assert models[0]["error_code"] == "model_http_429"
    assert all(r["error_code"] == "not_run_after_rate_limit" for r in models[1:])
    assert len(result["records"]) == 27
    assert all(r["observed_verdict"] == "ABSTAIN" for r in result["records"]
               if r["system_id"] == "baseline")
    assert "private rate-limit body" not in json.dumps(result)


def test_model_call_budget_never_omits_unrun_rows():
    result = run(frozen=plan(max_model_calls=2), allow_model_calls=True,
                 transport=lambda cfg, payload, timeout: response(cfg))
    assert result["model_calls_attempted"] == 2
    assert result["stop_reason"] == "model_call_budget_exhausted"
    assert len(result["records"]) == 18
    assert sum(r["error_code"] == "model_call_budget_exhausted" for r in result["records"]) == 7


def test_original_model_deadline_must_fit_remaining_budget():
    roster = systems()
    roster[1]["model_config"]["timeout_seconds"] = 60
    result = run(frozen=plan(roster=roster, max_wall_seconds=1), allow_model_calls=True,
                 transport=lambda *args: pytest.fail("deadline cannot fit budget"))
    assert result["model_calls_attempted"] == 0
    assert result["stop_reason"] == "wall_budget_insufficient_for_deadline"
    assert sum(r["error_code"] == "wall_budget_insufficient_for_deadline" for r in result["records"]) == 9


def test_exhausted_wall_budget_keeps_every_cell():
    with patch("keel_bench.experiment.time.monotonic", side_effect=[0] + [2] * 30):
        result = run(frozen=plan(max_wall_seconds=1), allow_model_calls=True,
                     transport=lambda *args: pytest.fail("wall budget exhausted"))
    assert result["stop_reason"] == "wall_budget_exhausted"
    assert len(result["records"]) == 18
    assert all(r["error_code"] == "wall_budget_exhausted" for r in result["records"])


@pytest.mark.parametrize("variant,expected", [("malformed", "invalid_model_json"),
    ("coverage", "assessment_claim_coverage_invalid"),
    ("inconsistent", "assessment_verdict_findings_inconsistent"),
    ("secret", "model_adapter_error")])
def test_failed_or_invalid_responses_are_errors_never_abstentions(variant, expected):
    def send(config, payload, timeout):
        if variant == "malformed":
            return HTTPResult(200, {"content-type": "application/json"}, b"not json")
        if variant == "coverage":
            return response(config, claims=["wrong"])
        if variant == "inconsistent":
            return response(config, verdict="ABSTAIN", findings=[])
        raise RuntimeError("SECRET-PROMPT-TEXT")
    result = run(allow_model_calls=True, transport=send)
    rows = [r for r in result["records"] if r["system_id"] == "local-a"]
    assert all(r["observed_verdict"] == "ERROR" and r["error_code"] == expected for r in rows)
    assert all(r["assessment_sha256"] is None for r in rows)
    assert "SECRET-PROMPT-TEXT" not in json.dumps(result)


@pytest.mark.parametrize("opt_in", [1, "yes", None])
def test_model_opt_in_requires_boolean(opt_in):
    with pytest.raises(BenchmarkError, match="model_call_opt_in_invalid"):
        run(allow_model_calls=opt_in)


def test_transport_must_be_callable():
    with pytest.raises(BenchmarkError, match="transport_not_callable"):
        run(transport="not-callable")


def test_reference_transport_mode_without_real_network():
    # Patch the adapter used by run_local, while exercising its default branch.
    with patch("keel_eval.evaluation.loopback_transport", side_effect=lambda cfg, body, timeout: response(cfg)):
        result = run(allow_model_calls=True)
    assert result["mode"] == "LOCAL_LOOPBACK"
    assert result["model_quality_validated"] is False


def test_bounded_total_rows_before_running():
    data = dataset()
    base = data["cases"][0]
    data["cases"] = [dict(deepcopy(base), case_id="case-" + str(i)) for i in range(256)]
    roster = systems(extra=True)
    with pytest.raises(BenchmarkError, match="experiment_row_limit"):
        plan(data, roster, trials=20)


def test_report_record_contract_is_exact():
    result = run()
    expected = {"system_id", "trial", "case_id", "subject_sha256", "expected_verdict",
                "observed_verdict", "latency_ms", "error_code", "assessment_sha256",
                "request_sha256", "response_sha256", "model_calls_attempted"}
    assert all(set(row) == expected for row in result["records"])
    assert sum(row["model_calls_attempted"] for row in result["records"]) == result["model_calls_attempted"]
    assert [(row["system_id"], row["trial"], row["case_id"]) for row in result["records"]] == schedule(plan(), dataset())
    assert all(row["error_code"] is None or row["error_code"] in ALL_ERROR_CODES for row in result["records"])
    assert NO_CALL_ERROR_CODES <= ALL_ERROR_CODES


def test_transport_preflight_accounts_for_preprocessing_elapsed_time():
    clock = [0.0]
    real_run_local = __import__("keel_eval.evaluation", fromlist=["run_local"]).run_local
    def delayed_validation(*args, **kwargs):
        clock[0] = 1.1
        return real_run_local(*args, **kwargs)
    with patch("keel_bench.experiment.time.monotonic", side_effect=lambda: clock[0]):
        with patch("keel_bench.experiment.run_local", side_effect=delayed_validation):
            result = run(frozen=plan(max_wall_seconds=1), allow_model_calls=True,
                         transport=lambda *args: pytest.fail("preprocessing used remaining budget"))
    assert result["model_calls_attempted"] == 0
    assert result["stop_reason"] == "wall_budget_exhausted"
    models = [r for r in result["records"] if r["system_id"] == "local-a"]
    assert all(r["error_code"] == "wall_budget_exhausted" for r in models)
    assert all(r["request_sha256"] is None and r["model_calls_attempted"] == 0 for r in models)


def test_digest_rejects_recursive_or_non_json_inputs():
    recursive = []
    recursive.append(recursive)
    with pytest.raises(BenchmarkError):
        digest(recursive)
    with pytest.raises(BenchmarkError):
        digest({"value": object()})
