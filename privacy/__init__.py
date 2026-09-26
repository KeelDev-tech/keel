"""Keel Privacy Release Controller (G1).

Deterministic, stdlib-only enforcement layer for the ABSOLUTE privacy-counsel
gate: nothing leaves the private workspace for publication/offering/pricing/
sale without a verified, separately-authorized counsel decision.

Hard rule baked into every module: NO LLM, security agent, orchestrator, or
deployment agent may grant G1. This package may only
  - prepare the review (counsel_packet),
  - identify risks (classifier, derivative_analysis, lineage),
  - verify a returned authorization (counsel_decision.verify_decision),
  - enforce it (publication_guard).
It can NEVER synthesize privacy_counsel_approved=True.

Coordinator contract:
    from keel.privacy.counsel_decision import verify_decision
    from keel.privacy.counsel_packet import prepare_packet

    verify_decision(release_id: str, artifact_digest: str) -> bool
    prepare_packet(...) -> dict   # machine-readable counsel review packet

Import note: keel/ has no __init__.py, so import as a namespace package with
the workspace root on sys.path:
    import sys, os
    sys.path.insert(0, os.path.expanduser("~/workspace"))
    from keel.privacy import verify_decision, prepare_packet
"""

from keel.privacy.counsel_decision import (
    verify_decision,
    record_decision,
    revoke_decision,
    register_authority,
    get_authority,
    list_authorities,
    decision_signing_payload,
    DecisionError,
    AuthorityNotRegistered,
)
from keel.privacy.counsel_packet import prepare_packet, PACKET_FIELDS
from keel.privacy.publication_guard import check_egress, check_release, EgressDenied
from keel.privacy.release_manifest import build_release, verify_manifest
from keel.privacy.classifier import classify, ArtifactClass, EGRESS_DENY
from keel.privacy.inventory import (
    hash_file,
    hash_bytes,
    inventory_artifacts,
    ArtifactRecord,
)
from keel.privacy.lineage import LineageGraph, LineageRecord, LineageInput, LineageTransform
from keel.privacy.derivative_analysis import analyze_derivative
from keel.privacy.canary_adapter import (
    load_canary_evidence,
    evidence_status,
    assert_evidence_ready,
    summarize_evidence,
    canary_packet_inputs,
    CanaryEvidence,
    CanarySchemaError,
    EvidenceBlocked,
    EVIDENCE_READY,
    EVIDENCE_BLOCKED,
)

__all__ = [
    "verify_decision",
    "record_decision",
    "revoke_decision",
    "register_authority",
    "get_authority",
    "list_authorities",
    "decision_signing_payload",
    "DecisionError",
    "AuthorityNotRegistered",
    "prepare_packet",
    "PACKET_FIELDS",
    "check_egress",
    "check_release",
    "EgressDenied",
    "build_release",
    "verify_manifest",
    "classify",
    "ArtifactClass",
    "EGRESS_DENY",
    "hash_file",
    "hash_bytes",
    "inventory_artifacts",
    "ArtifactRecord",
    "LineageGraph",
    "LineageRecord",
    "LineageInput",
    "LineageTransform",
    "analyze_derivative",
    "load_canary_evidence",
    "evidence_status",
    "assert_evidence_ready",
    "summarize_evidence",
    "canary_packet_inputs",
    "CanaryEvidence",
    "CanarySchemaError",
    "EvidenceBlocked",
    "EVIDENCE_READY",
    "EVIDENCE_BLOCKED",
]

CONTROLLER_VERSION = "1.0.0"
