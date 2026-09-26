"""Trust boundaries: instructions found in untrusted content are never
Keel instructions.

The rule is absolute and deterministic: content whose origin is not in
TRUSTED_ORIGINS may be *data* (read, quoted, hashed, stored) but may never
be *interpreted as instructions*. Any code path that would promote
untrusted text to an instruction, plan step, policy change, or tool-call
argument derived from an instruction must call enforce() first.
"""

from __future__ import annotations

from ..errors import TrustBoundaryViolation
from .content_classifier import TRUSTED_ORIGINS, Content


def may_carry_instructions(content: Content) -> bool:
    """True only if the content's origin is trusted."""
    return content.origin in TRUSTED_ORIGINS


def enforce(content: Content, intended_use: str = "instruction") -> Content:
    """Raise TrustBoundaryViolation if untrusted content would be used as
    an instruction/plan/policy/tool-call. Returns the content unchanged
    when the boundary holds."""
    if not may_carry_instructions(content):
        raise TrustBoundaryViolation(
            f"trust boundary: content from {content.origin.value} "
            f"({content.source_type or 'unknown source'}) may not be used "
            f"as {intended_use}; treat as data only")
    return content


def quote_as_data(content: Content) -> str:
    """Safely render untrusted content as quoted data (never instructions)."""
    return f"[untrusted:{content.origin.value}:{content.source_type}] " \
           f"<<<{content.text}>>>"
