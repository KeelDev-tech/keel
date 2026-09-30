"""Canonical fit admission and lead identity for local question resolution.

Tray overrides control visibility only. Queue clearing always observes the
current intake floor, and fit admission never grants verification or submission
authority.
"""
from __future__ import annotations

import hashlib
import json
import math

try:
    from . import fit_policy
except ImportError:
    import fit_policy


def identity_digest(row):
    """Bind observations to explicit lead identity fields and their presence."""
    names = ("role_id", "company", "employer", "title", "role_title", "role_key", "url",
             "ats_url", "application_url", "posting_url", "job_url", "apply_url",
             "ats", "ats_board", "job_id")
    identity = {name: row[name] for name in names if name in row}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def validate_min_fit(value=None):
    """Read the current intake floor; an explicit floor may only tighten it."""
    canonical = fit_policy.main_floor()
    if value is None:
        return canonical
    number = _number(value)
    if number is None or number < canonical:
        raise ValueError("question resolution fit floor cannot lower canonical intake policy")
    return number


def fit_admission(row, *, floor=None):
    """Describe current fit eligibility without modifying canonical state."""
    minimum = validate_min_fit(floor)
    value = row.get("fit_score") if isinstance(row, dict) else None
    # Canonical READY admission requires an actual numeric fit score. A string
    # may be valid visibility configuration but is not verified scoring data.
    number = _number(value) if type(value) in (int, float) else None
    reason = "missing_fit" if value is None else "invalid_fit"
    if number is not None:
        reason = "eligible" if number >= minimum else "below_floor"
    return {"eligible": reason == "eligible", "reason": reason,
            "score": number, "floor": minimum}
