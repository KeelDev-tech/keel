"""Actual assurance-core/operating-board integration; no live effects."""
import copy
from datetime import timedelta
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_flow.__main__ import main
from keel_flow.board import build, markdown
from keel_flow.common import digest
from tools.make_flow_demo import NOW, make_snapshot
from tools.make_assurance_demo import make_envelope


def bundle():
    snapshot = make_snapshot()
    return snapshot, make_envelope(snapshot)


def role(report, rid="role-1"):
    return next(row for row in report["readiness"]["rows"] if row["role_id"] == rid)


def test_absence_is_visible_and_never_claims_qualified_readiness():
    snapshot = make_snapshot()
    report = build(snapshot, now=NOW)
    assert report["readiness"]["executable_ready"] == 1
    assert report["readiness"]["base_executable_ready"] == 1
    assert report["readiness"]["assurance_qualified_ready"] is None
    assert report["assurance"]["state"] == "NOT_CONFIGURED"
    assert not report["assurance"]["execution_authorized"]
    assert report["forecast"]["assurance_basis"] == "NOT_CONFIGURED_LEGACY_ONLY"
    assert "UNKNOWN" in markdown(report)
    assert "Legacy readiness and forecasting remain diagnostic only" in markdown(report)


def test_valid_envelope_is_conjunctive_and_does_not_mutate_or_authorize():
    snapshot, assurance = bundle()
    before = copy.deepcopy((snapshot, assurance))
    report = build(snapshot, now=NOW, assurance=assurance)
    assert (snapshot, assurance) == before
    assert report["version"] == "0.6.0-review.1"
    assert report["assurance"]["state"] == "CHECKS_PASSED"
    assert report["assurance"]["coverage_complete"]
    assert report["readiness"]["executable_ready"] == 1
    assert report["readiness"]["assurance_qualified_ready"] == 1
    assert report["readiness"]["base_executable_ready"] == 1
    assert not role(report, "role-2")["executable"]  # canonical dedupe hold
    assert not role(report, "role-3")["executable"]  # unresolved dispatched attempt
    assert not role(report, "role-4")["executable"]  # unanswered question
    assert not role(report, "role-5")["executable"]  # consent hold
    assert all(row["assurance_checks_passed"] for row in report["readiness"]["rows"])
    assert report["forecast"]["runway_seconds"] == 300
    assert all(value == 0 for value in report["effects"].values())
    assert not report["execution_authorized"]
    assert not report["assurance"]["execution_authorized"]
    assert "Cognitive assurance" in markdown(report)


@pytest.mark.parametrize("field,value,match", [
    ("source_revision", "other-revision", "source revision mismatch"),
    ("export_sha256", "f" * 64, "export digest mismatch"),
])
def test_top_level_binding_rejects_other_snapshot(field, value, match):
    snapshot, assurance = bundle()
    assurance[field] = value
    with pytest.raises(ValueError, match=match):
        build(snapshot, now=NOW, assurance=assurance)


@pytest.mark.parametrize("field,value,match", [
    ("role_id", "unknown", "unknown role"),
    ("application_id", "f" * 64, "application identity mismatch"),
    ("lead_sha256", "f" * 64, "lead digest mismatch"),
    ("packet_dependency_hash", "f" * 64, "packet dependency hash mismatch"),
])
def test_action_binding_rejects_identity_or_dependency_drift(field, value, match):
    snapshot, assurance = bundle()
    assurance["actions"][0][field] = value
    with pytest.raises(ValueError, match=match):
        build(snapshot, now=NOW, assurance=assurance)


def test_export_rebinding_alone_cannot_reuse_changed_lead_review():
    snapshot, assurance = bundle()
    snapshot["leads"][0]["fit_score"] = 85
    assurance["export_sha256"] = digest(snapshot)
    with pytest.raises(ValueError, match="lead digest mismatch"):
        build(snapshot, now=NOW, assurance=assurance)


def test_duplicate_assurance_role_rejected():
    snapshot, assurance = bundle()
    assurance["actions"].append(copy.deepcopy(assurance["actions"][0]))
    with pytest.raises(ValueError, match="duplicate assurance role_id"):
        build(snapshot, now=NOW, assurance=assurance)


