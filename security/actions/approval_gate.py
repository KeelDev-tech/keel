"""Approval gate: human approvals are explicit, scoped, single-use records.

Nothing auto-approves. An approval binds (agent, action, resource-digest)
with a TTL; it is consumed on first use. The security boundary never
grants an approval itself — approvals arrive from outside (Trent), and
this gate only verifies them deterministically.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from threading import RLock


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Approval:
    approval_id: str
    agent_id: str
    action_name: str
    resource_digest: str
    approver: str            # who approved, e.g. "trent"
    issued_at: str = field(default_factory=lambda: _utcnow().isoformat())
    expires_at: str = ""
    used: bool = False
    used_at: str = ""

    def to_dict(self) -> dict:
        return {"approval_id": self.approval_id, "agent_id": self.agent_id,
                "action_name": self.action_name,
                "resource_digest": self.resource_digest,
                "approver": self.approver, "issued_at": self.issued_at,
                "expires_at": self.expires_at, "used": self.used,
                "used_at": self.used_at}


def digest_resource(resource: dict | None) -> str:
    """Stable digest binding an approval to one specific resource."""
    import json
    if resource is not None and type(resource) is not dict:
        raise ValueError("approval resource must be an object")
    canonical = json.dumps(resource or {}, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


class ApprovalStore:
    """Process-local approval registry with atomic single-use consumption.

    The embedding host authenticates the approver before calling grant(); an
    approver string is not authentication. Returned records are inspection
    handles, not authority to alter the issuance binding. This registry does
    not provide cross-process or restart-persistent approval consumption.
    """

    def __init__(self):
        self._approvals: dict[str, Approval] = {}
        self._issued: dict[str, Approval] = {}
        self._consumed: set[str] = set()
        self._lock = RLock()

    def grant(self, agent_id: str, action_name: str, resource: dict | None,
              approver: str, ttl_seconds: int = 3600) -> Approval:
        """Record an externally-issued approval. The gate never self-issues
        in the enforcement path — grant() is the explicit human entry point."""
        if not approver or not approver.strip():
            raise ValueError("approval refused: approver identity required")
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("approval refused: positive integer TTL required")
        now = _utcnow()
        aid = "appr_" + secrets.token_hex(8)
        appr = Approval(
            approval_id=aid, agent_id=agent_id, action_name=action_name,
            resource_digest=digest_resource(resource),
            approver=approver.strip(),
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat())
        with self._lock:
            self._approvals[aid] = appr
            self._issued[aid] = replace(appr)
        return appr

    def _live(self, appr, now):
        if (appr is None or appr.approval_id in self._consumed or
                appr != self._issued.get(appr.approval_id) or appr.used):
            return False
        try:
            issued = datetime.fromisoformat(appr.issued_at)
            expiry = datetime.fromisoformat(appr.expires_at)
            return issued <= now < expiry
        except (TypeError, ValueError):
            return False

    def find_match(self, agent_id: str, action_name: str,
                   resource: dict | None) -> Approval | None:
        """Find a live approval for (agent, action, resource)."""
        want = digest_resource(resource)
        with self._lock:
            now = _utcnow()
            for appr in self._approvals.values():
                if (self._live(appr, now) and appr.agent_id == agent_id and
                        appr.action_name == action_name and appr.resource_digest == want):
                    return replace(appr)
        return None

    def consume(self, approval_id: str) -> Approval | None:
        """Single-use: mark an approval consumed. Returns None if the
        approval was already used, expired, or unknown."""
        with self._lock:
            appr = self._approvals.get(approval_id)
            now = _utcnow()
            if not self._live(appr, now):
                return None
            self._consumed.add(approval_id)
            appr.used = True
            appr.used_at = now.isoformat()
            return replace(appr)

    def clear(self) -> None:
        """Test support only — never call in production paths."""
        with self._lock:
            self._approvals.clear()
            self._issued.clear()
            self._consumed.clear()
