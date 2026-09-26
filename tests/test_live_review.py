"""Synthetic host principals exercise the review boundary, never real consent."""
import copy
from dataclasses import FrozenInstanceError
from datetime import timedelta
import hashlib
import sqlite3
from contextlib import contextmanager

import pytest

from keel_agent.revisions import PREAPPROVAL_COMPONENTS
from keel_live.review import Principal, REVIEW_ACTIONS, ReviewError, ReviewService
from keel_sources import decisions
from test_sources_capture import NOW, SCOPE
from test_sources_decisions import setup_store, stamp, counts, update


def principal(*, scope=None, actions=REVIEW_ACTIONS, **changes):
    arguments = {"actor_id": "fixture-operator", "authority_record_ref": "fixture:host-authority",
                 "workspace_id": SCOPE["workspace_id"], "allowed_actions": actions,
                 "allowed_scopes": (scope or SCOPE,)}
    arguments.update(changes)
    return Principal(**arguments)


def pending(service, actor=None):
    return service.request(actor or principal(), SCOPE, expires_at=stamp(1200))


def approve(service, request, actor=None, **changes):
    arguments = {"decision": "APPROVE", "reviewed_sha256": request["review_sha256"],
                 "expires_at": stamp(300)}
    arguments.update(changes)
    return service.decide(actor or principal(), SCOPE, request["request_id"], **arguments)


def db_hash(store):
    return hashlib.sha256(store.db_path.read_bytes()).hexdigest()


def test_principal_copies_host_permissions_and_scopes_and_is_immutable():
    mutable_scope = dict(SCOPE)
    actions = set(REVIEW_ACTIONS)
    actor = principal(scope=mutable_scope, actions=actions)
    mutable_scope["role_id"] = "outside-role"
    actions.clear()
    assert actor.allowed_scopes[0] == SCOPE
    assert actor.allowed_actions == REVIEW_ACTIONS
    with pytest.raises(TypeError):
        actor.allowed_scopes[0]["role_id"] = "outside-role"
    with pytest.raises(FrozenInstanceError):
        actor.actor_id = "spoofed"


@pytest.mark.parametrize("changes,code", [
    ({"actor_id": "not an identifier"}, "principal_invalid"),
    ({"allowed_actions": {"*"}}, "permissions_invalid"),
    ({"allowed_scopes": ({**SCOPE, "workspace_id": "other-workspace"},)}, "workspace_mismatch"),
    ({"allowed_scopes": (SCOPE, SCOPE)}, "duplicate_scope"),
    ({"allowed_scopes": ({**SCOPE, "extra": "ignored"},)}, "scope_invalid"),
])
def test_invalid_host_principal_fails_closed(changes, code):
    with pytest.raises(ReviewError, match=code):
        principal(**changes)


def test_scope_inspection_with_missing_sources_is_read_only_and_not_human_blocked(tmp_path):
    store, _, _ = setup_store(tmp_path, missing="answers")
    service = ReviewService(store)
    before, digest = counts(store), db_hash(store)
    result = service.inspect(principal(), SCOPE)
    assert result["current_request"] is None
    assert not result["semantic_validation"]["ready_for_approval"]
    assert not result["revision_diagnostics"]["envelope_inputs_ready"]
    assert result["revision_diagnostics"]["components"]["approval"]["responsibility"] == "system"
    assert result["identity_authentication"] == "host_responsibility"
    assert not result["execution_authorized"]
    assert counts(store) == before
    assert db_hash(store) == digest


@pytest.mark.parametrize("component", PREAPPROVAL_COMPONENTS)
def test_explicit_request_with_missing_source_creates_nothing(tmp_path, component):
    store, _, _ = setup_store(tmp_path, missing=component)
    before = counts(store)
    with pytest.raises(ValueError, match="prerequisites_not_ready"):
        pending(ReviewService(store))
    assert counts(store) == before


