#!/usr/bin/env python3
"""Immutable human-review packets (Workstream F, Keel review lane).

Builds ONE compact immutable review packet from a validated six-family
canary export (sibling canary workstream, schema ``keel.canary.bundle/v1``).

A packet contains:
  - target identity: workspace_id / role_id / application_id / action
  - the six revision digests: policy, form, answers, attachments, target, route
  - the EXACT values being acted upon (the actual record payloads)
  - source provenance per family
  - unresolved warnings
  - the complete material digest (sha256 over the six ordered material digests)

Immutability: the packet carries ``packet_sha256``, a SHA-256 over the
canonical JSON of the packet contents. :func:`read_packet` recomputes it on
every read and raises :class:`PacketTampered` on ANY mismatch.

Status contract:
  - ``READY_FOR_REVIEW`` -- all six families present as authentic
    observations, digests verified, none expired or revoked.
  - ``BLOCKED-PENDING-EVIDENCE`` -- anything else. ``decision_request`` is
    null and :func:`issue_decision_request` REFUSES to issue a genuine
    approval request on incomplete evidence.

Stdlib only. No network, no model calls, no writes outside explicit paths.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PACKET_SCHEMA = "keel.review_packet.v1"
PACKET_VERSION = "keel-review-packet/1.0.0"

# Six pre-approval families (sibling canary capture FAMILIES). Approval is the
# human decision itself and is NEVER part of the evidence packet.
FAMILIES = ("policy", "form", "answers", "attachments", "target", "route")

STATUS_READY = "READY_FOR_REVIEW"
STATUS_BLOCKED = "BLOCKED-PENDING-EVIDENCE"

PROVENANCE_AUTHENTIC = "authentic_observation"
PROVENANCE_SYNTHETIC = "synthetic_fixture"
PROVENANCE_UNKNOWN = "unknown"

REVIEW_DIR = Path(__file__).resolve().parent
REQUESTS_FILE = REVIEW_DIR / "decision-requests.jsonl"


class PacketError(ValueError):
    """A packet contract violation. Never silently defaulted."""


class PacketTampered(PacketError):
    """The packet file no longer matches its embedded SHA-256."""


class NoApprovalRequest(PacketError):
    """Refusal: an approval request was attempted on incomplete evidence."""


# ---------------------------------------------------------------------------
# Canonical encoding / hashing
# ---------------------------------------------------------------------------

def _canonical(value) -> bytes:
    """Deterministic JSON encoding used for all digests."""
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise PacketError(f"value_not_json_serializable: {exc}") from exc
    return encoded


def sha256_hex(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value, *, field):
    if not isinstance(value, str) or not value:
        raise PacketError(f"timestamp_invalid: {field}")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PacketError(f"timestamp_invalid: {field}") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise PacketError(f"timestamp_invalid: {field}")
    return result.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Defensive record normalization
# ---------------------------------------------------------------------------
# Accepts the sibling canary per-record schema
#   (keel.canary.bundle/v1 records: workspace_id, role_id, application_id,
#    action, component, source_ref, source_version, observed_at, provenance,
#    expires_at, revoked, revocation_ref, material_digest, capture_version,
#    payload)
# and defensively maps near-equivalent shapes without coercing lookalikes
# (booleans, URLs, free text) into evidence.

_FIELD_ALIASES = {
    "source_ref": ("source_ref",),
    "source_version": ("source_version", "capture_version", "record_version",
                       "generation", "version"),
    "observed_at": ("observed_at",),
    "expires_at": ("expires_at", "expiration"),
    "revoked": ("revoked", "revocation"),
    "revocation_ref": ("revocation_ref",),
    "material_digest": ("material_digest", "revision", "digest"),
    "payload": ("payload", "record", "contents", "material"),
    "provenance": ("provenance",),
    "provenance_reason": ("provenance_reason", "reason"),
}


def _pick(raw: dict, logical: str):
    for alias in _FIELD_ALIASES[logical]:
        if alias in raw:
            return raw[alias]
    return None


def normalize_record(raw: dict) -> dict:
    """Normalize one canary record defensively.

    Raises PacketError on unknown component or missing identity; returns the
    normalized record with explicit None for absent optional fields.
    """
    if not isinstance(raw, dict):
        raise PacketError("record_not_a_mapping")
    component = raw.get("component")
    if component not in FAMILIES:
        raise PacketError(f"record_unknown_family: {component!r}")
    for key in ("workspace_id", "role_id", "application_id", "action"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PacketError(f"record_missing_identity: {key}")
    observed_at = _pick(raw, "observed_at")
    if not isinstance(observed_at, str) or not observed_at:
        raise PacketError("record_missing_observed_at")
    _parse_time(observed_at, field="observed_at")
    expires_at = _pick(raw, "expires_at")
    if expires_at is not None:
        _parse_time(expires_at, field="expires_at")
    revoked = _pick(raw, "revoked")
    revoked = bool(revoked) if revoked is not None else False
    return {
        "component": component,
        "workspace_id": raw["workspace_id"],
        "role_id": raw["role_id"],
        "application_id": raw["application_id"],
        "action": raw["action"],
        "source_ref": _pick(raw, "source_ref"),
        "source_version": _pick(raw, "source_version"),
        "observed_at": observed_at,
        "expires_at": expires_at,
        "revoked": revoked,
        "revocation_ref": _pick(raw, "revocation_ref"),
        "material_digest": _pick(raw, "material_digest"),
        "provenance": _pick(raw, "provenance"),
        "provenance_reason": _pick(raw, "provenance_reason"),
        "payload": _pick(raw, "payload"),
    }


def verify_material_digest(record: dict) -> bool:
    """Recompute sha256 over the canonical payload; compare to material_digest.

    A digest mismatch means the source record was altered after capture.
    UNKNOWN-provenance markers carry payload None; their digest is defined
    over None and still verifiable.
    """
    expected = record.get("material_digest")
    if not isinstance(expected, str) or not expected:
        return False
    return sha256_hex(record.get("payload")) == expected


# ---------------------------------------------------------------------------
# Packet build
# ---------------------------------------------------------------------------

def _family_assessment(record: dict | None, now) -> dict:
    """Assess one family. Anything short of a verified authentic observation
    is a blocker -- never evidence."""
    if record is None:
        return {"state": "MISSING", "warning": "family_not_captured"}
    provenance = record.get("provenance")
    if provenance == PROVENANCE_UNKNOWN:
        return {"state": "UNKNOWN",
                "warning": record.get("provenance_reason")
                           or "family_marked_unknown_by_capture"}
    if provenance == PROVENANCE_SYNTHETIC:
        return {"state": "SYNTHETIC",
                "warning": "synthetic_fixture_is_not_evidence"}
    if provenance != PROVENANCE_AUTHENTIC:
        return {"state": "INVALID",
                "warning": f"unrecognized_provenance: {provenance!r}"}
    if record.get("revoked"):
        return {"state": "REVOKED",
                "warning": record.get("revocation_ref") or "record_revoked"}
    expires_at = record.get("expires_at")
    if expires_at is not None and _parse_time(expires_at, field="expires_at") <= now:
        return {"state": "EXPIRED", "warning": "record_expired"}
    if not verify_material_digest(record):
        return {"state": "DIGEST_MISMATCH",
                "warning": "material_digest_does_not_match_payload"}
    return {"state": "PRESENT", "warning": None}


def build_packet(packet_id: str, records, *, now: datetime | None = None) -> dict:
    """Build ONE immutable review packet from canary records.

    ``records``: iterable of raw canary record mappings (may be empty).
    Returns the packet dict INCLUDING ``packet_sha256``. The caller writes it
    with :func:`write_packet`.
    """
    if not isinstance(packet_id, str) or not packet_id.strip():
        raise PacketError("packet_id_required")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    normalized = [normalize_record(raw) for raw in records]
    by_family: dict[str, dict] = {}
    for record in normalized:
        component = record["component"]
        if component in by_family:
            raise PacketError(f"duplicate_family_record: {component}")
        by_family[component] = record

    scopes = {(r["workspace_id"], r["role_id"], r["application_id"], r["action"])
              for r in normalized}
    if len(scopes) > 1:
        raise PacketError("scope_mismatch_across_records")
    scope = None
    if scopes:
        workspace_id, role_id, application_id, action = next(iter(scopes))
        scope = {"workspace_id": workspace_id, "role_id": role_id,
                 "application_id": application_id, "action": action}

    families: dict[str, dict] = {}
    unresolved_warnings: list[dict] = []
    for family in FAMILIES:
        record = by_family.get(family)
        assessment = _family_assessment(record, now)
        entry = {
            "component": family,
            "state": assessment["state"],
            "revision_digest": (record["material_digest"]
                                if record and assessment["state"] == "PRESENT"
                                else None),
            "source_ref": record["source_ref"] if record else None,
            "source_version": record["source_version"] if record else None,
            "observed_at": record["observed_at"] if record else None,
            "expires_at": record["expires_at"] if record else None,
            "provenance": record["provenance"] if record else None,
        }
        families[family] = entry
        if assessment["warning"] is not None:
            unresolved_warnings.append({"component": family,
                                        "state": assessment["state"],
                                        "warning": assessment["warning"]})

    # Exact values being acted upon: the actual payloads of PRESENT families.
    exact_values = {family: by_family[family]["payload"]
                    for family in FAMILIES
                    if families[family]["state"] == "PRESENT"}

    # Complete material digest: sha256 over the six ordered material digests
    # (None for absent families -- absence is part of the digest input, not
    # silently dropped).
    material_digest = sha256_hex(
        [by_family[f]["material_digest"] if f in by_family else None
         for f in FAMILIES])

    if scope is None:
        unresolved_warnings.insert(0, {"component": None, "state": "UNKNOWN",
                                       "warning": "scope_unknown_no_records"})
    status = (STATUS_READY if not unresolved_warnings else STATUS_BLOCKED)

    body = {
        "schema": PACKET_SCHEMA,
        "packet_version": PACKET_VERSION,
        "packet_id": packet_id,
        "built_at": _iso_now(),
        "scope": scope,
        "status": status,
        "revisions": {f: families[f]["revision_digest"] for f in FAMILIES},
        "exact_values": exact_values,
        "provenance": {f: {"source_ref": families[f]["source_ref"],
                           "source_version": families[f]["source_version"],
                           "observed_at": families[f]["observed_at"],
                           "provenance": families[f]["provenance"]}
                       for f in FAMILIES},
        "unresolved_warnings": unresolved_warnings,
        "material_digest": material_digest,
        # A genuine approval request is attached ONLY via issue_decision_request,
        # which refuses blocked packets. Never pre-populated here.
        "decision_request": None,
    }
    packet = {**body, "packet_sha256": sha256_hex(body)}
    return packet


def write_packet(packet: dict, path: str | Path) -> Path:
    """Write a built packet to disk (pretty, canonical key order)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(packet, sort_keys=True, indent=2,
                              ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def read_packet(path: str | Path) -> dict:
    """Read a packet and verify immutability. Raises PacketTampered."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "packet_sha256" not in raw:
        raise PacketError("not_a_review_packet")
    embedded = raw["packet_sha256"]
    body = {key: value for key, value in raw.items() if key != "packet_sha256"}
    if sha256_hex(body) != embedded:
        raise PacketTampered(
            f"packet_sha256 mismatch for {raw.get('packet_id')}: "
            "contents were altered after build")
    return raw


# ---------------------------------------------------------------------------
# Decision-request issuance (APPROVE / REJECT exposure)
# ---------------------------------------------------------------------------
# The packet EXPOSES approve/reject by carrying a decision request. The
# request is issued only on a READY packet; the genuine decision itself is
# recorded in review.decision and bound to the packet's SHA-256.

def issue_decision_request(packet: dict, *, now: datetime | None = None,
                           requests_file: str | Path | None = None) -> dict:
    """Issue a decision request for a READY packet.

    Raises NoApprovalRequest if the packet is not READY_FOR_REVIEW -- a
    genuine approval request is NEVER issued on incomplete evidence.
    """
    if packet.get("status") != STATUS_READY:
        raise NoApprovalRequest(
            "refusing_to_issue_approval_request: packet status is "
            f"{packet.get('status')}; complete evidence required first")
    if not packet.get("packet_sha256"):
        raise PacketError("packet_not_sealed")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    request = {
        "schema": "keel.decision_request.v1",
        "request_id": f"keel-decision-request-{uuid.uuid4().hex[:12]}",
        "packet_id": packet["packet_id"],
        "packet_sha256": packet["packet_sha256"],
        "scope": packet.get("scope"),
        "issued_at": now.isoformat(),
        # A request lapses if Trent does not decide; a lapsed request can
        # never carry a decision. (30 days; configurable per request.)
        "expires_at": (now + timedelta(days=30)).isoformat(),
        "status": "OPEN",
    }
    target = Path(requests_file) if requests_file else REQUESTS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(request, sort_keys=True) + "\n")
    return request


def load_requests(requests_file: str | Path | None = None) -> list[dict]:
    target = Path(requests_file) if requests_file else REQUESTS_FILE
    if not target.exists():
        return []
    requests = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            requests.append(json.loads(line))
    return requests
