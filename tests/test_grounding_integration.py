"""Fresh content verification composes with, and cannot clear, existing gates."""
from copy import deepcopy
import hashlib

import pytest

from keel_grounding import integration
from keel_grounding.demo import make_fixture
from keel_grounding.evidence import GroundingError
from keel_live.proof import build_proof
from keel_local.readiness import DEPENDENCIES, dependency_hash
from keel_trust.common import canonical, digest
from keel_workbench.demo import make_demo, NOW
from test_live_proof import complete_document, rebind_existing_fixture_checks


def mock_case(tmp_path, monkeypatch, *, passed=True):
    case = make_fixture(tmp_path)
    artifact = case["document"]["artifacts"][0]
    deps = {name: "synthetic-" + name for name in DEPENDENCIES}
    lead = {"role_id": "role-a", "identity": "application-a", "dependencies": deps,
            "packet_dependency_hash": dependency_hash(deps)}
    workbench = {"workspace_id": case["document"]["workspace_id"], "synthetic": True,
        "flow": {"leads": [lead]},
        "trust": {"evidence_export": deepcopy(case["document"]), "artifact_bindings": [{
            "role_id": lead["role_id"], "artifact_id": artifact["artifact_id"],
            "artifact_revision": artifact["revision"], "artifact_sha256": digest(artifact),
            "packet_dependency_hash": lead["packet_dependency_hash"]}]}}
    base = {"schema": "synthetic-isolated-base-proof", "roles": [{
        "scope": {"workspace_id": workbench["workspace_id"], "role_id": lead["role_id"],
                  "application_id": lead["identity"], "action": "PREPARE"},
        "capture_review_checks_passed": passed, "canonical_packet_content_verified": False,
        "execution_authorized": False, "reasons": [] if passed else ["canonical_hold_present"]}],
        "canonical_packet_content_verified": False, "execution_authorized": False}
    calls = []

    def checked(document, **kwargs):
        calls.append((deepcopy(document), kwargs))
        return deepcopy(base)

    monkeypatch.setattr(integration, "build_proof", checked)
    case.update(workbench=workbench, base=base, calls=calls)
    return case


def run(case, root, **kwargs):
    return integration.build_grounded_proof(case["workbench"], case["bindings"], case["packet"],
        expected_packet_sha256=kwargs.pop("expected_packet_sha256", digest(case["packet"])),
        workspace_id=case["workbench"]["workspace_id"], synthetic=True, action="PREPARE",
        evidence_root=root / "evidence", packet_root=root / "packet",
        attachment_root=root, now=kwargs.pop("now", case["now"]), **kwargs)


def test_composes_real_file_verification_and_preserves_base_proof(tmp_path, monkeypatch):
    case = mock_case(tmp_path, monkeypatch)
    original = deepcopy({key: case[key] for key in ("workbench", "bindings", "packet", "base")})
    report = run(case, tmp_path)
    assert report["state"] == "CONTENT_REVIEW_CHECKS_PASSED"
    assert report["packet_content_verified"] is True
    assert report["roles"][0]["packet_content_verified"] is True
    assert report["roles"][0]["canonical_artifact_bound"] is True
    assert report["base_proof"] == original["base"]
    assert report["base_proof"]["canonical_packet_content_verified"] is False
    assert report["counts"] == {"roles": 1, "packet_content_verified": 1,
        "content_review_checks_passed": 1, "synthetic_content_review_checks_passed": 1,
        "declared_operational_content_review_checks_passed": 0}
    assert {key: case[key] for key in original} == original
    assert len(case["calls"]) == 1
    assert report["execution_authorized"] is False
    assert report["roles"][0]["execution_authorized"] is False
    assert all(value == 0 for value in report["effects"].values())
    assert set(report["runtime_checks"].values()) == {"NOT_RUN"}
    assert not any(report[key] for key in ("truth_independently_verified", "source_authenticity_verified",
        "reviewer_authentication_verified", "operator_authenticity_verified", "factual_support_verified",
        "binary_semantic_content_verified", "field_bindings_approval_verified"))


def test_content_success_cannot_clear_prior_hold(tmp_path, monkeypatch):
    case = mock_case(tmp_path, monkeypatch, passed=False)
    report = run(case, tmp_path)
    assert report["packet_content_verified"] is True
    assert report["roles"][0]["packet_content_verified"] is True
    assert report["state"] == "BLOCKED"
    assert report["roles"][0]["state"] == "BLOCKED"
    assert "canonical_hold_present" in report["roles"][0]["reasons"]
    assert "BASE_CAPTURE_REVIEW_BLOCKED" in report["roles"][0]["reasons"]
    assert report["base_proof"] == case["base"]


