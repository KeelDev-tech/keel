"""Synthetic evaluator tests. No model, HTTP connection, browser, or approval."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from keel_agent.models import HTTPResult, ModelError, ReviewerConfig, reviewer_config_digest
from keel_eval import EvaluationError, dataset_digest, evaluate_replay, run_local, validate_dataset
from keel_eval.__main__ import main
from keel_eval import evaluation


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "grounding_eval"


def inputs():
    return (json.loads((FIXTURE / "dataset.json").read_text()),
            json.loads((FIXTURE / "replay.json").read_text()))


def assessment(ids, verdict="PASS"):
    return {"verdict": verdict, "covered_claim_ids": ids,
            "findings": [] if verdict == "PASS" else ["Synthetic finding."]}


def response(config, value, **changes):
    message = {"role": "assistant", "content": json.dumps(value)}
    body = ({"model": config.model, "created_at": "2030-01-01T00:00:00Z", "done": True,
             "done_reason": "stop", "message": message} if config.backend == "ollama" else
            {"model": config.model, "id": "synthetic-response", "choices": [
                {"finish_reason": "stop", "message": message}]})
    body.update(changes)
    return HTTPResult(200, {"Content-Type": "application/json"}, json.dumps(body).encode())


def test_synthetic_replay_is_bound_and_honest_without_transport(monkeypatch):
    dataset, replay = inputs()
    monkeypatch.setattr(evaluation, "loopback_transport", lambda *_: pytest.fail("unexpected model request"))
    report = evaluate_replay(dataset, replay)
    assert report["case_count"] == 9
    assert report["metrics"]["exact_label_agreement"] == {"numerator": 9, "denominator": 9, "value": 1.0}
    assert report["dataset_sha256"] == replay["dataset_sha256"]
    assert report["model_config_sha256"] == reviewer_config_digest(ReviewerConfig(**replay["model_config"]))
    assert report["mode"] == "REPLAY" and report["synthetic"] is True
    assert report["model_inference"] == "NOT_RUN"
    assert report["model_calls_attempted"] == report["transport_calls_attempted"] == 0
    assert report["canonical_writes"] == 0
    for key in ("model_quality_validated", "quality_release_gate_satisfied", "execution_authorized",
                "model_weights_attested", "model_identity_hardware_verified", "labels_independently_verified",
                "split_independence_verified"):
        assert report[key] is False
    assert report["latency"]["basis"] == "caller_supplied"


def test_false_pass_false_blocks_abstentions_and_errors_keep_all_denominators():
    dataset, replay = inputs()
    for index, verdict in {0: "FAIL", 1: "PASS", 2: "PASS", 7: "ABSTAIN"}.items():
        replay["results"][index]["assessment"] = assessment(dataset["cases"][index]["subject"]["required_claim_ids"], verdict)
    replay["results"][4] = {"case_id": dataset["cases"][4]["case_id"], "error_code": "timeout", "latency_ms": None}
    replay["results"][8]["assessment"]["covered_claim_ids"] = ["claim-1"]
    report = evaluate_replay(dataset, replay)
    metrics = report["metrics"]
    assert metrics["false_pass_on_nonpass_labels"] == {"numerator": 2, "denominator": 7, "value": 2 / 7}
    assert metrics["false_pass_among_pass_predictions"]["value"] == 1
    assert metrics["false_block_on_pass_labels"]["value"] == .5
    assert metrics["withheld_supported_cases"]["value"] == 1
    assert metrics["abstentions"]["numerator"] == 3
    assert metrics["errors"] == {"numerator": 2, "denominator": 9, "value": 2 / 9}
    assert metrics["validated_response_coverage"]["numerator"] == 7
    assert metrics["exact_label_agreement"]["numerator"] == 3
    assert report["confusion_matrix"]["ABSTAIN"]["ERROR"] == 2
    assert report["latency"]["count"] == 8 and report["latency"]["missing_count"] == 1
    assert report["latency"]["p95_ms"] == 9
    assert report["cases"][8]["error_code"] == "assessment_claim_coverage_invalid"


def test_absent_gold_class_and_no_predictions_are_null_not_perfect():
    dataset, replay = inputs()
    dataset["cases"] = [dataset["cases"][2]]
    replay["results"] = [replay["results"][2]]
    replay["dataset_sha256"] = dataset_digest(dataset)
    metrics = evaluate_replay(dataset, replay)["metrics"]
    assert metrics["false_block_on_pass_labels"] == {"numerator": 0, "denominator": 0, "value": None}
    assert metrics["false_pass_among_pass_predictions"]["value"] is None


@pytest.mark.parametrize("change", ["missing", "unknown", "duplicate"])
def test_replay_requires_exact_case_coverage(change):
    dataset, replay = inputs()
    if change == "missing":
        replay["results"].pop()
    elif change == "unknown":
        replay["results"][0]["case_id"] = "not-a-case"
    else:
        replay["results"].append(deepcopy(replay["results"][0]))
    with pytest.raises(EvaluationError, match="replay_case"):
        evaluate_replay(dataset, replay)


def test_replay_order_does_not_change_case_binding():
    dataset, replay = inputs()
    expected = evaluate_replay(dataset, replay)
    replay["results"].reverse()
    actual = evaluate_replay(dataset, replay)
    assert actual["cases"] == expected["cases"]
    assert actual["metrics"] == expected["metrics"]
    assert actual["replay_sha256"] != expected["replay_sha256"]


@pytest.mark.parametrize("change", ["label", "rationale", "text", "synthetic"])
def test_transcript_cannot_be_rescored_against_mutated_dataset(change):
    dataset, replay = inputs()
    if change == "label":
        dataset["cases"][0]["expected_verdict"] = "FAIL"
    elif change == "rationale":
        dataset["cases"][0]["label_rationale"] += " Altered."
    elif change == "text":
        dataset["cases"][0]["subject"]["claims"][0]["text"] += " Altered."
    else:
        dataset.update(synthetic=False, label_source="operator_supplied")
    with pytest.raises(EvaluationError, match="replay_dataset_mismatch"):
        evaluate_replay(dataset, replay)


@pytest.mark.parametrize("value", [-1, True, None, float("nan"), float("inf"), 86400001, "12"])
def test_invalid_latency_rejected(value):
    dataset, replay = inputs()
    replay["results"][0]["latency_ms"] = value
    with pytest.raises(EvaluationError):
        evaluate_replay(dataset, replay)


@pytest.mark.parametrize("change", ["extra_key", "duplicate_id", "empty", "too_many", "missing_claim",
                                   "duplicate_claim", "duplicate_evidence", "label", "bool", "large", "subject_size"])
def test_dataset_rejects_unbounded_or_ambiguous_contracts(change):
    dataset, _ = inputs()
    if change == "extra_key":
        dataset["execution_authorized"] = True
    elif change == "duplicate_id":
        dataset["cases"].append(deepcopy(dataset["cases"][0]))
    elif change == "empty":
        dataset["cases"] = []
    elif change == "too_many":
        dataset["cases"] *= 30
    elif change == "missing_claim":
        dataset["cases"][0]["subject"]["claims"] = []
    elif change == "duplicate_claim":
        dataset["cases"][8]["subject"]["claims"][1]["claim_id"] = "claim-1"
    elif change == "duplicate_evidence":
        dataset["cases"][3]["subject"]["evidence"][1]["evidence_id"] = "evidence-1"
    elif change == "label":
        dataset["cases"][0]["expected_verdict"] = "APPROVED"
    elif change == "bool":
        dataset["synthetic"] = 1
    elif change == "large":
        dataset["cases"][0]["subject"]["claims"][0]["text"] = "a" * 16385
    else:
        dataset["cases"][0]["subject"]["evidence"] = [
            {"evidence_id": f"e-{i}", "text": "a" * 16384} for i in range(17)]
    with pytest.raises(EvaluationError):
        validate_dataset(dataset)


def test_cyclic_input_is_rejected_before_json_serialization():
    value = {}
    value["recursive"] = value
    with pytest.raises(EvaluationError, match="json_structure_limit"):
        validate_dataset(value)


@pytest.mark.parametrize("bad_assessment", [
    {"verdict": "PASS", "covered_claim_ids": ["claim-1"], "findings": [], "authorized": True},
    {"verdict": "PASS", "covered_claim_ids": ["claim-1", "claim-1"], "findings": []},
    {"verdict": "PASS", "covered_claim_ids": ["claim-1"], "findings": ["unresolved"]},
    {"verdict": "ABSTAIN", "covered_claim_ids": ["claim-1"], "findings": []},
    None,
])
def test_malformed_assessments_are_errors_never_model_abstentions(bad_assessment):
    dataset, replay = inputs()
    replay["results"][0]["assessment"] = bad_assessment
    report = evaluate_replay(dataset, replay)
    assert report["cases"][0]["observed_verdict"] == "ERROR"
    assert report["metrics"]["errors"]["numerator"] == 1
    assert report["metrics"]["abstentions"]["numerator"] == 5


@pytest.mark.parametrize("backend", ["ollama", "llama_cpp"])
def test_injected_local_evaluation_hides_gold_and_identifies_test(backend):
    dataset, replay = inputs()
    config_data = replay["model_config"]
    config_data.update(backend=backend, endpoint="http://127.0.0.1:11434/api/chat" if backend == "ollama" else
                       "http://[::1]:8080/v1/chat/completions")
    cfg = ReviewerConfig(**config_data)
    sent = []
    def transport(config, payload, timeout):
        sent.append(payload)
        decoded = json.loads(payload["messages"][1]["content"])
        assert set(decoded) == {"phase", "subject_sha256", "subject"}
        serialized = json.dumps(payload)
        for field in ("expected_verdict", "label_rationale", "case_id", "dataset_id", "dataset_sha256"):
            assert field not in serialized
        assert "untrusted" in payload["messages"][0]["content"]
        return response(config, assessment(decoded["subject"]["required_claim_ids"], "ABSTAIN"))
    report = run_local(dataset, cfg, declared_weights_sha256="a" * 64, transport=transport)
    assert len(sent) == 9 and report["transport_calls_attempted"] == 9
    assert report["model_calls_attempted"] == 0 and report["model_inference"] == "NOT_RUN"
    assert report["mode"] == "INJECTED_TRANSPORT" and not report["model_quality_validated"]
    assert report["declared_weights_sha256"] == "a" * 64 and not report["model_weights_attested"]
    assert report["latency"]["basis"] == "monotonic_wall_time"
    assert all(row["request_sha256"] and row["response_sha256"] for row in report["cases"])
    assert report["metrics"]["validated_response_coverage"]["value"] == 1
    assert report["metrics"]["abstentions"]["value"] == 1


def test_http_429_hard_stops_remaining_cases_without_retry():
    dataset, replay = inputs()
    calls = []
    def transport(*args):
        calls.append(args)
        return HTTPResult(429, {}, b"")
    report = run_local(dataset, ReviewerConfig(**replay["model_config"]), transport=transport)
    assert len(calls) == report["transport_calls_attempted"] == 1
    assert report["metrics"]["errors"]["numerator"] == 9
    assert report["latency"]["count"] == 1 and report["latency"]["missing_count"] == 8
    assert report["cases"][0]["error_code"] == "model_http_429"
    assert {row["error_code"] for row in report["cases"][1:]} == {"not_run_after_rate_limit"}


@pytest.mark.parametrize("error", [OSError("sensitive-source-prose"), ModelError("sensitive-source-prose"),
                                 RuntimeError("sensitive-source-prose")])
def test_exception_text_is_not_reflected_in_reports(error):
    dataset, replay = inputs()
    def transport(*_):
        raise error
    report = run_local(dataset, ReviewerConfig(**replay["model_config"]), transport=transport)
    assert report["metrics"]["errors"]["numerator"] == 9
    assert "sensitive-source-prose" not in json.dumps(report)
    assert {row["error_code"] for row in report["cases"]} == {"model_adapter_error"}


@pytest.mark.parametrize("problem", ["identity", "incomplete", "tool", "syntax", "coverage", "non_json", "redirect"])
def test_local_protocol_failures_have_error_coverage(problem):
    dataset, replay = inputs()
    def transport(config, payload, timeout):
        ids = json.loads(payload["messages"][1]["content"])["subject"]["required_claim_ids"]
        if problem == "redirect":
            return HTTPResult(302, {"Location": "https://example.invalid"}, b"")
        if problem == "non_json":
            return HTTPResult(200, {"Content-Type": "text/plain"}, b"PASS")
        changes = {"model": "wrong-model"} if problem == "identity" else {"done": False} if problem == "incomplete" else {}
        if problem == "tool":
            changes["message"] = {"role": "assistant", "content": json.dumps(assessment(ids)), "tool_calls": [{"name": "shell"}]}
        if problem == "syntax":
            changes["message"] = {"role": "assistant", "content": '{"verdict":"PASS","verdict":"FAIL"}'}
        return response(config, assessment([] if problem == "coverage" else ids), **changes)
    report = run_local(dataset, ReviewerConfig(**replay["model_config"]), transport=transport)
    assert report["metrics"]["errors"]["value"] == 1
    assert report["metrics"]["false_pass_on_nonpass_labels"]["numerator"] == 0
    assert report["metrics"]["validated_response_coverage"]["value"] == 0


def test_dataset_is_snapshotted_before_a_transport_can_mutate_caller_input():
    dataset, replay = inputs()
    original = dataset_digest(dataset)
    def transport(config, payload, timeout):
        dataset["cases"].clear()
        ids = json.loads(payload["messages"][1]["content"])["subject"]["required_claim_ids"]
        return response(config, assessment(ids))
    report = run_local(dataset, ReviewerConfig(**replay["model_config"]), transport=transport)
    assert report["case_count"] == 9 and report["dataset_sha256"] == original


def test_invalid_dataset_never_reaches_transport():
    dataset, replay = inputs()
    dataset["cases"].append(deepcopy(dataset["cases"][0]))
    with pytest.raises(EvaluationError):
        run_local(dataset, ReviewerConfig(**replay["model_config"]), transport=lambda *_: pytest.fail("called"))


def test_all_error_replay_does_not_drop_cases_or_invent_latency():
    dataset, replay = inputs()
    replay["results"] = [{"case_id": case["case_id"], "error_code": "not_run", "latency_ms": None}
                         for case in dataset["cases"]]
    report = evaluate_replay(dataset, replay)
    assert report["metrics"]["errors"]["value"] == 1
    assert report["metrics"]["validated_response_coverage"]["value"] == 0
    assert report["metrics"]["exact_label_agreement"] == {"numerator": 0, "denominator": 9, "value": 0}
    assert report["latency"]["count"] == 0 and report["latency"]["missing_count"] == 9
    assert report["latency"]["p50_ms"] is None and report["latency"]["mean_ms"] is None


def test_declared_weight_identity_must_be_a_hash_before_any_call():
    dataset, replay = inputs()
    with pytest.raises(EvaluationError, match="declared_weights_sha256_invalid"):
        run_local(dataset, ReviewerConfig(**replay["model_config"]), declared_weights_sha256="installed-local",
                  transport=lambda *_: pytest.fail("called"))


@pytest.mark.parametrize("endpoint", ["https://example.invalid/api/chat", "http://localhost:11434/api/chat",
                                    "http://127.0.0.1:11434/api/chat?cloud=true"])
def test_replay_cannot_declare_a_nonlocal_config(endpoint):
    dataset, replay = inputs()
    replay["model_config"]["endpoint"] = endpoint
    with pytest.raises(EvaluationError, match="model_config_invalid"):
        evaluate_replay(dataset, replay)


def test_cli_replay_exclusive_private_output_and_no_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "loopback_transport", lambda *_: pytest.fail("unexpected request"))
    path = tmp_path / "report.json"
    args = ["replay", str(FIXTURE / "dataset.json"), str(FIXTURE / "replay.json"), "--out", str(path)]
    assert main(args) == 0
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert main(args) == 2
    assert path.read_bytes() == original


def test_cli_rejects_duplicate_json_keys_before_scoring(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema":"a","schema":"b"}')
    assert main(["replay", str(path), str(FIXTURE / "replay.json")]) == 2


def test_cli_errors_are_a_nonzero_completion_not_a_pass(tmp_path):
    dataset, replay = inputs()
    replay["results"][0]["assessment"] = None
    path = tmp_path / "bad-output.json"
    path.write_text(json.dumps(replay))
    assert main(["replay", str(FIXTURE / "dataset.json"), str(path), "--out", str(tmp_path / "report.json")]) == 3
