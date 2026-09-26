"""Local adapter contract tests with injected transport; never calls a model."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from keel_agent import models
from keel_agent.models import HTTPResult, ModelError, ReviewerConfig, run_blind_review, validate_assessment
from keel_workflow.reviews import ReviewStore


NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
SUBJECT = {"required_claim_ids": ["experience", "education"], "task": "Check source evidence",
           "facts": {"education": "operator-verified source", "experience": "operator-verified source"}}


def config(reviewer_id="reviewer_a", backend="ollama", **kwargs):
    return ReviewerConfig(reviewer_id, backend,
        kwargs.pop("endpoint", "http://127.0.0.1:11434/api/chat" if backend == "ollama" else "http://127.0.0.1:8080/v1/chat/completions"),
        kwargs.pop("model", "installed-local:1"), **kwargs)


def assessment(verdict="PASS", **kwargs):
    return dict(verdict=verdict, covered_claim_ids=SUBJECT["required_claim_ids"], findings=[] if verdict == "PASS" else ["evidence missing"], **kwargs)


def response(cfg, value=None, **changes):
    message = {"role": "assistant", "content": json.dumps(assessment() if value is None else value)}
    data = ({"model": cfg.model, "created_at": NOW.isoformat(), "done": True, "done_reason": "stop", "message": message}
            if cfg.backend == "ollama" else
            {"model": cfg.model, "id": "local-response-1", "choices": [{"finish_reason": "stop", "message": message}]})
    data.update(changes)
    return HTTPResult(200, {"Content-Type": "application/json"}, json.dumps(data).encode())


def run(tmp_path, transport, reviewers=None, clock=lambda: NOW):
    store = ReviewStore(tmp_path / "reviews.sqlite")
    reviewers = reviewers or [config(), config("reviewer_b", "llama_cpp")]
    subject = dict(SUBJECT, reviewer_config_sha256={r.reviewer_id: models.reviewer_config_digest(r) for r in reviewers})
    result = run_blind_review(store, round_id="round-1", subject=subject,
        reviewers=reviewers,
        expires_at=NOW + timedelta(seconds=600), clock=clock, transport=transport)
    return store, result


@pytest.mark.parametrize("endpoint", [
    "https://127.0.0.1:11434/api/chat", "http://localhost:11434/api/chat",
    "http://127.1:11434/api/chat", "http://2130706433:11434/api/chat",
    "http://127.0.0.2:11434/api/chat", "http://0.0.0.0:11434/api/chat",
    "http://127.0.0.1:11434/api/chat?token=x", "http://127.0.0.1:11434/api/chat#x",
    "http://secret@127.0.0.1:11434/api/chat", "http://127.0.0.1:0/api/chat",
    "http://127.0.0.1:65536/api/chat", "http://127.0.0.1:11434/api/pull",
    "http://127.0.0.1:11434//api/chat", "http://[::ffff:127.0.0.1]:11434/api/chat",
    "http://[::1%25lo]:11434/api/chat", "http://127.0.0.1:11434/api/chat\n",
])
def test_destination_validation(endpoint):
    with pytest.raises(ValueError, match="endpoint"):
        config(endpoint=endpoint)


@pytest.mark.parametrize("kwargs", [
    {"timeout_seconds": 0}, {"timeout_seconds": 181}, {"timeout_seconds": True},
    {"timeout_seconds": float("nan")}, {"max_response_bytes": 2},
    {"max_response_bytes": 1048577}, {"max_tokens": 0}, {"max_tokens": True},
    {"model": "model-cloud"}, {"model": "cloud:remote"}, {"model": "injected\nmodel"},
])
def test_limits_and_cloud_names_rejected(kwargs):
    with pytest.raises(ValueError):
        config(**kwargs)


def test_ipv6_exact_path_accepted_and_mismatched_backend_rejected():
    assert config(endpoint="http://[::1]:11434/api/chat").backend == "ollama"
    with pytest.raises(ValueError):
        config(endpoint="http://127.0.0.1:8080/v1/chat/completions")


def test_blind_phase_order_fresh_context_and_durable_metadata(tmp_path):
    seen = []
    def transport(cfg, payload, timeout):
        view = json.loads(payload["messages"][1]["content"])
        with sqlite3.connect(tmp_path / "reviews.sqlite") as db:
            committed = db.execute("SELECT count(*) FROM review_commitments").fetchone()[0]
        seen.append((cfg.reviewer_id, view["phase"], committed))
        assert len(payload["messages"]) == 2 and payload["stream"] is False
        assert "tools" not in payload and "api_key" not in payload
        if view["phase"] == "A":
            assert "immutable_phase_a_commitments" not in view
        else:
            assert committed == 2 and len(view["immutable_phase_a_commitments"]) == 2
        return response(cfg)
    store, result = run(tmp_path, transport)
    assert seen == [("reviewer_a", "A", 0), ("reviewer_b", "A", 1), ("reviewer_a", "B", 2), ("reviewer_b", "B", 2)]
    assert result["evaluation"]["state"] == "READY_FOR_HUMAN_REVIEW"
    assert result["execution_authorized"] is False and result["model_quality_validated"] is False
    assert all(not item["model_identity_hardware_verified"] for item in result["calls"])
    assert len({call["request_id"] for call in result["calls"]}) == 4
    with sqlite3.connect(store.path) as db:
        documents = [row[0] for row in db.execute("SELECT metadata_json FROM local_model_calls")]
        assert len(documents) == 4
        assert all("operator-verified source" not in doc and "messages" not in doc for doc in documents)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM local_model_calls")


@pytest.mark.parametrize("value", [
    {}, {"verdict": "PASS", "covered_claim_ids": ["education"], "findings": []},
    {"verdict": "PASS", "covered_claim_ids": ["education", "experience", "extra"], "findings": []},
    {"verdict": "PASS", "covered_claim_ids": ["education", "experience", "experience"], "findings": []},
    {"verdict": "PASS", "covered_claim_ids": ["education", "experience"], "findings": ["unresolved"]},
    {"verdict": "FAIL", "covered_claim_ids": ["education", "experience"], "findings": []},
    {"verdict": True, "covered_claim_ids": ["education", "experience"], "findings": []},
    dict(assessment(), hidden_reasoning="do not persist"),
])
def test_invalid_assessments_abstain_without_repair(tmp_path, value):
    store, result = run(tmp_path, lambda cfg, *_: response(cfg, value))
    assert result["evaluation"]["state"] == "HOLD"
    assert all(call["verdict"] == "ABSTAIN" for call in result["calls"])
    with sqlite3.connect(store.path) as db:
        documents = [json.loads(row[0]) for row in db.execute("SELECT document FROM review_commitments")]
        assert all(doc["covered_claim_ids"] == [] for doc in documents)
        assert "do not persist" not in json.dumps(documents)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 404, 429, 500])
def test_no_redirect_retry_or_cloud_fallback(tmp_path, status):
    endpoints = []
    def transport(cfg, *_):
        endpoints.append(cfg.endpoint)
        return HTTPResult(status, {"Location": "https://cloud.example/chat"}, b"")
    _, result = run(tmp_path, transport)
    assert len(endpoints) == 4 and all(endpoint.startswith("http://127.0.0.1:") for endpoint in endpoints)
    assert result["evaluation"]["state"] == "HOLD"
    assert all(call["error_code"] == "model_http_not_200" for call in result["calls"])


def test_connection_failure_recorded_abstain_not_product_pass(tmp_path):
    def unavailable(*_):
        raise OSError("private-path-or-secret must not be persisted")
    _, result = run(tmp_path, unavailable)
    assert result["evaluation"]["state"] == "HOLD"
    assert "private-path-or-secret" not in json.dumps(result)
    assert all(call["status"] == "ABSTAIN" for call in result["calls"])


@pytest.mark.parametrize("changes", [{"model": "another-model"}, {"done": False}, {"done_reason": "length"},
    {"created_at": None}, {"message": {"role": "assistant", "content": "{}", "tool_calls": [{"name": "send"}]}}])
def test_ollama_envelope_rejected(changes):
    with pytest.raises(ModelError):
        models._assessment_from_response(config(), response(config(), **changes), SUBJECT["required_claim_ids"])


@pytest.mark.parametrize("body", [b'{"model":"a","model":"b"}', b'NaN', b'[]', b'\xff', b'{'])
def test_strict_json_failures(body):
    with pytest.raises(ModelError):
        models._assessment_from_response(config(), HTTPResult(200, {"content-type": "application/json"}, body), SUBJECT["required_claim_ids"])


def test_oversize_content_type_and_content_encoding_rejected():
    cfg = config(max_response_bytes=1024)
    for reply in [HTTPResult(200, {"content-type": "application/json"}, b" " * 1025),
                  HTTPResult(200, {"content-type": "text/html"}, b"{}"),
                  HTTPResult(200, {"content-type": "application/json", "content-encoding": "gzip"}, b"{}")]:
        with pytest.raises(ModelError):
            models._assessment_from_response(cfg, reply, SUBJECT["required_claim_ids"])


def test_late_injected_transport_cannot_commit_pass(tmp_path, monkeypatch):
    monotonic = [10]
    monkeypatch.setattr(models.time, "monotonic", lambda: monotonic[0])
    def late(cfg, *_):
        monotonic[0] += 61
        return response(cfg)
    _, result = run(tmp_path, late)
    assert all(call["error_code"] == "model_deadline_exceeded" for call in result["calls"])
    assert result["evaluation"]["state"] == "HOLD"


def test_expiry_during_call_preserves_attempt_but_no_valid_review(tmp_path):
    current = [NOW]
    def late(cfg, *_):
        current[0] += timedelta(seconds=601)
        return response(cfg)
    store, result = run(tmp_path, late, clock=lambda: current[0])
    assert result["evaluation"]["state"] == "HOLD" and "review round expired" in result["evaluation"]["reasons"]
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM review_commitments").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM local_model_calls").fetchone()[0] == 1


def test_near_expiry_skips_model_and_records_abstention(tmp_path):
    calls = []
    moments = iter([NOW] + [NOW + timedelta(seconds=599)] * 100)
    _, result = run(tmp_path, lambda *_: calls.append(1), clock=lambda: next(moments))
    assert calls == [] and result["evaluation"]["state"] == "HOLD"
    assert all(call["error_code"] == "review_deadline_near_expiry" for call in result["calls"])


def test_repeat_round_refused_and_config_ids_not_model_selected(tmp_path):
    store, _ = run(tmp_path, lambda cfg, *_: response(cfg))
    reviewers = [config(), config("reviewer_b")]
    subject = dict(SUBJECT, reviewer_config_sha256={r.reviewer_id: models.reviewer_config_digest(r) for r in reviewers})
    with pytest.raises(ValueError, match="already exists"):
        run_blind_review(store, round_id="round-1", subject=subject, reviewers=reviewers,
                         expires_at=NOW + timedelta(seconds=600), clock=lambda: NOW,
                         transport=lambda *_: pytest.fail("must not execute reused round"))


def test_roster_config_swap_and_unbound_config_rejected_before_calls(tmp_path):
    reviewers = [config(), config("reviewer_b")]
    bound = dict(SUBJECT, reviewer_config_sha256={r.reviewer_id: models.reviewer_config_digest(r) for r in reviewers})
    swapped = [config(model="another-installed:1"), config("reviewer_b")]
    for subject, roster in [(SUBJECT, reviewers), (bound, swapped)]:
        with pytest.raises(ValueError, match="exact operator roster"):
            run_blind_review(ReviewStore(tmp_path / "reviews.sqlite"), round_id="unused",
                subject=subject, reviewers=roster, expires_at=NOW + timedelta(seconds=600), clock=lambda: NOW,
                transport=lambda *_: pytest.fail("unbound roster must not run"))


def test_direct_transport_ignores_proxy_and_dns_and_does_not_redirect(monkeypatch):
    actions = []
    class Sock:
        def settimeout(self, value):
            pass
        def connect(self, address):
            actions.append(("connect", address))
        def close(self):
            pass
        def shutdown(self, how):
            pass
    class Response:
        status = 302
        def getheaders(self):
            return [("Location", "https://cloud.example/redirect")]
    class Connection:
        def __init__(self, *args, **kwargs):
            actions.append(("http", args))
        def request(self, method, path, **kwargs):
            actions.append(("request", method, path, kwargs["headers"]))
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example:8080")
    monkeypatch.setattr(models.socket, "socket", lambda *_: Sock())
    monkeypatch.setattr(models.socket, "getaddrinfo", lambda *_: pytest.fail("DNS forbidden"))
    monkeypatch.setattr(models.http.client, "HTTPConnection", Connection)
    result = models.loopback_transport(config(), {"model": "installed-local:1"}, 1)
    assert result.status == 302
    assert [a for a in actions if a[0] == "connect"] == [("connect", ("127.0.0.1", 11434))]
    requests = [a for a in actions if a[0] == "request"]
    assert len(requests) == 1 and "Authorization" not in requests[0][3]


def test_private_thinking_not_persisted_or_returned(tmp_path):
    def answer(cfg, *_):
        result = response(cfg)
        data = json.loads(result.body)
        msg = data["message"] if cfg.backend == "ollama" else data["choices"][0]["message"]
        msg["thinking"] = "SECRET INTERNAL REASONING"
        return HTTPResult(200, result.headers, json.dumps(data).encode())
    store, result = run(tmp_path, answer)
    assert result["evaluation"]["state"] == "READY_FOR_HUMAN_REVIEW"
    assert "SECRET INTERNAL REASONING" not in json.dumps(result)
    assert b"SECRET INTERNAL REASONING" not in store.path.read_bytes()


def diverse_registry():
    return [dict(reviewer_id="reviewer_a", method="source-check", family="family-a", independence_group="group-a"),
            dict(reviewer_id="reviewer_b", method="falsification", family="family-b", independence_group="group-b")]


def test_diversity_returns_declared_only_evidence():
    result = models.validate_roster_diversity([config(model="model-a:1"), config("reviewer_b", model="model-b:1")], diverse_registry())
    assert result["declared_diversity_satisfied"] is True
    assert result["qualifying_reviewer_pairs"] == [["reviewer_a", "reviewer_b"]]
    assert result["model_weights_attested"] is False
    assert result["statistical_independence_proven"] is False
    assert result["execution_authorized"] is False


@pytest.mark.parametrize("left,right", [("model-a:1", "model-a:1"), ("Model-A:1", "model-a:1"),
                                       ("model-a", "model-a:latest"), ("model-a:LATEST", "model-a")])
def test_identical_configured_model_cannot_impersonate_two_families(left, right):
    with pytest.raises(ValueError, match="identical configured model"):
        models.validate_roster_diversity([config(model=left), config("reviewer_b", "llama_cpp", model=right)], diverse_registry())


@pytest.mark.parametrize("field", ["method", "family", "independence_group"])
def test_diversity_requires_one_jointly_distinct_pair(field):
    registry = diverse_registry()
    registry[1][field] = "  " + registry[0][field].upper() + "  "
    with pytest.raises(ValueError, match="no pair"):
        models.validate_roster_diversity([config(model="a"), config("reviewer_b", model="b")], registry)


def test_diversity_normalizes_unicode_and_rejects_unknown_declarations():
    registry = diverse_registry()
    registry[0]["family"], registry[1]["family"] = "Ａ", "a"
    with pytest.raises(ValueError, match="no pair"):
        models.validate_roster_diversity([config(model="a"), config("reviewer_b", model="b")], registry)
    registry[0]["family"] = "unknown"
    with pytest.raises(ValueError, match="explicit"):
        models.validate_roster_diversity([config(model="a"), config("reviewer_b", model="b")], registry)


def test_diversity_registry_exact_ids_and_duplicate_configs():
    roster = [config(model="a"), config("reviewer_b", model="b")]
    for registry in [diverse_registry()[:1], diverse_registry() + diverse_registry(),
                     [diverse_registry()[0], dict(diverse_registry()[1], reviewer_id="outsider")],
                     [diverse_registry()[0], diverse_registry()[0]]]:
        with pytest.raises(ValueError, match="registry"):
            models.validate_roster_diversity(roster, registry)
    with pytest.raises(ValueError, match="registry"):
        models.validate_roster_diversity([config(), config()], diverse_registry())


def test_diversity_cannot_combine_disjoint_pairs():
    registry = [dict(reviewer_id="reviewer_a", method="m1", family="f1", independence_group="g1"),
                dict(reviewer_id="reviewer_b", method="m2", family="f1", independence_group="g2"),
                dict(reviewer_id="reviewer_c", method="m1", family="f2", independence_group="g2")]
    with pytest.raises(ValueError, match="no pair"):
        models.validate_roster_diversity([config(model="a"), config("reviewer_b", model="b"),
                                          config("reviewer_c", model="c")], registry)
