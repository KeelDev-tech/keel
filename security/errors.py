"""Keel security subsystem — errors.

A single exception hierarchy so every module fails with a typed,
catchable security error. PolicyDenied subclasses ValueError so the
engine choke points (e.g. record_outcome.record, which refuses with
ValueError) preserve their existing fail-closed call contracts.
"""


class SecurityError(Exception):
    """Base class for all security-subsystem errors."""


class CapabilityDenied(SecurityError, ValueError):
    """An identity requested a capability it was not granted."""


class PolicyDenied(SecurityError, ValueError):
    """The deterministic policy engine refused an action."""


class InjectionDetected(SecurityError, ValueError):
    """Untrusted content carried an injection payload."""


class TrustBoundaryViolation(SecurityError, ValueError):
    """Untrusted content was treated as an instruction."""


class ApprovalRequired(SecurityError, ValueError):
    """Action needs a human approval that was not presented."""


class Quarantined(SecurityError, ValueError):
    """Artifact/content was quarantined; the action must not execute."""


class SecretViolation(SecurityError, ValueError):
    """A secret-handling rule was violated."""


class MemoryViolation(SecurityError, ValueError):
    """A memory provenance/validation rule was violated."""
