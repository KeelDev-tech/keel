"""Capture supplied records and actual attachment bytes; never invent evidence.

The normalized contract is intentionally narrow. Unknown constraints are rejected,
not removed. Source references are assertions, not proof of authenticity.
"""
from __future__ import annotations

import copy
import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

from keel_agent.revisions import (
    PREAPPROVAL_COMPONENTS, MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS,
    MAX_TOTAL_ATTACHMENT_BYTES, SourceError, _canonical, _directory_fd,
    _material_record, _time, _token, _url,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")
_RECORD_KEYS = {
    "policy": {"policy_id", "rules", "metadata"},
    "form": {"form_id", "fields", "required_attachment_purposes", "metadata"},
    "answers": {"fields", "provenance", "metadata"},
    "attachments": {"files", "none_required", "metadata"},
    "target": {"role_id", "application_id", "canonical_posting_url", "application_url",
               "verified", "verification_ref", "metadata"},
    "route": {"role_id", "application_id", "action", "account_id", "transport",
              "destination", "target_source_ref", "target_source_version", "metadata"},
}
_RULE_KEYS = {"allowed_actions", "allowed_accounts", "allowed_destinations",
              "requires_human_approval", "holds"}
_FIELD_KEYS = {"field_id", "label", "kind", "required", "options",
               "assistance_allowed", "answer_class", "metadata"}
_PROVENANCE_KEYS = {"source_ref", "source_version", "origin", "observed_at", "evidence_refs"}


def _require(condition, code):
    if not condition:
        raise SourceError(code)


def _keys(value, allowed, code):
    _require(isinstance(value, dict) and set(value) <= allowed, code)


def _strings(value, code, *, nonempty=False, identifiers=False, urls=False):
    _require(isinstance(value, list) and (not nonempty or bool(value)), code)
    _require(all(isinstance(x, str) and x.strip() for x in value), code)
    _require(len(set(value)) == len(value), code)
    for item in value:
        if identifiers:
            _token(item)
        if urls:
            _url(item)
    return value


def _now(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None
             and value.utcoffset() is not None, "now_timezone_required")
    return value.astimezone(timezone.utc)


def _scope(scope):
    _require(isinstance(scope, dict) and set(scope) == {
        "workspace_id", "role_id", "application_id", "action"}, "source_scope_invalid")
    for value in scope.values():
        _token(value)


def _metadata(source, now, allow_inactive):
    _require(isinstance(source, dict), "source_descriptor_missing")
    _canonical(source)
    _token(source.get("source_ref"))
    _token(source.get("source_version"))
    observed, expires = _time(source.get("observed_at")), _time(source.get("expires_at"))
    _require(observed <= now, "source_observation_in_future")
    _require(expires > observed, "source_validity_invalid")
    _require("revoked" not in source or type(source["revoked"]) is bool,
             "source_revocation_invalid")
    if not allow_inactive:
        _require(expires > now, "source_expired")
        _require(source.get("revoked") is not True, "source_revoked")
    _require("absence" not in source, "source_record_missing")


def _record(component, record, scope, now, observed_at):
    _keys(record, _RECORD_KEYS[component], f"{component}_unsupported_record_fields")
    if "metadata" in record:
        _require(isinstance(record["metadata"], dict), "metadata_invalid")
    if component == "policy":
        _token(record.get("policy_id"))
        rules = record.get("rules")
        _keys(rules, _RULE_KEYS, "policy_unsupported_rules")
        _require(set(rules) == _RULE_KEYS, "policy_rules_incomplete")
        for key in ("allowed_actions", "allowed_accounts"):
            _strings(rules[key], "policy_allowlist_invalid", nonempty=True, identifiers=True)
        _strings(rules["allowed_destinations"], "policy_allowlist_invalid", nonempty=True, urls=True)
        _require(type(rules["requires_human_approval"]) is bool, "policy_approval_rule_invalid")
        _strings(rules["holds"], "policy_holds_invalid")
    elif component == "form":
        _token(record.get("form_id"))
        fields = record.get("fields")
        _require(isinstance(fields, list) and bool(fields), "form_fields_missing")
        identifiers = []
        for field in fields:
            _keys(field, _FIELD_KEYS, "form_unsupported_field_constraints")
            identifiers.append(_token(field.get("field_id")))
            _require(isinstance(field.get("label"), str) and field["label"].strip(), "form_label_invalid")
            _require(field.get("kind") in {"text", "select", "checkbox"}, "form_kind_unsupported")
            _require(type(field.get("required")) is bool, "form_required_invalid")
            if "assistance_allowed" in field:
                _require(type(field["assistance_allowed"]) is bool, "form_assistance_invalid")
            if "answer_class" in field:
                _require(field["answer_class"] in {"fact", "statement", "attestation"},
                         "form_answer_class_invalid")
            if field["kind"] == "select":
                _strings(field.get("options"), "form_select_options_invalid", nonempty=True)
            else:
                _require("options" not in field, "form_unexpected_options")
        _require(len(set(identifiers)) == len(identifiers), "form_field_duplicate")
        _strings(record.get("required_attachment_purposes"),
                 "form_attachment_requirements_missing", identifiers=True)
    elif component == "answers":
        fields, provenance = record.get("fields"), record.get("provenance")
        _require(isinstance(fields, dict) and isinstance(provenance, dict), "answer_provenance_missing")
        _require(set(fields) == set(provenance), "answer_provenance_mismatch")
        for field_id, value in fields.items():
            _token(field_id)
            _require(type(value) in (str, bool), "answer_value_type_invalid")
            origin = provenance[field_id]
            _keys(origin, _PROVENANCE_KEYS, "answer_provenance_invalid")
            _require(set(origin) == _PROVENANCE_KEYS, "answer_provenance_incomplete")
            _token(origin["source_ref"])
            _token(origin["source_version"])
            _require(origin["origin"] in {"human", "verified_profile", "generated_draft"},
                     "answer_origin_invalid")
            _require(_time(origin["observed_at"]) <= observed_at <= now,
                     "answer_provenance_observation_invalid")
            _strings(origin["evidence_refs"], "answer_evidence_refs_invalid", identifiers=True)
            if origin["origin"] == "verified_profile":
                _require(bool(origin["evidence_refs"]), "answer_evidence_missing")
            if origin["origin"] == "generated_draft":
                _require(not origin["evidence_refs"], "generated_draft_not_factual_evidence")
    elif component == "attachments":
        files = record.get("files")
        _require(isinstance(files, list) and len(files) <= MAX_ATTACHMENTS, "attachment_manifest_invalid")
        if "none_required" in record:
            _require(type(record["none_required"]) is bool, "attachment_none_required_invalid")
        _require(bool(files) != (record.get("none_required") is True), "attachment_absence_not_explicit")
        for item in files:
            _keys(item, {"path", "purpose", "sha256", "size_bytes", "source_path", "filename",
                         "mime_type", "metadata"}, "attachment_unsupported_manifest_fields")
            _token(item.get("purpose"))
            _require(isinstance(item.get("sha256"), str) and bool(_HASH.fullmatch(item["sha256"])),
                     "attachment_hash_missing")
            _require(type(item.get("size_bytes")) is int and 0 <= item["size_bytes"] <= MAX_ATTACHMENT_BYTES,
                     "attachment_size_invalid")
    elif component == "target":
        for key in ("role_id", "application_id"):
            _require(record.get(key) == scope[key], "target_scope_mismatch")
        _url(record.get("canonical_posting_url"))
        _url(record.get("application_url"))
        _require(type(record.get("verified")) is bool, "target_verification_missing")
        _token(record.get("verification_ref"))
    elif component == "route":
        for key in ("role_id", "application_id", "action"):
            _require(record.get(key) == scope[key], "route_scope_mismatch")
        for key in ("account_id", "transport", "target_source_ref", "target_source_version"):
            _token(record.get(key))
        _url(record.get("destination"))


def _relative_parts(relative):
    _require(isinstance(relative, str) and bool(relative) and "\\" not in relative
             and "\x00" not in relative, "attachment_path_invalid")
    parts = relative.split("/")
    _require(all(part not in ("", ".", "..") for part in parts), "attachment_path_invalid")
    return parts


def _read_bytes(root, relative):
    parts = _relative_parts(relative)
    directory, descriptor = _directory_fd(root), None
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode), "attachment_not_regular")
        _require(before.st_nlink == 1, "attachment_hardlink_rejected")
        _require(before.st_size <= MAX_ATTACHMENT_BYTES, "attachment_too_large")
        chunks, size = [], 0
        while True:
            data = os.read(descriptor, 65536)
            if not data:
                break
            size += len(data)
            _require(size <= MAX_ATTACHMENT_BYTES, "attachment_too_large")
            chunks.append(data)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        _require(size == before.st_size and all(getattr(before, k) == getattr(after, k) for k in fields),
                 "attachment_changed_during_read")
        return b"".join(chunks)
    except OSError:
        raise SourceError("attachment_unavailable_or_symlink") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def validate_source(component, descriptor, *, scope, attachment_root, now, allow_inactive=False):
    """Return a defensive copy of a supported source, preserving its original facts.

    `allow_inactive` is only for storing expired/revoked evidence. It does not
    change timestamps or remove revocation, and never makes that evidence ready.
    """
    _require(component in PREAPPROVAL_COMPONENTS, "capture_component_invalid")
    _require(type(allow_inactive) is bool, "capture_inactive_flag_invalid")
    now = _now(now)
    _scope(scope)
    _metadata(descriptor, now, allow_inactive)
    _record(component, descriptor.get("record"), scope, now, _time(descriptor["observed_at"]))
    _material_record(component, descriptor["record"], scope, attachment_root)
    if component == "attachments":
        total, paths = 0, set()
        for item in descriptor["record"]["files"]:
            _require(item["path"] not in paths, "attachment_path_duplicate")
            paths.add(item["path"])
            data = _read_bytes(attachment_root, item["path"])
            total += len(data)
            _require(total <= MAX_TOTAL_ATTACHMENT_BYTES, "attachments_total_too_large")
            _require(len(data) == item["size_bytes"], "attachment_size_mismatch")
            _require(hashlib.sha256(data).hexdigest() == item["sha256"], "attachment_hash_mismatch")
    return copy.deepcopy(descriptor)


