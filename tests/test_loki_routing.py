"""Actual local-review adapter escalation with injected offline responses."""
from copy import deepcopy
from dataclasses import asdict
import json
import os

import pytest

from keel_agent.models import HTTPResult, ReviewerConfig
from keel_loki import routing as r


SOURCE = "a" * 64


def policy(**kwargs):
    configs = [ReviewerConfig(name, "ollama", "http://127.0.0.1:11434/api/chat",
                              "fixture-" + name, timeout_seconds=.05) for name in ("small", "strong")]
    return r.make_policy(policy_id="route", source_sha256=SOURCE,
                         small_config=configs[0], strong_config=configs[1], **kwargs)


def subject():
    return {"required_claim_ids": ["claim"], "claims": [{"claim_id": "claim", "text": "Private claim."}],
            "evidence": [{"evidence_id": "source", "text": "Private evidence."}]}


def permissions():
    return {"ai_allowed": True, "consent_for_model": True, "holds": [],
            "permission_revision_sha256": "b" * 64}


def response(config, verdict="PASS", **extra):
    assessment = dict(verdict=verdict, covered_claim_ids=["claim"],
                      findings=[] if verdict == "PASS" else ["Fixture finding."], **extra)
    return HTTPResult(200, {"content-type": "application/json"}, json.dumps({"model": config.model,
        "done": True, "created_at": "fixture", "message": {"role": "assistant",
            "content": json.dumps(assessment)}}).encode())


def run(tmp_path, *, frozen=None, data=None, perms=None, **kwargs):
    frozen = frozen or policy()
    return r.run_route(data or subject(), frozen, expected_policy_sha256=r.digest(frozen),
        source_sha256=SOURCE, permissions=perms or permissions(),
        state_path=str(tmp_path / "state.json"), **kwargs)


def test_no_model_default_does_not_touch_state(tmp_path):
    report = run(tmp_path, transport=lambda *a: pytest.fail("model called"))
    assert report["mode"] == "NO_MODEL"
    assert report["model_calls_attempted"] == 0
    assert not list(tmp_path.iterdir())


def test_absent_evidence_abstains_without_call(tmp_path):
    data = subject()
    data["evidence"] = []
    report = run(tmp_path, data=data, allow_model_calls=True, transport=lambda *a: pytest.fail("call"))
    assert report["stop_reason"] == "source_evidence_missing"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("key,value,reason", [("holds", ["unaided-writing"], "existing_holds"),
    ("ai_allowed", False, "no_ai_permission"), ("consent_for_model", False, "model_consent_absent")])
def test_permissions_dominate(tmp_path, key, value, reason):
    perms = permissions()
    perms[key] = value
    report = run(tmp_path, perms=perms, allow_model_calls=True, transport=lambda *a: pytest.fail("call"))
    assert report["recommendation"] == "BLOCKED"
    assert report["stop_reason"] == reason
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_supported_terminal_verdict_has_no_escalation_or_authority(tmp_path, verdict):
    seen = []
    def send(config, payload, timeout):
        seen.append(config.model)
        assert "expected_verdict" not in json.dumps(payload)
        return response(config, verdict)
    report = run(tmp_path, allow_model_calls=True, transport=send)
    assert len(seen) == 1
    assert report["mode"] == "INJECTED"
    assert report["recommendation"] == verdict
    assert not report["execution_authorized"]


def test_abstain_escalates_to_strong(tmp_path):
    report = run(tmp_path, allow_model_calls=True,
        transport=lambda c, *a: response(c, "ABSTAIN" if c.reviewer_id == "small" else "PASS"))
    assert [s["stage"] for s in report["records"]] == ["deterministic", "small_local", "strong_local"]
    assert report["model_calls_attempted"] == 2
    assert report["session_state"]["calls_attempted"] == 2


def test_429_persists_across_stages_and_invocations(tmp_path):
    first = run(tmp_path, allow_model_calls=True, transport=lambda *a: HTTPResult(429, {}, b""))
    second = run(tmp_path, allow_model_calls=True, transport=lambda *a: pytest.fail("429 retried"))
    assert first["stop_reason"] == "model_http_429"
    assert first["session_state"]["rate_limited"]
    assert second["stop_reason"] == "not_run_after_rate_limit"
    assert second["mode"] == "NO_MODEL"
    assert second["model_calls_attempted"] == 0


def test_uncertain_transport_failure_is_durable_hold(tmp_path):
    def fail(*args):
        raise OSError("private secret must not leak")
    first = run(tmp_path, allow_model_calls=True, transport=fail)
    second = run(tmp_path, allow_model_calls=True, transport=lambda *a: pytest.fail("retry"))
    assert first["session_state"]["in_flight"]
    assert second["stop_reason"] == "unknown_model_attempt"
    assert "private secret" not in json.dumps(first)


