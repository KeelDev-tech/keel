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


class PersistentApprovalStore(ApprovalStore):
    """Opt-in durable equivalent backed by the selected canonical authority.

    current_validator(db, approval, phase, now) must revalidate the real host
    identity, current policy and consent and return its nonempty authority
    revision. A changed revision invalidates the grant. operator_context is
    passed only to the host authentication callback; it is never persisted.
    This execution-policy approval does not replace exact content approval.
    """
    def __init__(self, authority, *, current_validator):
        from ..execution.durable import SQLiteAuthority
        if type(authority) is not SQLiteAuthority or not callable(current_validator):
            raise ValueError("canonical_authority_and_current_host_validator_required")
        self.authority = authority
        self.current_validator = current_validator

    @staticmethod
    def _record(row):
        def iso(seconds):
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        return Approval(row["approval_id"], row["actor"], row["action"], row["binding"],
                        row["approver"], iso(row["issued_at"]), iso(row["expires_at"]),
                        row["used_at"] is not None, iso(row["used_at"]) if row["used_at"] is not None else "")

    def _revision(self, db, approval, phase):
        from ..execution.host import Denied
        now = self.authority.now(db)
        revision = self.current_validator(db, replace(approval), phase, now)
        now = self.authority.now(db)
        if type(revision) is not str or not revision.strip():
            raise Denied("current_host_authority_required")
        return revision, now

    def grant(self, agent_id, action_name, resource, approver, ttl_seconds=3600,
              *, operator_context=None):
        from ..execution.durable import _json
        from ..execution.envelope import identifier
        from ..execution.host import Denied
        identifier(agent_id)
        if type(action_name) is not str or not action_name or len(action_name) > 128:
            raise ValueError("action_name_required")
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 86400:
            raise ValueError("approval_TTL_must_be_1_to_86400_seconds")
        if type(approver) is not str or not approver.strip():
            raise ValueError("approver_required")
        aid, binding = "appr_" + secrets.token_hex(16), digest_resource(resource)
        with self.authority.transaction() as (db, now):
            principal = self.authority.authorize(db, {"operation": "grant_policy_approval",
                         "approval_id": aid, "actor": agent_id, "action": action_name,
                         "resource_digest": binding, "ttl_seconds": ttl_seconds}, operator_context)
            if principal != approver.strip():
                raise Denied("authenticated_approver_mismatch")
            now = self.authority.now(db)
            row = {"approval_id": aid, "actor": agent_id, "action": action_name,
                   "binding": binding, "approver": principal, "issued_at": now,
                   "expires_at": now + ttl_seconds, "used_at": None}
            revision, current = self._revision(db, self._record(row), "grant")
            if current >= row["expires_at"]:
                raise Denied("approval_expired_during_validation")
            self.authority.check_subjects(db, (("identity", agent_id), ("approval", aid)))
            db.execute("INSERT INTO authority_approvals VALUES(?,?,?,?,?,?,?,?,NULL,0,?)",
                       (aid, "policy", agent_id, action_name, binding, principal, now,
                        row["expires_at"], _json({"authority_revision": revision})))
            self.authority.event(db, "policy_approved", aid, current, {"operator": principal})
            return self._record(row)

    def _usable(self, db, row, phase):
        import json
        from ..execution.host import Denied
        if row is None or row["purpose"] != "policy" or row["used_at"] is not None or row["revoked"]:
            return False
        try:
            self.authority.check_subjects(db, (("identity", row["actor"]), ("approval", row["approval_id"])))
            revision, now = self._revision(db, self._record(row), phase)
            return (row["issued_at"] <= now < row["expires_at"]
                    and json.loads(row["context"]) == {"authority_revision": revision})
        except Denied:
            return False

    def find_match(self, agent_id, action_name, resource):
        binding = digest_resource(resource)
        with self.authority.transaction() as (db, now):
            rows = db.execute("SELECT * FROM authority_approvals WHERE purpose='policy' "
                              "AND actor=? AND action=? AND binding=? ORDER BY issued_at,approval_id",
                              (agent_id, action_name, binding)).fetchall()
            for row in rows:
                if self._usable(db, row, "find"):
                    return self._record(row)
        return None

    def consume(self, approval_id):
        with self.authority.transaction() as (db, now):
            row = db.execute("SELECT * FROM authority_approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if not self._usable(db, row, "consume"):
                return None
            now = self.authority.now(db)
            if now >= row["expires_at"]:
                return None
            db.execute("UPDATE authority_approvals SET used_at=? WHERE approval_id=?", (now, approval_id))
            self.authority.event(db, "policy_approval_consumed", approval_id, now, {})
            result = dict(row); result["used_at"] = now
            return self._record(result)

    def revoke(self, approval_id, *, reason, operator_context):
        return self.authority.revoke("approval", approval_id, reason=reason, operator_context=operator_context)

    def clear(self):
        raise RuntimeError("durable_authority_cannot_be_cleared; use_revocation")
