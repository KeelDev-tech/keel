"""Actual packet coverage and non-authorization checks, all synthetic."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os

import pytest

from keel_grounding.demo import make_fixture
from keel_grounding.evidence import GroundingError
from keel_grounding.packet import verify_packet
from keel_trust.common import digest


def _run(case, root, **kwargs):
    return verify_packet(case["document"], case["bindings"], case["packet"],
                         evidence_root=root / "evidence", packet_root=root / "packet",
                         now=kwargs.get("now", case["now"]),
                         expected_packet_sha256=kwargs.get("expected_packet_sha256", digest(case["packet"])))


def _refresh(case):
    value = digest(case["document"])
    case["bindings"]["trust_snapshot_sha256"] = value
    case["packet"]["trust_snapshot_sha256"] = value


def _write(case, root, raw, *, row=None, update_hash=True):
    row = row or case["packet"]["artifacts"][0]
    (root / "packet" / row["path"]).write_bytes(raw)
    if update_hash:
        row["sha256"] = hashlib.sha256(raw).hexdigest()


def _wording(case):
    return case["document"]["artifacts"][0]["statements"][0]["wording"]


def _json_packet(case, root):
    row = case["packet"]["artifacts"][0]
    row.update(format="json_fields", fields=[{"field_id": "answer", "statement_index": 0}])
    _write(case, root, json.dumps({"answer": _wording(case)}).encode())


def test_real_evidence_and_packet_are_verified_without_creating_authority(tmp_path):
    case = make_fixture(tmp_path)
    original = deepcopy(case)
    report = _run(case, tmp_path)
    assert report["status"] == "VERIFIED"
    assert report["packet_content_verified"] is True
    assert report["packet_manifest_sha256"] == digest(case["packet"])
    assert report["declared_manifest_binding_verified"] is True
    assert all(row["content_matches_approved_wording"] for row in report["artifacts"])
    assert report["grounding_status"] == "VERIFIED"
    assert not any(report[k] for k in ("execution_authorized", "truth_independently_verified",
                                       "approver_authenticated", "binary_semantic_content_verified",
                                       "field_bindings_approval_verified", "queue_writes"))
    assert case == original


@pytest.mark.parametrize("alteration", ["prefix", "suffix", "missing", "newline", "crlf", "invalid_utf8"])
def test_matching_caller_hash_cannot_launder_unapproved_text(tmp_path, alteration):
    case = make_fixture(tmp_path)
    original = (tmp_path / "packet" / case["packet"]["artifacts"][0]["path"]).read_bytes()
    content = {"prefix": b"Unapproved introduction\n" + original,
               "suffix": original + b"Unapproved footer\n", "missing": b"",
               "newline": original.rstrip(b"\n"), "crlf": original.replace(b"\n", b"\r\n"),
               "invalid_utf8": b"\xff" + original}[alteration]
    _write(case, tmp_path, content)
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert "ARTIFACT_CONTENT_MISMATCH" in report["artifacts"][0]["reasons"]


def test_actual_bytes_are_checked_even_when_wording_is_still_correct(tmp_path):
    case = make_fixture(tmp_path)
    case["packet"]["artifacts"][0]["sha256"] = "0" * 64
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert report["artifacts"][0]["reasons"] == ["ARTIFACT_FILE_UNVERIFIED"]


def test_json_fields_allow_formatting_but_require_exact_field_and_statement_coverage(tmp_path):
    case = make_fixture(tmp_path)
    _json_packet(case, tmp_path)
    _write(case, tmp_path, json.dumps({"answer": _wording(case)}, indent=4).encode())
    assert _run(case, tmp_path)["status"] == "VERIFIED"


@pytest.mark.parametrize("alteration", ["extra", "missing", "changed", "wrapper", "duplicate", "nonfinite", "utf16"])
def test_json_content_failures_block_even_with_matching_hash(tmp_path, alteration):
    case = make_fixture(tmp_path)
    _json_packet(case, tmp_path)
    wording = _wording(case)
    raw = {"extra": json.dumps({"answer": wording, "hidden": "Unapproved"}).encode(),
           "missing": b"{}", "changed": json.dumps({"answer": wording + " fabricated"}).encode(),
           "wrapper": json.dumps({"fields": {"answer": wording}}).encode(),
           "duplicate": ('{"answer":' + json.dumps(wording) + ',"answer":' + json.dumps(wording) + '}').encode(),
           "nonfinite": b'{"answer": NaN}',
           "utf16": json.dumps({"answer": wording}).encode("utf-16")}[alteration]
    _write(case, tmp_path, raw)
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert "ARTIFACT_CONTENT_MISMATCH" in report["artifacts"][0]["reasons"]


@pytest.mark.parametrize("fields", [[], [{"field_id": "answer", "statement_index": 1}]])
def test_json_binding_must_cover_every_statement_at_its_exact_index(tmp_path, fields):
    case = make_fixture(tmp_path)
    _json_packet(case, tmp_path)
    case["packet"]["artifacts"][0]["fields"] = fields
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert "ARTIFACT_STATEMENT_COVERAGE_MISMATCH" in report["artifacts"][0]["reasons"]


@pytest.mark.parametrize("fields", [
    [{"field_id": "answer", "statement_index": True}],
    [{"field_id": "answer", "statement_index": -1}],
    [{"field_id": "answer", "statement_index": 0}, {"field_id": "other", "statement_index": 0}],
    [{"field_id": "answer", "statement_index": 0}, {"field_id": "answer", "statement_index": 1}],
    [{"field_id": "answer", "statement_index": 0, "approved": True}],
])
def test_ambiguous_field_bindings_are_rejected(tmp_path, fields):
    case = make_fixture(tmp_path)
    _json_packet(case, tmp_path)
    case["packet"]["artifacts"][0]["fields"] = fields
    with pytest.raises(GroundingError):
        _run(case, tmp_path)


@pytest.mark.parametrize("field,value,reason", [
    ("trust_snapshot_sha256", "0" * 64, "TRUST_SNAPSHOT_CHANGED"),
    ("workspace_id", "other-workspace", "WORKSPACE_MISMATCH"),
    ("scope", "other-scope", "ARTIFACT_SCOPE_MISMATCH"),
    ("subject_id", "other-subject", "CLAIM_SUBJECT_MISMATCH"),
])
def test_packet_cannot_cross_snapshot_workspace_scope_or_subject(tmp_path, field, value, reason):
    case = make_fixture(tmp_path)
    case["packet"][field] = value
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert reason in report["reasons"] + report["artifacts"][0]["reasons"]


def test_packet_requires_exact_artifact_revision(tmp_path):
    case = make_fixture(tmp_path)
    case["packet"]["artifacts"][0]["revision"] = "changed"
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert "ARTIFACT_REVISION_CHANGED" in report["artifacts"][0]["reasons"]


def test_unknown_artifact_cannot_use_identical_approved_bytes(tmp_path):
    case = make_fixture(tmp_path)
    case["packet"]["artifacts"][0]["artifact_id"] = "unknown"
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert "ARTIFACT_UNKNOWN" in report["artifacts"][0]["reasons"]


def test_declared_graph_dependencies_must_be_present_in_packet(tmp_path):
    case = make_fixture(tmp_path)
    dependency = deepcopy(case["document"]["artifacts"][0])
    dependency["artifact_id"] = "dependency"
    case["document"]["artifacts"].append(dependency)
    case["document"]["artifacts"][0]["depends_on"] = ["dependency"]
    _refresh(case)
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert "ARTIFACT_DEPENDENCY_MISSING" in report["artifacts"][0]["reasons"]
    row = deepcopy(case["packet"]["artifacts"][0])
    row.update(artifact_id="dependency", path="dependency.txt")
    case["packet"]["artifacts"].append(row)
    _write(case, tmp_path, (_wording(case) + "\n").encode(), row=row)
    assert _run(case, tmp_path)["status"] == "VERIFIED"


def test_expired_grounding_blocks_freshly_read_packet(tmp_path):
    case = make_fixture(tmp_path)
    report = _run(case, tmp_path, now=case["now"] + timedelta(seconds=91))
    assert report["status"] == "BLOCKED"
    assert "GROUNDING_BLOCKED" in report["reasons"]


def test_changed_evidence_is_reread_on_each_packet_verification(tmp_path):
    case = make_fixture(tmp_path)
    assert _run(case, tmp_path)["status"] == "VERIFIED"
    (tmp_path / "evidence" / case["bindings"]["sources"][0]["path"]).write_bytes(b"changed")
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert "GROUNDING_BLOCKED" in report["reasons"]


def test_attachments_get_identity_checks_and_never_semantic_approval(tmp_path):
    case = make_fixture(tmp_path)
    raw = b"\x00\xffsynthetic binary attachment"
    row = {"attachment_id": "attachment", "path": "attachment.bin",
           "sha256": hashlib.sha256(raw).hexdigest()}
    case["packet"]["attachments"].append(row)
    _write(case, tmp_path, raw, row=row)
    report = _run(case, tmp_path)
    assert report["status"] == "VERIFIED"
    assert report["attachments"] == [{"attachment_id": "attachment", "status": "IDENTITY_VERIFIED",
                                      "reasons": [], "identity_verified": True,
                                      "semantic_content_verified": False}]
    _write(case, tmp_path, raw + b"changed", row=row, update_hash=False)
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert report["attachments"][0]["reasons"] == ["ATTACHMENT_FILE_UNVERIFIED"]


@pytest.mark.parametrize("change", ["empty", "duplicate_artifact", "duplicate_path", "extra_top", "extra_artifact",
                                    "extra_attachment", "caller_report", "wrong_format", "text_fields"])
def test_closed_packet_schema_rejects_ambiguous_or_unhandled_content(tmp_path, change):
    case = make_fixture(tmp_path)
    packet = case["packet"]
    if change == "empty":
        packet["artifacts"] = []
    elif change == "duplicate_artifact":
        packet["artifacts"].append(deepcopy(packet["artifacts"][0]))
    elif change == "duplicate_path":
        row = packet["artifacts"][0]
        packet["attachments"] = [{"attachment_id": "alias", "path": row["path"], "sha256": row["sha256"]}]
    elif change == "extra_top":
        packet["footer"] = "Unapproved"
    elif change == "extra_artifact":
        packet["artifacts"][0]["prefix"] = "Unapproved"
    elif change == "extra_attachment":
        packet["attachments"] = [{"attachment_id": "item", "path": "item.bin", "sha256": "a" * 64, "approved": True}]
    elif change == "caller_report":
        packet["grounding_report"] = {"status": "VERIFIED"}
    elif change == "wrong_format":
        packet["artifacts"][0]["format"] = "pdf"
    else:
        packet["artifacts"][0]["fields"] = [{"field_id": "extra", "statement_index": 0}]
    with pytest.raises(GroundingError):
        _run(case, tmp_path)


@pytest.mark.parametrize("path", ["/absolute.txt", "../escape.txt", "nested/../artifact.txt", "./artifact.txt",
                                  "nested//artifact.txt", "nested\\artifact.txt", "C:/artifact.txt"])
def test_noncanonical_packet_paths_are_rejected(tmp_path, path):
    case = make_fixture(tmp_path)
    case["packet"]["artifacts"][0]["path"] = path
    with pytest.raises(GroundingError):
        _run(case, tmp_path)


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_aliases_cannot_hide_duplicate_physical_packet_files(tmp_path, alias):
    case = make_fixture(tmp_path)
    row = case["packet"]["artifacts"][0]
    original = tmp_path / "packet" / row["path"]
    destination = tmp_path / "packet" / "alias.txt"
    if alias == "symlink":
        destination.symlink_to(original)
    else:
        os.link(original, destination)
    case["packet"]["attachments"] = [{"attachment_id": "alias", "path": "alias.txt", "sha256": row["sha256"]}]
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert report["attachments"][0]["status"] == "BLOCKED"


def test_failure_reports_do_not_echo_sensitive_packet_content(tmp_path):
    case = make_fixture(tmp_path)
    secret = "DO_NOT_ECHO_SYNTHETIC_PRIVATE_MATERIAL"
    _write(case, tmp_path, secret.encode())
    report = _run(case, tmp_path)
    assert report["status"] == "BLOCKED"
    assert secret not in json.dumps(report)


def test_unapproved_field_mapping_change_cannot_supply_its_own_manifest_binding(tmp_path):
    case = make_fixture(tmp_path)
    _json_packet(case, tmp_path)
    reviewed_manifest = digest(case["packet"])
    case["packet"]["artifacts"][0]["fields"][0]["field_id"] = "other_field"
    _write(case, tmp_path, json.dumps({"other_field": _wording(case)}).encode())
    report = _run(case, tmp_path, expected_packet_sha256=reviewed_manifest)
    assert report["status"] == "BLOCKED"
    assert report["reasons"] == ["PACKET_MANIFEST_CHANGED"]
    assert report["declared_manifest_binding_verified"] is False


def test_packet_manifest_binding_is_mandatory_and_not_read_from_packet(tmp_path):
    case = make_fixture(tmp_path)
    with pytest.raises(TypeError):
        verify_packet(case["document"], case["bindings"], case["packet"],
                      evidence_root=tmp_path / "evidence", packet_root=tmp_path / "packet",
                      now=case["now"])


@pytest.mark.parametrize("expected", [None, True, "", "A" * 64, "0" * 63])
def test_manifest_binding_requires_exact_sha256(tmp_path, expected):
    case = make_fixture(tmp_path)
    with pytest.raises(GroundingError):
        _run(case, tmp_path, expected_packet_sha256=expected)
