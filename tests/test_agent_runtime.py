"""Synthetic integration tests: signed host snapshots, injected model/browser I/O.

No network, real model verdict, real person, consent or application is used. The
mutable test clock represents elapsed work; fixture refresh represents newly
observed unchanged source records, never a production timestamp rewrite API.
"""
from base64 import b64encode
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from keel_agent.browser import contract_hash
from keel_agent.models import HTTPResult
from keel_agent.runtime import LocalAgent, pack_bundle, unpack_bundle
from keel_flow.common import digest
from tools.selfhost_fixture import NOW, make_fixture


class SyntheticClock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class SyntheticTransport:
    """Explicit hard-coded transport fixtures; these are not quality evidence."""
    def __init__(self, clock, *, elapsed_per_call=0, unavailable=False):
        self.clock = clock
        self.elapsed_per_call = elapsed_per_call
        self.unavailable = unavailable
        self.calls = []

    def __call__(self, config, request, timeout):
        view = json.loads(request["messages"][1]["content"])
        self.calls.append({"reviewer_id": config.reviewer_id, "view": view, "timeout": timeout})
        self.clock.advance(self.elapsed_per_call)
        if self.unavailable:
            raise OSError("synthetic backend unavailable; private error must not be stored")
        assessment = {"verdict": "PASS", "covered_claim_ids": view["subject"]["required_claim_ids"], "findings": []}
        response = {"model": config.model, "done": True, "done_reason": "stop",
                    "created_at": self.clock().isoformat(),
                    "message": {"role": "assistant", "content": json.dumps(assessment)}}
        return HTTPResult(200, {"content-type": "application/json"}, json.dumps(response).encode())


class SyntheticBrowser:
    def __init__(self):
        self.calls = []

    def prepare(self, contract):
        self.calls.append(deepcopy(contract))
        return {"status": "PREPARED", "submitted": False, "synthetic": True,
                "bundle_hash": contract_hash(contract), "account_id": contract["account_id"],
                "origin": contract["origin"], "form_fingerprint": digest("synthetic-form")}


class Harness:
    def __init__(self, directory):
        self.bundle, self.flow, self.assurance, self.trust = make_fixture()
        self.clock = SyntheticClock()
        self.agent = LocalAgent(directory / "synthetic-agent", self.trust["workspace_id"], clock=self.clock)
        self.config = {"reviewers": [
            {"reviewer_id": row["reviewer_id"], "backend": "ollama",
             "endpoint": "http://127.0.0.1:11434/api/chat", "model": "synthetic-model-" + str(index)}
            for index, row in enumerate(self.assurance["reviewers"])]}
        self.sequence = 0
        self.import_current()

    def import_current(self):
        envelope = self.agent.state.sign_snapshot(
            {"flow": self.flow, "assurance": self.assurance, "trust": self.trust},
            self.flow["source_revision"], self.sequence + 1, self.clock(), self.clock() + timedelta(seconds=90))
        self.agent.state.import_snapshot(envelope, now=self.clock())
        self.sequence += 1
        return envelope

    def observe_again(self):
        """TEST ONLY: unchanged source state has been observed again at clock."""
        stamp = self.clock().isoformat()
        self.flow["observed_at"] = self.flow["pool"]["observed_at"] = stamp
        self.flow["capacity"]["observed_at"] = stamp
        for row in self.flow["leads"]:
            row["observed_at"] = stamp
        for row in self.flow["discovery"]["sources"]:
            row["observed_at"] = stamp
        self.assurance["observed_at"] = stamp
        self.assurance["export_sha256"] = digest(self.flow)
        # Keep ORIGINAL evidence dates and ORIGINAL scope-bound receipts.
        self.trust["observed_at"] = self.trust["evidence_export"]["observed_at"] = stamp
        self.trust["flow_export"] = deepcopy(self.flow)
        return self.import_current()

    def enqueue(self, *, key="synthetic-review-once", round_id="synthetic-round"):
        return self.agent.enqueue_review(self.bundle, self.config, idempotency_key=key, round_id=round_id)

    def reviewed(self, *, elapsed_per_call=0, unavailable=False):
        job = self.enqueue()
        transport = SyntheticTransport(self.clock, elapsed_per_call=elapsed_per_call, unavailable=unavailable)
        completed = self.agent.worker_once("synthetic-worker", transport=transport)
        return job, completed, transport

    def contract(self):
        name, raw = self.bundle.attachments[0]
        document = self.bundle.document
        return {"mode": "external_prepare", "origin": "https://synthetic.invalid",
                "url": document["destination"], "account_id": document["account_id"],
                "allowed_origins": ["https://synthetic.invalid"],
                "fields": [{"label": label, "kind": "text", "value": value}
                           for label, value in document["content"]["application"]["answers"].items()],
                "attachment": {"label": "Resume", "name": name, "mime_type": "text/plain",
                               "base64": b64encode(raw).decode()}}