@pytest.mark.parametrize("operation,permission", [
    ("inspect", "review:read"), ("request", "review:request"),
    ("get", "review:read"), ("decide", "review:decide"), ("revoke", "review:revoke"),
])
def test_operation_permission_is_enforced_before_any_store_mutation(tmp_path, operation, permission):
    store, _, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    actor = principal(actions=REVIEW_ACTIONS - {permission})
    payload = {"scope": SCOPE}
    if operation == "request":
        payload["expires_at"] = stamp(1200)
    if operation in ("get", "decide", "revoke"):
        payload["request_id"] = request["request_id"]
    if operation == "decide":
        payload.update(decision="APPROVE", reviewed_sha256=request["review_sha256"], expires_at=stamp(300))
    if operation == "revoke":
        payload["reason"] = "fixture withdrawal"
    before = counts(store)
    with pytest.raises(ReviewError, match="access_denied"):
        service.dispatch(actor, operation, payload)
    assert counts(store) == before


@pytest.mark.parametrize("key", ("actor_id", "authority_record_ref", "principal", "execution_authorized"))
def test_json_payload_cannot_spoof_actor_or_authority(tmp_path, key):
    store, _, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    before = counts(store)
    payload = {"scope": SCOPE, "request_id": request["request_id"], "decision": "APPROVE",
               "reviewed_sha256": request["review_sha256"], "expires_at": stamp(300), key: "spoofed"}
    with pytest.raises(ReviewError, match="payload_invalid"):
        service.dispatch(principal(), "decide", payload)
    assert counts(store) == before


def test_actor_dict_is_not_a_host_principal(tmp_path):
    store, _, _ = setup_store(tmp_path)
    with pytest.raises(ReviewError, match="principal_required"):
        ReviewService(store).inspect({"actor_id": "fixture-operator"}, SCOPE)


@pytest.mark.parametrize("field", ("workspace_id", "role_id", "application_id", "action"))
def test_all_four_scope_dimensions_bound_to_host_grant(tmp_path, field):
    store, _, _ = setup_store(tmp_path)
    with pytest.raises(ReviewError, match="access_denied"):
        ReviewService(store).inspect(principal(), {**SCOPE, field: "different"})


def test_request_id_cannot_cross_two_individually_permitted_scopes(tmp_path):
    store, _, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    other = {**SCOPE, "application_id": "other-application"}
    store.register_scope(other)
    actor = principal(allowed_scopes=(SCOPE, other))
    before = counts(store)
    with pytest.raises(ReviewError, match="request_scope_mismatch"):
        service.get(actor, other, request["request_id"])
    with pytest.raises(ReviewError, match="request_scope_mismatch"):
        service.decide(actor, other, request["request_id"], decision="APPROVE",
                       reviewed_sha256=request["review_sha256"], expires_at=stamp(300))
    assert counts(store) == before


def test_explicit_request_and_decision_bind_trusted_actor_and_exact_packet(tmp_path):
    store, _, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = service.dispatch(principal(), "request", {"scope": SCOPE, "expires_at": stamp(1200)})
    assert request["state"] == "PENDING"
    assert not request["approval_currently_valid"]
    before, digest = counts(store), db_hash(store)
    viewed = service.get(principal(), SCOPE, request["request_id"])
    assert viewed["review_payload"] == request["review_payload"]
    assert viewed["review_sha256"] == request["review_sha256"]
    assert counts(store) == before and db_hash(store) == digest
    result = approve(service, request)
    assert result["state"] == result["recorded_state"] == "APPROVED"
    assert result["approval_currently_valid"]
    assert result["decision"]["actor_id"] == "fixture-operator"
    assert result["decision"]["authority_record_ref"] == "fixture:host-authority"
    assert not result["execution_authorized"] and not result["source_authenticity_verified"]
    assert service.inspect(principal(), SCOPE)["current_request"]["request_id"] == request["request_id"]


def test_mismatched_displayed_digest_cannot_decide(tmp_path):
    store, _, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    before = counts(store)
    with pytest.raises(ValueError, match="review_digest_mismatch"):
        approve(service, request, reviewed_sha256="0" * 64)
    assert counts(store) == before


