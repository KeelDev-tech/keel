"""Sensitivity classification for field names, values, and free text.

Name-based rules (fail closed: unknown names are INTERNAL, never PUBLIC),
plus free-text detection of secrets, credentials, and personal identifiers.
"""
from __future__ import annotations

import enum
import re


class Sensitivity(enum.IntEnum):
    """Ordered so that ``max()`` reflects severity: PUBLIC < CREDENTIAL."""

    PUBLIC = 0
    INTERNAL = 1
    PII = 2
    FINANCIAL = 3
    CREDENTIAL = 4


# ---------------------------------------------------------------------------
# Name-based classification
# ---------------------------------------------------------------------------

# Checked in severity order via max(); a name matching several bands keeps
# the most severe one.
_NAME_PATTERNS = (
    (re.compile(r"passw|passwd|secret|token|api[_-]?key|private[_-]?key|\bkey\b"),
     Sensitivity.CREDENTIAL),
    (re.compile(r"e_?mail|\bmail\b|phone|mobile|\btel\b|ssn|social[_-]?security|"
                r"address|dob|date[_-]?of[_-]?birth|passport|driver[_-]?license"),
     Sensitivity.PII),
    (re.compile(r"amount|salary|price|cost|wage|\bpay\b|balance|invoice|"
                r"transaction|revenue|budget|credit[_-]?card|bank|account[_-]?(?:no|number)?"),
     Sensitivity.FINANCIAL),
)


def _looks_like_credential(value: str) -> bool:
    """True when a raw value itself looks like a secret or credential."""
    if not value or not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in _CREDENTIAL_TEXT_PATTERNS)


def classify_field(name: str, value=None) -> Sensitivity:
    """Classify a field by name, optionally raised by the value it carries.

    Unknown names default to INTERNAL (fail closed, never PUBLIC).  When a
    value is supplied and itself looks like a credential/secret, the result
    is raised to CREDENTIAL.
    """
    label = str(name).lower()
    sensitivity = Sensitivity.INTERNAL
    for pattern, level in _NAME_PATTERNS:
        if pattern.search(label):
            sensitivity = max(sensitivity, level)
    if value is not None and _looks_like_credential(str(value)):
        sensitivity = max(sensitivity, Sensitivity.CREDENTIAL)
    return sensitivity


# ---------------------------------------------------------------------------
# Free-text classification
# ---------------------------------------------------------------------------

# CRITICAL repo lore: every secret regex accepts BOTH ``_`` and ``-``
# separators (e.g. ``sk-live-`` AND ``sk_live_``).

_CREDENTIAL_TEXT_PATTERNS = (
    # AWS access key id.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # GitHub-style tokens.
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"),
    # Bearer JWTs (header.payload[.signature]).
    re.compile(r"\b[Bb]earer\s+eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-\.]*"),
    # Stripe-style secrets, either separator.
    re.compile(r"\bsk[_-]live[_-][A-Za-z0-9]+\b"),
    re.compile(r"\bsk[_-]test[_-][A-Za-z0-9]+\b"),
    # Private-key blocks.
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    # Generic secret assignments: password=..., api-key: ..., secret = ...
    # Both separators, case-insensitive; value must be long enough to matter.
    re.compile(r"(?i)(?:passw|passwd|secret|api[_-]?key|auth[_-]?token)\s*[:=]\s*"
               r"(?!['\"]?[\s'\"])\S{8,}"),
)

_PII_TEXT_PATTERNS = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # US phone numbers: 415-555-0132, 415.555.0132, 415 555 0132.
    re.compile(r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b"),
)

_FINANCIAL_TEXT_PATTERNS = (
    # SSN-like numbers; the data test pins these to the FINANCIAL band.
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Credit-card-like runs (16 digits, optional separators).
    re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"),
)


def classify_text(text: str) -> set:
    """Return the set of Sensitivity bands found in free text.

    Empty set for clean text.  Never returns PUBLIC: clean text simply has
    no findings; absence of a band is not a PUBLIC classification.
    """
    found = set()
    if not text or not isinstance(text, str):
        return found
    for pattern in _CREDENTIAL_TEXT_PATTERNS:
        if pattern.search(text):
            found.add(Sensitivity.CREDENTIAL)
            break
    for pattern in _PII_TEXT_PATTERNS:
        if pattern.search(text):
            found.add(Sensitivity.PII)
            break
    for pattern in _FINANCIAL_TEXT_PATTERNS:
        if pattern.search(text):
            found.add(Sensitivity.FINANCIAL)
            break
    return found


def max_sensitivity(sensitivities) -> Sensitivity:
    """Highest sensitivity in the set; empty set -> INTERNAL (fail closed)."""
    best = Sensitivity.INTERNAL
    for level in sensitivities:
        best = max(best, Sensitivity(level))
    return best


__all__ = ["Sensitivity", "classify_field", "classify_text", "max_sensitivity"]