@pytest.mark.parametrize("field,value,reason", [
    ("artifact_revision", "old", "CANONICAL_ARTIFACT_REVISION_MISMATCH"),
    ("artifact_sha256", "a" * 64, "CANONICAL_ARTIFACT_DIGEST_MISMATCH"),
    ("packet_dependency_hash", "b" * 64, "ROLE_PACKET_DEPENDENCY_MISMATCH"),
    ("artifact_id", "other", "CANONICAL_ARTIFACT_UNKNOWN"),
    ("role_id", "role-b", "CANONICAL_ARTIFACT_BINDING_MISSING"),
])
def test_role_binding_is_independently_matched_to_verified_content(tmp_path, monkeypatch, field, value, reason):
    case = mock_case(tmp_path, monkeypatch)
    case["workbench"]["trust"]["artifact_bindings"][0][field] = value
    report = run(case, tmp_path)
    assert report["packet_report"]["status"] == "VERIFIED"
    assert report["roles"][0]["packet_content_verified"] is False
    assert report["roles"][0]["state"] == "BLOCKED"
    assert reason in report["roles"][0]["reasons"]


def test_dependency_contents_are_rehashed_even_when_declared_hashes_match(tmp_path, monkeypatch):
    case = mock_case(tmp_path, monkeypatch)
    case["workbench"]["flow"]["leads"][0]["dependencies"]["answers"] = "changed"
    report = run(case, tmp_path)
    assert "ROLE_PACKET_DEPENDENCY_MISMATCH" in report["roles"][0]["reasons"]
    assert report["roles"][0]["state"] == "BLOCKED"


def test_existing_but_unselected_artifact_cannot_borrow_another_artifacts_success(tmp_path, monkeypatch):
    case = mock_case(tmp_path, monkeypatch)
    document = case["workbench"]["trust"]["evidence_export"]
    other = deepcopy(document["artifacts"][0])
    other["artifact_id"] = "unselected"
    document["artifacts"].append(other)
    binding = case["workbench"]["trust"]["artifact_bindings"][0]
    binding.update(artifact_id="unselected", artifact_sha256=digest(other))
    case["bindings"]["trust_snapshot_sha256"] = digest(document)
    case["packet"]["trust_snapshot_sha256"] = digest(document)
    report = run(case, tmp_path)
    assert report["packet_report"]["status"] == "VERIFIED"
    assert report["state"] == "BLOCKED"
    assert "ROLE_ARTIFACT_NOT_IN_PACKET" in report["roles"][0]["reasons"]


def test_one_role_cannot_borrow_another_roles_canonical_binding(tmp_path, monkeypatch):
    case = mock_case(tmp_path, monkeypatch)
    second = deepcopy(case["workbench"]["flow"]["leads"][0])
    second.update(role_id="role-b", identity="application-b")
    case["workbench"]["flow"]["leads"].append(second)
    second_base = deepcopy(case["base"]["roles"][0])
    second_base["scope"].update(role_id="role-b", application_id="application-b")
    case["base"]["roles"].append(second_base)
    report = run(case, tmp_path)
    assert report["roles"][0]["state"] == "CONTENT_REVIEW_CHECKS_PASSED"
    assert report["roles"][1]["state"] == "BLOCKED"
    assert "CANONICAL_ARTIFACT_BINDING_MISSING" in report["roles"][1]["reasons"]


def test_packet_uses_only_workbenchs_canonical_evidence_export(tmp_path, monkeypatch):
    case = mock_case(tmp_path, monkeypatch)
    case["workbench"]["trust"]["evidence_export"]["source_revision"] = "changed-canonical-export"
    report = run(case, tmp_path)
    assert report["state"] == "BLOCKED"
    assert report["packet_content_verified"] is False
    assert "PACKET_VERIFICATION_FAILED:trust_snapshot_changed" in report["reasons"]
    assert report["trust_snapshot_sha256"] == digest(case["workbench"]["trust"]["evidence_export"])
    assert report["trust_snapshot_sha256"] != digest(case["document"])


def test_manifest_review_pin_is_required_and_cannot_be_replaced_by_computed_hash(tmp_path, monkeypatch):
    case = mock_case(tmp_path, monkeypatch)
    report = run(case, tmp_path, expected_packet_sha256="a" * 64)
    assert report["state"] == "BLOCKED"
    assert "PACKET_MANIFEST_CHANGED" in report["roles"][0]["reasons"]
    assert report["base_proof"] == case["base"]
    with pytest.raises(GroundingError, match="invalid_expected_packet_sha256"):
        run(case, tmp_path, expected_packet_sha256=None)


def test_previous_content_success_is_not_reused_after_file_changes(tmp_path, monkeypatch):
    case = mock_case(tmp_path, monkeypatch)
    assert run(case, tmp_path)["state"] == "CONTENT_REVIEW_CHECKS_PASSED"
    (tmp_path / "packet" / "answer.txt").write_bytes(b"Unapproved changed material\n")
    report = run(case, tmp_path)
    assert report["state"] == "BLOCKED"
    assert report["packet_content_verified"] is False
    assert len(case["calls"]) == 2
    assert report["base_proof"] == case["base"]


