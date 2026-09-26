"""Read-only compatibility names for Keel's vendored reconciler.

The historical private module supplied a writer as well. This public adapter
deliberately exports only its path and status vocabulary; importing it neither
creates a ledger nor provides submission authority. A SUBMITTED label remains
a local claim, not authenticated provider acceptance.
"""
import os

try:
    from .keel_paths import DATA
except ImportError:
    from keel_paths import DATA

LEDGER = os.path.join(DATA, "application-ledger.json")
_STATUSES = frozenset({
    "SUBMITTED", "SUBMISSION_CLAIMED", "UNKNOWN", "FAILED", "REJECTED",
    "WITHDRAWN", "INTERVIEW", "OFFER", "HIRED", "READY", "PARKED",
    "IN-FLIGHT", "CANCELLED", "CANCELED",
})


def canon_ledger_status(value):
    """Normalize spelling, never infer success from synonyms or truthy values."""
    if type(value) is not str or len(value) > 64:
        return "UNKNOWN"
    normalized = value.strip().upper()
    return normalized if normalized in _STATUSES else "UNKNOWN"