def test_expired_request_does_not_produce_decision(tmp_path):
    store, _, clock = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    clock[0] = NOW + timedelta(seconds=1300)
    before = counts(store)
    with pytest.raises(ValueError, match="request_expired"):
        approve(service, request, expires_at=stamp(1400))
    assert counts(store) == before
    assert service.get(principal(), SCOPE, request["request_id"])["state"] == "EXPIRED"


def test_source_change_after_display_blocks_decision(tmp_path):
    store, sources, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    update(store, sources, "answers", mutate=lambda record: record["fields"].update(name="Changed fixture"))
    before = counts(store)
    assert service.get(principal(), SCOPE, request["request_id"])["state"] == "STALE"
    with pytest.raises(ValueError, match="material_changed"):
        approve(service, request)
    assert counts(store) == before


def test_mutation_between_wrapper_check_and_decision_still_fails(tmp_path, monkeypatch):
    store, sources, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    original = decisions.decide_request
    def raced(*args, **kwargs):
        update(store, sources, "answers", mutate=lambda record: record["fields"].update(name="Raced fixture"))
        return original(*args, **kwargs)
    monkeypatch.setattr(decisions, "decide_request", raced)
    with pytest.raises(ValueError, match="material_changed"):
        approve(service, request)
    assert counts(store)["source_approval_decisions"] == 0


def test_historical_approval_is_not_presented_as_current_after_source_change(tmp_path):
    store, sources, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    approve(service, request)
    update(store, sources, "answers", mutate=lambda record: record["fields"].update(name="Changed fixture"))
    before, digest = counts(store), db_hash(store)
    result = service.get(principal(), SCOPE, request["request_id"])
    assert result["recorded_state"] == "APPROVED"
    assert result["state"] == "STALE"
    assert not result["approval_currently_valid"]
    assert not result["execution_authorized"]
    assert counts(store) == before and db_hash(store) == digest


def test_approval_after_attachment_byte_change_is_blocked(tmp_path):
    store, _, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    approve(service, request)
    (store.attachment_root / "resume.txt").write_bytes(b"DIFFERENT SYNTHETIC ATTACHMENT\n")
    with pytest.raises(ValueError, match="source_material_integrity_failed"):
        service.get(principal(), SCOPE, request["request_id"])


def test_expired_approval_is_read_only_and_not_currently_valid(tmp_path):
    store, _, clock = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    approve(service, request)
    clock[0] = NOW + timedelta(seconds=301)
    result = service.get(principal(), SCOPE, request["request_id"])
    assert result["state"] == "EXPIRED"
    assert not result["approval_currently_valid"]


def test_rejection_remains_immutable_and_identical_material_cannot_be_reasked(tmp_path):
    store, _, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    result = approve(service, request, decision="REJECT")
    assert result["state"] == "REJECTED"
    assert not result["approval_currently_valid"]
    before = counts(store)
    with pytest.raises(ValueError, match="material_already_decided"):
        pending(service)
    with pytest.raises(ValueError, match="decision_immutable"):
        approve(service, request)
    assert counts(store) == before


def test_revoke_records_host_actor_and_keeps_immutable_original_decision(tmp_path):
    store, _, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    approve(service, request)
    revoker = principal(actor_id="fixture-second-operator", authority_record_ref="fixture:second-authority")
    result = service.revoke(revoker, SCOPE, request["request_id"], reason="Fixture consent withdrawn")
    assert result["state"] == "REVOKED"
    assert result["decision"]["actor_id"] == "fixture-operator"
    assert result["revocation"]["actor_id"] == "fixture-second-operator"
    assert result["revocation"]["reason"] == "Fixture consent withdrawn"
    assert not result["approval_currently_valid"]
    assert not result["execution_authorized"]


def test_revoke_also_requires_matching_scope(tmp_path):
    store, _, _ = setup_store(tmp_path)
    service = ReviewService(store)
    request = pending(service)
    approve(service, request)
    other = {**SCOPE, "role_id": "other-role"}
    actor = principal(allowed_scopes=(SCOPE, other))
    with pytest.raises(ReviewError, match="request_scope_mismatch"):
        service.revoke(actor, other, request["request_id"], reason="Fixture")
    assert counts(store)["source_approval_revocations"] == 0


