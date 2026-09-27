"""Quality routes reach the real adapter boundary only after shared reservation."""
from dataclasses import replace
import json

import pytest

from keel_agent.models import HTTPResult, ReviewerConfig, reviewer_config_digest
from keel_efficiency.execution import run_qualified_preparation
from keel_efficiency.ledger import ResourceLedger
from keel_efficiency.policy import RouteEvidence


HASH = "a" * 64
SUBJECT = {"required_claim_ids": ["claim"], "claims": [{"claim_id": "claim", "text": "Fixture fact."}],
           "evidence": [{"evidence_id": "source", "text": "Fixture record."}]}


def config(name):
    return ReviewerConfig(name, "ollama", "http://127.0.0.1:11434/api/chat",
                          "installed-" + name, timeout_seconds=1, max_tokens=128)


def evidence(cfg, cost=1):
    return RouteEvidence(cfg.reviewer_id, "preparation", reviewer_config_digest(cfg), HASH, HASH,
                         True, cost, 1000, 990, 1000, "low", True, True)


def envelope(cfg, *, usage=True, verdict="PASS", **changes):
    assessment = {"verdict": verdict, "covered_claim_ids": ["claim"],
                  "findings": [] if verdict == "PASS" else ["Fixture finding."]}
    body = {"model": cfg.model, "done": True, "created_at": "fixture-id",
            "message": {"role": "assistant", "content": json.dumps(assessment)}}
    if usage:
        body.update(prompt_eval_count=10, eval_count=10)
    body.update(changes)
    return HTTPResult(200, {"content-type": "application/json"}, json.dumps(body).encode())


def setup(tmp_path, **limit_changes):
    ledger = ResourceLedger(tmp_path / "budget.sqlite")
    limits = dict(calls=5, input_tokens=100000, output_tokens=1000, compute_ms=10000, external_credit_micros=0)
    limits.update(limit_changes)
    ledger.create_scope("shared", limits)
    small, large = config("small"), config("large")
    routes = [evidence(large, 3), evidence(small)]
    options = dict(context_key="preparation", budget_units=5,
                   current_bindings={r.route_id: r.bindings() for r in routes},
                   ledger=ledger, scope_id="shared")
    configs = {c.reviewer_id: c for c in (small, large)}
    return ledger, routes, configs, options


def test_cheapest_qualified_route_dispatches_once_after_durable_reservation(tmp_path):
    ledger, routes, configs, options = setup(tmp_path)
    seen = []
    def sender(cfg, payload, timeout):
        snapshot = ledger.snapshot("shared")
        assert snapshot["reserved"]["calls"] == 1
        assert ledger.snapshot()["requests_by_state"] == {"DISPATCHED": 1}
        assert payload["options"]["num_predict"] == cfg.max_tokens
        seen.append(cfg.reviewer_id)
        return envelope(cfg)
    result = run_qualified_preparation(SUBJECT, routes, configs, transport=sender, **options)
    assert result["status"] == "PREPARED"
    assert result["assessment"]["verdict"] == "PASS"
    assert seen == ["small"]
    assert result["execution_authorized"] is False
    assert result["replaces_mandatory_review"] is False
    assert result["model_truth_verified"] is False
    assert result["model_calls_attempted"] == 1
    assert ledger.snapshot("shared")["used"]["calls"] == 1
    assert ledger.snapshot("shared")["reserved"]["calls"] == 0


def test_every_eligible_configuration_is_checked_before_dispatch(tmp_path):
    ledger, routes, configs, options = setup(tmp_path)
    configs["large"] = replace(configs["large"], model="changed-local-model")
    result = run_qualified_preparation(SUBJECT, routes, configs, transport=lambda *a: pytest.fail("dispatch forbidden"), **options)
    assert result["reason"] == "eligible_model_configuration_mismatch"
    assert ledger.snapshot()["requests_by_state"] == {}


def test_stale_bindings_hold_without_dispatch(tmp_path):
    ledger, routes, configs, options = setup(tmp_path)
    options["current_bindings"] = {}
    result = run_qualified_preparation(SUBJECT, routes, configs, transport=lambda *a: pytest.fail("dispatch forbidden"), **options)
    assert result["reason"] == "no_qualified_route"
    assert result["model_calls_attempted"] == 0