def test_absent_trust_is_a_named_blocker_with_real_existing_reducers(tmp_path):
    case = make_fixture(tmp_path)
    case["workbench"] = make_demo()
    case["workbench"]["trust"] = None
    report = run(case, tmp_path, now=NOW)
    assert report["state"] == "BLOCKED"
    assert report["packet_report"] is None
    assert report["packet_content_verified"] is False
    assert "TRUST_EVIDENCE_MISSING" in report["reasons"]
    assert all("TRUST_EVIDENCE_MISSING" in row["reasons"] for row in report["roles"])
    assert report["base_proof"]["counts"]["capture_review_checks_passed"] == 0


def realistic_case(tmp_path):
    workbench = complete_document(tmp_path)
    document = workbench["trust"]["evidence_export"]
    evidence_root, packet_root = tmp_path / "evidence", tmp_path / "packet"
    evidence_root.mkdir()
    packet_root.mkdir()
    # The prior explicitly synthetic source hash already identifies these exact
    # JSON bytes. No real applicant evidence or approval is constructed.
    value = "synthetic-experience"
    raw = canonical(value)
    (evidence_root / "source.json").write_bytes(raw)
    source, claim = document["sources"][0], document["claims"][0]
    assert source["content_hash"] == hashlib.sha256(raw).hexdigest()
    bindings = {"schema": "keel.grounding.bindings.v1", "trust_snapshot_sha256": digest(document),
        "sources": [{"source_id": source["source_id"], "revision": source["revision"],
                     "path": "source.json", "media_type": "application/json", "selectors": [
                         {"selector_id": "value", "kind": "json_pointer", "pointer": ""}]}],
        "claims": [{"claim_id": claim["claim_id"], "revision": claim["revision"], "value": value,
                    "selections": [{"source_id": source["source_id"], "selector_id": "value"}]}]}
    packet = {"schema": "keel.grounding.packet.v1", "trust_snapshot_sha256": digest(document),
        "workspace_id": document["workspace_id"], "subject_id": claim["subject_id"],
        "scope": document["artifacts"][0]["scope"], "artifacts": [], "attachments": []}
    for artifact in document["artifacts"][:2]:
        content = (("\n".join(row["wording"] for row in artifact["statements"]) + "\n")
                   if artifact["statements"] else "").encode()
        path = artifact["artifact_id"] + ".txt"
        (packet_root / path).write_bytes(content)
        packet["artifacts"].append({"artifact_id": artifact["artifact_id"], "revision": artifact["revision"],
            "path": path, "sha256": hashlib.sha256(content).hexdigest(), "format": "text_lines", "fields": []})
    return {"workbench": workbench, "bindings": bindings, "packet": packet, "now": NOW}


def test_full_existing_source_assurance_trust_and_packet_chain_is_additive(tmp_path):
    case = realistic_case(tmp_path)
    old = build_proof(case["workbench"], workspace_id=case["workbench"]["workspace_id"],
                      synthetic=True, action="PREPARE", attachment_root=tmp_path, now=NOW)
    report = run(case, tmp_path)
    assert old["counts"]["capture_review_checks_passed"] == 1
    assert report["base_proof"] == old
    assert report["counts"]["content_review_checks_passed"] == 1
    assert report["roles"][0]["state"] == "CONTENT_REVIEW_CHECKS_PASSED"
    assert report["packet_content_verified"] is True
    assert report["base_proof"]["canonical_packet_content_verified"] is False
    assert report["execution_authorized"] is False


@pytest.mark.parametrize("gate", ["consent", "inactive", "assurance", "changed_source"])
def test_real_existing_gates_remain_blocking_with_verified_packet(tmp_path, gate):
    case = realistic_case(tmp_path)
    workbench = case["workbench"]
    if gate == "consent":
        workbench["flow"]["leads"][0]["holds"] = ["consent"]
        rebind_existing_fixture_checks(workbench)
    elif gate == "inactive":
        workbench["flow"]["active"] = False
        rebind_existing_fixture_checks(workbench)
    elif gate == "assurance":
        workbench["assurance"] = None
    else:
        workbench["revision_sources"]["roles"][0]["sources"]["answers"]["source_version"] = "changed"
    report = run(case, tmp_path)
    assert report["packet_content_verified"] is True
    assert report["roles"][0]["packet_content_verified"] is True
    assert report["state"] == "BLOCKED"
    assert report["counts"]["content_review_checks_passed"] == 0
    assert report["base_proof"]["roles"][0]["capture_review_checks_passed"] is False
