"""Content-bound export of real source records, never evidence invention.

This is a normalized host adapter contract, not a mapper for an unknown live DB.
Hashing establishes content identity; it does not authenticate a source or consent.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone
from urllib.parse import urlsplit


SCHEMA = "keel.revision_sources.v1"
COMPONENTS = ("policy", "form", "answers", "attachments", "target", "approval", "route")
PREAPPROVAL_COMPONENTS = tuple(name for name in COMPONENTS if name != "approval")
MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_ATTACHMENTS = 32
MAX_RECORD_BYTES = 2 * 1024 * 1024
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}$")


class SourceError(ValueError):
    """A fixed, non-sensitive diagnostic code."""


def _token(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise SourceError("identifier_invalid")
    return value


def _time(value):
    if not isinstance(value, str):
        raise SourceError("timestamp_invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SourceError("timestamp_invalid") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise SourceError("timestamp_invalid")
    return result.astimezone(timezone.utc)


def _canonical(value):
    pending = [value]
    inspected = 0
    while pending:
        inspected += 1
        if inspected > 100000 or len(pending) > 100000:
            raise SourceError("record_too_complex")
        item = pending.pop()
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise SourceError("record_not_json")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise SourceError("record_not_json")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise SourceError("record_not_json") from None
    if len(encoded) > MAX_RECORD_BYTES:
        raise SourceError("record_too_large")
    return encoded


def _url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(c.isspace() for c in value):
        raise SourceError("destination_invalid")
    try:
        url = urlsplit(value)
        valid = (url.scheme == "https" and url.hostname and not url.username
                 and not url.password and not url.fragment)
        _ = url.port
    except ValueError:
        valid = False
    if not valid:
        raise SourceError("destination_invalid")
    return value


def _directory_fd(root):
    """Open every absolute root component without following symlinks (POSIX)."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise SourceError("safe_attachment_open_unavailable")
    absolute = Path(os.path.abspath(os.fspath(root)))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise SourceError("attachment_root_unavailable_or_symlink") from None


def _read_attachment(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise SourceError("attachment_path_invalid")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts) or "\x00" in relative:
        raise SourceError("attachment_path_invalid")
    directory = _directory_fd(root)
    descriptor = None
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourceError("attachment_not_regular")
        if before.st_size > MAX_ATTACHMENT_BYTES:
            raise SourceError("attachment_too_large")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ATTACHMENT_BYTES:
                raise SourceError("attachment_too_large")
            digest.update(chunk)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if size != before.st_size or any(getattr(before, k) != getattr(after, k) for k in fields):
            raise SourceError("attachment_changed_during_read")
        return {"sha256": digest.hexdigest(), "size_bytes": size}
    except OSError:
        raise SourceError("attachment_unavailable_or_symlink") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _material_record(component, record, scope, attachment_root):
    if not isinstance(record, dict) or not record:
        raise SourceError("record_missing_or_invalid")
    _canonical(record)
    if component == "policy":
        _token(record.get("policy_id"))
        if not isinstance(record.get("rules"), dict) or not record["rules"]:
            raise SourceError("policy_rules_missing")
    elif component == "form":
        _token(record.get("form_id"))
        if not isinstance(record.get("fields"), list) or not record["fields"]:
            raise SourceError("form_fields_missing")
        identifiers = []
        for field in record["fields"]:
            if not isinstance(field, dict):
                raise SourceError("form_field_invalid")
            identifiers.append(_token(field.get("field_id")))
        if len(set(identifiers)) != len(identifiers):
            raise SourceError("form_field_duplicate")
    elif component == "answers":
        if not isinstance(record.get("fields"), dict):
            raise SourceError("answer_fields_missing")
        for field_id in record["fields"]:
            _token(field_id)
    elif component == "attachments":
        files = record.get("files")
        if not isinstance(files, list) or len(files) > MAX_ATTACHMENTS:
            raise SourceError("attachment_manifest_invalid")
        if not files and record.get("none_required") is not True:
            raise SourceError("attachment_absence_not_explicit")
        if files and record.get("none_required") is True:
            raise SourceError("attachment_manifest_conflict")
        hashes, total, paths = [], 0, set()
        for item in files:
            if not isinstance(item, dict):
                raise SourceError("attachment_manifest_invalid")
            purpose = _token(item.get("purpose"))
            relative = item.get("path")
            if not isinstance(relative, str) or relative in paths:
                raise SourceError("attachment_path_invalid_or_duplicate")
            paths.add(relative)
            actual = _read_attachment(attachment_root, relative)
            total += actual["size_bytes"]
            if total > MAX_TOTAL_ATTACHMENT_BYTES:
                raise SourceError("attachments_total_too_large")
            # Preserve original manifest metadata as material as well as actual bytes.
            hashes.append({"manifest": item, "purpose": purpose, **actual})
        return {"manifest": record, "files": hashes}
    elif component == "target":
        _url(record.get("canonical_posting_url"))
        if any(record.get(key) != scope[key] for key in ("role_id", "application_id")):
            raise SourceError("target_scope_mismatch")
    elif component == "route":
        _token(record.get("account_id"))
        _token(record.get("transport"))
        _url(record.get("destination"))
        if record.get("action") != scope["action"]:
            raise SourceError("route_action_mismatch")
    return record


