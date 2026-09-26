"""Real reducers over explicitly synthetic fixtures; never live readiness evidence."""
from copy import deepcopy
from datetime import timedelta
import hashlib

import pytest

from keel_agent.revisions import COMPONENTS, export_revisions
from keel_live.proof import build_proof
from keel_local.readiness import dependency_hash
from keel_workbench.demo import make_demo, NOW
from tools.make_assurance_demo import make_envelope
from tools.make_source_producer_demo import fixture_sources


def proof(document, tmp_path, *, now=NOW):
    return build_proof(document, workspace_id=document["workspace_id"],
                       synthetic=document["synthetic"], action="PREPARE",
                       attachment_root=tmp_path, now=now)


def rebind_existing_fixture_checks(document):
    """Synthetic builders only: establish genuinely matching reducer inputs."""
    document["trust"]["flow_export"] = deepcopy(document["flow"])
    for binding in document["trust"]["artifact_bindings"]:
        lead = next(row for row in document["flow"]["leads"] if row["role_id"] == binding["role_id"])
        binding["packet_dependency_hash"] = lead["packet_dependency_hash"]
    document["assurance"] = make_envelope(document["flow"])


def complete_document(tmp_path):
    document = make_demo()
    lead = document["flow"]["leads"][0]
    scope = {"workspace_id": document["workspace_id"], "role_id": lead["role_id"],
             "application_id": lead["identity"], "action": "PREPARE"}
    sources = fixture_sources(scope)
    data = b"SYNTHETIC PROOF FIXTURE ONLY; NOT APPLICANT EVIDENCE.\n"
    (tmp_path / "resume.txt").write_bytes(data)
    sources["attachments"]["record"]["files"] = [{"path": "resume.txt", "purpose": "resume",
        "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}]
    lead["posting_url"] = sources["target"]["record"]["canonical_posting_url"]
    snapshot = {"schema": "keel.revision_sources.v1", "workspace_id": scope["workspace_id"],
        "snapshot": {"source_ref": "synthetic:snapshot", "source_version": "fixture-v1",
                     "observed_at": NOW.isoformat(), "expires_at": (NOW + timedelta(seconds=90)).isoformat()},
        "roles": [{key: value for key, value in scope.items() if key != "workspace_id"} | {"sources": sources}]}
    first = export_revisions(snapshot, attachment_root=tmp_path, now=NOW)["roles"][0]
    sources["approval"] = {"source_ref": "synthetic:approval", "source_version": "fixture-v1",
        "observed_at": NOW.isoformat(), "expires_at": (NOW + timedelta(minutes=2)).isoformat(),
        "record": {"approval_id": "synthetic-approval", "actor_id": "synthetic-operator",
            "authority_record_ref": "synthetic:authority", "decision": "APPROVE", "scope": scope,
            "component_revisions": first["revisions"], "approved_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=2)).isoformat(), "revoked": False}}
    checked = export_revisions(snapshot, attachment_root=tmp_path, now=NOW)["roles"][0]
    lead["dependencies"] = checked["revisions"]
    lead["packet_dependency_hash"] = dependency_hash(lead["dependencies"])
    document["revision_sources"] = snapshot
    rebind_existing_fixture_checks(document)
    return document


def test_missing_sources_never_become_human_requests_or_qualification(tmp_path):
    document = make_demo()
    result = proof(document, tmp_path)
    assert result["counts"]["roles"] == 9
    assert result["counts"]["source_records_complete"] == 0
    assert result["counts"]["capture_review_checks_passed"] == 0
    assert all(value == 0 for value in result["effects"].values())
    assert all(component["responsibility"] == "system" for row in result["roles"]
               for component in row["components"].values())
    assert result["execution_authorized"] is False


def test_complete_synthetic_sources_pass_only_capture_to_review(tmp_path):
    document = complete_document(tmp_path)
    before = deepcopy(document)
    result = proof(document, tmp_path)
    role = result["roles"][0]
    assert role["state"] == "CAPTURE_TO_REVIEW_CHECKS_PASSED"
    assert role["packet_dependencies_bound"]
    assert role["assurance_checks_passed"] and role["trust_checks_passed"]
    assert result["counts"]["capture_review_checks_passed"] == 1
    assert result["counts"]["synthetic_capture_review_checks_passed"] == 1
    assert result["counts"]["declared_operational_capture_review_checks_passed"] == 0
    assert [row["status"] for row in result["milestones"]] == ["NOT_MET", "NOT_MET"]
    assert set(result["runtime_checks"].values()) == {"NOT_RUN"}
    assert result["counts"]["verified_rendered_preparations"] == 0
    assert not result["source_authenticity_verified"]
    assert not result["operator_authenticity_verified"]
    assert not result["canonical_packet_content_verified"]
    assert not result["execution_authorized"]
    assert document == before


@pytest.mark.parametrize("component", COMPONENTS)
def test_changing_any_component_invalidates_existing_capture_review_binding(tmp_path, component):
    document = complete_document(tmp_path)
    document["revision_sources"]["roles"][0]["sources"][component]["source_version"] = "fixture-v2"
    result = proof(document, tmp_path)
    assert result["counts"]["capture_review_checks_passed"] == 0
    assert result["roles"][0]["packet_dependencies_bound"] is False


def test_source_completion_does_not_substitute_for_missing_assurance_or_trust(tmp_path):
    document = complete_document(tmp_path)
    document["assurance"] = None
    document["trust"] = None
    result = proof(document, tmp_path)
    role = result["roles"][0]
    assert role["source_inputs_ready"] and role["packet_dependencies_bound"]
    assert role["assurance_checks_passed"] is False
    assert role["trust_checks_passed"] is False
    assert role["capture_review_checks_passed"] is False
    assert "trust_not_configured" in role["reasons"]


def test_packet_dependency_revision_change_is_visible(tmp_path):
    document = complete_document(tmp_path)
    lead = document["flow"]["leads"][0]
    lead["dependencies"]["answers"] = "b" * 64
    lead["packet_dependency_hash"] = dependency_hash(lead["dependencies"])
    rebind_existing_fixture_checks(document)
    role = proof(document, tmp_path)["roles"][0]
    assert role["source_inputs_ready"]
    assert not role["packet_dependencies_bound"]
    assert not role["capture_review_checks_passed"]


def test_packet_hash_alone_cannot_override_actual_dependencies(tmp_path):
    document = complete_document(tmp_path)
    document["flow"]["leads"][0]["packet_dependency_hash"] = "a" * 64
    rebind_existing_fixture_checks(document)
    role = proof(document, tmp_path)["roles"][0]
    assert "packet_dependency_hash_mismatch" in role["reasons"]
    assert not role["capture_review_checks_passed"]


def test_changed_attachment_bytes_invalidate_approval_even_with_unchanged_manifest(tmp_path):
    document = complete_document(tmp_path)
    (tmp_path / "resume.txt").write_bytes(b"OTHER SYNTHETIC MATERIAL\n")
    role = proof(document, tmp_path)["roles"][0]
    assert not role["source_semantics_reviewable"]
    assert role["components"]["approval"]["reason"] == "approval_dependency_mismatch"
    assert not role["capture_review_checks_passed"]


@pytest.mark.parametrize("field,expected", [("posting_url", "source_posting_binding_mismatch"),
                                          ("route", "source_route_binding_mismatch")])
def test_canonical_posting_and_transport_must_match_captured_target(tmp_path, field, expected):
    document = complete_document(tmp_path)
    document["flow"]["leads"][0][field] = "https://different.invalid/job" if field == "posting_url" else "api"
    if field == "route":
        document["flow"]["leads"][0]["provider_contract_validated"] = True
    rebind_existing_fixture_checks(document)
    role = proof(document, tmp_path)["roles"][0]
    assert expected in role["reasons"]
    assert not role["capture_review_checks_passed"]


def test_stale_export_cannot_report_current_qualification(tmp_path):
    document = complete_document(tmp_path)
    original_observation = document["flow"]["observed_at"]
    result = proof(document, tmp_path, now=NOW + timedelta(seconds=91))
    assert result["current"] is False
    assert result["counts"]["capture_review_checks_passed"] == 0
    assert document["flow"]["observed_at"] == original_observation


def test_expired_source_and_revoked_approval_are_named_blockers(tmp_path):
    document = complete_document(tmp_path)
    sources = document["revision_sources"]["roles"][0]["sources"]
    sources["policy"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    sources["approval"]["record"]["revoked"] = True
    role = proof(document, tmp_path)["roles"][0]
    assert role["components"]["policy"]["status"] == "STALE"
    assert role["components"]["approval"]["status"] == "REVOKED"
    assert not role["capture_review_checks_passed"]


def test_canonical_hold_remains_in_effect(tmp_path):
    document = complete_document(tmp_path)
    document["flow"]["leads"][0]["holds"] = ["consent"]
    rebind_existing_fixture_checks(document)
    role = proof(document, tmp_path)["roles"][0]
    assert role["source_inputs_ready"]
    assert not role["base_checks_passed"]
    assert "canonical_hold_present" in role["reasons"]
    assert not role["capture_review_checks_passed"]


def test_inactive_pipeline_is_not_qualified_despite_ready_sources(tmp_path):
    document = complete_document(tmp_path)
    document["flow"]["active"] = False
    rebind_existing_fixture_checks(document)
    role = proof(document, tmp_path)["roles"][0]
    assert role["source_inputs_ready"]
    assert "pipeline_inactive" in role["reasons"]
    assert not role["capture_review_checks_passed"]


@pytest.mark.parametrize("action", ["prepare", "SUBMIT", "SIMULATE_SUBMISSION", "UNKNOWN", None])
def test_unknown_or_unsupported_action_is_rejected(tmp_path, action):
    with pytest.raises(ValueError, match="PREPARE only"):
        build_proof(make_demo(), workspace_id="keel-demo", synthetic=True,
                    action=action, attachment_root=tmp_path, now=NOW)


def test_source_action_and_duplicate_role_cannot_mix_scopes(tmp_path):
    document = complete_document(tmp_path)
    document["revision_sources"]["roles"][0]["action"] = "SUBMIT"
    with pytest.raises(ValueError, match="source action mismatch"):
        proof(document, tmp_path)
    document["revision_sources"]["roles"].append(deepcopy(document["revision_sources"]["roles"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        proof(document, tmp_path)


def test_explicit_synthetic_sources_cannot_be_relabeled_operational(tmp_path):
    document = complete_document(tmp_path)
    document["synthetic"] = False
    result = proof(document, tmp_path)
    assert result["counts"]["declared_operational_capture_review_checks_passed"] == 0
    assert "synthetic_source_in_operational_snapshot" in result["roles"][0]["reasons"]
    assert all(item["status"] == "NOT_MET" for item in result["milestones"])


def test_malformed_source_descriptor_cannot_pass_or_leak_attribute_errors(tmp_path):
    document = complete_document(tmp_path)
    document["revision_sources"]["roles"][0]["sources"]["target"] = "invalid"
    result = proof(document, tmp_path)
    assert result["counts"]["capture_review_checks_passed"] == 0
    assert result["roles"][0]["components"]["target"]["status"] == "MISSING"


def test_trust_and_assurance_exports_require_exact_flow_binding(tmp_path):
    document = complete_document(tmp_path)
    document["flow"]["source_revision"] = "changed"
    with pytest.raises(ValueError, match="binding mismatch"):
        proof(document, tmp_path)
