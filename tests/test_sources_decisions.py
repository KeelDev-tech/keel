"""Explicit synthetic approvals; no actual user decision or application action."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import sqlite3

import pytest

from keel_agent.revisions import PREAPPROVAL_COMPONENTS, export_revisions
from keel_sources.decisions import prepare_request, decide_request, revoke_request, get_request
from keel_sources.store import SourceStore
from test_sources_capture import NOW, SCOPE, complete_sources


def stamp(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def setup_store(tmp_path, *, missing=None, mutate=None):
    clock = [NOW]
    store = SourceStore(tmp_path / "private", SCOPE["workspace_id"], clock=lambda: clock[0])
    store.register_scope(SCOPE)
    sources = complete_sources(store.attachment_root)
    if mutate:
        mutate(sources)
    for component, source in sources.items():
        if component != missing:
            store.put_source(SCOPE, component, source, expected_generation=0)
    return store, sources, clock


def request(store, *, expiry=1200):
    return prepare_request(store, SCOPE, expires_at=stamp(expiry))


def decide(store, pending, **kwargs):
    arguments = {"decision": "APPROVE", "actor_id": "synthetic-human",
                 "authority_record_ref": "synthetic:authority", "reviewed_sha256": pending["review_sha256"],
                 "expires_at": stamp(300)}
    arguments.update(kwargs)
    return decide_request(store, pending["request_id"], **arguments)


def export(store):
    return export_revisions(store.export_snapshot(), attachment_root=store.attachment_root,
                            now=store.clock())


def counts(store):
    with sqlite3.connect(store.db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                if name in tables else 0 for name in ("source_approval_requests", "source_approval_decisions",
                "source_approval_revocations", "source_records", "source_events")}


def update(store, sources, component, *, mutate=None, version="v2", observed_at=None, expires_at=None):
    descriptor = copy.deepcopy(sources[component])
    if mutate:
        mutate(descriptor["record"])
    descriptor["source_version"] = version
    if observed_at is not None:
        descriptor["observed_at"] = observed_at
    if expires_at is not None:
        descriptor["expires_at"] = expires_at
    with store.transaction() as connection:
        generation = store.current_generation(connection, SCOPE, component)
        store.write_source(connection, SCOPE, component, descriptor, generation)
    sources[component] = descriptor


@pytest.mark.parametrize("missing", PREAPPROVAL_COMPONENTS)
def test_missing_upstream_does_not_create_human_request_or_any_record(tmp_path, missing):
    store, _, _ = setup_store(tmp_path, missing=missing)
    before = counts(store)
    with pytest.raises(ValueError, match="prerequisites_not_ready"):
        request(store)
    assert counts(store) == before
    assert export(store)["human_root_cause_count"] == 0


def test_semantic_hold_prevents_request_before_any_write(tmp_path):
    store, _, _ = setup_store(tmp_path, mutate=lambda s: s["policy"]["record"]["rules"].update(holds=["dedupe"]))
    before = counts(store)
    with pytest.raises(ValueError, match="prerequisites_not_ready"):
        request(store)
    assert counts(store) == before
    assert not export(store)["human_root_cause_count"]


def test_private_review_packet_contains_exact_material_and_actual_attachment_hash(tmp_path):
    store, sources, _ = setup_store(tmp_path)
    pending = request(store)
    assert pending["state"] == "PENDING"
    assert pending["is_current"]
    payload = pending["review_payload"]
    assert payload["scope"] == SCOPE
    for component in PREAPPROVAL_COMPONENTS:
        assert payload["sources"][component]["record"] == sources[component]["record"]
        assert payload["sources"][component]["source_version"] == "v1"
        assert "observed_at" not in payload["sources"][component]
        assert pending["source_observations"][component]["expires_at"] == stamp(600)
    attachment = payload["verified_attachments"]["files"][0]
    assert attachment["manifest"]["path"] == "resume.txt"
    assert attachment["sha256"] == hashlib.sha256((store.attachment_root / "resume.txt").read_bytes()).hexdigest()
    assert not pending["execution_authorized"] and not pending["source_authenticity_verified"]
    assert len(pending["component_revisions"]) == 6
    report = export(store)
    assert report["human_root_cause_count"] == 1
    assert report["roles"][0]["components"]["approval"]["reason"] == "human_decision_required"


def test_decision_produces_seventh_revision_without_execution_or_authenticity_claim(tmp_path):
    store, _, _ = setup_store(tmp_path)
    pending = request(store)
    result = decide(store, pending)
    assert result["state"] == "APPROVED"
    assert result["decision"]["actor_id"] == "synthetic-human"
    assert not result["execution_authorized"] and not result["source_authenticity_verified"]
    report = export(store)
    assert report["ready_role_count"] == 1
    assert len(report["roles"][0]["revisions"]) == 7
    with store.transaction() as connection:
        record = store.read_scope(connection, SCOPE)["sources"]["approval"]["record"]
    assert record["component_revisions"] == pending["component_revisions"]
    assert record["approved_at"] == NOW.isoformat()
    assert record["decision"] == "APPROVE"


@pytest.mark.parametrize("decision", ["APPROVE", "REJECT"])
def test_request_accepts_one_immutable_decision_only(tmp_path, decision):
    store, _, _ = setup_store(tmp_path)
    pending = request(store)
    decide(store, pending, decision=decision)
    before = counts(store)
    with pytest.raises(ValueError, match="decision_immutable"):
        decide(store, pending, decision="REJECT" if decision == "APPROVE" else "APPROVE")
    assert counts(store) == before


def test_reject_remains_reject_and_cannot_be_reasked_for_identical_material(tmp_path):
    store, _, _ = setup_store(tmp_path)
    pending = request(store)
    result = decide(store, pending, decision="REJECT")
    assert result["state"] == "REJECTED"
    assert export(store)["roles"][0]["components"]["approval"]["reason"] == "approval_rejected"
    with pytest.raises(ValueError, match="material_already_decided"):
        request(store)
    with pytest.raises(ValueError, match="not_approved"):
        revoke_request(store, pending["request_id"], actor_id="synthetic-human", reason="fixture")


@pytest.mark.parametrize("component", PREAPPROVAL_COMPONENTS)
def test_any_changed_material_requires_new_review(tmp_path, component):
    store, sources, _ = setup_store(tmp_path)
    pending = request(store)
    update(store, sources, component, mutate=lambda record: record.update(metadata={"actual_change": "synthetic"}))
    before = counts(store)
    with pytest.raises(ValueError, match="material_changed|prerequisites_not_ready"):
        decide(store, pending)
    assert counts(store) == before


def test_changed_actual_attachment_bytes_cannot_be_approved(tmp_path):
    store, _, _ = setup_store(tmp_path)
    pending = request(store)
    (store.attachment_root / "resume.txt").write_bytes(b"DIFFERENT SYNTHETIC CONTENT\n")
    before = counts(store)
    with pytest.raises(ValueError, match="source_material_integrity_failed|prerequisites_not_ready"):
        decide(store, pending)
    assert counts(store) == before


def test_reobservation_of_same_material_preserves_review_beyond_original_freshness(tmp_path):
    store, sources, clock = setup_store(tmp_path)
    pending = request(store)
    clock[0] = NOW + timedelta(seconds=700)
    for component in PREAPPROVAL_COMPONENTS:
        update(store, sources, component, version="v1", observed_at=stamp(700), expires_at=stamp(1300))
    result = decide(store, pending, expires_at=stamp(1100))
    assert result["state"] == "APPROVED"
    assert result["review_sha256"] == pending["review_sha256"]
    assert result["decision"]["source_generations"]["answers"] > pending["source_generations"]["answers"]
    assert export(store)["ready_role_count"] == 1


@pytest.mark.parametrize("change,code", [
    ({"reviewed_sha256": "0" * 64}, "review_digest_mismatch"),
    ({"reviewed_sha256": "not-a-hash"}, "review_digest_invalid"),
    ({"decision": "PASS"}, "decision_invalid"),
    ({"expires_at": stamp(-1)}, "decision_expiry_invalid"),
    ({"expires_at": stamp(601)}, "decision_expiry_invalid"),
    ({"actor_id": ""}, "identifier_invalid"),
    ({"authority_record_ref": ""}, "identifier_invalid"),
])
def test_invalid_decision_has_no_partial_effect(tmp_path, change, code):
    store, _, _ = setup_store(tmp_path)
    pending = request(store)
    before = counts(store)
    with pytest.raises(ValueError, match=code):
        decide(store, pending, **change)
    assert counts(store) == before
    assert get_request(store, pending["request_id"])["state"] == "PENDING"


def test_request_expiry_checked_before_decision_write(tmp_path):
    store, _, clock = setup_store(tmp_path)
    pending = request(store, expiry=100)
    clock[0] = NOW + timedelta(seconds=101)
    before = counts(store)
    with pytest.raises(ValueError, match="request_expired"):
        decide(store, pending)
    assert counts(store) == before
    assert get_request(store, pending["request_id"])["state"] == "EXPIRED"


def test_stale_source_blocks_decision_even_before_request_expires(tmp_path):
    store, _, clock = setup_store(tmp_path)
    pending = request(store)
    clock[0] = NOW + timedelta(seconds=601)
    before = counts(store)
    with pytest.raises(ValueError, match="prerequisites_not_ready"):
        decide(store, pending, expires_at=stamp(900))
    assert counts(store) == before


def test_concurrent_decisions_only_one_can_commit(tmp_path):
    store, _, _ = setup_store(tmp_path)
    pending = request(store)
    def attempt(choice):
        try:
            return decide(store, pending, decision=choice)["state"]
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ["APPROVE", "REJECT"]))
    assert "approval_decision_immutable" in results
    assert len(set(results) & {"APPROVED", "REJECTED"}) == 1
    assert counts(store)["source_approval_decisions"] == 1


def test_fault_after_source_write_rolls_back_decision_and_approval_together(tmp_path, monkeypatch):
    store, _, _ = setup_store(tmp_path)
    pending = request(store)
    before = counts(store)
    original = store.audit_event
    def fail(connection, kind, *args, **kwargs):
        if kind == "APPROVAL_DECIDED":
            raise RuntimeError("synthetic crash before commit")
        return original(connection, kind, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(store, "audit_event", fail)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            decide(store, pending)
    assert counts(store) == before
    assert get_request(store, pending["request_id"])["state"] == "PENDING"
    assert export(store)["human_root_cause_count"] == 1


def test_fault_after_pending_source_write_rolls_back_request(tmp_path, monkeypatch):
    store, _, _ = setup_store(tmp_path)
    before = counts(store)
    original = store.audit_event
    def fail(connection, kind, *args, **kwargs):
        if kind == "APPROVAL_REQUESTED":
            raise RuntimeError("synthetic crash before commit")
        return original(connection, kind, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(store, "audit_event", fail)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            request(store)
    assert counts(store) == before
    assert export(store)["human_root_cause_count"] == 0


def test_revocation_is_immutable_and_blocks_export_even_after_original_expiry(tmp_path):
    store, _, clock = setup_store(tmp_path)
    pending = request(store)
    original = decide(store, pending, expires_at=stamp(100))
    clock[0] = NOW + timedelta(seconds=101)
    revoked = revoke_request(store, pending["request_id"], actor_id="synthetic-human", reason="Explicit fixture withdrawal")
    assert revoked["state"] == "REVOKED"
    assert revoked["decision"] == original["decision"]
    assert revoked["revocation"]["revoked_at"] == stamp(101)
    assert export(store)["roles"][0]["components"]["approval"]["reason"] == "approval_revoked"
    with pytest.raises(ValueError, match="already_revoked"):
        revoke_request(store, pending["request_id"], actor_id="synthetic-human", reason="again")


@pytest.mark.parametrize("terminal", ["expired", "revoked"])
def test_expired_or_revoked_approval_can_receive_explicit_new_request_without_fake_source_changes(tmp_path, terminal):
    store, _, clock = setup_store(tmp_path)
    pending = request(store)
    decide(store, pending, expires_at=stamp(100))
    if terminal == "expired":
        clock[0] = NOW + timedelta(seconds=101)
    else:
        revoke_request(store, pending["request_id"], actor_id="synthetic-human", reason="fixture")
    fresh = request(store)
    assert fresh["state"] == "PENDING"
    assert fresh["request_id"] != pending["request_id"]
    assert fresh["component_revisions"] == pending["component_revisions"]
    assert counts(store)["source_approval_decisions"] == 1
    assert not fresh["execution_authorized"]


def test_explicit_changed_material_request_supersedes_pending_and_old_cannot_commit(tmp_path):
    store, sources, _ = setup_store(tmp_path)
    old = request(store)
    update(store, sources, "answers", mutate=lambda record: record["fields"].update(name="Synthetic Corrected Name"))
    current = request(store)
    assert current["component_revisions"] != old["component_revisions"]
    assert get_request(store, old["request_id"])["state"] == "SUPERSEDED"
    with pytest.raises(ValueError, match="request_superseded"):
        decide(store, old)
    assert decide(store, current)["state"] == "APPROVED"


def test_approval_cannot_be_revoked_by_naming_superseded_request(tmp_path):
    store, sources, _ = setup_store(tmp_path)
    old = request(store)
    decide(store, old)
    update(store, sources, "answers", mutate=lambda record: record["fields"].update(name="Synthetic Corrected Name"))
    fresh = request(store)
    before = counts(store)
    with pytest.raises(ValueError, match="request_superseded"):
        revoke_request(store, old["request_id"], actor_id="synthetic-human", reason="fixture")
    assert counts(store) == before
    assert get_request(store, fresh["request_id"])["state"] == "PENDING"


def test_record_updates_and_deletes_are_rejected(tmp_path):
    store, _, _ = setup_store(tmp_path)
    pending = request(store)
    decide(store, pending)
    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE source_approval_decisions SET decision='REJECT'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM source_approval_requests")


def test_api_accepts_aware_datetime_and_rejects_naive_expiry(tmp_path):
    store, _, _ = setup_store(tmp_path)
    with pytest.raises(ValueError, match="timestamp_invalid"):
        prepare_request(store, SCOPE, expires_at=NOW.replace(tzinfo=None))
    pending = prepare_request(store, SCOPE, expires_at=NOW + timedelta(seconds=1200))
    assert decide(store, pending, expires_at=NOW + timedelta(seconds=300))["state"] == "APPROVED"


@pytest.mark.parametrize("change", ["material", "freshness"])
def test_unactionable_pending_request_is_reported_without_changing_source(tmp_path, change):
    store, sources, clock = setup_store(tmp_path)
    pending = request(store)
    if change == "material":
        update(store, sources, "answers", mutate=lambda record: record["fields"].update(name="Synthetic Corrected Name"))
    else:
        clock[0] = NOW + timedelta(seconds=601)
    before = counts(store)
    result = get_request(store, pending["request_id"])
    assert result["state"] == ("STALE" if change == "material" else "BLOCKED")
    assert not result["request_actionable"]
    if change == "material":
        assert result["current_material_matches"] is False
        assert result["reason"] == "approval_material_changed"
    assert result["review_payload"] == pending["review_payload"]
    assert counts(store) == before


def test_get_request_is_read_only_even_when_clock_advances_or_no_request_exists(tmp_path):
    store, _, clock = setup_store(tmp_path)
    before = store.db_path.read_bytes()
    with pytest.raises(ValueError, match="request_not_found"):
        get_request(store, "synthetic-nonexistent")
    assert store.db_path.read_bytes() == before
    pending = request(store)
    before = store.db_path.read_bytes()
    mtime = store.db_path.stat().st_mtime_ns
    clock[0] = NOW + timedelta(seconds=5)
    result = get_request(store, pending["request_id"])
    assert result["state"] == "PENDING" and result["request_actionable"]
    assert result["current_material_matches"] is True
    assert store.db_path.read_bytes() == before
    assert store.db_path.stat().st_mtime_ns == mtime