@pytest.fixture
def synthetic(tmp_path):
    return Harness(tmp_path)


def test_signed_snapshot_review_worker_preflight_end_to_end(synthetic):
    job, completed, transport = synthetic.reviewed()
    assert job["state"] == "QUEUED"
    assert completed["state"] == "COMPLETED"
    assert completed["result"]["evaluation"]["state"] == "READY_FOR_HUMAN_REVIEW"
    assert completed["result"]["execution_authorized"] is False
    assert [row["view"]["phase"] for row in transport.calls] == ["A", "A", "B", "B"]
    assert all("immutable_phase_a_commitments" not in row["view"] for row in transport.calls[:2])
    assert all(len(row["view"]["immutable_phase_a_commitments"]) == 2 for row in transport.calls[2:])
    preflight = synthetic.agent.preflight(job["job_id"])
    assert preflight["state"] == "PREFLIGHT_CHECKS_PASSED"
    assert preflight["execution_authorized"] is False
    assert preflight["candidate"]["reviewer_configuration_bound"] is True


def test_changed_current_source_stops_before_first_model_call(synthetic):
    job = synthetic.enqueue()
    synthetic.trust["evidence_export"]["sources"][0]["content_hash"] = digest("changed synthetic source bytes")
    synthetic.import_current()
    transport = SyntheticTransport(synthetic.clock)
    completed = synthetic.agent.worker_once("synthetic-worker", transport=transport)
    assert completed["job_id"] == job["job_id"]
    assert completed["state"] == "FAILED"
    assert transport.calls == []
    with pytest.raises(ValueError, match="completed review"):
        synthetic.agent.preflight(job["job_id"])


def test_stale_signed_snapshot_stops_before_model(synthetic):
    job = synthetic.enqueue()
    synthetic.clock.advance(91)
    transport = SyntheticTransport(synthetic.clock)
    result = synthetic.agent.worker_once("synthetic-worker", transport=transport)
    assert result["job_id"] == job["job_id"]
    assert result["state"] == "FAILED"
    assert transport.calls == []


def test_completed_review_cannot_age_in_snapshot(synthetic):
    job, _completed, _transport = synthetic.reviewed()
    synthetic.clock.advance(91)
    with pytest.raises(ValueError, match="stale"):
        synthetic.agent.preflight(job["job_id"])
    with pytest.raises(ValueError, match="stale"):
        synthetic.agent.approve_prepare(job["job_id"], expires_at=synthetic.clock() + timedelta(seconds=60))


def test_over_90_second_review_succeeds_only_after_real_fresh_export(synthetic):
    authority_before = deepcopy(synthetic.assurance["actions"][0]["authority"])
    job, completed, transport = synthetic.reviewed(elapsed_per_call=30)
    assert synthetic.clock() == NOW + timedelta(seconds=120)
    assert len(transport.calls) == 4
    assert completed["state"] == "COMPLETED"
    assert completed["result"]["evaluation"]["state"] == "READY_FOR_HUMAN_REVIEW"
    with pytest.raises(ValueError, match="stale"):
        synthetic.agent.preflight(job["job_id"])
    synthetic.observe_again()
    preflight = synthetic.agent.preflight(job["job_id"])
    assert preflight["state"] == "PREFLIGHT_CHECKS_PASSED"
    assert preflight["candidate"]["subject_sha256"] == job["material_hash"]
    assert authority_before == synthetic.assurance["actions"][0]["authority"]
    assert synthetic.assurance["actions"][0]["reviews"] == []


