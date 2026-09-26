"""Capability manifests: DEFAULT DENY for everything not explicitly granted.

No agent inherits its orchestrator's full authority. A capability manifest
is the exact set of named powers an identity holds; anything not in the
set is denied. Unknown capability names are denied (never granted).
"""

from __future__ import annotations

from ..errors import CapabilityDenied

# Canonical capability names. DEFAULT DENY: a name not in a manifest is
# denied; a name not in this registry can never be granted.
READ_LEADS = "read_leads"
WRITE_PACKET = "write_packet"
BROWSER_NAVIGATE = "browser_navigate"
BROWSER_SUBMIT = "browser_submit"
READ_SECRETS = "read_secrets"
MODIFY_POLICY = "modify_policy"
DEPLOY = "deploy"
# Extended operational capabilities (Phase 1).
RECORD_SUBMISSION = "record_submission"
READ_TELEMETRY = "read_telemetry"
WRITE_TELEMETRY = "write_telemetry"
MANAGE_QUEUE = "manage_queue"
READ_MEMORY = "read_memory"
WRITE_MEMORY = "write_memory"
APPROVE_HIGH_IMPACT = "approve_high_impact"

KNOWN_CAPABILITIES = frozenset({
    READ_LEADS, WRITE_PACKET, BROWSER_NAVIGATE, BROWSER_SUBMIT,
    READ_SECRETS, MODIFY_POLICY, DEPLOY, RECORD_SUBMISSION,
    READ_TELEMETRY, WRITE_TELEMETRY, MANAGE_QUEUE, READ_MEMORY,
    WRITE_MEMORY, APPROVE_HIGH_IMPACT,
})

# Every action name must map to exactly one required capability. An action
# name absent from this table maps to NOTHING grantable -> denied.
# (Fail-closed by construction.)
ACTION_CAPABILITY = {
    "read_leads": READ_LEADS,
    "search_leads": READ_LEADS,
    "list_queue": READ_LEADS,
    "write_packet": WRITE_PACKET,
    "update_queue": MANAGE_QUEUE,
    "park_lead": MANAGE_QUEUE,
    "browser_navigate": BROWSER_NAVIGATE,
    "browser_submit": BROWSER_SUBMIT,
    "submit_application": BROWSER_SUBMIT,
    "record_submission": RECORD_SUBMISSION,
    "read_telemetry": READ_TELEMETRY,
    "write_telemetry": WRITE_TELEMETRY,
    "read_secret": READ_SECRETS,
    "use_credential": READ_SECRETS,
    "modify_policy": MODIFY_POLICY,
    "deploy": DEPLOY,
    "restart_service": DEPLOY,
    "read_memory": READ_MEMORY,
    "write_memory": WRITE_MEMORY,
    "approve": APPROVE_HIGH_IMPACT,
    # Blocker Resolution Directive §9 actions. These map to real
    # capabilities so the abstain path (conditions satisfied) still flows
    # through capability + class + approval checks — the blocker layer
    # itself can only DENY or abstain, never grant.
    "api_direct_write": DEPLOY,
    "synthetic_to_live": DEPLOY,
    "human_decision_impersonation": APPROVE_HIGH_IMPACT,
    "publication": DEPLOY,
    "keel_0_8_promotion": DEPLOY,
}


def required_capability(action_name: str) -> str | None:
    """Return the capability an action requires, or None if unmapped.

    None means: no manifest can satisfy this action -> default deny.
    """
    return ACTION_CAPABILITY.get(action_name)


class CapabilityManifest:
    """The exact set of capabilities granted to one identity."""

    def __init__(self, capabilities=()):
        unknown = set(capabilities) - KNOWN_CAPABILITIES
        if unknown:
            raise ValueError(
                f"cannot grant unknown capabilities {sorted(unknown)}; "
                f"known: {sorted(KNOWN_CAPABILITIES)}")
        self._grants = frozenset(capabilities)

    def has(self, capability: str) -> bool:
        """Default deny: True only if explicitly granted."""
        return capability in self._grants

    def check(self, capability: str) -> None:
        """Raise CapabilityDenied unless the capability was granted."""
        if not self.has(capability):
            raise CapabilityDenied(
                f"capability {capability!r} not granted "
                f"(granted: {sorted(self._grants)}) — default deny")

    def check_action(self, action_name: str) -> str:
        """Resolve an action to its capability and check it.

        Returns the capability name on success. Raises CapabilityDenied
        for unmapped actions and ungranted capabilities.
        """
        cap = required_capability(action_name)
        if cap is None:
            raise CapabilityDenied(
                f"action {action_name!r} maps to no known capability — "
                "default deny")
        self.check(cap)
        return cap

    def grants(self) -> frozenset:
        return self._grants

    def to_dict(self) -> dict:
        return {"capabilities": sorted(self._grants)}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CapabilityManifest({sorted(self._grants)})"
