"""Host bridge tests never launch a real browser or bind a socket."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading

import pytest

from keel_agent.browser import BrowserError, contract_hash
from keel_agent.models import HTTPResult, ReviewerConfig, reviewer_config_digest
from keel_bench import host
from tools.local_application_fixture import FixtureState


class FakeServer:
    server_address = ("127.0.0.1", 43127)
    server_port = 43127

    def __init__(self):
        self.stopped = threading.Event()
        self.closed = False

    def serve_forever(self):
        self.stopped.wait(5)

    def shutdown(self):
        self.stopped.set()

    def server_close(self):
        self.closed = True


class FakeAdapter:
    def __init__(self, mutate=None, error=None):
        self.mutate = mutate
        self.error = error
        self.contracts = []

    def prepare(self, contract):
        self.contracts.append(deepcopy(contract))
        if self.error:
            raise self.error
        import base64
        raw = base64.b64decode(contract["attachment"]["base64"], validate=True)
        output = {"status": "PREPARED", "bundle_hash": contract_hash(contract),
                  "form_fingerprint": "a" * 64, "account_id": contract["account_id"],
                  "origin": contract["origin"], "submitted": False, "execution_authorized": False,
                  "readback": {"Motivation": contract["fields"][0]["value"],
                               "attachment": {"name": "answer.txt", "size": len(raw),
                                              "sha256": hashlib.sha256(raw).hexdigest()}}}
        if self.mutate:
            self.mutate(output, contract)
        return output

    def submit_local(self, *args):
        pytest.fail("host rehearsal must never submit")


@pytest.fixture
def fake_fixture(monkeypatch):
    trials = []

    def create(**kwargs):
        assert kwargs == {"port": 0}
        server, state = FakeServer(), FixtureState(nonce=secrets.token_hex(32))
        trials.append((server, state))
        return server, state

    monkeypatch.setattr(host, "create_fixture", create)
    return trials


def test_default_is_actual_file_validation_without_network_or_browser(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("default must not start runtime work")
    monkeypatch.setattr(host, "create_fixture", forbidden)
    monkeypatch.setattr(host, "_REAL_ADAPTER", forbidden)
    report = host.run_host_trial(tmp_path / "new-trial")
    assert report["status"] == "PARTIAL"
    assert {name: check["status"] for name, check in report["checks"].items()} == {
        "evidence": "PASS", "scoped_answer": "PASS", "packet": "PASS",
        "answer_packet_binding": "PASS", "rendered_readback": "NOT_RUN"}
    assert report["browser"] == {"status": "NOT_RUN", "fixture_server_started": False}
    assert report["model"]["status"] == "NOT_RUN"
    assert report["synthetic"] is True
    assert report["trial_scope"] == "PARTIAL_FORM_READBACK"
    assert report["blockers"] == ["rendered_browser_not_verified"]
    assert report["full_application_trial"] is False
    assert report["whole_form_prepared"] is False
    assert report["live_integration"] == "NOT_RUN"
    assert report["canonical_writes"] == 0
    assert not any(report[k] for k in ("execution_authorized", "external_submission", "local_submission",
                                      "truth_independently_verified", "human_approval_verified"))
    assert (tmp_path / "new-trial").stat().st_mode & 0o777 == 0o700


def test_injected_adapter_is_simulated_even_when_readback_is_exact(tmp_path, fake_fixture):
    adapter = FakeAdapter()
    report = host.run_host_trial(tmp_path / "injected", render_browser=True, adapter=adapter)
    assert report["status"] == "PARTIAL"
    assert report["browser"]["status"] == "SIMULATED"
    assert report["rendered_browser_verified"] is False
    assert report["checks"]["rendered_readback"]["status"] == "SIMULATED"
    assert len(adapter.contracts) == 1
    contract = adapter.contracts[0]
    assert contract["operation"] == "prepare"
    assert contract["mode"] == "local_fixture"
    assert len(contract["fields"]) == 1
    assert contract["fields"][0]["label"] == "Motivation"
    assert contract["attachment"]["name"] == "answer.txt"
    assert report["prepared_field_labels"] == []
    assert report["simulated_field_labels"] == ["Motivation"]
    assert report["planned_field_labels"] == ["Motivation"]
    assert report["unprepared_field_labels"] == ["Full name", "Email", "Motivation", "Work authorization", "Confirm accuracy"]
    assert fake_fixture[0][0].closed
    assert fake_fixture[0][0].stopped.is_set()
    assert fake_fixture[0][1].receipts == {}


@pytest.mark.parametrize("field,value", [
    ("status", "LOCAL_FIXTURE_CONFIRMED"), ("bundle_hash", "b" * 64), ("account_id", "other"),
    ("origin", "https://employer.invalid"), ("form_fingerprint", "bad"), ("submitted", True),
    ("execution_authorized", True), ("local_fixture_submitted", True), ("receipt", {}),
    ("readback", {}),
])
def test_bad_readback_never_becomes_dependency_unavailable(tmp_path, fake_fixture, field, value):
    adapter = FakeAdapter(mutate=lambda output, contract: output.update({field: value}))
    report = host.run_host_trial(tmp_path / "bad", render_browser=True, adapter=adapter)
    assert report["status"] == "FAIL"
    assert report["browser"]["status"] == "FAIL"
    assert report["checks"]["rendered_readback"]["status"] == "FAIL"
    assert not report["rendered_browser_verified"]
    assert fake_fixture[0][0].closed


def test_adapter_cannot_change_packet_fields_and_match_its_own_readback(tmp_path, fake_fixture):
    def alter(output, contract):
        contract["fields"][0]["value"] = "unapproved text"
        output["readback"]["Motivation"] = "unapproved text"
        output["bundle_hash"] = contract_hash(contract)
    report = host.run_host_trial(tmp_path / "changed", render_browser=True, adapter=FakeAdapter(alter))
    assert report["browser"]["status"] == "FAIL"


@pytest.mark.parametrize("kind", ["field", "extra_field", "attachment_hash", "attachment_name"])
def test_exact_grounded_field_and_attachment_readback_required(tmp_path, fake_fixture, kind):
    def alter(output, contract):
        readback = output["readback"]
        if kind == "field":
            readback["Motivation"] += " embellished"
        elif kind == "extra_field":
            readback["Confirm accuracy"] = True
        elif kind == "attachment_hash":
            readback["attachment"]["sha256"] = "b" * 64
        else:
            readback["attachment"]["name"] = "other.txt"
    report = host.run_host_trial(tmp_path / "bad-readback", render_browser=True, adapter=FakeAdapter(alter))
    assert report["browser"]["status"] == "FAIL"


def test_repeated_trials_use_new_home_and_nonce(tmp_path, fake_fixture):
    adapter = FakeAdapter()
    first = host.run_host_trial(tmp_path / "one", render_browser=True, adapter=adapter)
    second = host.run_host_trial(tmp_path / "two", render_browser=True, adapter=adapter)
    assert first["browser"]["status"] == second["browser"]["status"] == "SIMULATED"
    assert adapter.contracts[0]["fixture_nonce"] != adapter.contracts[1]["fixture_nonce"]
    assert first["browser"]["bundle_sha256"] != second["browser"]["bundle_sha256"]
    assert all(server.closed for server, _ in fake_fixture)
    with pytest.raises(ValueError, match="new_private_home_required"):
        host.run_host_trial(tmp_path / "one")


@pytest.mark.parametrize("kind", ["existing_file", "existing_dir", "link", "parent_link", "dotdot", "dot", "missing_parent"])
def test_only_new_safe_home_is_accepted(tmp_path, kind):
    target = tmp_path / "trial"
    if kind == "existing_file":
        target.write_text("do not overwrite")
    elif kind == "existing_dir":
        target.mkdir()
    elif kind == "link":
        target.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "parent_link":
        (tmp_path / "linked").symlink_to(tmp_path, target_is_directory=True)
        target = tmp_path / "linked" / "trial"
    elif kind == "dotdot":
        target = str(tmp_path) + "/../trial"
    elif kind == "dot":
        target = str(tmp_path) + "/./trial"
    elif kind == "missing_parent":
        target = tmp_path / "missing" / "trial"
    with pytest.raises(ValueError, match="new_private_home_required"):
        host.run_host_trial(target)


def test_adapter_requires_explicit_render_request(tmp_path):
    with pytest.raises(ValueError, match="explicit_browser_trial_required"):
        host.run_host_trial(tmp_path / "trial", adapter=FakeAdapter())
    assert not (tmp_path / "trial").exists()


def test_exclusive_home_creation_exposes_actual_destination_to_path_audit(tmp_path, monkeypatch):
    original = os.mkdir
    observed = []
    target = tmp_path / "audit-visible"

    def audited_mkdir(path, mode=0o777, *, dir_fd=None):
        # Match the unchanged regression guard's path interpretation: a
        # basename plus dir_fd must not resolve into the protected code cwd.
        resolved = Path(path).resolve()
        assert resolved.is_relative_to(tmp_path)
        observed.append((os.fspath(path), resolved, dir_fd))
        return original(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", audited_mkdir)
    report = host.run_host_trial(target)
    assert report["checks"]["evidence"]["status"] == "PASS"
    assert observed[0][0].startswith("/proc/self/fd/")
    assert observed[0][1] == target
    assert observed[0][2] is None


@pytest.mark.parametrize("value", [1, "true", None])
def test_render_flag_requires_boolean(tmp_path, value):
    with pytest.raises(ValueError, match="explicit_browser_trial_required"):
        host.run_host_trial(tmp_path / "trial", render_browser=value)


def test_changed_packet_fails_before_browser(tmp_path, monkeypatch):
    original = host.make_fixture
    def changed(home):
        case = original(home)
        (home / "packet" / "answer.txt").write_text("not approved\n")
        return case
    monkeypatch.setattr(host, "make_fixture", changed)
    report = host.run_host_trial(tmp_path / "trial")
    assert report["status"] == "FAIL"
    assert report["checks"]["packet"]["status"] == "FAIL"
    assert report["checks"]["answer_packet_binding"]["status"] == "NOT_RUN"
    assert report["browser"]["status"] == "NOT_RUN"


def test_unaided_boundary_stops_chain(tmp_path, monkeypatch):
    original = host.make_fixture
    def unaided(home):
        case = original(home)
        case["context"]["kind"] = "unaided"
        return case
    monkeypatch.setattr(host, "make_fixture", unaided)
    report = host.run_host_trial(tmp_path / "trial")
    assert report["checks"]["scoped_answer"]["status"] == "FAIL"
    assert report["checks"]["packet"]["status"] == "NOT_RUN"


def test_filesystem_inspection_does_not_expose_configuration_or_claim_runnability(monkeypatch, tmp_path):
    marker = "secret-marker-not-for-report"
    monkeypatch.setenv("KEEL_PLAYWRIGHT_MODULE", marker)
    monkeypatch.setenv("KEEL_CHROMIUM_EXECUTABLE", str(tmp_path / marker))
    report = host.inspect_host()
    assert marker not in json.dumps(report)
    assert report["inspection"] == "FILESYSTEM_ONLY"
    assert report["network_calls"] == report["subprocess_calls"] == 0
    assert report["model_inference"] == report["rendered_browser"] == "NOT_RUN"
    assert report["playwright"]["configured"] is True
    assert report["chromium"]["status"] == "NOT_FOUND"
    assert all(not report[k]["runnability_verified"] for k in ("node", "playwright", "chromium"))


def test_missing_node_is_unavailable_and_no_fixture_is_started(tmp_path, monkeypatch):
    observation = host.inspect_host()
    observation["node"]["status"] = "NOT_FOUND"
    monkeypatch.setattr(host, "inspect_host", lambda: observation)
    def no_server(**kwargs):
        pytest.fail("dependency missing: no fixture required")
    monkeypatch.setattr(host, "create_fixture", no_server)
    report = host.run_host_trial(tmp_path / "trial", render_browser=True)
    assert report["browser"]["status"] == "UNAVAILABLE"
    assert report["browser"]["reason"] == "node_unavailable"
    assert report["status"] == "PARTIAL"


@pytest.mark.parametrize("error", [BrowserError("field readback mismatch"),
                                  BrowserError("browser unavailable or timed out; outcome unverified"),
                                  BrowserError("Chromium executable is not installed; fake injected failure")])
def test_injected_adapter_errors_are_failures_not_real_host_observations(tmp_path, fake_fixture, error):
    report = host.run_host_trial(tmp_path / "trial", render_browser=True, adapter=FakeAdapter(error=error))
    assert report["browser"]["status"] == "FAIL"
    assert report["status"] == "FAIL"
    assert fake_fixture[0][0].closed


def test_receipt_created_by_adapter_invalidates_prepare_only_trial(tmp_path, fake_fixture):
    def add_receipt(output, contract):
        fake_fixture[0][1].receipts["unauthorized"] = {}
    report = host.run_host_trial(tmp_path / "trial", render_browser=True, adapter=FakeAdapter(add_receipt))
    assert report["browser"]["status"] == "FAIL"


def model_config():
    return ReviewerConfig(reviewer_id="host-reviewer", backend="ollama",
                          endpoint="http://127.0.0.1:11434/api/chat", model="synthetic-test-model")


def assessment_transport(verdict, captured):
    def transport(config, request, timeout):
        captured.append(request)
        assessment = {"verdict": verdict, "covered_claim_ids": ["synthetic-claim"],
                      "findings": [] if verdict == "PASS" else ["Synthetic test finding."]}
        body = {"model": config.model, "done": True, "done_reason": "stop",
                "created_at": "2026-09-19T12:00:00Z",
                "message": {"role": "assistant", "content": json.dumps(assessment)}}
        return HTTPResult(200, {"Content-Type": "application/json"}, json.dumps(body).encode())
    return transport


def test_model_stage_receives_verified_source_and_no_label_then_can_prepare_simulated_browser(tmp_path, fake_fixture):
    captured = []
    cfg = model_config()
    report = host.run_host_trial(tmp_path / "model", render_browser=True, adapter=FakeAdapter(),
                                 reviewer_config=cfg, allow_model_calls=True,
                                 transport=assessment_transport("PASS", captured))
    assert report["model"]["status"] == "SIMULATED"
    assert report["model"]["observed_verdict"] == "PASS"
    assert report["model"]["model_calls_attempted"] == 0
    assert report["model"]["transport_calls_attempted"] == 1
    assert report["model"]["model_inference"] == "NOT_RUN"
    assert report["model"]["model_config_sha256"] == reviewer_config_digest(cfg)
    assert report["browser"]["status"] == "SIMULATED"
    assert report["model_to_rendered_slice_verified"] is False
    assert len(captured) == 1
    encoded = json.dumps(captured[0])
    assert "Synthetic fixture qualification; not a real applicant fact." in encoded
    assert "expected_verdict" not in encoded
    assert "label_rationale" not in encoded
    assert report["model"]["model_quality_validated"] is False
    assert report["model"]["model_weights_attested"] is False


@pytest.mark.parametrize("verdict", ["FAIL", "ABSTAIN"])
def test_model_withholding_blocks_browser_before_server_exists(tmp_path, monkeypatch, verdict):
    def forbidden(**kwargs):
        pytest.fail("withheld model review must not start a fixture")
    monkeypatch.setattr(host, "create_fixture", forbidden)
    report = host.run_host_trial(tmp_path / "blocked", render_browser=True, adapter=FakeAdapter(),
                                 reviewer_config=model_config(), allow_model_calls=True,
                                 transport=assessment_transport(verdict, []))
    assert report["status"] == "FAIL"
    assert report["model"]["status"] == "SIMULATED"
    assert report["model"]["observed_verdict"] == verdict
    assert report["checks"]["model_review"]["status"] == "BLOCKED"
    assert report["browser"]["status"] == "NOT_RUN"
    assert report["browser"]["reason"] == "model_review_blocked"


@pytest.mark.parametrize("error_kind", ["rate_limit", "malformed", "throw"])
def test_model_errors_and_429_prevent_render_and_never_retry(tmp_path, monkeypatch, error_kind):
    calls = []
    def transport(*args):
        calls.append(args)
        if error_kind == "throw":
            raise RuntimeError("secret transport details")
        if error_kind == "malformed":
            return HTTPResult(200, {"Content-Type": "application/json"}, b"unparseable")
        return HTTPResult(429, {}, b"")
    report = host.run_host_trial(tmp_path / "blocked", render_browser=True, adapter=FakeAdapter(),
                                 reviewer_config=model_config(), allow_model_calls=True, transport=transport)
    assert report["model"]["observed_verdict"] == "ERROR"
    assert report["status"] == "FAIL"
    assert report["browser"]["status"] == "NOT_RUN"
    assert len(calls) == 1
    assert "secret transport details" not in json.dumps(report)


@pytest.mark.parametrize("kwargs", [
    {"reviewer_config": model_config()},
    {"allow_model_calls": True},
    {"allow_model_calls": 1},
    {"reviewer_config": {}, "allow_model_calls": True},
    {"transport": lambda *_: None},
    {"reviewer_config": model_config(), "allow_model_calls": True, "transport": "bad"},
])
def test_model_calls_require_explicit_valid_configuration_before_writes(tmp_path, kwargs):
    with pytest.raises(ValueError, match="explicit_local_model_configuration_required"):
        host.run_host_trial(tmp_path / "trial", **kwargs)
    assert not (tmp_path / "trial").exists()


def test_model_calls_do_not_happen_when_grounding_fails(tmp_path, monkeypatch):
    original = host.make_fixture
    def changed(home):
        case = original(home)
        (home / "evidence" / "profile.json").write_text("changed")
        return case
    monkeypatch.setattr(host, "make_fixture", changed)
    def no_transport(*args):
        pytest.fail("invalid evidence must block model call")
    report = host.run_host_trial(tmp_path / "changed", reviewer_config=model_config(),
                                 allow_model_calls=True, transport=no_transport)
    assert report["model"]["status"] == "NOT_RUN"
    assert report["checks"]["evidence"]["status"] == "FAIL"
