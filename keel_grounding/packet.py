"""Verify actual packet bytes against the grounded, approved wording graph.

The supported renderers deliberately cover every output byte or JSON field.
Attachments receive an identity check only; no binary semantic verification,
reviewer authentication, consent, or execution authorization is inferred.
"""
from __future__ import annotations

import re

from keel_trust.common import canonical, digest, strict_json

from .claims import verify_grounding
from .evidence import GroundingError, read_verified_file


MAX_FILES = 256
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def _require(condition, code):
    if not condition:
        raise GroundingError(code.lower())


def _keys(value, expected):
    _require(type(value) is dict and set(value) == expected, "PACKET_SCHEMA_INVALID")


def _text(value):
    _require(type(value) is str and 0 < len(value) <= 4096
             and bool(value.strip()) and not any(ord(c) < 32 for c in value),
             "PACKET_TEXT_INVALID")
    return value


def _hash(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
             "PACKET_HASH_INVALID")
    return value


def _path(value):
    _text(value)
    _require(not value.startswith("/") and "\\" not in value
             and ":" not in value and all(part not in {"", ".", ".."}
                                         for part in value.split("/")),
             "PACKET_PATH_INVALID")
    return value


def _snapshot(value):
    try:
        return strict_json(canonical(value))
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise GroundingError("packet_json_invalid") from None


def _rows(value, *, nonempty=False):
    _require(type(value) is list and len(value) <= MAX_FILES
             and (bool(value) or not nonempty), "PACKET_FILE_LIST_INVALID")
    return value


def _validate(packet):
    _keys(packet, {"schema", "trust_snapshot_sha256", "workspace_id", "subject_id",
                   "scope", "artifacts", "attachments"})
    _require(packet["schema"] == "keel.grounding.packet.v1", "PACKET_SCHEMA_INVALID")
    _hash(packet["trust_snapshot_sha256"])
    for field in ("workspace_id", "subject_id", "scope"):
        _text(packet[field])
    artifacts = _rows(packet["artifacts"], nonempty=True)
    attachments = _rows(packet["attachments"])
    _require(len(artifacts) + len(attachments) <= MAX_FILES, "PACKET_FILE_LIMIT")
    paths, artifact_ids, attachment_ids = set(), set(), set()
    for row in artifacts:
        _keys(row, {"artifact_id", "revision", "path", "sha256", "format", "fields"})
        aid = _text(row["artifact_id"])
        _require(aid not in artifact_ids, "PACKET_DUPLICATE_ARTIFACT")
        artifact_ids.add(aid)
        _text(row["revision"])
        _hash(row["sha256"])
        path = _path(row["path"])
        _require(path not in paths, "PACKET_DUPLICATE_PATH")
        paths.add(path)
        _require(row["format"] in ("text_lines", "json_fields"), "PACKET_FORMAT_UNSUPPORTED")
        _require(type(row["fields"]) is list and len(row["fields"]) <= 1000,
                 "PACKET_FIELDS_INVALID")
        if row["format"] == "text_lines":
            _require(row["fields"] == [], "PACKET_FIELDS_INVALID")
        seen_fields, seen_indices = set(), set()
        for field in row["fields"]:
            _keys(field, {"field_id", "statement_index"})
            fid, index = _text(field["field_id"]), field["statement_index"]
            _require(type(index) is int and 0 <= index < 1000, "PACKET_FIELDS_INVALID")
            _require(fid not in seen_fields and index not in seen_indices,
                     "PACKET_DUPLICATE_FIELD_BINDING")
            seen_fields.add(fid)
            seen_indices.add(index)
    for row in attachments:
        _keys(row, {"attachment_id", "path", "sha256"})
        attachment_id = _text(row["attachment_id"])
        _require(attachment_id not in attachment_ids, "PACKET_DUPLICATE_ATTACHMENT")
        attachment_ids.add(attachment_id)
        _hash(row["sha256"])
        path = _path(row["path"])
        _require(path not in paths, "PACKET_DUPLICATE_PATH")
        paths.add(path)


