"""Memory validator: deterministic checks on every mutation.

Rules (all fail-closed):
  - all provenance fields present; confidence in [0, 1];
  - verification in {verified, unverified, quarantined};
  - content_hash matches the content (tamper-evident);
  - untrusted sources (web/ATS/scraped/...) may NEVER be stored as
    "verified" — they must be "unverified" or "quarantined";
  - sensitivity must be a known level.
"""

from __future__ import annotations

from ..errors import MemoryViolation
from ..injection.content_classifier import classify_source
from .provenance import MemoryEntry, Provenance, hash_content

KNOWN_SENSITIVITIES = frozenset(
    {"PUBLIC", "INTERNAL", "PII", "CREDENTIAL", "FINANCIAL"})

REQUIRED_FIELDS = ("source", "writer", "timestamp", "confidence",
                   "sensitivity", "verification", "content_hash")


def validate(entry: MemoryEntry) -> list[str]:
    """Return violation strings; empty means the mutation is valid."""
    violations: list[str] = []
    p: Provenance = entry.provenance
    d = p.to_dict()
    for f in REQUIRED_FIELDS:
        if d.get(f) in (None, ""):
            violations.append(f"provenance missing required field {f!r}")
    if not isinstance(p.confidence, (int, float)) or isinstance(
            p.confidence, bool) or not (0.0 <= p.confidence <= 1.0):
        violations.append(
            f"confidence {p.confidence!r} out of range [0, 1]")
    if p.verification not in ("verified", "unverified", "quarantined"):
        violations.append(
            f"unknown verification state {p.verification!r}")
    if p.sensitivity not in KNOWN_SENSITIVITIES:
        violations.append(f"unknown sensitivity {p.sensitivity!r}")
    if p.content_hash != hash_content(entry.content):
        violations.append("content_hash does not match content — "
                          "possible tampering")
    # The memory-poisoning rule: untrusted content cannot self-certify.
    origin = classify_source(p.source)
    if origin.value.startswith("untrusted") and p.verification == "verified":
        violations.append(
            f"source {p.source!r} is {origin.value}: untrusted content "
            "may not be stored as verified")
    if not entry.key or not entry.key.strip():
        violations.append("memory key must be non-empty")
    return violations


def enforce(entry: MemoryEntry) -> MemoryEntry:
    """Raise MemoryViolation on any validation failure."""
    violations = validate(entry)
    if violations:
        raise MemoryViolation("; ".join(violations))
    return entry
