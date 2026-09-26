"""Integration contracts using fictional data; no operational readiness claims."""
from copy import deepcopy
from datetime import timedelta
import json
import socket
import subprocess

import pytest

from keel_agent.revisions import PREAPPROVAL_COMPONENTS
from keel_live.proof import build_proof
from tools.make_live_demo import create_demo, main as demo_main, NOW, SYNTHETIC_WORKSPACE
from tools.rehearse_live import run_rehearsal, main as rehearsal_main


@pytest.fixture(autouse=True)
def isolated_audit_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def demo(tmp_path):
    return create_demo(tmp_path / "synthetic-fixture")


def approve_and_bind(demo):
    pending = demo.request()
    demo.decide(pending)
    demo.bind_synthetic_checks()
    return pending


def test_default_demo_captures_six_sources_and_never_invents_request(demo):
    result = demo.export()
    observed = demo.review.inspect(demo.principal, demo.scope)
    assert result["source_export"]["source_record_coverage"]["approval"] == 0
    assert sum("record" in value for value in observed["sources"].values()) == 6
    assert observed["sources"]["approval"] == {"absence": {"kind": "SOURCE_RECORD_MISSING"}}
    assert observed["current_request"] is None
    assert observed["semantic_validation"]["ready_for_approval"]
    assert demo.body["assurance"] is None and demo.body["trust"] is None
    assert demo.proof()["state"] == "BLOCKED"
    with pytest.raises(ValueError, match="explicit_current_approval"):
        demo.bind_synthetic_checks()


def test_seven_sources_do_not_substitute_for_existing_reviews(demo):
    demo.decide(demo.request())
    report = demo.proof()
    assert report["roles"][0]["source_records_complete"]
    assert report["roles"][0]["source_semantics_reviewable"]
    assert not report["roles"][0]["packet_dependencies_bound"]
    assert not report["roles"][0]["assurance_checks_passed"]
    assert not report["roles"][0]["trust_checks_passed"]
    assert report["state"] == "BLOCKED"


def test_explicit_synthetic_end_to_end_pass_retains_honest_runtime_limits(demo):
    approve_and_bind(demo)
    result = demo.proof()
    assert result["state"] == "CAPTURE_TO_REVIEW_CHECKS_PASSED"
    assert result["counts"]["synthetic_capture_review_checks_passed"] == 1
    assert result["counts"]["declared_operational_capture_review_checks_passed"] == 0
    assert result["counts"]["verified_rendered_preparations"] == 0
    assert all(item["status"] == "NOT_MET" for item in result["milestones"])
    assert set(result["runtime_checks"].values()) == {"NOT_RUN"}
    assert not result["execution_authorized"]
    assert not result["source_authenticity_verified"]
    assert not result["operator_authenticity_verified"]
    assert not result["canonical_packet_content_verified"]
    assert set(result["effects"].values()) == {0}


@pytest.mark.parametrize("component", PREAPPROVAL_COMPONENTS)
def test_each_changed_source_family_invalidates_existing_bound_fixture(demo, component):
    pending = approve_and_bind(demo)
    changed = deepcopy(demo.sources[component])
    changed["source_version"] = "synthetic-changed-v2"
    changed["record"]["metadata"]["synthetic_material_change"] = True
    demo.put(component, changed)
    proof = demo.proof()
    assert proof["state"] == "BLOCKED"
    assert proof["roles"][0]["components"]["approval"]["reason"] == "approval_dependency_mismatch"
    assert not demo.review.get(demo.principal, demo.scope, pending["request_id"])["approval_currently_valid"]


def test_new_observation_does_not_change_exact_material_review(demo):
    pending = demo.request()
    demo.clock.advance(4)
    source = deepcopy(demo.sources["policy"])
    source["observed_at"] = demo.clock().isoformat()
    result = demo.put("policy", source)
    assert result["revision"] == pending["component_revisions"]["policy"]
    approved = demo.decide(pending)
    assert approved["state"] == "APPROVED"
    assert approved["review_sha256"] == pending["review_sha256"]
    demo.bind_synthetic_checks()
    assert demo.proof()["state"] == "CAPTURE_TO_REVIEW_CHECKS_PASSED"


