"""Agent identity: content-addressed unique IDs for every actor.

Every agent, sub-agent, tool, worker, and browser session receives a unique
identity. System identities (spawned by Keel itself) get deterministic IDs
so the security ledger stays correlatable across processes. Ephemeral
actors get a random nonce so IDs are unguessable.

No external PKI is required: identity here is a registry discipline, not
a certificate chain. The registry is the authority.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

VALID_KINDS = frozenset(
    {"agent", "subagent", "tool", "worker", "browser_session", "service"}
)

_SYSTEM_SALT = "keel-security-identity:v1"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mint_identity_id(kind: str, name: str, parent_id: str = "",
                     nonce: str | None = None) -> str:
    """Mint a content-addressed identity ID.

    id = sha256(kind | name | parent_id | nonce). Ephemeral actors pass no
    nonce and get a fresh random one; system identities pass a fixed nonce
    (via register_system_identity) so the ID is stable across processes.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown identity kind {kind!r}; "
                         f"valid: {sorted(VALID_KINDS)}")
    if not name or not name.strip():
        raise ValueError("identity name must be non-empty")
    nonce = nonce if nonce is not None else secrets.token_hex(16)
    raw = f"{_SYSTEM_SALT}:{kind}:{name.strip()}:{parent_id}:{nonce}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Identity:
    """A single actor in the agent ecosystem."""
    id: str
    kind: str
    name: str
    parent_id: str = ""
    created_at: str = field(default_factory=_utcnow_iso)
    note: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name,
                "parent_id": self.parent_id, "created_at": self.created_at,
                "note": self.note}


def mint_identity(kind: str, name: str, parent_id: str = "",
                  note: str = "") -> Identity:
    """Mint an ephemeral identity (random nonce — unguessable)."""
    ident_id = mint_identity_id(kind, name, parent_id)
    parent = IdentityRegistry.get(parent_id) if parent_id else None
    _ = parent  # parent linkage recorded via parent_id; registry optional
    return Identity(id=ident_id, kind=kind, name=name.strip(),
                    parent_id=parent_id, note=note)


def register_system_identity(name: str, kind: str = "service",
                             note: str = "") -> Identity:
    """Register a Keel-spawned system identity with a STABLE id.

    Deterministic: the same (kind, name) always yields the same id, so the
    ledger can correlate decisions across process restarts. Only Keel's own
    engine code may call this (it is the spawning authority).
    """
    ident_id = mint_identity_id(kind, name, parent_id="",
                                nonce=f"system:{kind}:{name.strip()}")
    ident = Identity(id=ident_id, kind=kind, name=name.strip(), note=note)
    IdentityRegistry.register(ident)
    return ident


class IdentityRegistry:
    """Process-local registry of known identities."""

    _identities: dict[str, Identity] = {}
    _revoked: set[str] = set()

    @classmethod
    def register(cls, identity: Identity) -> Identity:
        cls._identities[identity.id] = identity
        return identity

    @classmethod
    def get(cls, identity_id: str) -> Identity | None:
        return cls._identities.get(identity_id)

    @classmethod
    def revoke(cls, identity_id: str, reason: str = "") -> bool:
        if identity_id in cls._identities:
            cls._revoked.add(identity_id)
            return True
        return False

    @classmethod
    def is_revoked(cls, identity_id: str) -> bool:
        return identity_id in cls._revoked

    @classmethod
    def clear(cls) -> None:
        """Test support only — never call in production paths."""
        cls._identities.clear()
        cls._revoked.clear()