def test_read_does_not_create_missing_schema_or_register_unknown_scope(tmp_path):
    store, _, _ = setup_store(tmp_path)
    service = ReviewService(store)
    before, digest = counts(store), db_hash(store)
    with pytest.raises(ReviewError, match="request_not_found"):
        service.get(principal(), SCOPE, "fixture-unknown-request")
    other = {**SCOPE, "role_id": "unknown-role"}
    with pytest.raises(ValueError, match="scope_not_registered"):
        service.inspect(principal(scope=other), other)
    assert counts(store) == before and db_hash(store) == digest


def test_read_rejects_partially_missing_approval_schema_without_recreating_it(tmp_path):
    store, _, _ = setup_store(tmp_path)
    service = ReviewService(store)
    pending(service)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE source_approval_revocations")
    before = db_hash(store)
    with pytest.raises(ReviewError, match="schema_incomplete"):
        service.inspect(principal(), SCOPE)
    assert db_hash(store) == before


@pytest.mark.parametrize("operation", ("request", "decide", "revoke"))
def test_transaction_guard_runs_after_write_lock_and_again_before_commit(tmp_path, operation):
    store, _, clock = setup_store(tmp_path)
    plain = ReviewService(store)
    request = None if operation == "request" else pending(plain)
    if operation == "revoke":
        approve(plain, request)
    observed = []
    def guard(now):
        observed.append(now)
        if len(observed) == 1:
            clock[0] = NOW + timedelta(seconds=2)
    service = ReviewService(store, transaction_guard=guard)
    if operation == "request":
        pending(service)
    elif operation == "decide":
        approve(service, request)
    else:
        service.revoke(principal(), SCOPE, request["request_id"], reason="Fixture")
    assert observed == [NOW, NOW + timedelta(seconds=2)]


@pytest.mark.parametrize("operation", ("request", "decide", "revoke"))
def test_guard_failure_before_commit_rolls_back_whole_operation(tmp_path, operation):
    store, _, _ = setup_store(tmp_path)
    plain = ReviewService(store)
    request = None if operation == "request" else pending(plain)
    if operation == "revoke":
        approve(plain, request)
    calls = []
    def guard(now):
        calls.append(now)
        if len(calls) == 2:
            raise ReviewError("fixture_canonical_flow_expired")
    service = ReviewService(store, transaction_guard=guard)
    before, digest = counts(store), db_hash(store)
    with pytest.raises(ReviewError, match="canonical_flow_expired"):
        if operation == "request":
            pending(service)
        elif operation == "decide":
            approve(service, request)
        else:
            service.revoke(principal(), SCOPE, request["request_id"], reason="Fixture")
    assert len(calls) == 2
    assert counts(store) == before and db_hash(store) == digest


def test_guard_checks_after_simulated_lock_wait_not_just_http_precheck(tmp_path, monkeypatch):
    store, _, clock = setup_store(tmp_path)
    request = pending(ReviewService(store))
    original = store.transaction
    @contextmanager
    def waited(*args, **kwargs):
        # Simulate lock acquisition completing after canonical freshness ends.
        clock[0] = NOW + timedelta(seconds=91)
        with original(*args, **kwargs) as conn:
            yield conn
    monkeypatch.setattr(store, "transaction", waited)
    def guard(now):
        if (now - NOW).total_seconds() > 90:
            raise ReviewError("fixture_canonical_flow_expired")
    service = ReviewService(store, transaction_guard=guard)
    before = counts(store)
    with pytest.raises(ReviewError, match="canonical_flow_expired"):
        approve(service, request)
    assert counts(store) == before


def test_json_payload_cannot_supply_transaction_guard(tmp_path):
    store, _, _ = setup_store(tmp_path)
    with pytest.raises(ReviewError, match="payload_invalid"):
        ReviewService(store).dispatch(principal(), "request", {
            "scope": SCOPE, "expires_at": stamp(1200), "transaction_guard": None})
