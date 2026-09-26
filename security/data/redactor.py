"""Deterministic redaction for logs and strict output surfaces.

Two levels:
    "log"    — safe for internal logs: credentials are removed entirely,
               email local parts are masked but the domain stays.
    "strict" — safe for anything leaving the trust boundary: everything in
               "log", plus SSNs, phone numbers, and full email addresses.

Credentials never survive either level.  Clean text is returned unchanged
with an empty findings list.
"""
from __future__ import annotations

import re

_LEVELS = ("log", "strict")

# CRITICAL repo lore: every secret regex accepts BOTH ``_`` and ``-``
# separators (e.g. ``sk-live-`` AND ``sk_live_``).
_CREDENTIAL_PATTERNS = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b")),
    ("bearer_jwt", re.compile(r"\b[Bb]earer\s+eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-\.]*")),
    ("stripe_secret", re.compile(r"\bsk[_-](?:live|test)[_-][A-Za-z0-9]+\b")),
    ("private_key_block",
     re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
                re.DOTALL)),
    ("secret_assignment",
     re.compile(r"(?i)(?:passw|passwd|secret|api[_-]?key|auth[_-]?token)\s*[:=]\s*\S{8,}")),
)

_CREDIT_CARD = ("credit_card", re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"))
_SSN = ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"))
_PHONE = ("phone", re.compile(r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b"))
_EMAIL = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _scrub(text, patterns, findings, level, replacement):
    for name, pattern in patterns:
        text, count = pattern.subn(replacement, text)
        if count:
            findings.append({"pattern": name, "level": level, "matches": count})
    return text


def redact(text: str, level: str = "log"):
    """Return ``(redacted_text, findings)``.

    ``findings`` is a non-empty list whenever anything was redacted; it is
    empty when the text was clean.
    """
    if level not in _LEVELS:
        raise ValueError("level must be one of %r" % (_LEVELS,))
    if not isinstance(text, str):
        raise TypeError("text must be str")
    findings = []

    # Credentials never survive, at either level.
    redacted = _scrub(text, _CREDENTIAL_PATTERNS, findings, level, "[REDACTED]")
    # 16-digit credit cards are redacted at both levels.
    redacted = _scrub(redacted, (_CREDIT_CARD,), findings, level, "[REDACTED]")

    # Emails: mask the local part at "log" (domain stays for triage),
    # remove entirely at "strict".
    if level == "log":
        def _mask(match):
            findings.append({"pattern": "email", "level": level, "matches": 1})
            return "***@" + match.group(2)
        redacted = _EMAIL.sub(_mask, redacted)
    else:
        redacted = _scrub(redacted, (("email", _EMAIL),), findings, level, "[REDACTED]")

    if level == "strict":
        # SSNs and phone numbers are redacted only at "strict".
        redacted = _scrub(redacted, (_SSN, _PHONE), findings, level, "[REDACTED]")

    return redacted, findings


__all__ = ["redact"]
