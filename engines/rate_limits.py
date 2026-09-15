"""Employer application rate-limit budgets.

Some employers cap how many applications one candidate may submit in a window
(e.g. an employer whose form states a cap — record the exact stated budget).
This module enforces those budgets against the append-only application ledger.

Read-only: never writes the ledger. Counts only exact `status == SUBMITTED` rows.

Budgets live in rate_limits.json next to this file (rate_limits.example.json is the template):
    employer -> {"limit": int, "window_days": int, "evidence": str, "notes": str}

Usage:
    from rate_limits import is_allowed, assert_allowed
    ok, reason = is_allowed("openai")
    assert_allowed("openai")   # raises RateLimitExceeded when exhausted
"""

import json
import os
from datetime import date, datetime, timedelta

from keel_paths import HOME, ENGINES  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))


def _budgets_path():
    for cand in (os.path.join(ENGINES, "rate_limits.json"),
                 os.path.join(ENGINES, "rate_limits.example.json")):
        if os.path.exists(cand):
            return cand
    return os.path.join(ENGINES, "rate_limits.example.json")


BUDGETS_PATH = _budgets_path()
DEFAULT_LEDGER = os.path.join(HOME, "data", "application-ledger.json")


class RateLimitExceeded(Exception):
    """Raised by assert_allowed when an employer's application budget is exhausted."""


def load_budgets(path=None):
    with open(path or BUDGETS_PATH) as f:
        return json.load(f)


def _submitted_date(row):
    """Best-effort submission date for a ledger row; None if unparseable."""
    ds = row.get("date_submitted")
    if ds:
        try:
            return datetime.strptime(str(ds)[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    for key in ("submitted_at", "last_activity", "date_confirmed"):
        val = row.get(key)
        if not val:
            continue
        try:
            # Handles "2026-09-14 19:03 PDT" and ISO "2026-09-14T19:51:23-07:00".
            return datetime.fromisoformat(str(val).split(" PDT")[0]
                                          .replace(" ", "T", 1) if "T" not in str(val)
                                          else str(val)).date()
        except ValueError:
            continue
    return None


def _matches_employer(row, employer):
    needle = (employer or "").strip().lower()
    if not needle:
        return False
    haystacks = [str(row.get("company") or ""), str(row.get("role_id") or "")]
    return any(needle in h.lower() for h in haystacks)


def count_used(employer, ledger_path=None, window_days=180, today=None):
    """Count exact SUBMITTED rows for employer within the trailing window_days."""
    try:
        ledger = json.load(open(ledger_path or DEFAULT_LEDGER))
    except FileNotFoundError:
        return 0  # no ledger yet -> no submissions used
    rows = ledger if isinstance(ledger, list) else ledger.get("applications", [])
    today = today or date.today()
    cutoff = today - timedelta(days=window_days)
    used = 0
    for row in rows:
        if row.get("status") != "SUBMITTED":
            continue
        if not _matches_employer(row, employer):
            continue
        d = _submitted_date(row)
        if d is None:
            # Undated submission: count it (fail closed -- never undercount a budget).
            used += 1
        elif d >= cutoff:
            used += 1
    return used


def is_allowed(employer, ledger_path=None, today=None):
    """Return (allowed: bool, reason: str) for one more application to employer."""
    budgets = load_budgets()
    key = next((k for k in budgets if k.lower() == (employer or "").strip().lower()), None)
    if key is None:
        return True, f"no budget on file for '{employer}' -- allowed"
    cfg = budgets[key]
    used = count_used(employer, ledger_path, cfg["window_days"], today=today)
    limit = cfg["limit"]
    if used < limit:
        return True, (f"{key}: {used}/{limit} used in trailing "
                      f"{cfg['window_days']}d -- allowed")
    return False, (f"{key}: budget exhausted ({used}/{limit} in trailing "
                   f"{cfg['window_days']}d). Evidence: {cfg.get('evidence', 'n/a')}")


def assert_allowed(employer, ledger_path=None, today=None):
    """Raise RateLimitExceeded with a clear message if the budget is exhausted."""
    ok, reason = is_allowed(employer, ledger_path=ledger_path, today=today)
    if not ok:
        raise RateLimitExceeded(
            f"Application blocked: {reason}. Park the lead instead of submitting.")
    return reason