def verify_packet(document, bindings, packet, *, evidence_root, packet_root, now,
                  expected_packet_sha256):
    """Read and verify a complete declared packet, with no writes or execution.

    Malformed contracts raise ``GroundingError``. Unavailable or changed files
    and valid but incompatible metadata produce a ``BLOCKED`` report. Grounding
    is freshly evaluated here; a caller-supplied successful report is never used.
    ``VERIFIED`` describes this local observation, not a reusable dispatch token.
    The expected manifest hash must come from the host's existing authenticated
    review context, independently of this untrusted packet declaration. Matching
    that binding does not authenticate its issuer or authorize any execution.
    """
    document = _snapshot(document)
    bindings = _snapshot(bindings)
    grounding = verify_grounding(document, bindings, root=evidence_root, now=now)
    packet = _snapshot(packet)
    _validate(packet)
    _hash(expected_packet_sha256)
    manifest_sha256 = digest(packet)
    manifest_matches = expected_packet_sha256 == manifest_sha256

    reasons = []
    if not manifest_matches:
        reasons.append("PACKET_MANIFEST_CHANGED")
    if grounding["status"] != "VERIFIED":
        reasons.append("GROUNDING_BLOCKED")
    if packet["trust_snapshot_sha256"] != grounding["trust_snapshot_sha256"]:
        reasons.append("TRUST_SNAPSHOT_CHANGED")
    if packet["workspace_id"] != document["workspace_id"]:
        reasons.append("WORKSPACE_MISMATCH")
    artifacts = {row["artifact_id"]: row for row in document["artifacts"]}
    claims = {row["claim_id"]: row for row in document["claims"]}
    verified_artifacts = {row["artifact_id"]: row for row in grounding["artifacts"]}
    selected = {row["artifact_id"] for row in packet["artifacts"]}
    artifact_rows, attachment_rows = [], []
    observed_bytes = 0

    for row in packet["artifacts"]:
        aid, failures = row["artifact_id"], []
        approved = artifacts.get(aid)
        if approved is None:
            failures.append("ARTIFACT_UNKNOWN")
        else:
            current = verified_artifacts.get(aid)
            if current is None or current["status"] != "VERIFIED":
                failures.append("ARTIFACT_GROUNDING_BLOCKED")
            if approved["revision"] != row["revision"]:
                failures.append("ARTIFACT_REVISION_CHANGED")
            if approved["scope"] != packet["scope"]:
                failures.append("ARTIFACT_SCOPE_MISMATCH")
            if not set(approved["depends_on"]) <= selected:
                failures.append("ARTIFACT_DEPENDENCY_MISSING")
            if any(claims[item["claim_id"]]["subject_id"] != packet["subject_id"]
                   for item in approved["statements"]):
                failures.append("CLAIM_SUBJECT_MISMATCH")

        raw = None
        if observed_bytes >= MAX_TOTAL_BYTES:
            failures.append("PACKET_BYTE_LIMIT")
        else:
            try:
                raw = read_verified_file(packet_root, row["path"], row["sha256"],
                                         max_bytes=min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - observed_bytes))
                observed_bytes += len(raw)
            except (GroundingError, OSError):
                failures.append("ARTIFACT_FILE_UNVERIFIED")

        if approved is not None and raw is not None:
            statements = approved["statements"]
            if row["format"] == "text_lines":
                expected = (("\n".join(item["wording"] for item in statements) + "\n")
                            if statements else "").encode("utf-8")
                if raw != expected:
                    failures.append("ARTIFACT_CONTENT_MISMATCH")
            else:
                if {field["statement_index"] for field in row["fields"]} != set(range(len(statements))):
                    failures.append("ARTIFACT_STATEMENT_COVERAGE_MISMATCH")
                else:
                    expected = {field["field_id"]: statements[field["statement_index"]]["wording"]
                                for field in row["fields"]}
                    try:
                        # Decode UTF-8 explicitly: json.loads(bytes) also accepts
                        # UTF-16/32, which are outside this packet format.
                        actual = strict_json(raw.decode("utf-8"))
                        equal = type(actual) is dict and canonical(actual) == canonical(expected)
                    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
                        equal = False
                    if not equal:
                        failures.append("ARTIFACT_CONTENT_MISMATCH")
        artifact_rows.append({"artifact_id": aid, "revision": row["revision"],
                              "status": "BLOCKED" if failures else "VERIFIED",
                              "reasons": sorted(set(failures)),
                              "content_matches_approved_wording": not failures})

    for row in packet["attachments"]:
        failures = []
        if observed_bytes >= MAX_TOTAL_BYTES:
            failures.append("PACKET_BYTE_LIMIT")
        else:
            try:
                raw = read_verified_file(packet_root, row["path"], row["sha256"],
                                         max_bytes=min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - observed_bytes))
                observed_bytes += len(raw)
            except (GroundingError, OSError):
                failures.append("ATTACHMENT_FILE_UNVERIFIED")
        attachment_rows.append({"attachment_id": row["attachment_id"],
                                "status": "BLOCKED" if failures else "IDENTITY_VERIFIED",
                                "reasons": failures, "identity_verified": not failures,
                                "semantic_content_verified": False})

    if any(row["status"] == "BLOCKED" for row in artifact_rows):
        reasons.append("PACKET_ARTIFACT_BLOCKED")
    if any(row["status"] == "BLOCKED" for row in attachment_rows):
        reasons.append("PACKET_ATTACHMENT_BLOCKED")
    return {"schema": "keel.grounding.packet-report.v1",
            "status": "BLOCKED" if reasons else "VERIFIED",
            "packet_manifest_sha256": manifest_sha256,
            "declared_manifest_binding_verified": manifest_matches,
            "trust_snapshot_sha256": grounding["trust_snapshot_sha256"],
            "workspace_id": packet["workspace_id"], "subject_id": packet["subject_id"],
            "scope": packet["scope"], "reasons": sorted(set(reasons)),
            "grounding_status": grounding["status"],
            "artifacts": artifact_rows, "attachments": attachment_rows,
            "packet_content_verified": not reasons,
            "binary_semantic_content_verified": False,
            "field_bindings_approval_verified": False,
            "truth_independently_verified": False, "approver_authenticated": False,
            "reviewer_authentication_verified": False,
            "execution_authorized": False, "queue_writes": 0,
            "boundary": "Observation of declared local files only; exact approved wording coverage; "
                        "attachments have identity checks only; no dispatch authority."}
