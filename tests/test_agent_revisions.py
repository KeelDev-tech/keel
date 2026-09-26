"""Synthetic contract tests; never evidence that the user's 837 leads are ready."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os

import pytest

from keel_agent.revisions import COMPONENTS, PREAPPROVAL_COMPONENTS, SCHEMA, export_revisions


NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def timestamp(delta=0):
    return (NOW + timedelta(seconds=delta)).isoformat()


def source(record, name="source"):
    return {"source_ref": f"test:{name}", "source_version": "v1",
            "observed_at": timestamp(-20), "expires_at": timestamp(600), "record": record}


def snapshot():
    return {"schema": SCHEMA, "workspace_id": "test-workspace", "snapshot": source({}, "snapshot"),
            "roles": [{"role_id": "test-role", "application_id": "test-application",
                       "action": "submit", "sources": {}}]}


def complete_sources(tmp_path):
    result = snapshot()
    (tmp_path / "resume.txt").write_bytes(b"SYNTHETIC RESUME v1\n")
    sources = result["roles"][0]["sources"]
    sources.update({
        "policy": source({"policy_id": "test-policy", "rules": {"requires_approval": True}}, "policy"),
        "form": source({"form_id": "test-form", "fields": [{"field_id": "name", "required": True}]}, "form"),
        "answers": source({"fields": {"name": "Synthetic Candidate"}}, "answers"),
        "attachments": source({"files": [{"path": "resume.txt", "purpose": "resume"}]}, "attachments"),
        "target": source({"canonical_posting_url": "https://jobs.example.invalid/role",
                          "role_id": "test-role", "application_id": "test-application"}, "target"),
        "route": source({"account_id": "test-account", "action": "submit", "transport": "browser",
                         "destination": "https://apply.example.invalid/role"}, "route"),
    })
    first = export_revisions(result, attachment_root=tmp_path, now=NOW)["roles"][0]
    sources["approval"] = source({
        "approval_id": "test-approval", "actor_id": "test-human", "authority_record_ref": "test:auth",
        "decision": "APPROVE", "scope": first["scope"], "component_revisions": first["revisions"],
        "approved_at": timestamp(-15), "expires_at": timestamp(500), "revoked": False}, "approval")
    # The approval source must actually have been observed after the recorded decision.
    sources["approval"]["observed_at"] = timestamp(-10)
    return result


def export(value, tmp_path, now=NOW):
    return export_revisions(value, attachment_root=tmp_path, now=now)


def component(value, tmp_path, name):
    return export(value, tmp_path)["roles"][0]["components"][name]


def test_837_missing_exports_are_seven_system_roots_not_human_requests(tmp_path):
    value = snapshot()
    value["roles"] = [{"role_id": f"role-{n}", "application_id": f"application-{n}",
                       "action": "submit", "sources": {}} for n in range(837)]
    result = export(value, tmp_path)
    assert result["blocked_role_count"] == 837
    assert result["ready_role_count"] == 0
    assert result["system_root_cause_count"] == 7
    assert result["human_root_cause_count"] == 0
    assert len(result["root_causes"]) == 7
    assert {x["affected_roles"] for x in result["root_causes"]} == {837}
    assert {x["component"] for x in result["root_causes"]} == set(COMPONENTS)
    assert not result["execution_authorized"]
    assert all(not row["revisions"] for row in result["roles"])


def test_complete_content_has_seven_revisions_without_claiming_authenticity_or_permission(tmp_path):
    result = export(complete_sources(tmp_path), tmp_path)
    row = result["roles"][0]
    assert result["ready_role_count"] == 1
    assert row["envelope_inputs_ready"]
    assert set(row["revisions"]) == set(COMPONENTS)
    assert len(set(row["revisions"].values())) == 7
    assert not row["execution_authorized"]
    assert not row["source_authenticity_verified"]
    assert "Synthetic Candidate" not in json.dumps(result)
    assert "SYNTHETIC RESUME" not in json.dumps(result)


def test_missing_approval_is_exporter_problem_until_explicit_human_request(tmp_path):
    value = complete_sources(tmp_path)
    value["roles"][0]["sources"].pop("approval")
    assert export(value, tmp_path)["human_root_cause_count"] == 0
    value["roles"][0]["sources"]["approval"] = {
        **source({}, "approval-search"),
        "absence": {"kind": "HUMAN_DECISION_REQUIRED", "decision_request_ref": "test:request"}}
    result = export(value, tmp_path)
    assert result["human_root_cause_count"] == 1
    assert result["root_causes"][0]["owner"] == "human_reviewer"


def test_human_absence_requires_current_source_and_request_record(tmp_path):
    value = snapshot()
    value["roles"][0]["sources"]["approval"] = {"absence": {"kind": "HUMAN_DECISION_REQUIRED"}}
    assert export(value, tmp_path)["human_root_cause_count"] == 0


@pytest.mark.parametrize("name", PREAPPROVAL_COMPONENTS)
def test_material_change_invalidates_exact_approval(tmp_path, name):
    value = complete_sources(tmp_path)
    value["roles"][0]["sources"][name]["record"]["material_change"] = "changed"
    assert component(value, tmp_path, "approval")["reason"] == "approval_dependency_mismatch"


def test_attachment_bytes_change_without_manifest_change_invalidates_approval(tmp_path):
    value = complete_sources(tmp_path)
    (tmp_path / "resume.txt").write_bytes(b"SYNTHETIC RESUME edited\n")
    assert component(value, tmp_path, "approval")["reason"] == "approval_dependency_mismatch"


def test_refresh_only_changes_observation_but_preserves_material_revision(tmp_path):
    value = complete_sources(tmp_path)
    previous = export(value, tmp_path)["roles"][0]["revisions"]
    for descriptor in value["roles"][0]["sources"].values():
        descriptor["observed_at"] = timestamp(-1)
        descriptor["expires_at"] = timestamp(700)
    current = export(value, tmp_path)["roles"][0]
    assert current["revisions"] == previous
    assert current["envelope_inputs_ready"]


def test_source_version_change_invalidates_approval_even_if_values_match(tmp_path):
    value = complete_sources(tmp_path)
    value["roles"][0]["sources"]["answers"]["source_version"] = "v2"
    assert component(value, tmp_path, "approval")["reason"] == "approval_dependency_mismatch"


@pytest.mark.parametrize("field", ["role_id", "application_id", "action"])
def test_approval_cannot_be_replayed_across_scopes(tmp_path, field):
    value = complete_sources(tmp_path)
    value["roles"][0][field] += "-other"
    assert component(value, tmp_path, "approval")["reason"] == "approval_scope_mismatch"


def test_workspace_binding(tmp_path):
    value = complete_sources(tmp_path)
    value["workspace_id"] = "other-workspace"
    assert component(value, tmp_path, "approval")["reason"] == "approval_scope_mismatch"


@pytest.mark.parametrize("field,value,reason", [
    ("revoked", True, "approval_revoked"), ("decision", "REJECT", "approval_rejected"),
    ("decision", "PASS", "approval_decision_missing"),
    ("expires_at", timestamp(-1), "approval_expired"),
    ("approved_at", timestamp(1), "approval_validity_invalid"),
])
def test_approval_rejection_revocation_expiry_and_future_time_hold(tmp_path, field, value, reason):
    sample = complete_sources(tmp_path)
    sample["roles"][0]["sources"]["approval"]["record"][field] = value
    assert component(sample, tmp_path, "approval")["reason"] == reason


def test_seventh_digest_cannot_be_in_approval_binding(tmp_path):
    value = complete_sources(tmp_path)
    value["roles"][0]["sources"]["approval"]["record"]["component_revisions"]["approval"] = "a" * 64
    assert component(value, tmp_path, "approval")["reason"] == "approval_dependency_mismatch"


@pytest.mark.parametrize("metadata,value,reason", [
    ("observed_at", timestamp(1), "source_observation_in_future"),
    ("expires_at", timestamp(-1), "source_expired"),
    ("expires_at", timestamp(-30), "source_validity_invalid"),
    ("observed_at", "2026-09-18T12:00:00", "timestamp_invalid"),
    ("revoked", True, "source_revoked"), ("revoked", "false", "source_revocation_invalid"),
])
def test_freshness_is_enforced_separately_from_hash(tmp_path, metadata, value, reason):
    sample = complete_sources(tmp_path)
    sample["roles"][0]["sources"]["policy"][metadata] = value
    assert component(sample, tmp_path, "policy")["reason"] == reason
    assert not export(sample, tmp_path)["roles"][0]["envelope_inputs_ready"]


def test_no_timestamp_is_invented_and_missing_snapshot_is_explicit(tmp_path):
    value = complete_sources(tmp_path)
    del value["roles"][0]["sources"]["form"]["observed_at"]
    del value["snapshot"]
    result = export(value, tmp_path)
    assert component(value, tmp_path, "form")["reason"] == "source_timestamps_missing"
    assert any(x["component"] == "snapshot" for x in result["root_causes"])
    assert result["ready_role_count"] == 0


@pytest.mark.parametrize("relative", ["../secret", "/etc/passwd", "a/../resume.txt", "./resume.txt", "a\\resume.txt"])
def test_attachment_traversal_rejected(tmp_path, relative):
    value = complete_sources(tmp_path)
    value["roles"][0]["sources"]["attachments"]["record"]["files"][0]["path"] = relative
    assert component(value, tmp_path, "attachments")["status"] == "INVALID"


def test_symlink_file_and_parent_directory_rejected(tmp_path):
    value = complete_sources(tmp_path)
    (tmp_path / "alias").symlink_to(tmp_path / "resume.txt")
    manifest = value["roles"][0]["sources"]["attachments"]["record"]
    manifest["files"][0]["path"] = "alias"
    assert component(value, tmp_path, "attachments")["reason"] == "attachment_unavailable_or_symlink"
    (tmp_path / "nested").symlink_to(tmp_path, target_is_directory=True)
    manifest["files"][0]["path"] = "nested/resume.txt"
    assert component(value, tmp_path, "attachments")["reason"] == "attachment_unavailable_or_symlink"


def test_fifo_is_rejected_without_blocking(tmp_path):
    value = complete_sources(tmp_path)
    os.mkfifo(tmp_path / "fifo")
    value["roles"][0]["sources"]["attachments"]["record"]["files"][0]["path"] = "fifo"
    assert component(value, tmp_path, "attachments")["reason"] == "attachment_not_regular"


def test_oversized_attachment_rejected_before_hashing(tmp_path, monkeypatch):
    value = complete_sources(tmp_path)
    monkeypatch.setattr("keel_agent.revisions.MAX_ATTACHMENT_BYTES", 4)
    assert component(value, tmp_path, "attachments")["reason"] == "attachment_too_large"


def test_explicit_empty_attachment_manifest_is_actual_source_record(tmp_path):
    value = complete_sources(tmp_path)
    descriptor = value["roles"][0]["sources"]["attachments"]
    descriptor["record"] = {"files": []}
    assert component(value, tmp_path, "attachments")["reason"] == "attachment_absence_not_explicit"
    descriptor["record"]["none_required"] = True
    assert component(value, tmp_path, "attachments")["status"] == "READY"
    assert component(value, tmp_path, "approval")["reason"] == "approval_dependency_mismatch"


def test_output_does_not_mutate_input_or_create_files(tmp_path):
    value = complete_sources(tmp_path)
    original = copy.deepcopy(value)
    files = list(tmp_path.iterdir())
    export(value, tmp_path)
    assert value == original
    assert list(tmp_path.iterdir()) == files


def test_role_scope_duplicate_rejected(tmp_path):
    value = snapshot()
    value["roles"].append(copy.deepcopy(value["roles"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        export(value, tmp_path)


def test_unknown_live_schema_is_not_guessed(tmp_path):
    with pytest.raises(ValueError, match="schema"):
        export({"leads": [{"job_id": "x"}]}, tmp_path)


def test_non_string_json_keys_cannot_collide_with_string_keys(tmp_path):
    value = complete_sources(tmp_path)
    value["roles"][0]["sources"]["answers"]["record"]["extra"] = {1: "ambiguous"}
    assert component(value, tmp_path, "answers")["reason"] == "record_not_json"


def test_approval_cannot_have_been_observed_before_it_happened(tmp_path):
    value = complete_sources(tmp_path)
    value["roles"][0]["sources"]["approval"]["observed_at"] = timestamp(-30)
    assert component(value, tmp_path, "approval")["reason"] == "approval_not_yet_observed"


def test_counts_distinguish_unique_leads_and_action_scopes(tmp_path):
    value = snapshot()
    other = copy.deepcopy(value["roles"][0])
    other["action"] = "prepare"
    value["roles"].append(other)
    result = export(value, tmp_path)
    assert result["scope_count"] == 2
    assert result["unique_role_count"] == 1
    assert all(item["affected_scopes"] == 2 and item["affected_roles"] == 1
               for item in result["root_causes"])