def test_failed_local_model_persists_hold_and_never_grants_preparation(synthetic):
    job, completed, transport = synthetic.reviewed(unavailable=True)
    assert len(transport.calls) == 4
    assert completed["state"] == "COMPLETED"  # Processing completion is not an approved review.
    assert completed["result"]["evaluation"]["state"] == "HOLD"
    assert completed["result"]["execution_authorized"] is False
    assert all(row["status"] == "ABSTAIN" for row in completed["result"]["calls"])
    assert "private error" not in json.dumps(completed)
    assert synthetic.agent.preflight(job["job_id"])["state"] == "BLOCKED"
    with pytest.raises(ValueError, match="blocked"):
        synthetic.agent.approve_prepare(job["job_id"], expires_at=synthetic.clock() + timedelta(seconds=60))


@pytest.mark.parametrize("change", ["bytes", "fields", "account", "destination"])
def test_changed_browser_material_denied_before_adapter_and_preserves_approval(synthetic, change):
    job, _completed, _transport = synthetic.reviewed()
    approval = synthetic.agent.approve_prepare(job["job_id"], expires_at=synthetic.clock() + timedelta(seconds=60))
    contract = synthetic.contract()
    if change == "bytes":
        contract["attachment"]["base64"] = b64encode(b"different synthetic attachment").decode()
    elif change == "fields":
        contract["fields"][0]["value"] = "Unreviewed Synthetic Name"
    elif change == "account":
        contract["account_id"] = "unreviewed-synthetic-account"
    else:
        contract["url"] = "https://synthetic.invalid/apply/different-role"
    browser = SyntheticBrowser()
    with pytest.raises(ValueError, match="differs|differ"):
        synthetic.agent.prepare_browser(job["job_id"], contract, approval_id=approval, adapter=browser)
    assert browser.calls == []
    assert not any(row["event_type"] == "APPROVAL_CONSUMED" for row in synthetic.agent.state.events())
    good = synthetic.agent.prepare_browser(job["job_id"], synthetic.contract(), approval_id=approval, adapter=browser)
    assert good["state"] == "PREPARED"
    assert good["external_submission"] is False
    assert good["execution_authorized"] is False
    assert len(browser.calls) == 1


def test_browser_approval_is_consumed_once(synthetic):
    job, _completed, _transport = synthetic.reviewed()
    approval = synthetic.agent.approve_prepare(job["job_id"], expires_at=synthetic.clock() + timedelta(seconds=60))
    browser = SyntheticBrowser()
    first = synthetic.agent.prepare_browser(job["job_id"], synthetic.contract(), approval_id=approval, adapter=browser)
    assert first["state"] == "PREPARED"
    with pytest.raises(ValueError, match="consumed"):
        synthetic.agent.prepare_browser(job["job_id"], synthetic.contract(), approval_id=approval, adapter=browser)
    assert len(browser.calls) == 1


def test_queue_idempotency_restart_and_duplicate_worker_do_not_recall_model(synthetic):
    first = synthetic.enqueue()
    second = synthetic.enqueue()
    assert first["job_id"] == second["job_id"]
    transport = SyntheticTransport(synthetic.clock)
    completed = synthetic.agent.worker_once("synthetic-worker", transport=transport)
    assert completed["state"] == "COMPLETED"
    restarted = LocalAgent(synthetic.agent.home, synthetic.trust["workspace_id"], clock=synthetic.clock)
    assert restarted.worker_once("synthetic-restarted-worker", transport=transport)["state"] == "IDLE"
    assert len(transport.calls) == 4
    assert restarted.preflight(first["job_id"])["state"] == "PREFLIGHT_CHECKS_PASSED"
    assert restarted.enqueue_review(synthetic.bundle, synthetic.config,
                                    idempotency_key="synthetic-review-once", round_id="synthetic-round")["job_id"] == first["job_id"]
    with pytest.raises(ValueError, match="idempotency"):
        restarted.enqueue_review(synthetic.bundle, synthetic.config,
                                  idempotency_key="synthetic-review-once", round_id="different-round")


def test_snapshot_replay_cannot_create_new_observation(synthetic):
    current = synthetic.agent.state.latest_snapshot(now=synthetic.clock())
    with pytest.raises(ValueError, match="replay"):
        synthetic.agent.state.import_snapshot(current, now=synthetic.clock())
    assert len([row for row in synthetic.agent.state.events() if row["event_type"] == "SNAPSHOT_IMPORTED"]) == 1


def test_serialized_bundle_bytes_are_verified_before_use(synthetic):
    packed = pack_bundle(synthetic.bundle)
    name = next(iter(packed["attachments"]))
    packed["attachments"][name] = b64encode(b"tampered synthetic bytes").decode()
    with pytest.raises(ValueError):
        unpack_bundle(packed)