def _metadata(source, now):
    if not isinstance(source, dict):
        raise SourceError("source_descriptor_missing")
    for key in ("source_ref", "source_version"):
        _token(source.get(key))
    if "observed_at" not in source or "expires_at" not in source:
        raise SourceError("source_timestamps_missing")
    observed, expires = _time(source["observed_at"]), _time(source["expires_at"])
    if observed > now:
        raise SourceError("source_observation_in_future")
    if expires <= observed:
        raise SourceError("source_validity_invalid")
    if expires <= now:
        raise SourceError("source_expired")
    if source.get("revoked") is True:
        raise SourceError("source_revoked")
    if "revoked" in source and not isinstance(source["revoked"], bool):
        raise SourceError("source_revocation_invalid")


def revision_digest(scope, component, source, material_record):
    """Stable material identity; observation/expiry is enforced separately.

    Public for inspection; only export_revisions validates the source and bytes.
    """
    if component not in COMPONENTS:
        raise ValueError("unknown revision component")
    content = {"schema": "keel.material_revision.v1", "purpose": component,
               "scope": scope, "source_ref": source["source_ref"],
               "source_version": source["source_version"], "record": material_record}
    return hashlib.sha256(_canonical(content)).hexdigest()


def _failure(component, code, source=None, *, human=False):
    source = source if isinstance(source, dict) else {}
    ref = source.get("source_ref")
    ref = ref if isinstance(ref, str) and _TOKEN.fullmatch(ref) else None
    state = "MISSING" if code in {
        "source_not_exported", "source_descriptor_missing", "source_timestamps_missing",
        "human_decision_required", "source_record_missing"} else "INVALID"
    if code in {"source_expired", "approval_expired"}:
        state = "STALE"
    elif code in {"source_revoked", "approval_revoked"}:
        state = "REVOKED"
    elif code == "approval_rejected":
        state = "REJECTED"
    elif code in {"approval_dependency_mismatch", "approval_scope_mismatch"}:
        state = "DEPENDENCY_MISMATCH"
    return {"component": component, "status": state, "revision": None,
            "reason": code, "owner": "human_reviewer" if human else "export_adapter",
            "responsibility": "human" if human else "system", "source_ref": ref}


def _approval(record, scope, revisions, now):
    if not isinstance(record, dict):
        raise SourceError("record_missing_or_invalid")
    _token(record.get("approval_id"))
    _token(record.get("actor_id"))
    _token(record.get("authority_record_ref"))
    if record.get("revoked") is not False:
        raise SourceError("approval_revoked" if record.get("revoked") is True else "approval_revocation_missing")
    decision = record.get("decision")
    if decision != "APPROVE":
        raise SourceError("approval_rejected" if decision == "REJECT" else "approval_decision_missing")
    if record.get("scope") != scope:
        raise SourceError("approval_scope_mismatch")
    approved = _time(record.get("approved_at"))
    expires = _time(record.get("expires_at"))
    if approved > now or expires <= approved:
        raise SourceError("approval_validity_invalid")
    if expires <= now:
        raise SourceError("approval_expired")
    expected = {name: revisions[name] for name in PREAPPROVAL_COMPONENTS if name in revisions}
    if len(expected) != len(PREAPPROVAL_COMPONENTS):
        raise SourceError("approval_dependencies_unavailable")
    if record.get("component_revisions") != expected:
        raise SourceError("approval_dependency_mismatch")


