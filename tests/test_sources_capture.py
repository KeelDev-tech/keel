"""Synthetic records exercise capture semantics; they are not live lead evidence."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import os
import stat

import pytest

from keel_sources.capture import SourceError, ingest_attachments, validate_semantics, validate_source


NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
SCOPE = {"workspace_id": "synthetic-workspace", "role_id": "synthetic-role",
         "application_id": "synthetic-application", "action": "submit"}
DESTINATION = "https://apply.example.invalid/synthetic-role"


@pytest.fixture(autouse=True)
def fixture_cwd_for_descriptor_relative_writes(tmp_path, monkeypatch):
    # The unchanged legacy audit guard resolves dir_fd-relative path strings
    # against cwd. Keep that representation and the actual destination within
    # the isolated fixture tree; never relax the guard or use production paths.
    monkeypatch.chdir(tmp_path)


def source(name, record):
    return {"source_ref": f"synthetic:{name}", "source_version": "v1",
            "observed_at": (NOW - timedelta(seconds=20)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=10)).isoformat(), "record": record}


def complete_sources(root):
    """Reproducible synthetic six-source example, including actual local bytes."""
    data = b"SYNTHETIC RESUME: fixture only\n"
    (root / "resume.txt").write_bytes(data)
    return {
        "policy": source("policy", {"policy_id": "synthetic-policy", "rules": {
            "allowed_actions": ["submit"], "allowed_accounts": ["synthetic-account"],
            "allowed_destinations": [DESTINATION], "requires_human_approval": True, "holds": []}}),
        "form": source("form", {"form_id": "synthetic-form", "fields": [{
            "field_id": "name", "label": "Name", "kind": "text", "required": True,
            "assistance_allowed": True, "answer_class": "fact"}],
            "required_attachment_purposes": ["resume"]}),
        "answers": source("answers", {"fields": {"name": "Synthetic Candidate"}, "provenance": {
            "name": {"source_ref": "synthetic:profile-name", "source_version": "v1",
                     "origin": "verified_profile", "observed_at": (NOW - timedelta(minutes=1)).isoformat(),
                     "evidence_refs": ["synthetic:name-confirmation"]}}}),
        "attachments": source("attachments", {"files": [{"path": "resume.txt", "purpose": "resume",
            "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}]}),
        "target": source("target", {"role_id": SCOPE["role_id"], "application_id": SCOPE["application_id"],
            "canonical_posting_url": "https://jobs.example.invalid/synthetic-role", "application_url": DESTINATION,
            "verified": True, "verification_ref": "synthetic:posting-capture"}),
        "route": source("route", {"role_id": SCOPE["role_id"], "application_id": SCOPE["application_id"],
            "action": "submit", "account_id": "synthetic-account", "transport": "browser",
            "destination": DESTINATION, "target_source_ref": "synthetic:target", "target_source_version": "v1"}),
    }


def check(sources, root, now=NOW):
    return validate_semantics(sources, scope=SCOPE, attachment_root=root, now=now)


def codes(sources, root, now=NOW):
    return {item["code"] for item in check(sources, root, now)["issues"]}


def test_six_actual_records_are_reviewable_not_authorized_or_authenticated(tmp_path):
    inputs = complete_sources(tmp_path)
    before = copy.deepcopy(inputs)
    result = check(inputs, tmp_path)
    assert result["ready_for_approval"]
    assert not result["execution_authorized"]
    assert not result["source_authenticity_verified"]
    assert not result["factual_support_verified"]
    assert inputs == before


@pytest.mark.parametrize("component", ["policy", "form", "answers", "attachments", "target", "route"])
def test_missing_family_never_becomes_ready(tmp_path, component):
    inputs = complete_sources(tmp_path)
    del inputs[component]
    assert not check(inputs, tmp_path)["ready_for_approval"]


def test_preserves_original_descriptors_and_restrictions(tmp_path):
    item = complete_sources(tmp_path)["policy"]
    item["record"]["rules"]["holds"] = ["existing-dedupe-hold"]
    item["record"]["metadata"] = {"display_name": "Original policy"}
    validated = validate_source("policy", item, scope=SCOPE, attachment_root=tmp_path, now=NOW)
    assert validated == item
    validated["record"]["rules"]["holds"].clear()
    assert item["record"]["rules"]["holds"] == ["existing-dedupe-hold"]


@pytest.mark.parametrize("mutate,expected", [
    (lambda s: s["route"]["record"].update(destination="https://evil.example.invalid/apply"), "route_target_destination_mismatch"),
    (lambda s: s["route"]["record"].update(target_source_version="v2"), "route_target_revision_mismatch"),
    (lambda s: s["route"]["record"].update(account_id="other-account"), "policy_account_not_allowed"),
    (lambda s: s["route"]["record"].update(role_id="another-role"), "route_scope_mismatch"),
    (lambda s: s["target"]["record"].update(verified=False), "target_not_verified"),
    (lambda s: s["policy"]["record"]["rules"].update(holds=["dedupe"]), "policy_active_holds"),
    (lambda s: s["policy"]["record"]["rules"].update(max_applications=1), "policy_unsupported_rules"),
    (lambda s: s["policy"]["record"]["rules"].update(requires_human_approval=False), "human_approval_required_by_capture_contract"),
    (lambda s: s["form"]["record"]["fields"][0].update(assistance_allowed=False), "answer_human_origin_required"),
    (lambda s: s["form"]["record"]["fields"][0].pop("assistance_allowed"), "answer_human_origin_required"),
    (lambda s: s["form"]["record"]["fields"][0].update(max_length=10), "form_unsupported_field_constraints"),
    (lambda s: s["answers"]["record"]["fields"].update(name=""), "answer_required_empty"),
    (lambda s: s["answers"]["record"]["provenance"]["name"].update(evidence_refs=[]), "answer_evidence_missing"),
    (lambda s: s["answers"]["record"]["provenance"]["name"].update(origin="generated_draft", evidence_refs=[]), "generated_draft_not_factual_evidence"),
])
def test_cross_record_restrictions_fail_closed(tmp_path, mutate, expected):
    inputs = complete_sources(tmp_path)
    mutate(inputs)
    assert expected in codes(inputs, tmp_path)


def test_required_answer_absence_and_unknown_field(tmp_path):
    inputs = complete_sources(tmp_path)
    answers = inputs["answers"]["record"]
    answers["fields"]["unknown"] = answers["fields"].pop("name")
    answers["provenance"]["unknown"] = answers["provenance"].pop("name")
    assert {"answer_required_missing", "answer_unknown_field"} <= codes(inputs, tmp_path)


@pytest.mark.parametrize("kind,value,extra,expected", [
    ("checkbox", "true", {}, "answer_checkbox_type_invalid"),
    ("checkbox", False, {}, "answer_required_checkbox_unchecked"),
    ("text", True, {}, "answer_text_type_invalid"),
    ("select", "no", {"options": ["yes"]}, "answer_select_option_invalid"),
])
def test_answer_types_and_options(tmp_path, kind, value, extra, expected):
    inputs = complete_sources(tmp_path)
    inputs["form"]["record"]["fields"][0].update(kind=kind, **extra)
    inputs["answers"]["record"]["fields"]["name"] = value
    assert expected in codes(inputs, tmp_path)


def test_generated_statement_remains_draft_not_factual_source(tmp_path):
    inputs = complete_sources(tmp_path)
    inputs["form"]["record"]["fields"][0]["answer_class"] = "statement"
    inputs["answers"]["record"]["provenance"]["name"].update(origin="generated_draft", evidence_refs=[])
    assert check(inputs, tmp_path)["ready_for_approval"]
    inputs["form"]["record"]["fields"][0]["answer_class"] = "attestation"
    assert "answer_human_origin_required" in codes(inputs, tmp_path)


def test_stale_and_revoked_evidence_can_be_stored_but_never_made_current(tmp_path):
    item = complete_sources(tmp_path)["policy"]
    item.update(expires_at=(NOW - timedelta(seconds=1)).isoformat(), revoked=True)
    assert validate_source("policy", item, scope=SCOPE, attachment_root=tmp_path,
                           now=NOW, allow_inactive=True) == item
    with pytest.raises(SourceError, match="source_expired"):
        validate_source("policy", item, scope=SCOPE, attachment_root=tmp_path, now=NOW)


def test_attachment_ingest_copies_exact_bytes_and_keeps_source_metadata(tmp_path):
    original = tmp_path / "source"
    original.mkdir(mode=0o700)
    (original / "resume.txt").write_bytes(b"supplied actual bytes")
    item = source("attachments", {"files": [{"path": "resume.txt", "purpose": "resume"}]})
    captured = ingest_attachments(item, source_root=original, attachment_root=tmp_path / "private",
                                  scope=SCOPE, now=NOW)
    record = captured["record"]["files"][0]
    assert record["source_path"] == "resume.txt"
    assert (tmp_path / "private" / record["path"]).read_bytes() == b"supplied actual bytes"
    assert stat.S_IMODE((tmp_path / "private" / record["path"]).stat().st_mode) == 0o600
    for key in ("source_ref", "source_version", "observed_at", "expires_at"):
        assert captured[key] == item[key]
    assert "sha256" not in item["record"]["files"][0]
    assert ingest_attachments(item, source_root=original, attachment_root=tmp_path / "private",
                              scope=SCOPE, now=NOW) == captured


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "a/../b", "a\\b", "a//b"])
def test_attachment_paths_rejected(tmp_path, path):
    item = source("attachments", {"files": [{"path": path, "purpose": "resume"}]})
    with pytest.raises(SourceError, match="attachment_path_invalid"):
        ingest_attachments(item, source_root=tmp_path, attachment_root=tmp_path / "private",
                           scope=SCOPE, now=NOW)


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_attachment_link_aliases_rejected(tmp_path, link_kind):
    (tmp_path / "original").write_bytes(b"real")
    if link_kind == "symlink":
        (tmp_path / "linked").symlink_to(tmp_path / "original")
    else:
        os.link(tmp_path / "original", tmp_path / "linked")
    item = source("attachments", {"files": [{"path": "linked", "purpose": "resume"}]})
    with pytest.raises(SourceError):
        ingest_attachments(item, source_root=tmp_path, attachment_root=tmp_path / "private",
                           scope=SCOPE, now=NOW)


def test_existing_content_address_is_never_overwritten(tmp_path):
    (tmp_path / "input").write_bytes(b"real")
    digest = hashlib.sha256(b"real").hexdigest()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    objects = private / "objects"
    objects.mkdir(mode=0o700)
    (objects / digest).write_bytes(b"corrupt")
    item = source("attachments", {"files": [{"path": "input", "purpose": "resume"}]})
    with pytest.raises(SourceError, match="content_address_collision"):
        ingest_attachments(item, source_root=tmp_path, attachment_root=private, scope=SCOPE, now=NOW)
    assert (objects / digest).read_bytes() == b"corrupt"


def test_bytes_changed_after_capture_block_review(tmp_path):
    inputs = complete_sources(tmp_path)
    (tmp_path / "resume.txt").write_bytes(b"changed!")
    assert {"attachment_size_mismatch", "attachment_hash_mismatch"} & codes(inputs, tmp_path)


def test_required_attachment_cannot_be_replaced_by_empty_manifest(tmp_path):
    inputs = complete_sources(tmp_path)
    inputs["attachments"]["record"] = {"files": [], "none_required": True}
    assert "required_attachment_missing" in codes(inputs, tmp_path)


def test_actual_future_provenance_is_not_backdated(tmp_path):
    inputs = complete_sources(tmp_path)
    inputs["answers"]["record"]["provenance"]["name"]["observed_at"] = NOW.isoformat()
    assert "answer_provenance_observation_invalid" in codes(inputs, tmp_path)


def test_atomic_publication_error_leaves_no_authoritative_or_partial_file(tmp_path, monkeypatch):
    import keel_sources.capture as capture
    (tmp_path / "input").write_bytes(b"real")
    item = source("attachments", {"files": [{"path": "input", "purpose": "resume"}]})
    observed = []
    def crash_before_publish(directory, pending, digest):
        # The pending file is complete; no authoritative final filename exists yet.
        objects = tmp_path / "private" / "objects"
        assert (objects / pending).read_bytes() == b"real"
        assert not (objects / digest).exists()
        observed.append(True)
        raise SourceError("injected_publication_failure")
    monkeypatch.setattr(capture, "_publish_atomic", crash_before_publish)
    with pytest.raises(SourceError, match="injected_publication_failure"):
        ingest_attachments(item, source_root=tmp_path, attachment_root=tmp_path / "private",
                           scope=SCOPE, now=NOW)
    assert observed
    assert list((tmp_path / "private" / "objects").iterdir()) == []


def test_orphan_pending_file_does_not_prevent_fresh_capture(tmp_path):
    (tmp_path / "input").write_bytes(b"real")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    objects = private / "objects"
    objects.mkdir(mode=0o700)
    (objects / "pending-dead-process.tmp").write_bytes(b"incomplete")
    item = source("attachments", {"files": [{"path": "input", "purpose": "resume"}]})
    result = ingest_attachments(item, source_root=tmp_path, attachment_root=private,
                                scope=SCOPE, now=NOW)
    published = private / result["record"]["files"][0]["path"]
    assert published.read_bytes() == b"real"
    assert published.stat().st_nlink == 1


def test_atomic_unavailable_never_falls_back_to_overwrite(tmp_path, monkeypatch):
    import keel_sources.capture as capture
    (tmp_path / "input").write_bytes(b"real")
    monkeypatch.setattr(capture.ctypes, "CDLL", lambda *a, **k: object())
    item = source("attachments", {"files": [{"path": "input", "purpose": "resume"}]})
    with pytest.raises(SourceError, match="atomic_attachment_publication_unavailable"):
        ingest_attachments(item, source_root=tmp_path, attachment_root=tmp_path / "private",
                           scope=SCOPE, now=NOW)
    assert list((tmp_path / "private" / "objects").iterdir()) == []


def test_repeated_identical_capture_reuses_complete_single_link_object(tmp_path):
    (tmp_path / "input").write_bytes(b"real")
    item = source("attachments", {"files": [{"path": "input", "purpose": "resume"}]})
    root = tmp_path / "private"
    first = ingest_attachments(item, source_root=tmp_path, attachment_root=root, scope=SCOPE, now=NOW)
    before = (root / first["record"]["files"][0]["path"]).stat()
    second = ingest_attachments(item, source_root=tmp_path, attachment_root=root, scope=SCOPE, now=NOW)
    after = (root / second["record"]["files"][0]["path"]).stat()
    assert first == second
    assert before.st_ino == after.st_ino
    assert after.st_nlink == 1
    assert len(list((root / "objects").iterdir())) == 1


def test_duplicate_content_is_rejected_before_copy(tmp_path):
    (tmp_path / "one").write_bytes(b"same")
    (tmp_path / "two").write_bytes(b"same")
    item = source("attachments", {"files": [{"path": "one", "purpose": "resume"},
                                               {"path": "two", "purpose": "cover-letter"}]})
    with pytest.raises(SourceError, match="attachment_duplicate_content"):
        ingest_attachments(item, source_root=tmp_path, attachment_root=tmp_path / "private",
                           scope=SCOPE, now=NOW)
    assert not (tmp_path / "private").exists()
