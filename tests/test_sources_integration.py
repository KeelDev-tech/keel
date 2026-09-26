"""Cross-module source producer tests; all records and people are fictional."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from keel_agent.revisions import COMPONENTS, PREAPPROVAL_COMPONENTS
from keel_sources import __main__ as cli
from keel_sources.decisions import get_request, revoke_request
from keel_sources.service import build_export, register_flow_scopes, status_summary
from keel_sources.store import SourceStore
from tools.make_flow_demo import NOW, make_snapshot
from tools.make_source_producer_demo import SyntheticClock, SyntheticProducer, fixture_scope, fixture_sources


@pytest.fixture(autouse=True)
def isolated_audit_directory(tmp_path, monkeypatch):
    # The unchanged legacy audit guard interprets descriptor-relative paths
    # against cwd; both that path and the actual FD target stay in test scratch.
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def producer(tmp_path):
    value = SyntheticProducer(tmp_path / "synthetic-fixture")
    value.capture()
    return value


def test_837_registered_scopes_remain_missing_without_human_requests(tmp_path):
    store = SourceStore(tmp_path / "store", fixture_scope()["workspace_id"], clock=SyntheticClock())
    for index in range(1, 838):
        store.register_scope(fixture_scope(index))
    result = build_export(store)
    revision = result["revision_report"]
    assert result["scope_count"] == 837
    assert result["producer_inputs_complete_count"] == 0
    assert result["source_record_coverage"] == dict.fromkeys(COMPONENTS, 0)
    assert revision["system_root_cause_count"] == 7
    assert revision["human_root_cause_count"] == 0
    assert {row["affected_scopes"] for row in revision["root_causes"]} == {837}
    assert all(not row["revisions"] for row in revision["roles"])
    assert sum(len(row["components"]) for row in revision["roles"]) == 5859
    assert not result["execution_authorized"]


def test_capture_six_then_explicit_request_then_explicit_decision(producer):
    before = producer.export()
    assert before["source_record_coverage"] == {key: int(key != "approval") for key in COMPONENTS}
    assert before["revision_report"]["human_root_cause_count"] == 0
    assert before["producer_inputs_complete_count"] == 0
    assert before["rows"][0]["semantic_validation"]["ready_for_approval"]
    request = producer.request()
    assert request["state"] == "PENDING"
    assert producer.export()["revision_report"]["human_root_cause_count"] == 1
    decision = producer.decide_synthetic(request)
    assert decision["state"] == "APPROVED"
    result = producer.export()
    assert result["producer_inputs_complete_count"] == 1
    assert len(result["revision_report"]["roles"][0]["revisions"]) == 7
    assert result["source_authenticity_verified"] is False
    assert result["execution_authorized"] is False
    assert result["canonical_writes"] == 0


def test_fresh_observation_same_material_keeps_review_challenge(producer):
    request = producer.request()
    producer.clock.advance(4)
    old_revision = request["component_revisions"]["policy"]
    descriptor = deepcopy(producer.sources["policy"])
    descriptor["observed_at"] = producer.clock().isoformat()
    descriptor["expires_at"] = (producer.clock() + timedelta(minutes=10)).isoformat()
    written = producer.put("policy", descriptor)
    assert written["revision"] == old_revision
    assert producer.decide_synthetic(request)["state"] == "APPROVED"
    assert producer.export()["producer_inputs_complete_count"] == 1


def test_changed_material_during_review_refuses_decision(producer):
    request = producer.request()
    producer.changed_answer()
    with pytest.raises(ValueError, match="approval_material_changed"):
        producer.decide_synthetic(request)
    assert get_request(producer.store, request["request_id"])["decision"] is None
    assert producer.export()["producer_inputs_complete_count"] == 0


def test_changed_material_after_approval_blocks_export(producer):
    producer.approve_synthetic()
    producer.changed_answer()
    result = producer.export()
    assert result["producer_inputs_complete_count"] == 0
    assert result["revision_report"]["roles"][0]["components"]["approval"]["reason"] == "approval_dependency_mismatch"


@pytest.mark.parametrize("invalid_value,code", [
    ("Not an available option", "answer_select_option_invalid"),
    ("", "answer_required_empty"),
    (True, "answer_text_type_invalid"),
])
def test_structurally_valid_wrong_required_answer_blocks_request(tmp_path, invalid_value, code):
    producer = SyntheticProducer(tmp_path / "fixture")
    producer.sources["answers"]["record"]["fields"]["location"] = invalid_value
    producer.capture()
    result = producer.export()
    issues = result["rows"][0]["semantic_validation"]["issues"]
    assert any(issue["code"] == code for issue in issues)
    assert len(result["revision_report"]["roles"][0]["revisions"]) == 6
    with pytest.raises(ValueError, match="approval_prerequisites_not_ready"):
        producer.request()
    assert producer.export()["revision_report"]["human_root_cause_count"] == 0


@pytest.mark.parametrize("changed_component", PREAPPROVAL_COMPONENTS)
def test_every_material_family_invalidates_approval(producer, changed_component):
    producer.approve_synthetic()
    descriptor = deepcopy(producer.sources[changed_component])
    descriptor["source_version"] = "fixture-v2"
    descriptor["record"]["metadata"]["synthetic_material_change"] = True
    producer.put(changed_component, descriptor)
    assert producer.export()["producer_inputs_complete_count"] == 0


def test_reject_is_real_record_but_never_ready(producer):
    producer.decide_synthetic(producer.request(), "REJECT")
    result = producer.export()
    assert result["source_record_coverage"]["approval"] == 1
    assert result["revision_report"]["roles"][0]["components"]["approval"]["reason"] == "approval_rejected"
    assert result["producer_inputs_complete_count"] == 0


def test_revocation_survives_restart(producer):
    decision = producer.approve_synthetic()
    revoke_request(producer.store, decision["request_id"], actor_id="synthetic-operator",
                   reason="Synthetic revocation")
    producer.store = SourceStore(producer.store.home, producer.scope["workspace_id"], clock=producer.clock)
    assert get_request(producer.store, decision["request_id"])["state"] == "REVOKED"
    result = producer.export()
    assert result["producer_inputs_complete_count"] == 0
    assert result["revision_report"]["roles"][0]["components"]["approval"]["reason"] == "approval_revoked"


def test_expired_approval_does_not_expire_sources_early(producer):
    producer.approve_synthetic()
    producer.clock.advance(121)
    result = producer.export()
    assert result["producer_inputs_complete_count"] == 0
    assert len(result["revision_report"]["roles"][0]["revisions"]) == 6


def test_stale_sources_cannot_be_refreshed_by_export(producer):
    original_times = {key: value["observed_at"] for key, value in producer.sources.items()}
    producer.clock.advance(601)
    result = producer.export()
    sources = result["snapshot"]["roles"][0]["sources"]
    assert {key: sources[key]["observed_at"] for key in PREAPPROVAL_COMPONENTS} == original_times
    assert not result["rows"][0]["semantic_validation"]["ready_for_approval"]
    with pytest.raises(ValueError, match="approval_prerequisites_not_ready"):
        producer.request()


def test_readonly_queries_leave_database_bytes_unchanged(producer):
    producer.approve_synthetic()
    before = producer.store.db_path.read_bytes()
    producer.clock.advance(1)
    build_export(producer.store)
    status_summary(build_export(producer.store))
    producer.store.events(limit=1000)
    assert producer.store.db_path.read_bytes() == before


def test_restart_preserves_original_descriptors_and_bindings(producer):
    producer.approve_synthetic()
    before = producer.export()
    producer.store = SourceStore(producer.store.home, producer.scope["workspace_id"], clock=producer.clock)
    after = producer.export()
    assert after == before
    for component in PREAPPROVAL_COMPONENTS:
        assert after["snapshot"]["roles"][0]["sources"][component] == producer.sources[component]


def test_actual_attachment_bytes_are_retained_in_content_addressed_store(producer):
    manifest = producer.sources["attachments"]["record"]["files"][0]
    original = producer.source_root / manifest["source_path"]
    captured = producer.store.attachment_root / manifest["path"]
    assert original.read_bytes() == captured.read_bytes()
    assert captured.stat().st_mode & 0o777 == 0o600
    original.unlink()
    assert producer.export()["rows"][0]["semantic_validation"]["ready_for_approval"]


def test_canonical_flow_scope_registration_never_captures_sources(tmp_path):
    clock = SyntheticClock()
    store = SourceStore(tmp_path / "store", fixture_scope()["workspace_id"], clock=clock)
    flow = make_snapshot()
    result = register_flow_scopes(store, flow, action="PREPARE")
    assert result["registered_scope_count"] == len(flow["leads"])
    assert result["source_records_created"] == result["approval_requests_created"] == 0
    assert build_export(store)["revision_report"]["human_root_cause_count"] == 0
    clock.advance(91)
    with pytest.raises(ValueError, match="fresh canonical flow export required"):
        register_flow_scopes(store, flow, action="PREPARE")


def test_flow_join_requires_exact_application_identity(producer):
    producer.approve_synthetic()
    flow = make_snapshot()
    assert producer.export(flow=flow)["producer_inputs_complete_count"] == 1
    flow["leads"][0]["identity"] = "a" * 64
    result = producer.export(flow=flow)
    assert result["producer_inputs_complete_count"] == 0
    assert result["rows"][0]["current_flow_identity_matched"] is False
    assert all(value is None for value in result["rows"][0]["columns"].values())


def test_cli_full_capture_review_decision_and_failure_exit(tmp_path, monkeypatch, capsys):
    clock = SyntheticClock()
    monkeypatch.setattr("keel_sources.store.SourceStore",
        lambda home, workspace: SourceStore(home, workspace, clock=clock))
    scope = fixture_scope()
    home = tmp_path / "cli-store"
    common = ["--home", str(home), "--workspace", scope["workspace_id"]]
    scope_path = tmp_path / "scope.json"
    scope_path.write_text(json.dumps(scope))
    assert cli.main(["init", *common]) == 0
    capsys.readouterr()
    assert cli.main(["scope-add", str(scope_path), *common]) == 0
    capsys.readouterr()
    assert cli.main(["status", *common]) == 3
    assert json.loads(capsys.readouterr().out)["producer_inputs_complete_count"] == 0
    originals = tmp_path / "originals"
    originals.mkdir()
    (originals / "synthetic-resume.txt").write_bytes(b"SYNTHETIC CLI FIXTURE ONLY\n")
    for component, descriptor in fixture_sources(scope).items():
        path = tmp_path / (component + ".json")
        path.write_text(json.dumps(descriptor))
        if component == "attachments":
            command = ["capture-attachments", str(path), "--source-root", str(originals)]
        else:
            command = ["capture", str(path), "--component", component]
        assert cli.main([*command, *common, "--scope", str(scope_path), "--expected-generation", "0"]) == 0
        capsys.readouterr()
    assert cli.main(["approval-request", *common, "--scope", str(scope_path), "--expires-at",
                     (NOW + timedelta(minutes=4)).isoformat()]) == 0
    request = json.loads(capsys.readouterr().out)
    assert request["state"] == "PENDING"
    assert cli.main(["approval-decide", *common, "--request-id", request["request_id"],
        "--decision", "APPROVE", "--actor", "synthetic-cli-operator", "--authority-ref", "synthetic:cli-authority",
        "--reviewed-sha256", request["review_sha256"], "--expires-at",
        (NOW + timedelta(minutes=2)).isoformat()]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "APPROVED"
    assert cli.main(["export", *common]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["producer_inputs_complete_count"] == 1
    assert exported["execution_authorized"] is False
    assert cli.main(["capture", str(tmp_path / "policy.json"), *common, "--component", "policy",
                     "--scope", str(scope_path), "--expected-generation", "0"]) == 2
    assert "generation_conflict" in capsys.readouterr().err
    assert cli.main(["approval-revoke", *common, "--request-id", request["request_id"],
                     "--actor", "synthetic-cli-operator", "--reason", "Synthetic test only"]) == 0
    capsys.readouterr()
    assert cli.main(["status", *common]) == 3
    assert json.loads(capsys.readouterr().out)["producer_inputs_complete_count"] == 0