def _component(component, source, scope, revisions, attachment_root, now):
    if source is None:
        return _failure(component, "source_not_exported")
    if isinstance(source, dict) and "absence" in source:
        absence = source["absence"]
        if not isinstance(absence, dict):
            return _failure(component, "absence_descriptor_invalid", source)
        kind = absence.get("kind")
        # A human request requires an observed authoritative absence record, not a guess.
        if kind == "HUMAN_DECISION_REQUIRED" and component == "approval":
            try:
                _metadata(source, now)
                _token(absence.get("decision_request_ref"))
            except SourceError as error:
                return _failure(component, str(error), source)
            return _failure(component, "human_decision_required", source, human=True)
        if kind in {"SYSTEM_NOT_EXPORTED", "SOURCE_RECORD_MISSING"}:
            return _failure(component, "source_not_exported" if kind == "SYSTEM_NOT_EXPORTED"
                            else "source_record_missing", source)
        return _failure(component, "absence_descriptor_invalid", source)
    try:
        _metadata(source, now)
        material = _material_record(component, source.get("record"), scope, attachment_root)
        if component == "approval":
            _approval(material, scope, revisions, now)
            if _time(material["approved_at"]) > _time(source["observed_at"]):
                raise SourceError("approval_not_yet_observed")
        revision = revision_digest(scope, component, source, material)
    except SourceError as error:
        return _failure(component, str(error), source)
    return {"component": component, "status": "READY", "revision": revision,
            "source_ref": source["source_ref"], "source_version": source["source_version"],
            "observed_at": source["observed_at"], "expires_at": source["expires_at"],
            "source_authenticity_verified": False}


def export_revisions(snapshot, *, attachment_root, now):
    """Export seven revisions per role and deduplicate infrastructure blockers.

    No writes, network, model calls, new observations, approvals, or gate bypasses.
    `envelope_inputs_ready` means structurally complete inputs ONLY. The trusted
    host must authenticate their origins and enforce every existing hold.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be an explicit timezone-aware datetime")
    now = now.astimezone(timezone.utc)
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA:
        raise ValueError("normalized source schema is required")
    try:
        workspace = _token(snapshot.get("workspace_id"))
    except SourceError:
        raise ValueError("workspace_id is required") from None
    roles = snapshot.get("roles")
    if not isinstance(roles, list):
        raise ValueError("roles must be a list")
    snapshot_error = None
    try:
        _metadata(snapshot.get("snapshot"), now)
    except SourceError as error:
        snapshot_error = str(error)
    outputs, groups, identities = [], {}, set()
    for item in roles:
        if not isinstance(item, dict):
            raise ValueError("each role must be a mapping")
        try:
            scope = {"workspace_id": workspace, **{key: _token(item.get(key))
                     for key in ("role_id", "application_id", "action")}}
        except SourceError:
            raise ValueError("each role requires role_id, application_id and action") from None
        identity = (scope["role_id"], scope["application_id"], scope["action"])
        if identity in identities:
            raise ValueError("duplicate role/application/action scope")
        identities.add(identity)
        sources = item.get("sources")
        if sources is None:
            sources = {}
        if not isinstance(sources, dict):
            raise ValueError("sources must be an explicit component mapping")
        revisions, components = {}, {}
        for component in (*PREAPPROVAL_COMPONENTS, "approval"):
            result = _component(component, sources.get(component), scope, revisions,
                                attachment_root, now)
            components[component] = result
            if result["status"] == "READY":
                revisions[component] = result["revision"]
        issues = [value for value in components.values() if value["status"] != "READY"]
        if snapshot_error:
            issues.append(_failure("snapshot", snapshot_error, snapshot.get("snapshot")))
        for issue in issues:
            key = (issue["component"], issue["reason"], issue["source_ref"], issue["responsibility"])
            if key not in groups:
                groups[key] = {key_name: issue[key_name] for key_name in
                               ("component", "reason", "source_ref", "owner", "responsibility")}
                groups[key]["affected_roles"] = 0
                groups[key]["affected_scopes"] = 0
                groups[key]["_role_ids"] = set()
            groups[key]["_role_ids"].add(scope["role_id"])
            groups[key]["affected_roles"] = len(groups[key]["_role_ids"])
            groups[key]["affected_scopes"] += 1
        outputs.append({"scope": scope, "revisions": revisions, "components": components,
                        "envelope_inputs_ready": not issues, "execution_authorized": False,
                        "source_authenticity_verified": False})
    ready = sum(row["envelope_inputs_ready"] for row in outputs)
    for group in groups.values():
        group.pop("_role_ids")
    return {"schema": "keel.revision_export.v1", "roles": outputs,
            "root_causes": list(groups.values()), "role_count": len(outputs),
            "scope_count": len(outputs), "unique_role_count": len({row["scope"]["role_id"] for row in outputs}),
            "ready_role_count": ready, "blocked_role_count": len(outputs) - ready,
            "system_root_cause_count": sum(x["responsibility"] == "system" for x in groups.values()),
            "human_root_cause_count": sum(x["responsibility"] == "human" for x in groups.values()),
            "execution_authorized": False, "source_authenticity_verified": False}