def validate_semantics(sources, *, scope, attachment_root, now):
    """Check six actual records together. Ready means reviewable, never executable."""
    now = _now(now)
    _scope(scope)
    _require(isinstance(sources, dict), "source_mapping_invalid")
    valid, issues = {}, []
    def issue(component, code, field_id=None):
        row = {"component": component, "code": code}
        if field_id is not None:
            row["field_id"] = field_id
        issues.append(row)
    for component in PREAPPROVAL_COMPONENTS:
        try:
            valid[component] = validate_source(component, sources.get(component), scope=scope,
                                               attachment_root=attachment_root, now=now)
        except SourceError as error:
            issue(component, str(error))
    if "policy" in valid:
        rules = valid["policy"]["record"]["rules"]
        if rules["holds"]:
            issue("policy", "policy_active_holds")
        if rules["requires_human_approval"] is not True:
            issue("policy", "human_approval_required_by_capture_contract")
        if scope["action"] not in rules["allowed_actions"]:
            issue("policy", "policy_action_not_allowed")
        if "route" in valid:
            route = valid["route"]["record"]
            if route["account_id"] not in rules["allowed_accounts"]:
                issue("route", "policy_account_not_allowed")
            if route["destination"] not in rules["allowed_destinations"]:
                issue("route", "policy_destination_not_allowed")
    if "target" in valid:
        target = valid["target"]
        if target["record"]["verified"] is not True:
            issue("target", "target_not_verified")
        if "route" in valid:
            route = valid["route"]["record"]
            if route["destination"] != target["record"]["application_url"]:
                issue("route", "route_target_destination_mismatch")
            if (route["target_source_ref"], route["target_source_version"]) != (
                    target["source_ref"], target["source_version"]):
                issue("route", "route_target_revision_mismatch")
    if "form" in valid and "answers" in valid:
        fields = {field["field_id"]: field for field in valid["form"]["record"]["fields"]}
        answers = valid["answers"]["record"]
        for field_id in sorted(set(answers["fields"]) - set(fields)):
            issue("answers", "answer_unknown_field", field_id)
        for field_id, field in fields.items():
            if field_id not in answers["fields"]:
                if field["required"]:
                    issue("answers", "answer_required_missing", field_id)
                continue
            value, provenance = answers["fields"][field_id], answers["provenance"][field_id]
            if field["kind"] == "checkbox":
                if type(value) is not bool:
                    issue("answers", "answer_checkbox_type_invalid", field_id)
                elif field["required"] and value is not True:
                    issue("answers", "answer_required_checkbox_unchecked", field_id)
            else:
                if not isinstance(value, str):
                    issue("answers", "answer_text_type_invalid", field_id)
                elif field["required"] and not value.strip():
                    issue("answers", "answer_required_empty", field_id)
                elif field["kind"] == "select" and value not in field["options"]:
                    issue("answers", "answer_select_option_invalid", field_id)
            origin = provenance["origin"]
            answer_class = field.get("answer_class", "fact")
            if (field.get("assistance_allowed") is not True or answer_class == "attestation") and origin != "human":
                issue("answers", "answer_human_origin_required", field_id)
            if origin == "generated_draft" and answer_class != "statement":
                issue("answers", "generated_draft_not_factual_evidence", field_id)
        if "attachments" in valid:
            purposes = {item["purpose"] for item in valid["attachments"]["record"]["files"]}
            required = set(valid["form"]["record"]["required_attachment_purposes"])
            if not required <= purposes:
                issue("attachments", "required_attachment_missing")
    return {"schema": "keel.source_semantics.v1", "ready_for_approval": not issues,
            "issues": issues, "execution_authorized": False, "source_authenticity_verified": False,
            "factual_support_verified": False}


