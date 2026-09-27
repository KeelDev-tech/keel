"""No live models: resource governance at the actual dispatch boundary."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from keel_agent import models
from keel_agent.models import HTTPResult, ReviewerConfig, reviewer_config_digest, run_blind_review
from keel_efficiency.ledger import BudgetExceeded, ResourceLedger
from keel_efficiency.transport import GovernedTransport
from keel_efficiency.usage import estimate_resources, parse_usage
from keel_workflow.reviews import ReviewStore


NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


def config(reviewer_id="reviewer_a", backend="ollama"):
    endpoint = "http://127.0.0.1:11434/api/chat" if backend == "ollama" else "http://127.0.0.1:8080/v1/chat/completions"
    return ReviewerConfig(reviewer_id, backend, endpoint, "local-model:1", max_tokens=128)


def payload(cfg):
    request = {"model": cfg.model, "stream": False, "messages": [{"role": "user", "content": "evidence"}]}
    request.update({"options": {"num_predict": cfg.max_tokens}} if cfg.backend == "ollama" else {"max_tokens": cfg.max_tokens})
    return request


def response(cfg, *, counts=True, invalid_assessment=False, **updates):
    message = {"role": "assistant", "content": json.dumps({"verdict": "PASS", "covered_claim_ids": ["claim"], "findings": []})}
    if invalid_assessment:
        message["content"] = "invalid assessment"
    if cfg.backend == "ollama":
        data = {"model": cfg.model, "created_at": NOW.isoformat(), "done": True, "message": message}
        if counts:
            data.update(prompt_eval_count=31, eval_count=9)
    else:
        data = {"model": cfg.model, "id": "response-1", "choices": [{"finish_reason": "stop", "message": message}]}
        if counts:
            data["usage"] = {"prompt_tokens": 31, "completion_tokens": 9, "total_tokens": 40,
                             "prompt_tokens_details": {"cached_tokens": 20}}
    data.update(updates)
    return HTTPResult(200, {"Content-Type": "application/json"}, json.dumps(data).encode())


def budget(tmp_path, calls=20):
    ledger = ResourceLedger(tmp_path / "resources.sqlite")
    ledger.create_scope("shared", {"calls": calls, "input_tokens": 1000000, "output_tokens": 100000,
                                   "compute_ms": 1000000, "external_credit_micros": 0})
    return ledger


def run(tmp_path, transport, ledger=None):
    reviewers = [config(), config("reviewer_b", "llama_cpp")]
    subject = {"required_claim_ids": ["claim"], "facts": {"claim": "source"},
               "reviewer_config_sha256": {cfg.reviewer_id: reviewer_config_digest(cfg) for cfg in reviewers}}
    return run_blind_review(ReviewStore(tmp_path / "reviews.sqlite"), round_id="review", subject=subject,
                           reviewers=reviewers, expires_at=NOW + timedelta(seconds=600), clock=lambda: NOW,
                           transport=transport, ledger=ledger, scope_id="shared" if ledger else None)


@pytest.mark.parametrize("bad", [True, False, -1, 1.0, "10", None, float("nan"), float("inf"), 2**53])
@pytest.mark.parametrize("backend,field", [("ollama", "prompt_eval_count"), ("llama_cpp", "prompt_tokens")])
def test_counts_reject_coercion_and_invalid_numbers(bad, backend, field):
    envelope = {field: bad} if backend == "ollama" else {"usage": {field: bad}}
    parsed = parse_usage(backend, envelope)
    assert parsed["status"] == "INVALID"
    assert parsed["input_tokens"] is None and parsed["output_tokens"] is None


def test_missing_zero_partial_and_cached_counts_are_distinct():
    assert parse_usage("ollama", {})["status"] == "UNKNOWN"
    assert parse_usage("ollama", {"prompt_eval_count": 0})["status"] == "PARTIAL"
    counts = parse_usage("llama_cpp", {"usage": {"prompt_tokens": 100, "completion_tokens": 20,
        "total_tokens": 120, "prompt_tokens_details": {"cached_tokens": 80},
        "completion_tokens_details": {"reasoning_tokens": 10}}})
    assert counts["input_tokens"] == 100 and counts["output_tokens"] == 20
    assert counts["cached_input_tokens"] == 80 and counts["reasoning_output_tokens"] == 10
    assert counts["provenance"] == "backend_reported_unverified"


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 11},
    {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 11}},
    {"completion_tokens": 2, "completion_tokens_details": {"reasoning_tokens": 3}},
    {"prompt_tokens": 10, "prompt_tokens_details": None},
])
def test_inconsistent_usage_retains_unknown(usage):
    assert parse_usage("llama_cpp", {"usage": usage})["status"] == "INVALID"


def test_utf8_allowance_and_no_credit_conversion():
    cfg = config()
    data = payload(cfg)
    data["messages"][0]["content"] = "é漢"
    estimate = estimate_resources(data, max_tokens=128, timeout_seconds=0.051)
    assert estimate["input_tokens"] >= len(json.dumps(data, ensure_ascii=False).encode())
    assert estimate["compute_ms"] == 51 and estimate["external_credit_micros"] == 0


def test_reserves_before_dispatch_and_settles_actual_without_cache_discount(tmp_path):
    ledger = budget(tmp_path)
    cfg = config(backend="llama_cpp")
    def send(cfg, *_):
        request = ledger.request(governed.last_receipt["request_id"])
        assert request["state"] == "DISPATCHED"
        assert ledger.snapshot("shared")["reserved"]["calls"] == 1
        return response(cfg)
    governed = GovernedTransport(ledger, "shared", send)
    governed(cfg, payload(cfg), 1)
    assert governed.last_receipt["state"] == "SETTLED"
    assert governed.last_receipt["usage"]["input_tokens"] == 31
    assert governed.last_usage["cached_input_tokens"] == 20
    assert ledger.snapshot("shared")["used"]["input_tokens"] == 31


def test_budget_denial_never_calls_transport(tmp_path):
    ledger = budget(tmp_path, calls=0)
    transport = GovernedTransport(ledger, "shared", lambda *_: pytest.fail("network must not run"))
    with pytest.raises(BudgetExceeded):
        transport(config(), payload(config()), 1)
    assert transport.last_receipt is None
    assert ledger.snapshot("shared")["used"]["calls"] == 0


@pytest.mark.parametrize("failure", [TimeoutError, OSError, KeyboardInterrupt])
def test_dispatched_failure_and_interruption_keep_unknown_token_reservations(tmp_path, failure):
    ledger = budget(tmp_path)
    def failed(*_):
        raise failure("private text")
    governed = GovernedTransport(ledger, "shared", failed)
    with pytest.raises(failure):
        governed(config(), payload(config()), 1)
    receipt = governed.last_receipt
    assert receipt["state"] == "UNKNOWN" and receipt["usage"]["calls"] == 1
    assert receipt["remaining"]["input_tokens"] > 0 and receipt["remaining"]["output_tokens"] == 128
    assert receipt["usage"]["input_tokens"] is None
    assert "private text" not in json.dumps(receipt)


def test_missing_and_partial_usage_do_not_refund_unknown_dimensions(tmp_path):
    ledger = budget(tmp_path)
    governed = GovernedTransport(ledger, "shared", lambda cfg, *_: response(cfg, counts=False, prompt_eval_count=7))
    governed(config(), payload(config()), 1)
    assert governed.last_receipt["state"] == "UNKNOWN"
    assert governed.last_receipt["usage"]["input_tokens"] == 7
    assert governed.last_receipt["remaining"]["output_tokens"] == 128


def test_governed_review_meters_all_four_calls_and_retains_metadata(tmp_path):
    ledger = budget(tmp_path)
    result = run(tmp_path, lambda cfg, *_: response(cfg), ledger)
    assert result["resource_budget_enforced"] is True
    assert result["evaluation"]["state"] == "READY_FOR_HUMAN_REVIEW"
    assert ledger.snapshot("shared")["used"]["calls"] == 4
    assert all(call["resource_reservation"]["state"] == "SETTLED" for call in result["calls"])
    assert len({call["resource_reservation"]["request_id"] for call in result["calls"]}) == 4


def test_governed_review_stops_on_budget_with_persisted_abstention(tmp_path):
    ledger = budget(tmp_path, calls=1)
    sent = []
    def send(cfg, *_):
        sent.append(cfg.reviewer_id)
        return response(cfg)
    result = run(tmp_path, send, ledger)
    assert sent == ["reviewer_a"]
    assert result["evaluation"]["state"] != "READY_FOR_HUMAN_REVIEW"
    assert result["stopped_reason"] == "resource_budget_hold"
    assert result["calls"][-1]["error_code"] == "resource_budget_exceeded"


def test_invalid_assessment_still_charges_reported_usage(tmp_path):
    ledger = budget(tmp_path)
    result = run(tmp_path, lambda cfg, *_: response(cfg, invalid_assessment=True), ledger)
    assert result["evaluation"]["state"] == "HOLD"
    assert ledger.snapshot("shared")["used"]["output_tokens"] == 36


def test_truthful_overage_locks_further_dispatch(tmp_path):
    ledger = budget(tmp_path)
    cfg = config()
    governed = GovernedTransport(ledger, "shared", lambda cfg, *_: response(cfg, eval_count=200))
    governed(cfg, payload(cfg), 1)
    assert ledger.snapshot("shared")["used"]["output_tokens"] == 200
    assert ledger.snapshot("shared")["locked"] is True
    with pytest.raises(BudgetExceeded):
        governed(cfg, payload(cfg), 1)


def test_legacy_review_explicitly_marks_ungoverned(tmp_path):
    result = run(tmp_path, lambda cfg, *_: response(cfg))
    assert result["resource_budget_enforced"] is False
    assert all(call["resource_accounting_status"] == "LEGACY_UNGOVERNED" for call in result["calls"])


def test_environment_boundary_and_explicit_wrapper_do_not_double_charge(tmp_path, monkeypatch):
    ledger = budget(tmp_path)
    monkeypatch.setenv("KEEL_BUDGET_LEDGER", str(ledger.path))
    monkeypatch.setenv("KEEL_BUDGET_SCOPE", "shared")
    monkeypatch.setattr(models, "_loopback_transport_raw", lambda cfg, *_: response(cfg))
    cfg = config()
    models.loopback_transport(cfg, payload(cfg), 1)
    wrapped = GovernedTransport(ledger, "shared", lambda *args: models.loopback_transport(*args))
    wrapped(cfg, payload(cfg), 1)
    assert ledger.snapshot("shared")["used"]["calls"] == 2


def test_environment_mismatch_and_absent_scope_block_before_transport(tmp_path, monkeypatch):
    ledger = budget(tmp_path)
    monkeypatch.setattr(models, "_loopback_transport_raw", lambda *_: pytest.fail("must not dispatch"))
    monkeypatch.setenv("KEEL_BUDGET_LEDGER", str(ledger.path))
    monkeypatch.delenv("KEEL_BUDGET_SCOPE", raising=False)
    with pytest.raises(models.ModelError, match="budget_environment_incomplete"):
        models.loopback_transport(config(), payload(config()), 1)
    monkeypatch.setenv("KEEL_BUDGET_SCOPE", "absent")
    with pytest.raises(ValueError):
        models.loopback_transport(config(), payload(config()), 1)


def test_nested_send_cannot_cover_two_calls_with_one_reservation(tmp_path, monkeypatch):
    ledger = budget(tmp_path)
    sent = []
    monkeypatch.setattr(models, "_loopback_transport_raw", lambda cfg, *_: sent.append(1) or response(cfg))
    def twice(*args):
        models.loopback_transport(*args)
        return models.loopback_transport(*args)
    governed = GovernedTransport(ledger, "shared", twice)
    with pytest.raises(ValueError, match="one loopback send"):
        governed(config(), payload(config()), 1)
    assert len(sent) == 1
    assert governed.last_receipt["usage"]["calls"] == 1


@pytest.mark.parametrize("change", ["config", "payload", "timeout"])
def test_nested_send_cannot_replace_reserved_request(tmp_path, monkeypatch, change):
    ledger = budget(tmp_path)
    monkeypatch.setattr(models, "_loopback_transport_raw", lambda *_: pytest.fail("changed call must not send"))
    def modified(cfg, data, timeout):
        if change == "config":
            cfg = ReviewerConfig(cfg.reviewer_id, cfg.backend, cfg.endpoint, "other-model", max_tokens=128)
        if change == "payload":
            data = dict(data, options={"num_predict": 8192})
        if change == "timeout":
            timeout += 1
        return models.loopback_transport(cfg, data, timeout)
    governed = GovernedTransport(ledger, "shared", modified)
    with pytest.raises(ValueError, match="resource binding"):
        governed(config(), payload(config()), 1)
    assert governed.last_receipt["state"] == "UNKNOWN"


@pytest.mark.parametrize("change", [{"max_tokens": 1000000}, {"max_tokens": True}, {"stream": True}, {"model": "other"}])
def test_payload_cannot_exceed_bound_config(tmp_path, change):
    cfg = config(backend="llama_cpp")
    governed = GovernedTransport(budget(tmp_path), "shared", lambda *_: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError):
        governed(cfg, dict(payload(cfg), **change), 1)


def test_ollama_requires_its_actual_output_cap_field(tmp_path):
    cfg = config()
    data = payload(cfg)
    del data["options"]
    data["max_tokens"] = 128  # llama.cpp cap is ignored by an Ollama server.
    governed = GovernedTransport(budget(tmp_path), "shared", lambda *_: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError, match="output cap"):
        governed(cfg, data, 1)