@pytest.mark.parametrize("limits", [{"calls": 0}, {"output_tokens": 0}, {"input_tokens": 0}, {"compute_ms": 0}])
def test_each_shared_budget_dimension_blocks_before_transport(tmp_path, limits):
    ledger, routes, configs, options = setup(tmp_path, **limits)
    result = run_qualified_preparation(SUBJECT, routes, configs, transport=lambda *a: pytest.fail("dispatch forbidden"), **options)
    assert result["reason"] == "resource_budget_exceeded"
    assert result["model_calls_attempted"] == 0
    assert ledger.snapshot()["requests_by_state"] == {}


def test_unknown_usage_holds_and_retains_token_reservations(tmp_path):
    ledger, routes, configs, options = setup(tmp_path)
    result = run_qualified_preparation(SUBJECT, routes, configs,
                                       transport=lambda cfg, *a: envelope(cfg, usage=False), **options)
    assert result["status"] == "HOLD"
    assert result["reason"] == "usage_unresolved"
    assert result["resource_receipt"]["state"] == "UNKNOWN"
    snapshot = ledger.snapshot("shared")
    assert snapshot["reserved"]["input_tokens"] > 0
    assert snapshot["reserved"]["output_tokens"] == 128
    assert snapshot["used"]["calls"] == 1


def test_usage_overage_holds_and_locks_parent(tmp_path):
    ledger, routes, configs, options = setup(tmp_path)
    result = run_qualified_preparation(SUBJECT, routes, configs,
                                       transport=lambda cfg, *a: envelope(cfg, eval_count=129), **options)
    assert result["reason"] == "resource_reservation_overage"
    assert ledger.snapshot("shared")["locked"] is True


def test_no_retry_or_escalation_on_response_failure(tmp_path):
    ledger, routes, configs, options = setup(tmp_path)
    seen = []
    def sender(cfg, *args):
        seen.append(cfg.reviewer_id)
        return HTTPResult(429, {}, b"")
    result = run_qualified_preparation(SUBJECT, routes, configs, transport=sender, **options)
    assert result["status"] == "HOLD"
    assert seen == ["small"]
    assert ledger.snapshot("shared")["used"]["calls"] == 1


def test_missing_source_evidence_never_dispatches(tmp_path):
    ledger, routes, configs, options = setup(tmp_path)
    result = run_qualified_preparation({**SUBJECT, "evidence": []}, routes, configs,
                                       transport=lambda *a: pytest.fail("dispatch forbidden"), **options)
    assert result["reason"] == "source_evidence_missing"


@pytest.mark.parametrize("risk", ["medium", "high", "critical", None])
def test_non_low_risk_is_not_accepted(tmp_path, risk):
    ledger, routes, configs, options = setup(tmp_path)
    with pytest.raises(ValueError, match="low-risk"):
        run_qualified_preparation(SUBJECT, routes, configs, risk=risk, **options)
    assert ledger.snapshot()["requests_by_state"] == {}


def test_malformed_subject_never_dispatches(tmp_path):
    ledger, routes, configs, options = setup(tmp_path)
    with pytest.raises(ValueError):
        run_qualified_preparation({"required_claim_ids": ["claim"]}, routes, configs,
                                   transport=lambda *a: pytest.fail("dispatch forbidden"), **options)


def test_model_tool_call_response_is_rejected_after_accounting(tmp_path):
    ledger, routes, configs, options = setup(tmp_path)
    result = run_qualified_preparation(SUBJECT, routes, configs, transport=lambda cfg, *a:
        envelope(cfg, message={"role": "assistant", "content": "{}", "tool_calls": [{"name": "act"}]}), **options)
    assert result["status"] == "HOLD"
    assert result["assessment"] is None
    assert result["reason"] == "model_message_invalid_or_tool_call"
    assert ledger.snapshot("shared")["used"]["calls"] == 1


def test_abstention_stays_hold_without_fallback(tmp_path):
    _, routes, configs, options = setup(tmp_path)
    result = run_qualified_preparation(SUBJECT, routes, configs,
                                       transport=lambda cfg, *a: envelope(cfg, verdict="ABSTAIN"), **options)
    assert result["reason"] == "model_abstained"
    assert result["model_calls_attempted"] == 1
