"""Counsel packet generation for the Privacy Release Controller.

prepare_packet(...) -> dict builds the machine-readable review packet a
separately-authorized human reviewer (privacy counsel) needs to grant or deny
G1. Every required field must be present and typed; non-applicable sections
must say so explicitly ({"not_applicable": True, "reason": ...}) rather than
being omitted or nulled — absence is not evidence.

The packet NEVER contains a decision. It is review input only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

CONTROLLER_VERSION = "1.0.0"

# The exact required field set. PACKET_FIELDS is exported so the coordinator
# and tests can assert completeness programmatically.
PACKET_FIELDS = [
    "packet_version",
    "generated_at",
    "generated_by",
    "release_id",                        # immutable release ID
    "manifest_digest",
    "artifact_sha256",                   # digest per artifact_id
    "artifact_inventory",                # exact artifact inventory
    "intended_destination",
    "purpose",
    "personal_data_inventory",
    "sensitive_data_inventory",
    "secrets_scan",
    "derivative_data_inventory",
    "data_lineage",
    "telemetry_schema",
    "aggregation_methodology",
    "contribution_bounds",
    "differencing_reidentification_analysis",
    "retention_behavior",
    "deletion_behavior",
    "third_parties",
    "external_processors",
    "known_risks",
    "unresolved_questions",
    "security_controls",
    "proposed_public_claims",             # exact proposed public claims
    "packet_digest",
]

_LIST_FIELDS = {
    "artifact_inventory", "personal_data_inventory", "sensitive_data_inventory",
    "derivative_data_inventory", "third_parties", "external_processors",
    "known_risks", "unresolved_questions", "security_controls",
    "proposed_public_claims",
}
_DICT_FIELDS = {
    "artifact_sha256", "secrets_scan", "data_lineage", "telemetry_schema",
    "aggregation_methodology", "contribution_bounds",
    "differencing_reidentification_analysis", "retention_behavior",
    "deletion_behavior",
}
_STR_FIELDS = {"release_id", "manifest_digest", "intended_destination", "purpose"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _check_not_applicable(value: dict, field_name: str) -> None:
    """telemetry_schema / aggregation_methodology may be explicitly N/A."""
    if value.get("not_applicable") is True:
        if not value.get("reason"):
            raise ValueError(
                f"{field_name}: not_applicable=True requires a 'reason'")
        return
    # otherwise it is a real schema/methodology dict — no further structural
    # demands here; counsel judges sufficiency.


def prepare_packet(
    release_id: str,
    manifest_digest: str,
    artifact_inventory: list[dict],
    artifact_sha256: dict[str, str],
    intended_destination: str,
    purpose: str,
    personal_data_inventory: list[dict],
    sensitive_data_inventory: list[dict],
    secrets_scan: dict,
    derivative_data_inventory: list[dict],
    data_lineage: dict,
    telemetry_schema: dict,
    aggregation_methodology: dict,
    contribution_bounds: dict,
    differencing_reidentification_analysis: dict,
    retention_behavior: dict,
    deletion_behavior: dict,
    third_parties: list[dict],
    external_processors: list[dict],
    known_risks: list[dict],
    unresolved_questions: list[str],
    security_controls: list[dict],
    proposed_public_claims: list[str],
) -> dict:
    """Build and validate the counsel review packet. Returns the packet dict.

    Raises ValueError listing every defect when the packet is incomplete —
    a partial packet is never returned, because counsel must never review
    a packet that silently omits a risk surface.
    """
    supplied: dict[str, object] = {
        "release_id": release_id,
        "manifest_digest": manifest_digest,
        "artifact_inventory": artifact_inventory,
        "artifact_sha256": artifact_sha256,
        "intended_destination": intended_destination,
        "purpose": purpose,
        "personal_data_inventory": personal_data_inventory,
        "sensitive_data_inventory": sensitive_data_inventory,
        "secrets_scan": secrets_scan,
        "derivative_data_inventory": derivative_data_inventory,
        "data_lineage": data_lineage,
        "telemetry_schema": telemetry_schema,
        "aggregation_methodology": aggregation_methodology,
        "contribution_bounds": contribution_bounds,
        "differencing_reidentification_analysis": differencing_reidentification_analysis,
        "retention_behavior": retention_behavior,
        "deletion_behavior": deletion_behavior,
        "third_parties": third_parties,
        "external_processors": external_processors,
        "known_risks": known_risks,
        "unresolved_questions": unresolved_questions,
        "security_controls": security_controls,
        "proposed_public_claims": proposed_public_claims,
    }

    errors: list[str] = []
    for name in _STR_FIELDS:
        v = supplied[name]
        if not isinstance(v, str) or not v.strip():
            errors.append(f"{name}: required non-empty string")
    for name in _LIST_FIELDS:
        v = supplied[name]
        if not isinstance(v, list):
            errors.append(f"{name}: required list (may be empty, never null)")
    for name in _DICT_FIELDS:
        v = supplied[name]
        if not isinstance(v, dict):
            errors.append(f"{name}: required dict (may be empty/explicit-N/A, never null)")

    # Cross-consistency: every inventoried artifact needs a digest entry,
    # and every digest entry must correspond to an inventoried artifact.
    if isinstance(artifact_inventory, list) and isinstance(artifact_sha256, dict):
        inv_ids = {a.get("artifact_id") for a in artifact_inventory
                   if isinstance(a, dict)}
        digest_ids = set(artifact_sha256.keys())
        missing = inv_ids - digest_ids
        extra = digest_ids - inv_ids
        if missing:
            errors.append(f"artifact_sha256 missing digests for: {sorted(missing)}")
        if extra:
            errors.append(f"artifact_sha256 has digests for unknown artifacts: "
                          f"{sorted(extra)}")
        for aid, digest in artifact_sha256.items():
            if not (isinstance(digest, str) and len(digest) == 64
                    and all(c in "0123456789abcdef" for c in digest)):
                errors.append(f"artifact_sha256[{aid}]: not a 64-char hex digest")

    # Explicit-N/A discipline for the two conditionally-applicable sections.
    if isinstance(telemetry_schema, dict):
        try:
            _check_not_applicable(telemetry_schema, "telemetry_schema")
        except ValueError as e:
            errors.append(str(e))
    if isinstance(aggregation_methodology, dict):
        try:
            _check_not_applicable(aggregation_methodology, "aggregation_methodology")
        except ValueError as e:
            errors.append(str(e))

    # Proposed public claims must be exact quoted strings, non-empty.
    if isinstance(proposed_public_claims, list):
        for i, claim in enumerate(proposed_public_claims):
            if not isinstance(claim, str) or not claim.strip():
                errors.append(f"proposed_public_claims[{i}]: must be a non-empty "
                              f"exact claim string")

    if errors:
        raise ValueError("counsel packet incomplete — refusing to emit partial "
                         "packet:\n- " + "\n- ".join(errors))

    packet: dict[str, object] = {
        "packet_version": 1,
        "generated_at": _utcnow_iso(),
        "generated_by": f"keel-privacy-controller/{CONTROLLER_VERSION}",
        **supplied,  # type: ignore[arg-type]
    }
    # packet_digest covers everything except itself (tamper evidence).
    packet["packet_digest"] = hashlib.sha256(
        _canonical({k: v for k, v in packet.items() if k != "packet_digest"}
                   ).encode("utf-8")).hexdigest()

    # Final structural guarantee: the emitted packet carries exactly the
    # required field set, no more, no less.
    assert set(packet.keys()) == set(PACKET_FIELDS), (
        f"packet field drift: {set(packet.keys()) ^ set(PACKET_FIELDS)}")
    return packet


def verify_packet_digest(packet: dict) -> tuple[bool, str]:
    """Recompute the packet digest; return (ok, reason)."""
    expected = packet.get("packet_digest")
    recomputed = hashlib.sha256(
        _canonical({k: v for k, v in packet.items() if k != "packet_digest"}
                   ).encode("utf-8")).hexdigest()
    if recomputed != expected:
        return False, "packet digest mismatch: packet altered after generation"
    missing = [f for f in PACKET_FIELDS if f not in packet]
    if missing:
        return False, f"packet missing required fields: {missing}"
    return True, "packet intact and complete"


def save_packet(packet: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(packet, f, indent=2, sort_keys=True)
        f.write("\n")
