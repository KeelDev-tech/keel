"""Independent adversarial review of benchmark evidence consistency.

These tests inject protocol responses only. They do not establish real model
quality, a rendered browser, or authenticity of caller-supplied reports.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib

import pytest

from keel_agent.browser import contract_hash
from keel_agent.models import HTTPResult, ReviewerConfig
from keel_bench.comparison import analyze_run, validate_run
from keel_bench.experiment import BenchmarkError, make_plan, plan_digest, run_experiment
from keel_bench.host import _readback_valid


def _limited_run():
    data = {
        "schema": "keel.eval.dataset.v1", "dataset_id": "review-gold",
        "synthetic": True, "split": "held_out", "label_source": "synthetic_fixture",
        "cases": [{"case_id": "case-" + str(index), "tags": ["review"],
                   "expected_verdict": verdict, "label_rationale": "Synthetic label.",
                   "subject": {"required_claim_ids": ["claim"],
                               "claims": [{"claim_id": "claim", "text": "Claim " + str(index)}],
                               "evidence": []}}
                  for index, verdict in enumerate(("PASS", "FAIL", "ABSTAIN"))],
    }
    systems = [{"system_id": "baseline", "kind": "abstain_baseline",
                "model_config": None, "declared_weights_sha256": None},
               {"system_id": "model", "kind": "local_model",
                "model_config": asdict(ReviewerConfig("review", "ollama",
                    "http://127.0.0.1:11434/api/chat", "review-fixture", timeout_seconds=.05)),
                "declared_weights_sha256": None}]
    plan = make_plan(data, systems, experiment_id="independent-review",
                     source_sha256="1" * 64, trials=2)
    pin = plan_digest(plan)
    report = run_experiment(data, plan, expected_plan_sha256=pin,
        source_sha256="1" * 64, allow_model_calls=True,
        transport=lambda *args: HTTPResult(429, {}, b"Do not reveal this body"))
    return data, plan, pin, report


def _skipped(report):
    return next(row for row in report["records"] if row["error_code"] == "not_run_after_rate_limit")


@pytest.mark.parametrize("mutation", ["resume_after_429", "erase_stop_reason", "reverse_schedule",
                                     "unattempted_exchange", "unknown_error", "skip_without_429"])
def test_inconsistent_rate_limit_reports_cannot_be_analyzed(mutation):
    data, plan, pin, report = _limited_run()
    if mutation == "resume_after_429":
        _skipped(report).update(observed_verdict="PASS", error_code=None,
            assessment_sha256="2" * 64, request_sha256="3" * 64,
            response_sha256="4" * 64, latency_ms=1, model_calls_attempted=1)
        report["model_calls_attempted"] += 1
    elif mutation == "erase_stop_reason":
        report["stop_reason"] = None
    elif mutation == "reverse_schedule":
        report["records"].reverse()
    elif mutation == "unattempted_exchange":
        _skipped(report).update(request_sha256="3" * 64, response_sha256="4" * 64)
    elif mutation == "unknown_error":
        _skipped(report)["error_code"] = "arbitrary_unknown_error"
    else:
        next(row for row in report["records"] if row["error_code"] == "model_http_429")["error_code"] = "model_adapter_error"
        report["stop_reason"] = None
    with pytest.raises(BenchmarkError):
        analyze_run(data, plan, report, expected_plan_sha256=pin, bootstrap_samples=100)


def test_valid_429_report_retains_all_failures_and_truth_boundaries():
    data, plan, pin, report = _limited_run()
    result = analyze_run(data, plan, report, expected_plan_sha256=pin, bootstrap_samples=100)
    assert result["record_count"] == 12
    model = next(row for row in result["systems"] if row["system_id"] == "model")
    assert model["metrics"]["errors"] == {"numerator": 6, "denominator": 6, "value": 1}
    assert model["metrics"]["exact_label_agreement"]["denominator"] == 6
    assert result["report_authenticity_verified"] is False
    assert result["state_of_the_art_established"] is False
    assert result["heldout_independence_verified"] is False
    assert result["execution_authorized"] is False


def test_plan_pin_cannot_be_replaced_by_report_owned_pin():
    data, plan, original_pin, report = _limited_run()
    changed = deepcopy(plan)
    changed["systems"][1]["model_config"]["model"] = "different-config"
    report["plan"] = changed
    report["plan_sha256"] = plan_digest(changed)
    with pytest.raises(BenchmarkError):
        validate_run(data, changed, report, expected_plan_sha256=original_pin)


@pytest.mark.parametrize("size", [True, 1.0])
def test_rendered_attachment_size_requires_an_integer_observation(size):
    # Protocol evidence must preserve JSON types: True and 1.0 are not a
    # measured integer byte count, even though Python compares them equal.
    packet = b"X"
    contract = {"mode": "local_fixture", "account_id": "account",
                "origin": "http://127.0.0.1:12345", "url": "http://127.0.0.1:12345/apply",
                "allowed_origins": ["http://127.0.0.1:12345"], "fixture_nonce": "a" * 64,
                "fields": [{"label": "Motivation", "value": "X", "kind": "text"}],
                "attachment": {"label": "Résumé", "name": "answer.txt", "mime_type": "text/plain",
                               "base64": "WA=="}}
    output = {"status": "PREPARED", "bundle_hash": contract_hash(contract),
              "account_id": "account", "origin": contract["origin"],
              "form_fingerprint": "a" * 64,
              "readback": {"Motivation": "X", "attachment": {
                  "name": "answer.txt", "size": size, "sha256": hashlib.sha256(packet).hexdigest()}},
              "submitted": False, "execution_authorized": False}
    assert _readback_valid(output, contract, packet) is False