def test_changed_actual_attachment_bytes_fail_read_only_proof(demo):
    pending = approve_and_bind(demo)
    snapshot = demo.export()["workbench_snapshot"]
    manifest = snapshot["revision_sources"]["roles"][0]["sources"]["attachments"]["record"]["files"][0]
    (demo.store.attachment_root / manifest["path"]).write_bytes(b"DIFFERENT SYNTHETIC BYTES\n")
    before = demo.store.db_path.read_bytes()
    proof = build_proof(snapshot, workspace_id=SYNTHETIC_WORKSPACE, synthetic=True,
        action="PREPARE", attachment_root=demo.store.attachment_root, now=demo.clock())
    assert proof["state"] == "BLOCKED"
    with pytest.raises(ValueError, match="source_material_integrity_failed"):
        demo.export()
    with pytest.raises(ValueError, match="source_material_integrity_failed"):
        demo.review.get(demo.principal, demo.scope, pending["request_id"])
    assert demo.store.db_path.read_bytes() == before


def test_restart_preserves_exact_review_and_original_observations(demo):
    pending = approve_and_bind(demo)
    snapshot = demo.export()["workbench_snapshot"]
    view = demo.review.get(demo.principal, demo.scope, pending["request_id"])
    proof = demo.proof()
    demo.restart()
    assert demo.export()["workbench_snapshot"] == snapshot
    assert demo.review.get(demo.principal, demo.scope, pending["request_id"]) == view
    assert demo.proof() == proof
    for component in PREAPPROVAL_COMPONENTS:
        observed = snapshot["revision_sources"]["roles"][0]["sources"][component]["observed_at"]
        assert observed == (NOW-timedelta(seconds=20)).isoformat()


def test_stale_canonical_flow_cannot_capture_or_emit_new_export(demo):
    demo.clock.advance(91)
    before = demo.store.db_path.read_bytes()
    with pytest.raises(ValueError, match="fresh canonical flow"):
        demo.put("policy", demo.sources["policy"])
    with pytest.raises(ValueError, match="fresh canonical flow"):
        demo.export()
    assert demo.store.db_path.read_bytes() == before


def test_previous_valid_snapshot_remains_stale_when_checked_later(demo):
    approve_and_bind(demo)
    snapshot = demo.export()["workbench_snapshot"]
    original = deepcopy(snapshot)
    proof = build_proof(snapshot, workspace_id=SYNTHETIC_WORKSPACE, synthetic=True,
        action="PREPARE", attachment_root=demo.store.attachment_root,
        now=NOW+timedelta(seconds=601))
    assert proof["state"] == "BLOCKED" and not proof["current"]
    assert snapshot == original


def test_existing_directory_is_refused_before_any_changes(tmp_path):
    path = tmp_path / "existing"
    path.mkdir()
    sentinel = path / "sentinel.txt"
    sentinel.write_text("leave unchanged")
    with pytest.raises(FileExistsError):
        create_demo(path)
    assert sentinel.read_text() == "leave unchanged"
    assert list(path.iterdir()) == [sentinel]


def test_demo_cli_is_synthetic_six_source_only(tmp_path, capsys):
    path = tmp_path / "demo-cli"
    assert demo_main(["--out", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["synthetic"] and output["not_live_evidence"]
    assert output["approval_requests_created"] == 0
    snapshot = json.loads((path / "synthetic-workbench-snapshot.json").read_text())
    assert sum("record" in value for value in snapshot["revision_sources"]["roles"][0]["sources"].values()) == 6
    assert snapshot["assurance"] is None and snapshot["trust"] is None


def test_rehearsal_exercises_real_local_modules_without_network_or_processes(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("synthetic rehearsal attempted external effect")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    output = tmp_path / "rehearsal"
    report = run_rehearsal(output)
    assert report["status"] == "PASS"
    assert report["checks_passed"] == report["checks_total"] == 20
    assert all(row["passed"] for row in report["checks"])
    assert len({row["name"] for row in report["checks"]}) == report["checks_total"]
    assert report["synthetic"] and report["not_live_evidence"]
    assert report["declared_operational_roles_tested"] == 0
    assert not report["execution_authorized"]
    for name in ("model_calls", "http_calls", "browser_actions", "canonical_writes"):
        assert report[name] == 0
    assert json.loads((output / "live-rehearsal.json").read_text()) == report
    proof = json.loads((output / "synthetic-capture-review-proof.json").read_text())
    assert proof["counts"]["synthetic_capture_review_checks_passed"] == 1
    with pytest.raises(FileExistsError):
        run_rehearsal(output)


def test_rehearsal_cli_reports_only_synthetic_success(tmp_path, capsys):
    assert rehearsal_main(["--out", str(tmp_path / "cli-rehearsal")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "PASS", "synthetic": True, "checks_passed": 20,
                      "checks_total": 20, "execution_authorized": False}