def _objects_fd(attachment_root):
    path = Path(os.path.abspath(os.fspath(attachment_root)))
    _require(path != Path("/"), "attachment_root_not_private")
    parent = _directory_fd(path.parent)
    root = None
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        root = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        _require(os.fstat(root).st_mode & 0o077 == 0, "attachment_root_not_private")
        try:
            os.mkdir("objects", 0o700, dir_fd=root)
        except FileExistsError:
            pass
        objects = os.open("objects", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        if os.fstat(objects).st_mode & 0o077:
            os.close(objects)
            raise SourceError("attachment_root_not_private")
        return objects
    except OSError:
        raise SourceError("attachment_root_unavailable_or_symlink") from None
    finally:
        if root is not None:
            os.close(root)
        os.close(parent)


def _publish_atomic(directory, pending, digest):
    """Linux atomic rename without replacement; never fall back to overwrite.

    Using link/unlink would leave a forbidden hardlink after a crash between the
    two operations. RENAME_NOREPLACE publishes the complete single-link file.
    """
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        raise SourceError("atomic_attachment_publication_unavailable") from None
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(directory, os.fsencode(pending), directory, os.fsencode(digest), 1) == 0:
        return True
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        return False
    if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise SourceError("atomic_attachment_publication_unavailable")
    raise SourceError("attachment_copy_failed")


def ingest_attachments(descriptor, *, source_root, attachment_root, scope, now):
    """Copy supplied regular files into private content-addressed storage.

    Source metadata is kept. Hash/size/path describe bytes actually read. Existing
    files are never overwritten; matching hashes are verified before reuse.
    """
    now = _now(now)
    _scope(scope)
    _metadata(descriptor, now, True)
    result = copy.deepcopy(descriptor)
    record = result.get("record")
    _keys(record, _RECORD_KEYS["attachments"], "attachments_unsupported_record_fields")
    files = record.get("files")
    _require(isinstance(files, list) and len(files) <= MAX_ATTACHMENTS, "attachment_manifest_invalid")
    _require(bool(files) != (record.get("none_required") is True), "attachment_absence_not_explicit")
    prepared, total, paths, digests = [], 0, set(), set()
    # Read every input before writing; invalid manifests cannot create partial captures.
    for item in files:
        _keys(item, {"path", "purpose", "sha256", "size_bytes", "source_path", "filename",
                     "mime_type", "metadata"}, "attachment_unsupported_manifest_fields")
        _token(item.get("purpose"))
        relative = item.get("path")
        _relative_parts(relative)
        _require(relative not in paths, "attachment_path_duplicate")
        paths.add(relative)
        data = _read_bytes(source_root, relative)
        total += len(data)
        _require(total <= MAX_TOTAL_ATTACHMENT_BYTES, "attachments_total_too_large")
        digest = hashlib.sha256(data).hexdigest()
        _require(digest not in digests, "attachment_duplicate_content")
        digests.add(digest)
        if "sha256" in item:
            _require(item["sha256"] == digest, "attachment_hash_mismatch")
        if "size_bytes" in item:
            _require(type(item["size_bytes"]) is int and item["size_bytes"] == len(data),
                     "attachment_size_mismatch")
        _require("source_path" not in item or item["source_path"] == relative,
                 "attachment_source_path_mismatch")
        item.update(source_path=relative, path=f"objects/{digest}", sha256=digest, size_bytes=len(data))
        prepared.append((digest, data))
    _record("attachments", record, scope, now, _time(result["observed_at"]))
    objects = _objects_fd(attachment_root)
    try:
        for digest, data in prepared:
            pending = "pending-" + secrets.token_hex(24) + ".tmp"
            descriptor_fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o600, dir_fd=objects)
            try:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(descriptor_fd, remaining)
                    _require(written > 0, "attachment_copy_failed")
                    remaining = remaining[written:]
                os.fsync(descriptor_fd)
                published = _publish_atomic(objects, pending, digest)
                if not published:
                    existing = _read_bytes(attachment_root, f"objects/{digest}")
                    _require(existing == data, "attachment_content_address_collision")
            finally:
                os.close(descriptor_fd)
                try:
                    os.unlink(pending, dir_fd=objects)
                except FileNotFoundError:
                    pass
        os.fsync(objects)
    except OSError:
        raise SourceError("attachment_copy_failed") from None
    finally:
        os.close(objects)
    return validate_source("attachments", result, scope=scope, attachment_root=attachment_root,
                           now=now, allow_inactive=True)
