"""Explicit delegation: no agent inherits its orchestrator's full authority.

A delegation is a registry-recorded, time-boxed, revocable grant
of a SUBSET of the delegator's capabilities. Escalation attempts —
delegating a capability the delegator does not hold — are refused.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import RLock

from ..errors import CapabilityDenied
from .capabilities import CapabilityManifest


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Delegation:
    """A time-boxed grant of a capability subset from delegator to delegate."""
    id: str
    delegator_id: str
    delegate_id: str
    capabilities: frozenset
    issued_at: str = field(default_factory=_utcnow_iso)
    expires_at: str = ""
    revoked: bool = False
    revoke_reason: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "delegator_id": self.delegator_id,
                "delegate_id": self.delegate_id,
                "capabilities": sorted(self.capabilities),
                "issued_at": self.issued_at, "expires_at": self.expires_at,
                "revoked": self.revoked, "revoke_reason": self.revoke_reason}


class DelegationRegistry:
    """Issues and validates delegations. The delegator's manifest is the
    ceiling: a delegation can only ever narrow authority, never widen it."""

    _delegations: dict[str, Delegation] = {}
    _lock = RLock()

    @classmethod
    def delegate(cls, delegator_id: str,
                 delegator_manifest: CapabilityManifest,
                 delegate_id: str, capabilities,
                 ttl_seconds: int = 3600) -> Delegation:
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("delegation refused: positive integer TTL required")
        caps = frozenset(capabilities)
        unknown = set(caps) - set(delegator_manifest.grants())
        if unknown:
            # Escalation attempt: delegator cannot grant what it lacks.
            raise CapabilityDenied(
                f"delegation refused: delegator {delegator_id} lacks "
                f"{sorted(unknown)} — cannot delegate unheld capabilities")
        if not caps:
            raise ValueError("delegation refused: empty capability set")
        issued = datetime.now(timezone.utc)
        # Deterministic-ish id: content + random suffix so two identical
        # delegations are distinct ledger entries.
        raw = (f"{delegator_id}:{delegate_id}:{','.join(sorted(caps))}:"
               f"{issued.isoformat()}:{secrets.token_hex(8)}")
        did = hashlib.sha256(raw.encode()).hexdigest()[:32]
        from datetime import timedelta
        expires = (issued + timedelta(seconds=ttl_seconds)).isoformat()
        d = Delegation(id=did, delegator_id=delegator_id,
                       delegate_id=delegate_id, capabilities=caps,
                       issued_at=issued.isoformat(), expires_at=expires)
        with cls._lock:
            # Keep the authoritative issuance independent of mutable handles.
            cls._delegations[did] = replace(d)
        return d

    @classmethod
    def revoke(cls, delegation_id: str, reason: str = "") -> bool:
        with cls._lock:
            d = cls._delegations.get(delegation_id)
            if d is None:
                return False
            d.revoked = True
            d.revoke_reason = reason
            return True

    @classmethod
    def is_valid(cls, delegation: Delegation,
                 now: datetime | None = None) -> tuple[bool, str]:
        """Validate against this process's issuance and revocation registry.

        Registry administration is trusted host code, not a signature or an
        authentication boundary against arbitrary in-process Python execution.
        """
        if type(delegation) is not Delegation:
            return False, "delegation type invalid — fail closed"
        with cls._lock:
            registered = cls._delegations.get(delegation.id)
            if registered is None:
                return False, "delegation not registered — fail closed"
            if registered.revoked:
                return False, f"delegation {registered.id} revoked: {registered.revoke_reason}"
            if delegation != registered:
                return False, "delegation altered — fail closed"
            now = now or datetime.now(timezone.utc)
            try:
                issued = datetime.fromisoformat(registered.issued_at)
                exp = datetime.fromisoformat(registered.expires_at)
                if now < issued:
                    return False, "delegation not yet valid — fail closed"
                if now >= exp:
                    return False, f"delegation {registered.id} expired at {registered.expires_at}"
            except (TypeError, ValueError):
                return False, "delegation has malformed expiry — fail closed"
            return True, "valid"

    @classmethod
    def clear(cls) -> None:
        """Test support only — never call in production paths."""
        with cls._lock:
            cls._delegations.clear()
