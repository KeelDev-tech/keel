"""Revocation: deterministic kill-switch primitives.

Revoke capability tokens, identities, and delegations. Revocation is
immediate, registry-local, and ledger-auditable. A revoked identity fails
every subsequent policy evaluation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..identity.agent_identity import IdentityRegistry
from ..identity.delegation import DelegationRegistry


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RevocationRegistry:
    """Central revocation list for identities, tokens, and delegations."""

    def __init__(self):
        self._revoked_identities: dict[str, str] = {}   # id -> "ts: reason"
        self._revoked_tokens: dict[str, str] = {}       # token -> "ts: reason"

    def revoke_identity(self, identity_id: str, reason: str = "") -> bool:
        if not IdentityRegistry.get(identity_id):
            return False
        IdentityRegistry.revoke(identity_id, reason)
        self._revoked_identities[identity_id] = f"{_utcnow_iso()}: {reason}"
        return True

    def revoke_token(self, token_id: str, reason: str = "") -> bool:
        self._revoked_tokens[token_id] = f"{_utcnow_iso()}: {reason}"
        return True

    def revoke_delegation(self, delegation_id: str, reason: str = "") -> bool:
        return DelegationRegistry.revoke(delegation_id, reason)

    def is_identity_revoked(self, identity_id: str) -> bool:
        return IdentityRegistry.is_revoked(identity_id)

    def is_token_revoked(self, token_id: str) -> bool:
        return token_id in self._revoked_tokens

    def clear(self) -> None:
        """Test support only — never call in production paths."""
        self._revoked_identities.clear()
        self._revoked_tokens.clear()
