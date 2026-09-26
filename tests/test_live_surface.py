"""Private review/Workbench integration; every record and identity is fictional."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from keel_live.review import Principal, REVIEW_ACTIONS, ReviewError
from keel_live.surface import LiveSurface
from keel_sources.store import SourceStore
from keel_trust.common import digest
from keel_workbench.api import APIError
from keel_workbench.service import Conflict
from tools.make_live_demo import create_demo


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return create_demo(tmp_path / "synthetic-demo")


def surface(demo, **kwargs):
    return LiveSurface(demo.store, demo.body, action="PREPARE", principal=demo.principal,
                       synthetic=True, **kwargs)


def context(demo):
    return {"scope": deepcopy(demo.scope), "flow_sha256": digest(demo.flow)}


def request(demo, app):
    return app.review_command("request", {**context(demo), "confirmed": True,
        "expires_at": (demo.clock() + timedelta(minutes=5)).isoformat()})


def decide(demo, app, pending, decision="APPROVE"):
    return app.review_command("decide", {**context(demo), "confirmed": True,
        "request_id": pending["request_id"], "decision": decision,
        "reviewed_sha256": pending["review_sha256"],
        "expires_at": (demo.clock() + timedelta(minutes=2)).isoformat()})


def test_read_views_do_not_create_requests_or_write_sources(demo):
    before = demo.store.events()
    app = surface(demo)
    overview = app.dispatch("GET", "/api/live/v1/review")
    detail = app.dispatch("POST", "/api/live/v1/review/show", context(demo))
    assert overview["current"] and overview["synthetic"]
    assert overview["operator"]["actor_id"] == demo.principal.actor_id
    assert detail["current_request"] is None
    assert detail["semantic_validation"]["ready_for_approval"]
    assert demo.store.events() == before
    assert not overview["execution_authorized"]


def test_unregistered_scope_displays_absence_without_registering(demo, tmp_path):
    store = SourceStore(tmp_path / "empty-store", demo.store.workspace_id, clock=demo.clock)
    app = LiveSurface(store, demo.body, action="PREPARE", principal=demo.principal, synthetic=True)
    before = store.events()
    detail = app.review_command("show", context(demo))
    assert detail["scope_registered"] is False and detail["sources"] == {}
    assert detail["current_request"] is None
    assert {row["reason"] for row in detail["revision_diagnostics"]["components"].values()} == {"scope_not_registered"}
    assert store.events() == before


def test_explicit_review_lifecycle_reprojects_without_authorizing(demo):
    app = surface(demo)
    pending = request(demo, app)
    assert pending["state"] == "PENDING"
    shown = app.review_command("show", context(demo))
    assert shown["current_request"]["review_sha256"] == pending["review_sha256"]
    approved = decide(demo, app, pending)
    assert approved["state"] == "APPROVED"
    assert approved["decision"]["actor_id"] == demo.principal.actor_id
    assert approved["decision"]["authority_record_ref"] == demo.principal.authority_record_ref
    view = app.dispatch("GET", "/api/v1/overview")
    assert view["roles"][0]["sources"]["approval"]["status"] == "READY"
    assert view["counts"]["review_checks_passed"] == 0  # canonical checks still absent
    revoked = app.review_command("revoke", {**context(demo), "confirmed": True,
        "request_id": pending["request_id"], "reason": "Synthetic operator withdrawal"})
    assert revoked["state"] == "REVOKED"
    assert not app.dispatch("GET", "/api/live/v1/proof")["execution_authorized"]


def test_reject_is_immutable_and_does_not_become_approval(demo):
    app = surface(demo)
    pending = request(demo, app)
    assert decide(demo, app, pending, "REJECT")["state"] == "REJECTED"
    with pytest.raises(ValueError, match="immutable"):
        decide(demo, app, pending)


@pytest.mark.parametrize("field,value", [("actor_id", "attacker"), ("authority_record_ref", "injected"),
    ("attachment_root", "/tmp"), ("body_provider", "payload"), ("principal", {})])
def test_http_payload_cannot_select_identity_or_host_paths(demo, field, value):
    app = surface(demo)
    before = demo.store.events()
    with pytest.raises(ValueError):
        app.review_command("request", {**context(demo), "confirmed": True,
            "expires_at": (demo.clock() + timedelta(minutes=5)).isoformat(), field: value})
    assert demo.store.events() == before


@pytest.mark.parametrize("confirmed", [False, None, 1, "true"])
def test_confirmation_is_explicit_boolean(demo, confirmed):
    app = surface(demo)
    with pytest.raises(ValueError, match="confirmation"):
        app.review_command("request", {**context(demo), "confirmed": confirmed,
            "expires_at": (demo.clock() + timedelta(minutes=5)).isoformat()})
    assert demo.review.inspect(demo.principal, demo.scope)["current_request"] is None


@pytest.mark.parametrize("field", ["workspace_id", "role_id", "application_id", "action"])
def test_exact_scope_is_bound_to_current_flow(demo, field):
    app = surface(demo)
    payload = context(demo)
    payload["scope"][field] = "other"
    with pytest.raises(Conflict, match="SCOPE_CHANGED"):
        app.review_command("show", payload)


def test_current_flow_digest_is_mandatory(demo):
    app = surface(demo)
    payload = context(demo)
    payload["flow_sha256"] = "f" * 64
    with pytest.raises(Conflict, match="HOST_EXPORT_CHANGED"):
        app.review_command("show", payload)


def test_workspace_refuses_broader_document_than_principal(demo):
    principal = Principal("synthetic-reader", "synthetic:auth", demo.store.workspace_id,
                          REVIEW_ACTIONS, ())
    with pytest.raises(ReviewError, match="access_denied"):
        LiveSurface(demo.store, demo.body, action="PREPARE", principal=principal, synthetic=True)


def test_read_only_principal_cannot_create_decision(demo):
    principal = Principal("synthetic-reader", "synthetic:auth", demo.store.workspace_id,
                          frozenset({"review:read"}), (demo.scope,))
    app = LiveSurface(demo.store, demo.body, action="PREPARE", principal=principal, synthetic=True)
    assert app.review_overview()["allowed_actions"] == ["review:read"]
    with pytest.raises(ReviewError, match="access_denied"):
        request(demo, app)


def test_unknown_action_and_snapshot_import_cannot_mutate_host(demo):
    with pytest.raises(ValueError, match="PREPARE"):
        LiveSurface(demo.store, demo.body, action="SUBMIT", principal=demo.principal)
    app = surface(demo)
    with pytest.raises(APIError) as result:
        app.dispatch("POST", "/api/v1/snapshot", {"snapshot": {}, "previous_sha256": "a" * 64})
    assert result.value.status == 405
    assert "post" not in app.dispatch("GET", "/openapi.json")["paths"]["/api/v1/snapshot"]


def test_stale_export_keeps_original_time_and_blocks_decision(demo):
    app = surface(demo)
    pending = request(demo, app)
    before = demo.store.events()
    observed = demo.flow["observed_at"]
    demo.clock.advance(91)
    overview = app.review_overview()
    assert not overview["current"] and overview["observed_at"] == observed
    assert not app.dispatch("GET", "/api/v1/overview")["current"]
    shown = app.review_command("show", context(demo))
    assert not shown["current_request"]["request_actionable"]
    with pytest.raises(Conflict, match="STALE"):
        decide(demo, app, pending)
    assert demo.store.events() == before


def test_provider_refreshes_same_host_path_but_never_silently_reuses_failed_read(demo):
    state = {"body": demo.body}
    def provider():
        if state.get("fail"):
            raise OSError("synthetic unreadable host file")
        return deepcopy(state["body"])
    app = surface(demo, body_provider=provider)
    before = app.review_overview()["flow_sha256"]
    demo.clock.advance(2)
    state["body"]["flow"]["observed_at"] = demo.clock().isoformat()
    assert app.review_overview()["flow_sha256"] != before
    state["fail"] = True
    with pytest.raises(APIError) as error:
        app.review_overview()
    assert error.value.code == "HOST_EXPORT_UNAVAILABLE"
    with pytest.raises(APIError):
        request(demo, app)
    assert demo.review.inspect(demo.principal, demo.scope)["current_request"] is None


def test_material_changes_invalidate_display_and_exact_decision(demo):
    app = surface(demo)
    pending = request(demo, app)
    changed = deepcopy(demo.sources["answers"])
    changed["source_version"] = "fixture-v2"
    changed["record"]["fields"]["name"] = "Another Synthetic Applicant"
    demo.put("answers", changed)
    assert app.review_command("show", context(demo))["current_request"]["state"] == "STALE"
    with pytest.raises(ValueError, match="material_changed"):
        decide(demo, app, pending)


def test_canonical_route_mismatch_blocks_every_live_surface(demo):
    demo.decide(demo.request())
    demo.bind_synthetic_checks()
    demo.flow["leads"][0]["route"] = "api"
    demo.flow["leads"][0]["provider_contract_validated"] = True
    # Author deliberately conflicting fixture evidence through fixture helpers.
    # Production capture and the normal demo binder correctly reject this.
    from tools.make_assurance_demo import make_envelope
    from keel_workbench.model import project
    demo.trust["flow_export"] = deepcopy(demo.flow)
    demo.assurance = make_envelope(demo.flow)
    baseline = project(demo.export()["workbench_snapshot"], attachment_root=demo.store.attachment_root,
                       now=demo.clock())
    assert baseline["roles"][0]["review_checks_passed"]
    app = surface(demo)
    for path in ("/api/v1/overview", "/api/live/v1/review"):
        view = app.dispatch("GET", path)
        assert not view["roles"][0]["review_checks_passed"]
        assert "source_route_binding_mismatch" in view["roles"][0]["reasons"]
    shown = app.review_command("show", context(demo))
    assert not shown["semantic_validation"]["ready_for_approval"]
    assert not shown["current_request"]["approval_currently_valid"]
    with pytest.raises(Conflict, match="CANONICAL_BINDING"):
        request(demo, app)
    view = app.dispatch("GET", "/api/v1/overview")
    command = {"request_id": "fixture-material-review", "workflow_id": "material-review",
               "snapshot_sha256": view["snapshot_sha256"], "role_ids": [], "options": {}}
    report = app.dispatch("POST", "/api/v1/run", command)
    assert not report["result"]["roles"][0]["review_checks_passed"]


def test_workflow_digest_stays_usable_without_renewing_wrapper_observation(demo):
    app = surface(demo)
    view = app.dispatch("GET", "/api/v1/overview")
    snapshot = app.dispatch("GET", "/api/v1/snapshot")
    observed = snapshot["revision_sources"]["snapshot"]["observed_at"]
    demo.clock.advance(3)
    assert app.dispatch("GET", "/api/v1/overview")["snapshot_sha256"] == view["snapshot_sha256"]
    assert app.dispatch("GET", "/api/v1/snapshot")["revision_sources"]["snapshot"]["observed_at"] == observed
    command = {"request_id": "fixture-workflow", "workflow_id": "source-repair",
               "snapshot_sha256": view["snapshot_sha256"], "role_ids": [], "options": {}}
    result = app.dispatch("POST", "/api/v1/run", command)
    assert result == app.dispatch("POST", "/api/v1/run", command)
    assert result["result"]["system_tasks"] > 0
    assert not result["execution_authorized"]


def test_approval_waiting_for_writer_cannot_outlive_canonical_flow(demo, monkeypatch):
    from contextlib import contextmanager
    app = surface(demo)
    pending = request(demo, app)
    before = demo.store.events()
    original = demo.store.transaction
    @contextmanager
    def queued(*args, **kwargs):
        demo.clock.advance(91)
        with original(*args, **kwargs) as conn:
            yield conn
    monkeypatch.setattr(demo.store, 'transaction', queued)
    with pytest.raises(ValueError, match='fresh canonical flow'):
        decide(demo, app, pending)
    assert demo.store.events() == before
    assert demo.review.get(demo.principal, demo.scope, pending['request_id'])['decision'] is None


def test_host_body_change_inside_transaction_rolls_back_decision(demo, monkeypatch):
    state = {'body': demo.body}
    app = surface(demo, body_provider=lambda: deepcopy(state['body']))
    pending = request(demo, app)
    before = demo.store.events()
    original = demo.store.write_source
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        state['body']['flow']['source_revision'] = 'SYNTHETIC-CONCURRENT-HOST-CHANGE'
        return result
    monkeypatch.setattr(demo.store, 'write_source', changed)
    with pytest.raises(Conflict, match='HOST_EXPORT_CHANGED'):
        decide(demo, app, pending)
    assert demo.store.events() == before
    assert demo.review.get(demo.principal, demo.scope, pending['request_id'])['decision'] is None


def test_positive_synthetic_fixture_remains_explicitly_non_authorizing(demo):
    demo.decide(demo.request())
    demo.bind_synthetic_checks()
    app = surface(demo)
    view = app.dispatch("GET", "/api/v1/overview")
    assert view["counts"]["review_checks_passed"] == 1
    proof = app.dispatch("GET", "/api/live/v1/proof")
    assert proof["counts"]["synthetic_capture_review_checks_passed"] == 1
    assert proof["counts"]["verified_rendered_preparations"] == 0
    assert not proof["execution_authorized"]