def test_missing_ready_coverage_blocks_readiness_but_preserves_diagnostic():
    snapshot, assurance = bundle()
    assurance["actions"] = [a for a in assurance["actions"] if a["role_id"] != "role-1"]
    report = build(snapshot, now=NOW, assurance=assurance)
    assert report["assurance"]["state"] == "REVIEW_REQUIRED"
    assert report["assurance"]["missing_role_ids"] == ["role-1"]
    assert report["readiness"]["base_executable_ready"] == 1
    assert report["readiness"]["executable_ready"] == 0
    assert report["readiness"]["nominal_to_actual_loss"] == 5
    assert role(report)["base_executable"]
    assert not role(report)["executable"]
    assert "missing_assurance" in role(report)["reasons"]
    assert report["readiness"]["blocked_reasons"]["missing_assurance"] == 1
    assert report["forecast"]["runway_seconds"] == 0


def test_review_failure_reduces_readiness_and_forecast():
    snapshot, assurance = bundle()
    action = next(a for a in assurance["actions"] if a["role_id"] == "role-1")
    action["reviews"][0]["verdict"] = "FAIL"
    report = build(snapshot, now=NOW, assurance=assurance)
    assert role(report)["base_executable"] and not role(report)["executable"]
    assert report["readiness"]["executable_ready"] == 0
    assert "assurance:reviewer_disagreement" in role(report)["reasons"]
    assert report["forecast"]["runway_seconds"] == 0
    assert not report["execution_authorized"]


@pytest.mark.parametrize("failure", ["missing", "failed"])
def test_future_supply_is_never_counted_without_passed_assurance(failure):
    snapshot, assurance = bundle()
    rid = snapshot["releases"][0]["role_ids"][0]
    if failure == "missing":
        assurance["actions"] = [a for a in assurance["actions"] if a["role_id"] != rid]
    else:
        action = next(a for a in assurance["actions"] if a["role_id"] == rid)
        action["reviews"][0]["verdict"] = "ABSTAIN"
    report = build(snapshot, now=NOW, assurance=assurance)
    assert report["readiness"]["executable_ready"] == 1
    assert report["forecast"]["state"] == "UNKNOWN"
    assert report["forecast"]["blocked_release_role_ids"] == [rid]
    assert not report["forecast"]["discovery_recommended"]
    assert "release_opportunities" not in report["forecast"]
    actions = {a["action"] for a in report["actions"]}
    assert "REVIEW_FUTURE_SUPPLY_ASSURANCE" in actions
    assert "MEASURE_SUPPLIED_CAPACITY" not in actions


@pytest.mark.parametrize("failure", ["partial", "stale", "future"])
def test_global_assurance_failure_is_unverified_and_fail_closed(failure):
    snapshot, assurance = bundle()
    if failure == "partial": assurance["complete"] = False
    if failure == "stale": assurance["observed_at"] = (NOW - timedelta(seconds=91)).isoformat()
    if failure == "future": assurance["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    report = build(snapshot, now=NOW, assurance=assurance)
    assert report["status"] == "UNVERIFIED"
    assert report["assurance"]["state"] == "UNVERIFIED"
    assert report["readiness"]["executable_ready"] == 0
    assert not report["readiness"]["current_estimate_verified"]
    assert report["forecast"]["state"] == "UNKNOWN"
    assert not report["forecast"]["discovery_recommended"]
    assert report["discovery"]["allocated_minutes"] == 0


def test_reduced_current_inventory_cannot_be_double_counted_as_release():
    snapshot = make_snapshot()
    snapshot["releases"][0]["role_ids"][0] = "role-1"
    assurance = make_envelope(snapshot)
    assurance["actions"][0]["reviews"] = []
    with pytest.raises(ValueError, match="double-counts ready inventory"):
        build(snapshot, now=NOW, assurance=assurance)


def test_cli_loads_assurance_file_without_side_effects(tmp_path, capsys):
    snapshot, assurance = bundle()
    export_path, assurance_path = tmp_path / "export.json", tmp_path / "assurance.json"
    export_path.write_text(json.dumps(snapshot))
    assurance_path.write_text(json.dumps(assurance))
    assert main(["board", str(export_path), "--now", NOW.isoformat(), "--assurance", str(assurance_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["assurance"]["state"] == "CHECKS_PASSED"
    assert report["readiness"]["assurance_qualified_ready"] == 1
    assert not report["execution_authorized"]


@pytest.mark.parametrize("invalid", [None, [], "not-an-envelope", {"schema_version": 1}])
def test_cli_rejects_explicit_malformed_assurance_instead_of_legacy_fallback(invalid, tmp_path, capsys):
    export_path, assurance_path = tmp_path / "export.json", tmp_path / "assurance.json"
    export_path.write_text(json.dumps(make_snapshot()))
    assurance_path.write_text(json.dumps(invalid))
    assert main(["board", str(export_path), "--now", NOW.isoformat(), "--assurance", str(assurance_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "keel-flow:" in captured.err
