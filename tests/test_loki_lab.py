from copy import deepcopy
import json
from unittest.mock import patch
import pytest
from keel_agent.models import HTTPResult, ModelError
from keel_loki.common import digest
from keel_loki.lab import (demo, mutation_dataset, fixture_configs, run_lab, validate_report,
    optimize, make_lab_plan, STAGES)


def reply(config, verdict="ABSTAIN"):
    return HTTPResult(200, {"content-type": "application/json"}, json.dumps({"model": config.model,
        "done": True, "created_at": "test", "message": {"role": "assistant", "content": json.dumps({
        "verdict": verdict, "covered_claim_ids": ["claim"], "findings": [] if verdict == "PASS" else ["fixture"]})}}).encode())


def run(**kwargs):
    data = mutation_dataset()
    return run_lab(data, fixture_configs(), expected_dataset_sha256=digest(data), **kwargs)


def test_mutations_are_deterministic_explicitly_synthetic_and_relabelled():
    a = mutation_dataset()
    assert a == mutation_dataset()
    assert a["synthetic"] is True and a["label_source"] == "synthetic_fixture"
    labels = {c["case_id"]: c["expected_verdict"] for c in a["cases"]}
    assert labels == {"original": "PASS", "removed": "ABSTAIN", "contradiction": "FAIL", "expired": "ABSTAIN",
                      "retargeted": "ABSTAIN", "irrelevant": "PASS", "injection": "PASS", "conflict": "ABSTAIN"}


def test_blind_first_responses_and_no_gold_in_requests():
    calls = []
    def send(config, request, timeout):
        body = json.loads(request["messages"][1]["content"])
        serialized = json.dumps(request)
        assert "expected_verdict" not in serialized and "label_rationale" not in serialized and "case_manifest" not in serialized
        position = len(calls) % 5
        assert bool(body["peer_assessments"]) == (position >= 3)
        calls.append(body)
        return reply(config)
    report = run(allow_model_calls=True, transport=send)
    assert len(calls) == report["calls"] == 40
    assert validate_report(report) == report
    assert report["mode"] == "INJECTED"
    assert report["metrics"]["stages"]["second_revision"]["useful_pass"]["value"] == 0


def test_predeclared_plan_pin_prevents_mode_or_policy_changes():
    data, configs = mutation_dataset(), fixture_configs()
    plan = make_lab_plan(data, configs, expected_dataset_sha256=digest(data), mode="INJECTED")
    report = run_lab(data, configs, expected_dataset_sha256=digest(data), expected_plan_sha256=digest(plan),
                     allow_model_calls=True, transport=lambda c, p, t: reply(c))
    assert report["plan_sha256"] == digest(plan)
    with pytest.raises(ValueError, match="lab_plan_pin_mismatch"):
        run_lab(data, configs, expected_dataset_sha256=digest(data), expected_plan_sha256=digest(plan))


def test_disabled_and_budgeted_rows_keep_all_denominators():
    disabled = run()
    assert disabled["calls"] == 0
    assert len(disabled["records"]) == 8
    assert all(item["error_code"] == "local_model_not_enabled" for row in disabled["records"] for item in row["stages"].values())
    limited = run(allow_model_calls=True, max_calls=3, transport=lambda c, p, t: reply(c))
    assert limited["calls"] == 3
    assert sum(v["error_code"] == "call_budget_exhausted" for r in limited["records"] for v in r["stages"].values()) == 37
    assert validate_report(limited) == limited


@pytest.mark.parametrize("exception", [False, True])
def test_429_stops_every_subsequent_role_and_case(exception):
    def send(*args):
        if exception:
            raise ModelError("model_http_429")
        return HTTPResult(429, {}, b"private")
    report = run(allow_model_calls=True, transport=send)
    assert report["calls"] == 1 and report["rate_limited"] is True
    rows = [v for r in report["records"] for v in r["stages"].values()]
    assert rows[0]["error_code"] == "model_http_429"
    assert all(r["error_code"] == "not_run_after_rate_limit" for r in rows[1:])
    assert validate_report(report) == report