def test_total_budget_not_reset_by_new_invocation(tmp_path):
    frozen = policy(max_model_calls=1)
    first = run(tmp_path, frozen=frozen, allow_model_calls=True, transport=lambda c, *a: response(c, "ABSTAIN"))
    second = run(tmp_path, frozen=frozen, allow_model_calls=True, transport=lambda *a: pytest.fail("budget"))
    assert first["stop_reason"] == second["stop_reason"] == "model_call_budget_exhausted"
    assert first["model_calls_attempted"] == 1
    assert second["model_calls_attempted"] == 0


def test_insufficient_remaining_deadline_is_no_call(tmp_path):
    frozen = policy(max_wall_seconds=1)
    frozen["small_config"]["timeout_seconds"] = 2
    report = run(tmp_path, frozen=frozen, allow_model_calls=True, transport=lambda *a: pytest.fail("deadline"))
    assert report["stop_reason"] == "wall_budget_insufficient_for_deadline"
    assert report["model_calls_attempted"] == 0


def test_model_cannot_approve_itself(tmp_path):
    report = run(tmp_path, allow_model_calls=True,
                 transport=lambda c, *a: response(c, execution_authorized=True))
    assert report["recommendation"] == "ERROR"
    assert report["model_calls_attempted"] == 1
    assert not report["execution_authorized"]


@pytest.mark.parametrize("endpoint", ["https://example.com/api/chat", "http://localhost:11434/api/chat",
    "http://127.0.0.1:11434/api/pull", "http://169.254.169.254:80/api/chat"])
def test_remote_or_unpinned_endpoint_rejected(tmp_path, endpoint):
    frozen = policy()
    frozen["small_config"]["endpoint"] = endpoint
    with pytest.raises(r.RoutingError):
        run(tmp_path, frozen=frozen)


@pytest.mark.parametrize("variant", ["pin", "source", "escalate", "bool-budget", "nan", "confidence"])
def test_invalid_contract_before_side_effect(tmp_path, variant):
    frozen, perms = policy(), permissions()
    if variant == "pin":
        with pytest.raises(r.RoutingError, match="pin"):
            r.run_route(subject(), frozen, expected_policy_sha256="c" * 64, source_sha256=SOURCE,
                        permissions=perms, state_path=str(tmp_path / "state.json"))
        return
    if variant == "source":
        frozen["source_sha256"] = "c" * 64
    elif variant == "escalate":
        frozen["escalate_on"] = ["ERROR"]
    elif variant == "bool-budget":
        frozen["max_model_calls"] = True
    elif variant == "nan":
        frozen["max_wall_seconds"] = float("nan")
    elif variant == "confidence":
        perms["model_confidence"] = .99
    with pytest.raises(r.RoutingError):
        run(tmp_path, frozen=frozen, perms=perms)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("variant", ["corrupt", "symlink", "hardlink", "inflight", "duplicate-json", "policy"])
def test_bad_durable_state_never_resets(tmp_path, variant):
    path = tmp_path / "state.json"
    state = r._new_state(policy())
    if variant == "corrupt":
        path.write_text("{truncated")
    elif variant == "symlink":
        target = tmp_path / "target"
        target.write_text(json.dumps(state))
        path.symlink_to(target)
    elif variant == "hardlink":
        target = tmp_path / "target"
        target.write_text(json.dumps(state))
        os.link(target, path)
    elif variant == "inflight":
        state.update(in_flight=True, calls_attempted=1)
        path.write_text(json.dumps(state))
    elif variant == "duplicate-json":
        path.write_text('{"schema":1,"schema":2}')
    else:
        state["policy_sha256"] = "c" * 64
        path.write_text(json.dumps(state))
    if variant != "symlink":
        path.chmod(0o600)
    if variant == "inflight":
        report = run(tmp_path, allow_model_calls=True, transport=lambda *a: pytest.fail("unknown retry"))
        assert report["stop_reason"] == "unknown_model_attempt"
    else:
        with pytest.raises(r.RoutingError):
            run(tmp_path, allow_model_calls=True, transport=lambda *a: pytest.fail("state bypass"))


def test_demo_is_injected_with_sticky_stop():
    report = r.demo()
    assert report["real_model_calls"] == 0
    assert report["escalation"]["recommendation"] == "PASS"
    assert report["after_rate_limit"]["model_calls_attempted"] == 0
