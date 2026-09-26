"""Authentication, durable lineage, conservative adapters, and offline CLI."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from keel_observability import ObservationError, ObservationStore, Verification, canonical_event, event_digest
from keel_observability.adapters import receipt_event, session_effort_event, source_feedback_snapshot
from keel_observability.store import KINDS, LINEAGE

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
SECRET = b"synthetic-unit-test-secret-not-production"


def event(kind, *, event_id=None, account="account-A", **overrides):
    values = {"source": (1, "2026-09-01T00:00:00Z", {"name": "Synthetic source", "evidence_ref": "test:source"}),
              "opportunity": (2, "2026-09-02T00:00:00Z", {"qualified": True, "evidence_ref": "test:opportunity"}),
              "application": (3, "2026-09-02T01:00:00Z", {"submitted_at": "2026-09-02T01:00:00Z", "fit_score": 85, "evidence_ref": "test:application"}),
              "attempt": (4, "2026-09-02T02:00:00Z", {"evidence_ref": "test:attempt"}),
              "effort": (2, "2026-09-02T03:00:00Z", {"human_minutes": 7.25, "measurement": "human_reported", "task_ids": ["review-1"], "evidence_ref": "test:effort"}),
              "outcome": (4, "2026-09-02T04:00:00Z", {"outcome": "SUBMISSION_CONFIRMED", "evidence_ref": "test:submission", "provider_receipt_id": "receipt-1", "receipt_sha256": "1" * 64}),
              "window": (1, NOW.isoformat(), {"window_start": "2026-09-02T00:00:00Z", "window_end": "2026-09-03T00:00:00Z", "observed_through": NOW.isoformat(), "complete": True, "evidence_ref": "test:window"}),
              "revocation": (0, NOW.isoformat(), {"target_event_id": "source-event", "target_sha256": "a" * 64, "reason": "human reviewed revocation"})}
    depth, at, payload = deepcopy(values[kind])
    lineage = {key: value if i < depth else None for i, (key, value) in enumerate(zip(LINEAGE, ("S", "O", "A", "T")))}
    return {"schema": "keel.observation.v1", "event_id": event_id or kind + "-event", "account_id": account,
            "producer_id": "synthetic-host", "kind": kind, "occurred_at": at, "lineage": lineage,
            "revisions": {"evidence": "e" * 64, "adapter": "b" * 64}, "payload": payload, **overrides}


def signature(value):
    return hmac.new(SECRET, canonical_event(value), hashlib.sha256).hexdigest()


def verifier(raw, context, now):
    if not isinstance(context, str) or not hmac.compare_digest(hmac.new(SECRET, raw, hashlib.sha256).hexdigest(), context):
        return None
    value = json.loads(raw)
    # Synthetic trusted configuration; a real host binds session/producer scopes.
    assurance = {"effort": "human_reported", "window": "host_measured", "outcome": "provider_verified"}.get(value["kind"], "authenticated_producer")
    return Verification("account-A", "synthetic-host", "test-operator", hashlib.sha256(raw).hexdigest(),
                        tuple(KINDS), now, now + 86400, assurance)


@pytest.fixture
def store(tmp_path):
    tmp_path.chmod(0o700)
    return ObservationStore.create(tmp_path / "observations.sqlite", store_id="workspace-test", verifier=verifier, clock=lambda: NOW.timestamp())


def put(store, value):
    return store.ingest(value, authenticate=True, context=signature(value))


def chain(store):
    for kind in ("source", "opportunity", "application", "attempt"):
        assert put(store, event(kind))["status"] == "CURRENT"


def test_import_cannot_self_certify_and_no_verifier_fails_closed(store):
    result = store.ingest(event("source"))
    assert result["trust"] == result["status"] == "UNVERIFIED"
    assert result["verification"] is None
    no_verifier = ObservationStore(store.path, store_id=store.store_id, clock=store.clock)
    with pytest.raises(ObservationError, match="host_verifier"):
        no_verifier.ingest(event("source"), authenticate=True, context=True)
    forged = event("source", authenticated=True)
    with pytest.raises(ObservationError, match="invalid_fields"):
        store.ingest(forged)


def test_payload_signature_mutation_and_bad_context_fail_before_write(store):
    original = event("source")
    changed = deepcopy(original)
    changed["payload"]["name"] = "Altered"
    for value, proof in ((original, None), (changed, signature(original))):
        with pytest.raises(ObservationError, match="host_verification"):
            store.ingest(value, authenticate=True, context=proof)
    assert store.events("account-A") == []


@pytest.mark.parametrize("change", [
    {"account_id": "account-B"}, {"producer_id": "someone-else"}, {"event_sha256": "f" * 64},
    {"allowed_kinds": ("effort",)}, {"verified_at": NOW.timestamp() + 1}, {"expires_at": NOW.timestamp()},
    {"assurance": "verified-because-json-says-so"}, {"verified_at": True}, {"expires_at": 10**1000}])
def test_verifier_must_return_exact_fresh_scoped_decision(store, change):
    original = verifier(canonical_event(event("source")), signature(event("source")), NOW.timestamp())
    store.verifier = lambda raw, context, now: replace(original, **change)
    with pytest.raises(ObservationError):
        put(store, event("source"))
    assert not store.events("account-A")


def test_callback_exception_does_not_leak_context_or_write(store):
    def fail(*args):
        raise RuntimeError("private token must not escape")
    store.verifier = fail
    with pytest.raises(ObservationError, match="^host_verification_failed$"):
        put(store, event("source"))
    assert not store.events("account-A")


def test_missing_or_cross_account_lineage_is_rejected(store):
    store.ingest(event("source"))
    with pytest.raises(ObservationError, match="lineage_not_authenticated"):
        put(store, event("opportunity"))
    put(store, event("source"))
    other = event("opportunity", account="account-B")
    store.verifier = lambda raw, context, now: Verification("account-B", "synthetic-host", "other-operator", hashlib.sha256(raw).hexdigest(), tuple(KINDS), now, now + 60)
    with pytest.raises(ObservationError, match="lineage_not_authenticated"):
        put(store, other)
    assert store.inspect("account-B", "source-event") is None


def test_exact_ancestry_prevents_cross_source_application_attachment(store):
    chain(store)
    source = event("source", event_id="source-2")
    source["lineage"]["source_id"] = "S2"
    put(store, source)
    crossed = event("effort")
    crossed["lineage"]["source_id"] = "S2"
    with pytest.raises(ObservationError, match="lineage_mismatch"):
        put(store, crossed)


def test_persistence_replay_and_import_promotion_are_idempotent(store):
    imported = store.ingest(event("source"))
    promoted = put(store, event("source"))
    assert imported["status"] == "UNVERIFIED" and promoted["status"] == "CURRENT"
    reopened = ObservationStore(store.path, store_id=store.store_id, verifier=verifier, clock=store.clock)
    replay = put(reopened, event("source"))
    assert replay["ingestion"] == "DUPLICATE" and replay["sequence"] == promoted["sequence"]
    assert len(reopened.events("account-A")) == 1
    assert not reopened.export("account-A")["execution_authorized"]


def test_unverified_collision_cannot_poison_authenticated_event(store):
    malicious = event("source")
    malicious["payload"]["name"] = "Wrong import"
    store.ingest(malicious)
    result = put(store, event("source"))
    assert result["status"] == "CURRENT" and result["event"]["payload"]["name"] == "Synthetic source"
    malicious["payload"]["name"] = "Another wrong import"
    result = store.ingest(malicious)
    assert result["status"] == "CURRENT"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM variants").fetchone()[0] == 3


def test_authenticated_payload_conflict_holds_descendants_and_survives_restart(store):
    chain(store)
    changed = event("opportunity")
    changed["payload"]["qualified"] = False
    assert put(store, changed)["status"] == "HELD"
    assert "ANCESTOR_HELD" in store.inspect("account-A", "attempt-event")["holds"]
    reopened = ObservationStore(store.path, store_id=store.store_id, verifier=verifier, clock=store.clock)
    assert reopened.inspect("account-A", "opportunity-event")["status"] == "HELD"
    assert put(reopened, event("opportunity"))["status"] == "HELD"


def test_two_authenticated_registration_ids_cannot_alias_one_entity(store):
    chain(store)
    conflict = event("source", event_id="duplicate-source")
    assert put(store, conflict)["ingestion"] == "HELD_CONFLICT"
    assert "ENTITY_ID_CONFLICT" in store.inspect("account-A", "source-event")["holds"]
    assert store.inspect("account-A", "application-event")["status"] == "HELD"


def test_revocation_is_authenticated_exact_digest_bound_and_transitive(store):
    chain(store)
    original = event("source")
    revoke = event("revocation")
    revoke["payload"]["target_sha256"] = event_digest(original)
    store.ingest(revoke)
    assert store.inspect("account-A", "attempt-event")["status"] == "CURRENT"
    put(store, revoke)
    assert store.inspect("account-A", "source-event")["holds"] == ["REVOKED"]
    assert store.inspect("account-A", "attempt-event")["status"] == "HELD"
    assert put(store, original)["status"] == "HELD"
    revoke["event_id"] = "wrong-target-revocation"
    revoke["payload"]["target_sha256"] = "f" * 64
    with pytest.raises(ObservationError, match="target_mismatch"):
        put(store, revoke)


def test_verification_expiry_is_not_evergreen_truth(store):
    chain(store)
    store.clock = lambda: NOW.timestamp() + 86401
    assert "VERIFICATION_EXPIRED" in store.inspect("account-A", "source-event")["holds"]
    assert store.inspect("account-A", "attempt-event")["status"] == "HELD"
    assert put(store, event("source"))["status"] == "CURRENT"


def test_monotonic_clock_and_store_identity_required(store):
    with pytest.raises(ObservationError, match="store_identity"):
        ObservationStore(store.path, store_id="wrong-workspace", clock=store.clock)
    store.clock = lambda: NOW.timestamp() - 1
    with pytest.raises(ObservationError, match="clock_regressed"):
        store.events("account-A")


def test_private_storage_and_symlink_or_hardlink_rejection(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(ObservationError, match="private"):
        ObservationStore.create(tmp_path / "unsafe.sqlite", store_id="private")
    tmp_path.chmod(0o700)
    store = ObservationStore.create(tmp_path / "safe.sqlite", store_id="private")
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(store.path)
    with pytest.raises(ObservationError, match="private"):
        ObservationStore(alias, store_id="private")
    os.link(store.path, tmp_path / "hardlink.sqlite")
    with pytest.raises(ObservationError, match="private"):
        store.events("account-A")


@pytest.mark.parametrize("minutes", [True, -1, "7", float("nan"), float("inf"), 1000001, 10**1000])
def test_malformed_actual_effort_is_rejected(store, minutes):
    chain(store)
    effort = event("effort")
    effort["payload"]["human_minutes"] = minutes
    with pytest.raises(ObservationError):
        store.ingest(effort)
    assert store.inspect("account-A", "effort-event") is None


def test_actual_effort_not_clamped_and_auth_assurance_must_match(store):
    chain(store)
    effort = event("effort")
    assert put(store, effort)["event"]["payload"]["human_minutes"] == 7.25
    effort["event_id"] = "effort-2"
    effort["payload"]["measurement"] = "host_measured"
    with pytest.raises(ObservationError, match="effort_assurance"):
        put(store, effort)


@pytest.mark.parametrize("change", [
    {"observed_through": "2026-09-02T12:00:00Z"}, {"window_end": "2026-09-30T00:00:00Z"},
    {"window_start": "2026-09-05T00:00:00Z"}, {"complete": "true"}, {"observed_through": "2026-10-01T00:00:00Z"}])
def test_false_window_completeness_is_rejected(store, change):
    value = event("window")
    value["payload"].update(change)
    with pytest.raises(ObservationError):
        store.ingest(value)


def test_incomplete_window_remains_incomplete(store):
    put(store, event("source"))
    window = event("window")
    window["payload"].update(complete=False, window_end="2026-09-30T00:00:00Z")
    assert put(store, window)["event"]["payload"]["complete"] is False


@pytest.mark.parametrize("mutation", ["naive", "future", "revision", "lineage", "oversize", "kind"])
def test_malformed_envelopes_cannot_enter_store(store, mutation):
    value = event("source")
    if mutation == "naive": value["occurred_at"] = "2026-09-01T00:00:00"
    if mutation == "future": value["occurred_at"] = "2027-01-01T00:00:00Z"
    if mutation == "revision": value["revisions"]["evidence"] = "not-a-hash"
    if mutation == "lineage": value["lineage"]["attempt_id"] = "T"
    if mutation == "oversize": value["payload"]["name"] = "x" * 65536
    if mutation == "kind": value["kind"] = []
    with pytest.raises(ObservationError):
        store.ingest(value)


def test_cursor_observes_authentication_and_revocation_changes(store):
    imported = store.ingest(event("source"))
    promoted = put(store, event("source"))
    assert store.events("account-A", after_sequence=imported["sequence"])[0]["sequence"] == promoted["sequence"]
    assert not store.events("account-A", after_sequence=promoted["sequence"])
    assert not store.events("account-B")


def policies():
    return [{"source_id": "S", "permitted": True, "cap_minutes": 30, "cooldown_until": None,
             "cooldown_verified": True, "evidence_ref": "test:reviewed-policy"}]


def feedback(store, **kwargs):
    return source_feedback_snapshot(store, "account-A", source_policies=policies(),
                                    window_start="2026-09-02T00:00:00Z", window_end="2026-09-03T00:00:00Z",
                                    observed_at=NOW.isoformat(), now=NOW, **kwargs)


def test_source_feedback_uses_real_minutes_and_requires_complete_authenticated_window(store):
    chain(store)
    put(store, event("effort"))
    result = feedback(store)
    assert not result["document"]["telemetry_complete"]
    assert result["document"]["observations"][0]["human_minutes"] == 7.25
    store.ingest(event("window"))
    assert not feedback(store)["document"]["telemetry_complete"]
    put(store, event("window"))
    result = feedback(store)
    assert result["document"]["telemetry_complete"]
    assert result["validation"]["sources"][0]["recorded_human_minutes"] == 7.25
    assert not result["execution_authorized"]


def test_unmeasured_effort_and_unattributed_shared_time_are_not_free(store):
    chain(store)
    put(store, event("window"))
    assert feedback(store)["document"]["observations"][0]["human_minutes"] is None
    shared = event("effort")
    shared["lineage"]["opportunity_id"] = None
    put(store, shared)
    assert "EFFORT_ATTRIBUTION_INCOMPLETE" in feedback(store)["holds"]


def test_mature_interview_cohort_requires_provider_receipt_and_complete_followup(store):
    chain(store)
    put(store, event("effort")); put(store, event("window"))
    put(store, event("outcome"))
    assert feedback(store, objective="interviews")["document"]["applications"] == []
    followup = event("window", event_id="application-followup")
    followup["lineage"].update(opportunity_id="O", application_id="A")
    followup["payload"].update(window_start="2026-09-02T01:00:00Z", window_end="2026-09-16T01:00:00Z")
    put(store, followup)
    interview = event("outcome", event_id="interview", occurred_at="2026-09-05T01:00:00Z")
    interview["payload"].update(outcome="INTERVIEW", evidence_ref="test:interview", provider_receipt_id="interview-1")
    put(store, interview)
    result = feedback(store, objective="interviews")
    assert result["document"]["telemetry_complete"]
    assert len(result["document"]["applications"]) == 1
    assert result["validation"]["outcome_report"]["denominator"] == 1


def test_authenticated_producer_receipt_is_not_provider_confirmation(store):
    chain(store)
    original = store.verifier
    store.verifier = lambda raw, context, now: replace(original(raw, context, now), assurance="authenticated_producer")
    put(store, event("outcome"))
    store.verifier = original
    put(store, event("effort")); put(store, event("window"))
    assert feedback(store, objective="interviews")["document"]["applications"] == []


def test_receipt_bridge_uses_existing_validation_and_exact_observed_ids():
    receipt = {"kind": "provider_receipt", "source": "fixture", "receipt_id": "receipt-1", "content_sha256": "c" * 64,
               "received_at": "2026-09-02T04:00:00Z", "recorded_at": NOW.isoformat(), "outcome": "AUTO_ACK",
               "role_id": "O", "application_id": "A", "attempt_id": "T", "verified": True}
    claim = {"role_id": "O", "application_id": "A", "attempt_id": "T", "date_submitted": "2026-09-02T01:00:00Z"}
    args = dict(claim=claim, event_id="receipt-event", account_id="account-A", producer_id="synthetic-host",
                lineage=event("outcome")["lineage"], adapter_revision="a" * 64, now=NOW)
    mapped = receipt_event(receipt, **args)
    assert mapped["payload"]["outcome"] == "ACKNOWLEDGMENT"
    from engines.outcome_tracking.receipt_intake import ProviderValidation
    validators = {"fixture": lambda normalized, bound: ProviderValidation(event_digest(normalized), event_digest(bound), "fixture", NOW.isoformat(), True)}
    assert receipt_event(receipt, provider_validators=validators, **args)["payload"]["outcome"] == "SUBMISSION_CONFIRMED"
    for change in ({"attempt_id": "OTHER"}, {"role_id": ""}, {"application_id": "OTHER"}):
        with pytest.raises(ObservationError, match="exact_lineage"):
            receipt_event({**receipt, **change}, **args)
    with pytest.raises(ObservationError, match="conflicted"):
        receipt_event(receipt, receipt_conflicted=True, **args)


def test_session_bridge_validates_real_projection_and_preserves_overrun():
    from keel_loki.common import digest
    from keel_muse.review import example_snapshot, project_review, record_session_effort
    snapshot = example_snapshot()
    report = project_review(snapshot, now=snapshot["captured_at"], session_minutes=5)
    qid = report["decision_session"]["selected_question_ids"][0]
    record = record_session_effort(report, event_id="session-effort", question_id=qid, human_minutes=7,
                                   now=report["as_of"], expected_report_sha256=digest(report))
    now = datetime.fromtimestamp(report["as_of"], timezone.utc)
    args = dict(report=report, account_id="account-A", producer_id="synthetic-host", lineage=event("effort")["lineage"], now=now)
    mapped = session_effort_event(record, **args)
    assert mapped["payload"]["human_minutes"] == 7
    assert "authentication" not in mapped
    altered = {**record, "completed_task_ids": ["fabricated"]}
    with pytest.raises(ObservationError, match="projection_mismatch"):
        session_effort_event(altered, **args)


def test_cli_init_import_inspect_export_never_self_certifies(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "cli.sqlite"
    base = [sys.executable, "-B", "-m", "keel_observability", "--db", str(path), "--store-id", "cli-workspace"]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    def run(*args):
        return subprocess.run(base + list(args), env=env, capture_output=True, text=True, check=False, timeout=15)
    assert json.loads(run("init").stdout)["authentication_configured"] is False
    value = event("source", occurred_at="2020-01-01T00:00:00Z")
    input_file = tmp_path / "event.json"
    input_file.write_text(json.dumps(value))
    assert json.loads(run("import", str(input_file)).stdout)["status"] == "UNVERIFIED"
    assert json.loads(run("inspect", "--account", "account-A", "--event-id", "source-event").stdout)["trust"] == "UNVERIFIED"
    assert json.loads(run("export", "--account", "account-A").stdout)["events"][0]["status"] == "UNVERIFIED"
    input_file.write_text('{"event_id":"one","event_id":"two"}')
    result = run("import", str(input_file))
    assert result.returncode == 2 and "duplicate_json_key" in result.stderr


def measurement(target, *, event_id="measurement-1", values=None, previous=None):
    return {"schema": "keel.observation.v1", "event_id": event_id, "account_id": target["account_id"],
            "producer_id": "synthetic-host", "kind": "measurement", "occurred_at": NOW.isoformat(),
            "lineage": deepcopy(target["lineage"]), "revisions": {"evidence": "c" * 64},
            "payload": {"target_event_id": target["event_id"], "target_sha256": event_digest(target),
                        "previous_event_id": previous["event_id"] if previous else None,
                        "previous_sha256": event_digest(previous) if previous else None,
                        "values": values if values is not None else {"qualified": True}, "evidence_ref": "test:new-measurement"}}


def test_new_evidence_observation_reuses_exact_lineage_without_reregistering_identity(store):
    chain(store)
    evidence = event("effort", event_id="evidence-v2", payload={"evidence_ref": "test:document-v2"})
    evidence["kind"] = "evidence"
    assert put(store, evidence)["status"] == "CURRENT"
    assert store.inspect("account-A", "opportunity-event")["status"] == "CURRENT"


def test_unknown_opportunity_can_gain_measured_qualification(store):
    put(store, event("source"))
    original = event("opportunity")
    original["payload"]["qualified"] = None
    put(store, original)
    put(store, event("effort")); put(store, event("window"))
    assert feedback(store)["document"]["observations"][0]["qualified"] is None
    revised = measurement(original)
    assert put(store, revised)["measurement_current"]
    assert feedback(store)["document"]["observations"][0]["qualified"] is True
    assert store.inspect("account-A", original["event_id"])["event"]["payload"]["qualified"] is None
    assert put(store, revised)["ingestion"] == "DUPLICATE"


def test_measurement_predecessor_pin_prevents_stale_overwrite_and_old_refresh_cannot_roll_back(store):
    chain(store)
    first = measurement(event("opportunity"))
    put(store, first)
    stale = measurement(event("opportunity"), event_id="stale-measurement", values={"qualified": False})
    with pytest.raises(ObservationError, match="head_changed"):
        put(store, stale)
    second = measurement(event("opportunity"), event_id="measurement-2", values={"qualified": False}, previous=first)
    assert put(store, second)["measurement_current"]
    assert not store.inspect("account-A", first["event_id"])["measurement_current"]
    store.clock = lambda: NOW.timestamp() + 1
    assert not put(store, first)["measurement_current"]
    assert store.inspect("account-A", second["event_id"])["measurement_current"]


def test_application_unknown_fields_gain_exact_measurement_without_identity_conflict(store):
    put(store, event("source")); put(store, event("opportunity"))
    original = event("application")
    original["payload"].update(submitted_at=None, fit_score=None)
    put(store, original)
    update = measurement(original, values={"submitted_at": "2026-09-02T01:00:00Z", "fit_score": 88})
    assert put(store, update)["status"] == "CURRENT"
    assert store.inspect("account-A", original["event_id"])["status"] == "CURRENT"
    wrong = measurement(original, event_id="wrong-target-fields", previous=update)
    with pytest.raises(ObservationError, match="target_fields"):
        put(store, wrong)


def test_measurement_must_bind_current_target_and_full_lineage(store):
    chain(store)
    value = measurement(event("opportunity"))
    value["payload"]["target_sha256"] = "f" * 64
    with pytest.raises(ObservationError, match="target_not_current"):
        put(store, value)
    value = measurement(event("opportunity"))
    value["lineage"].update(application_id="A")
    with pytest.raises(ObservationError, match="lineage_or_kind"):
        put(store, value)


def test_measurement_revocation_holds_snapshot_instead_of_using_old_value(store):
    chain(store)
    put(store, event("effort")); put(store, event("window"))
    revised = measurement(event("opportunity"), values={"qualified": False})
    put(store, revised)
    revoke = event("revocation", event_id="revoke-measurement")
    revoke["payload"].update(target_event_id=revised["event_id"], target_sha256=event_digest(revised))
    put(store, revoke)
    snapshot = feedback(store)
    assert not snapshot["document"]["telemetry_complete"]
    from engines.source_feedback import propose
    assert propose(snapshot["document"], budget_minutes=20, now=NOW)["allocated_minutes"] == 0


def test_known_receipt_duplicates_and_conflicts_cannot_multiply_confirmations(store):
    chain(store)
    put(store, event("outcome"))
    duplicate = event("outcome", event_id="same-receipt-new-id")
    assert "DUPLICATE_RECEIPT" in put(store, duplicate)["holds"]
    changed = event("outcome", event_id="conflicting-receipt")
    changed["payload"]["receipt_sha256"] = "f" * 64
    assert "RECEIPT_ID_CONFLICT" in put(store, changed)["holds"]
    assert store.inspect("account-A", "outcome-event")["status"] == "HELD"


def test_exact_acknowledgment_can_gain_provider_acceptance_without_receipt_conflict(store):
    chain(store)
    ack = event("outcome", event_id="ack-event")
    ack["payload"]["outcome"] = "ACKNOWLEDGMENT"
    put(store, ack)
    assert put(store, event("outcome"))["status"] == "CURRENT"
    assert store.inspect("account-A", "ack-event")["status"] == "CURRENT"


def test_producer_only_ack_cannot_preempt_a_provider_receipt_binding(store):
    chain(store)
    original_verifier = store.verifier
    store.verifier = lambda raw, context, now: replace(original_verifier(raw, context, now), assurance="authenticated_producer")
    ack = event("outcome", event_id="producer-ack")
    ack["payload"]["outcome"] = "ACKNOWLEDGMENT"
    put(store, ack)
    store.verifier = original_verifier
    assert put(store, event("outcome"))["status"] == "CURRENT"


def test_later_incomplete_window_cannot_reuse_earlier_completion_claim(store):
    chain(store); put(store, event("effort")); put(store, event("window"))
    incomplete = event("window", event_id="window-incomplete")
    incomplete["payload"].update(complete=False, observed_through="2026-09-02T12:00:00Z")
    put(store, incomplete)
    snapshot = feedback(store)
    assert not snapshot["document"]["telemetry_complete"]
    assert not snapshot["document"]["sources"][0]["measurement_complete"]


@pytest.mark.parametrize("untrusted", [False, True])
def test_plain_exported_feedback_document_cannot_bypass_adapter_holds(store, untrusted):
    chain(store); put(store, event("effort")); put(store, event("window"))
    extra = event("effort", event_id="additional-effort")
    if untrusted:
        store.ingest(extra)
    else:
        extra["lineage"]["opportunity_id"] = None
        put(store, extra)
    from engines.source_feedback import propose
    snapshot = feedback(store)
    assert not snapshot["document"]["telemetry_complete"]
    result = propose(snapshot["document"], budget_minutes=20, now=NOW)
    assert result["allocated_minutes"] == 0


def test_stored_digest_corruption_fails_closed_without_repairing_bytes(store):
    put(store, event("source"))
    with sqlite3.connect(store.path) as db:
        bad = event("source")
        bad["payload"]["name"] = "tampered"
        db.execute("UPDATE variants SET document=?", (json.dumps(bad),))
    with pytest.raises(ObservationError, match="stored_payload_digest"):
        store.inspect("account-A", "source-event")


def test_writable_nonsticky_ancestor_is_rejected_even_with_private_child(tmp_path):
    shared = tmp_path / "shared"
    private = shared / "private"
    shared.mkdir(mode=0o777); shared.chmod(0o777)
    private.mkdir(mode=0o700)
    with pytest.raises(ObservationError, match="writable_nonsticky"):
        ObservationStore.create(private / "db.sqlite", store_id="test")


def test_concurrent_replay_uses_one_observation(store):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: put(store, event("source")), range(24)))
    assert sum(result["ingestion"] == "AUTHENTICATED" for result in results) == 1
    assert len(store.events("account-A")) == 1


def key_policy(**updates):
    from keel_observability import ProducerKey
    return ProducerKey("key-1", "account-A", "synthetic-host", "local-worker", hashlib.sha256(SECRET).digest(),
                       tuple(KINDS), **updates)


def hmac_context(value, key=None, **overrides):
    from keel_observability import sign_context
    return sign_context(value, key=key or key_policy(), store_id="workspace-test", issued_at=int(NOW.timestamp()),
                        expires_at=int(NOW.timestamp()) + 60, nonce="request-1") | overrides


def test_local_hmac_verifier_binds_payload_and_host_policy(store):
    from keel_observability import HMACProducerVerifier
    store.verifier = HMACProducerVerifier(store_id=store.store_id, keys={"key-1": key_policy()})
    value = event("source")
    result = store.ingest(value, authenticate=True, context=hmac_context(value))
    assert result["status"] == "CURRENT" and result["verification"]["assurance"] == "authenticated_producer"
    assert store.ingest(value, authenticate=True, context=hmac_context(value))["ingestion"] == "DUPLICATE"
    changed = deepcopy(value); changed["payload"]["name"] = "different"
    with pytest.raises(ObservationError, match="host_verification_failed"):
        store.ingest(changed, authenticate=True, context=hmac_context(value))


@pytest.mark.parametrize("change", [{"store_id": "other"}, {"account_id": "other"}, {"producer_id": "other"},
                                     {"key_id": "missing"}, {"expires_at": int(NOW.timestamp())},
                                     {"nonce": "changed"}, {"issued_at": True}, {"assurance": "provider_verified"}])
def test_hmac_context_replay_across_scope_or_modified_fields_fails(store, change):
    from keel_observability import HMACProducerVerifier
    store.verifier = HMACProducerVerifier(store_id=store.store_id, keys={"key-1": key_policy()})
    with pytest.raises(ObservationError, match="host_verification_failed"):
        store.ingest(event("source"), authenticate=True, context=hmac_context(event("source"), **change))


def test_hmac_verifier_cannot_be_attached_to_wrong_store_or_revoked_key(store):
    from keel_observability import HMACProducerVerifier
    value = event("source")
    store.verifier = HMACProducerVerifier(store_id="other-workspace", keys={"key-1": key_policy()})
    with pytest.raises(ObservationError, match="verifier_store"):
        store.ingest(value, authenticate=True, context=hmac_context(value))
    for key in (key_policy(revoked=True), key_policy(expires_at=int(NOW.timestamp()) - 1),
                key_policy(not_before=int(NOW.timestamp()) + 1)):
        store.verifier = HMACProducerVerifier(store_id=store.store_id, keys={"key-1": key})
        with pytest.raises(ObservationError, match="host_verification_failed"):
            store.ingest(value, authenticate=True, context=hmac_context(value))


def test_hmac_key_rotation_and_no_provider_or_human_assurance(store):
    from keel_observability import HMACProducerVerifier, sign_context
    from dataclasses import replace
    first = key_policy()
    second = replace(first, key_id="key-2", secret=hashlib.sha256(b"new-synthetic-key-material").digest())
    store.verifier = HMACProducerVerifier(store_id=store.store_id, keys={"key-1": replace(first, revoked=True), "key-2": second})
    signed = sign_context(event("source"), key=second, store_id=store.store_id,
                          issued_at=int(NOW.timestamp()), expires_at=int(NOW.timestamp()) + 60, nonce="rotation")
    assert store.ingest(event("source"), authenticate=True, context=signed)["status"] == "CURRENT"
    for kind in ("opportunity", "application", "attempt"):
        value = event(kind)
        context = sign_context(value, key=second, store_id=store.store_id, issued_at=int(NOW.timestamp()), expires_at=int(NOW.timestamp()) + 60, nonce=kind)
        store.ingest(value, authenticate=True, context=context)
    outcome = event("outcome")
    context = sign_context(outcome, key=second, store_id=store.store_id, issued_at=int(NOW.timestamp()), expires_at=int(NOW.timestamp()) + 60, nonce="outcome")
    assert store.ingest(outcome, authenticate=True, context=context)["verification"]["assurance"] == "authenticated_producer"
    effort = event("effort")
    context = sign_context(effort, key=second, store_id=store.store_id, issued_at=int(NOW.timestamp()), expires_at=int(NOW.timestamp()) + 60, nonce="effort")
    with pytest.raises(ObservationError, match="effort_assurance"):
        store.ingest(effort, authenticate=True, context=context)


def test_short_hmac_keys_and_too_long_context_lifetime_rejected(store):
    from keel_observability import HMACProducerVerifier, ProducerKey, sign_context
    with pytest.raises(ObservationError, match="32_random_bytes"):
        ProducerKey("key", "account-A", "synthetic-host", "actor", b"short")
    store.verifier = HMACProducerVerifier(store_id=store.store_id, keys={"key-1": key_policy()})
    context = sign_context(event("source"), key=key_policy(), store_id=store.store_id,
                           issued_at=int(NOW.timestamp()), expires_at=int(NOW.timestamp()) + 301, nonce="too-long")
    with pytest.raises(ObservationError, match="host_verification_failed"):
        store.ingest(event("source"), authenticate=True, context=context)


def test_receipt_store_later_conflict_can_be_reconciled_without_fabricating_auth(store, tmp_path):
    from engines.outcome_tracking.receipt_intake import ReceiptStore, ProviderValidation
    from keel_observability.adapters import reconcile_receipt_observations
    chain(store)
    receipts = ReceiptStore(tmp_path / "receipts.json")
    receipt = {"kind": "provider_receipt", "source": "fixture", "receipt_id": "receipt-1", "content_sha256": "c" * 64,
               "received_at": "2026-09-02T04:00:00Z", "recorded_at": "2026-09-02T05:00:00Z", "outcome": "AUTO_ACK",
               "role_id": "O", "application_id": "A", "attempt_id": "T"}
    receipts.put(receipt, now=NOW)
    claim = {"role_id": "O", "application_id": "A", "attempt_id": "T", "date_submitted": "2026-09-02T01:00:00Z"}
    validators = {"fixture": lambda normalized, bound: ProviderValidation(event_digest(normalized), event_digest(bound), "fixture", NOW.isoformat(), True)}
    mapped = receipt_event(receipt, claim=claim, event_id="synced-receipt", account_id="account-A", producer_id="synthetic-host",
                           lineage=event("outcome")["lineage"], adapter_revision="a" * 64, now=NOW, provider_validators=validators)
    put(store, mapped)
    assert not reconcile_receipt_observations(store, receipts, account_id="account-A", producer_id="synthetic-host", now=NOW)["revocations"]
    receipts.put({**receipt, "content_sha256": "f" * 64}, now=NOW)
    proposed = reconcile_receipt_observations(store, receipts, account_id="account-A", producer_id="synthetic-host", now=NOW)
    assert proposed["requires_host_revocations"] and len(proposed["revocations"]) == 1
    assert store.inspect("account-A", "synced-receipt")["status"] == "CURRENT"
    snapshot = source_feedback_snapshot(store, "account-A", source_policies=policies(),
                                        window_start="2026-09-02T00:00:00Z", window_end="2026-09-03T00:00:00Z",
                                        observed_at=NOW.isoformat(), now=NOW, receipt_store=receipts)
    assert "RECEIPT_RECONCILIATION_REQUIRED" in snapshot["holds"] and not snapshot["document"]["telemetry_complete"]
    result = reconcile_receipt_observations(store, receipts, account_id="account-A", producer_id="synthetic-host", now=NOW, context_factory=signature)
    assert result["results"][0]["status"] == "CURRENT"
    assert store.inspect("account-A", "synced-receipt")["holds"] == ["REVOKED"]
