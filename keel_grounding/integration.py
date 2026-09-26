"""Add actual packet-content observations to the unchanged capture/review gates.

The evidence graph comes only from the supplied Workbench's canonical trust
export. A successful file check cannot replace a failed prior gate, establish
truth or authenticate a review. This report never grants dispatch authority.
"""
from __future__ import annotations

import re

from keel_flow.common import clock
from keel_live.proof import build_proof
from keel_local.readiness import dependency_hash
from keel_trust.common import canonical, digest, strict_json

from .evidence import GroundingError
from .packet import verify_packet


SCHEMA = "keel.grounding.capture_content_proof.v1"


def _snapshot(value):
    try:
        return strict_json(canonical(value))
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise GroundingError("invalid_grounded_proof_input") from None


def build_grounded_proof(workbench, bindings, packet, *, expected_packet_sha256,
                         workspace_id, synthetic, action, evidence_root, packet_root,
                         attachment_root=None, now):
    """Recheck existing gates and actual files against one captured input set.

    ``expected_packet_sha256`` must be supplied from the host's authenticated
    review context. This function does not authenticate its issuer. The top
    level ``packet_content_verified`` describes the declared packet's observed
    files; each role additionally requires its exact canonical artifact binding.
    ``CONTENT_REVIEW_CHECKS_PASSED`` also requires every existing capture/review
    gate to pass. A prior report cannot be supplied as authority.

    Malformed Workbench contracts retain the existing validator's rejection.
    An absent trust export (``trust: null``) is a normal named blocker. File or
    grounding failures become named blockers without changing the base proof.
    """
    workbench, bindings, packet = (_snapshot(value) for value in (workbench, bindings, packet))
    now = clock(now)
    if (type(expected_packet_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_packet_sha256) is None):
        raise GroundingError("invalid_expected_packet_sha256")
    base = build_proof(workbench, workspace_id=workspace_id, synthetic=synthetic,
                       action=action, attachment_root=attachment_root, now=now)

    trust = workbench.get("trust")
    document = trust.get("evidence_export") if type(trust) is dict else None
    common_reasons = []
    packet_report = None
    if type(document) is not dict:
        common_reasons.append("TRUST_EVIDENCE_MISSING")
    else:
        try:
            packet_report = verify_packet(document, bindings, packet,
                evidence_root=evidence_root, packet_root=packet_root, now=now,
                expected_packet_sha256=expected_packet_sha256)
        except GroundingError as exc:
            common_reasons.append("PACKET_VERIFICATION_FAILED:" + exc.code)
        if packet_report is not None and packet_report["status"] != "VERIFIED":
            common_reasons.extend(packet_report["reasons"])
            common_reasons.append("PACKET_CONTENT_UNVERIFIED")

    packet_verified = bool(packet_report is not None
        and packet_report["status"] == "VERIFIED"
        and packet_report["packet_content_verified"] is True)
    selected = {row["artifact_id"]: row for row in packet_report["artifacts"]} if packet_report else {}
    artifacts = {row["artifact_id"]: row for row in document["artifacts"]} if document else {}
    canonical_bindings = {row["role_id"]: row for row in trust["artifact_bindings"]} if trust else {}
    leads = {row["role_id"]: row for row in workbench["flow"]["leads"]}
    roles = []
    for old in base["roles"]:
        rid = old["scope"]["role_id"]
        lead = leads[rid]
        binding = canonical_bindings.get(rid)
        binding_reasons = []
        if binding is None:
            binding_reasons.append("CANONICAL_ARTIFACT_BINDING_MISSING")
        else:
            aid = binding["artifact_id"]
            artifact = artifacts.get(aid)
            observed = selected.get(aid)
            if artifact is None:
                binding_reasons.append("CANONICAL_ARTIFACT_UNKNOWN")
            else:
                if binding["artifact_revision"] != artifact["revision"]:
                    binding_reasons.append("CANONICAL_ARTIFACT_REVISION_MISMATCH")
                if binding["artifact_sha256"] != digest(artifact):
                    binding_reasons.append("CANONICAL_ARTIFACT_DIGEST_MISMATCH")
            if observed is None:
                binding_reasons.append("ROLE_ARTIFACT_NOT_IN_PACKET")
            elif (observed["status"] != "VERIFIED"
                    or observed["revision"] != binding["artifact_revision"]):
                binding_reasons.append("ROLE_ARTIFACT_CONTENT_UNVERIFIED")
            try:
                dependencies_match = (binding["packet_dependency_hash"] == lead["packet_dependency_hash"]
                                      == dependency_hash(lead["dependencies"]))
            except (ValueError, TypeError, KeyError):
                dependencies_match = False
            if not dependencies_match:
                binding_reasons.append("ROLE_PACKET_DEPENDENCY_MISMATCH")
        artifact_bound = not binding_reasons
        content_verified = packet_verified and artifact_bound
        capture_passed = old["capture_review_checks_passed"] is True
        qualified = capture_passed and content_verified
        reasons = list(old["reasons"]) + common_reasons + binding_reasons
        if not capture_passed:
            reasons.append("BASE_CAPTURE_REVIEW_BLOCKED")
        roles.append({"scope": _snapshot(old["scope"]),
            "state": "CONTENT_REVIEW_CHECKS_PASSED" if qualified else "BLOCKED",
            "capture_review_checks_passed": capture_passed,
            "canonical_artifact_bound": artifact_bound,
            "packet_content_verified": content_verified,
            "content_review_checks_passed": qualified,
            "artifact_id": binding["artifact_id"] if binding is not None else None,
            "artifact_revision": binding["artifact_revision"] if binding is not None else None,
            "reasons": sorted(set(reasons)),
            "truth_independently_verified": False,
            "source_authenticity_verified": False,
            "reviewer_authentication_verified": False,
            "execution_authorized": False})

    passed = sum(row["content_review_checks_passed"] for row in roles)
    reasons = list(common_reasons)
    if not passed:
        reasons.append("NO_ROLE_CONTENT_REVIEW_PASSED")
    return {"schema": SCHEMA, "schema_version": 1, "workspace_id": workspace_id,
        "synthetic": synthetic, "action": action, "evaluated_at": now.isoformat(),
        "snapshot_sha256": digest(workbench),
        "trust_snapshot_sha256": digest(document) if document is not None else None,
        "expected_packet_sha256": expected_packet_sha256,
        "state": "CONTENT_REVIEW_CHECKS_PASSED" if passed else "BLOCKED",
        "packet_content_verified": packet_verified,
        "counts": {"roles": len(roles),
            "packet_content_verified": sum(row["packet_content_verified"] for row in roles),
            "content_review_checks_passed": passed,
            "synthetic_content_review_checks_passed": passed if synthetic else 0,
            "declared_operational_content_review_checks_passed": 0 if synthetic else passed},
        "roles": roles, "reasons": sorted(set(reasons)),
        "base_proof": base, "packet_report": packet_report,
        "truth_independently_verified": False,
        "source_authenticity_verified": False,
        "operator_authenticity_verified": False,
        "reviewer_authentication_verified": False,
        "factual_support_verified": False,
        "binary_semantic_content_verified": False,
        "field_bindings_approval_verified": False,
        "runtime_checks": {"local_model": "NOT_RUN", "rendered_browser": "NOT_RUN",
            "rendered_preparation": "NOT_RUN", "form_readback": "NOT_RUN"},
        "effects": {"canonical_writes": 0, "source_writes": 0, "model_calls": 0,
            "outbound_requests": 0, "browser_actions": 0, "messages_sent": 0},
        "execution_authorized": False,
        "boundary": "Current local file observations composed with existing gates; source truth, "
                    "reviewer identity, binary semantics and rendered form values remain unverified; "
                    "not a dispatch token or a replacement for rechecking before use."}