def test_harmful_and_beneficial_changes_are_separate():
    counter = [0]
    def send(config, request, timeout):
        stage = counter[0] % 5
        counter[0] += 1
        # Initial PASS -> reviewer ABSTAIN: harmful on supported cases,
        # beneficial on ABSTAIN labels, still wrong on contradicted case.
        return reply(config, "PASS" if stage < 3 else "ABSTAIN")
    report = run(allow_model_calls=True, transport=send)
    corrections = report["metrics"]["transitions"]["drafter_blind__reviewer_revision"]
    assert corrections["beneficial"]["numerator"] == 4
    assert corrections["harmful"]["numerator"] == 3
    assert corrections["retained_error"]["numerator"] == 1
    assert all(v["denominator"] == 8 for v in corrections.values())


def test_invalid_response_is_error_not_model_abstention():
    report = run(allow_model_calls=True, transport=lambda *args: HTTPResult(200, {"content-type": "application/json"}, b"bad"))
    stage = report["metrics"]["stages"]["drafter_blind"]
    assert stage["errors"]["numerator"] == 8 and stage["abstentions"]["numerator"] == 0
    assert validate_report(report) == report


def test_wall_budget_prevents_calls():
    configs = fixture_configs()
    from dataclasses import replace
    configs = {k: replace(v, timeout_seconds=60) for k, v in configs.items()}
    data = mutation_dataset()
    report = run_lab(data, configs, expected_dataset_sha256=digest(data), max_wall_seconds=1,
                     allow_model_calls=True, transport=lambda *args: pytest.fail("no budget"))
    assert report["calls"] == 0
    assert all(v["error_code"] == "wall_budget_exhausted" for r in report["records"] for v in r["stages"].values())


@pytest.mark.parametrize("field,value", [("synthetic", False), ("split", "held_out"), ("mode", "LOCAL_LOOPBACK")])
def test_provenance_metadata_cannot_be_upgraded(field, value):
    report = demo()
    report[field] = value
    with pytest.raises(ValueError):
        validate_report(report)


def test_optimizer_evaluates_candidates_but_never_promotes_or_uses_held_out():
    data = mutation_dataset()
    report = optimize(data, fixture_configs(), allow_model_calls=True, transport=lambda c, p, t: reply(c), max_calls=80)
    assert report["calls"] == 80
    assert report["results"][-1]["status"] == "NOT_RUN"
    assert report["selected_candidate"] in ("evidence_first", "contradiction_first")
    assert report["promoted"] is False and report["held_out_evaluated"] is False
    data["split"] = "held_out"
    with pytest.raises(ValueError, match="development"):
        optimize(data, fixture_configs())


def test_optimizer_429_is_global_across_candidates():
    report = optimize(mutation_dataset(), fixture_configs(), allow_model_calls=True,
                      transport=lambda *args: HTTPResult(429, {}, b""))
    assert report["calls"] == 1 and report["selected_candidate"] is None
    assert all(r["status"] == "NOT_RUN" for r in report["results"][1:])


@pytest.mark.parametrize("max_calls", [True, 0, 1025])
def test_invalid_budgets_rejected(max_calls):
    with pytest.raises(ValueError):
        run(max_calls=max_calls)


def test_external_dataset_and_plan_pin_checks():
    report = demo()
    changed = mutation_dataset()
    changed["cases"][0]["expected_verdict"] = "FAIL"
    with pytest.raises(ValueError):
        validate_report(report, dataset=changed)
    with pytest.raises(ValueError):
        validate_report(report, expected_plan_sha256="f" * 64)


def test_optional_local_proposals_are_allowlisted_and_not_promoted():
    from keel_loki.lab import propose_candidates
    development = demo()
    def send(config, request, timeout):
        payload = json.loads(request["messages"][1]["content"])
        assert set(payload) == {"allowlist", "metrics"}
        return HTTPResult(200, {"content-type": "application/json"}, json.dumps({"model": config.model,
            "done": True, "message": {"role": "assistant", "content": json.dumps({"candidate_ids": ["scope_first"]})}}).encode())
    result = propose_candidates(fixture_configs()["drafter"], development, allow_model_calls=True, transport=send)
    assert result["candidate_ids"] == ["scope_first"] and result["promoted"] is False
    assert result["mode"] == "INJECTED" and result["calls"] == 1


def test_proposal_cannot_reset_prior_rate_limit():
    from keel_loki.lab import propose_candidates
    report = run(allow_model_calls=True, transport=lambda *args: HTTPResult(429, {}, b""))
    result = propose_candidates(fixture_configs()["drafter"], report, allow_model_calls=True,
                                transport=lambda *args: pytest.fail("rate limit persists"))
    assert result["calls"] == 0 and result["error_code"] == "not_run_after_rate_limit"
