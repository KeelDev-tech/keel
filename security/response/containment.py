"""Containment: freeze agent trees and quarantine artifacts.

Deterministic incident-response primitives. Freezing marks every identity
in a tree as revoked (fail-closed for all future evaluations).
Quarantining an artifact records it in the security ledger as untrusted.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..identity.agent_identity import IdentityRegistry
from .revoke import RevocationRegistry


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def freeze_agent_tree(root_agent_id: str,
                      revocations: RevocationRegistry,
                      reason: str = "") -> list[str]:
    """Revoke the root identity and every identity parented to it.

    Returns the list of revoked identity ids. Deterministic: walks the
    registry's parent links; unknown root -> empty list (nothing to freeze).
    """
    root = IdentityRegistry.get(root_agent_id)
    if root is None:
        return []
    frozen: list[str] = []
    for ident in list(IdentityRegistry._identities.values()):
        if ident.id == root_agent_id or ident.parent_id == root_agent_id:
            if revocations.revoke_identity(ident.id, reason or
                                           "agent tree frozen"):
                frozen.append(ident.id)
    return frozen


def quarantine_artifact(artifact_hash: str, reason: str,
                        ledger=None) -> dict:
    """Record an artifact as quarantined. Returns the quarantine record.

    Optionally appends a QUARANTINE_ARTIFACT event to the security ledger
    (ledger = SecurityLedger instance) for an immutable audit trail.
    """
    record = {"artifact_hash": artifact_hash, "reason": reason,
              "quarantined_at": _utcnow_iso(), "status": "quarantined"}
    if ledger is not None:
        ledger.record_system_event(
            action_name="quarantine_artifact", decision="QUARANTINE",
            reasons=[reason], input_hashes={"artifact": artifact_hash})
    return record
