#!/usr/bin/env python3
"""Standing preference architecture (§17 of the input-resolution spec).

Canonical preference families with scope, exceptions, provenance, and expiry.
A preference is reusable authority for resolving blockers WITHOUT asking the
applicant. Matching rules:
  - employer-specific exception beats employer scope beats global scope
  - expired preferences never match (re-ask required)
  - provenance is mandatory: every preference cites the applicant's own
    words + date

This module is READ-ONLY at runtime in dry-run mode. The seed below is an
EXAMPLE set — illustrative values only, not any real applicant's decisions.
Replace it with the applicant's own standing decisions before use. Live
writes (requirement: backup first) are a parent-authorized step, not this
module's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class Preference:
    family: str
    value: str
    scope: str = "global"          # "global" | "employer:<name>" | "role:<role_id>"
    exceptions: tuple = ()         # scopes where this preference does NOT apply
    source: str = ""               # e.g. "applicant's own words, 2026-09-15"
    created_at: str = ""
    last_confirmed_at: str = ""
    expires_at: Optional[str] = None   # ISO date; None = no expiry
    reusable: bool = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_expired(pref: Preference, at: Optional[datetime] = None) -> bool:
    if not pref.expires_at:
        return False
    at = at or _now()
    try:
        exp = datetime.fromisoformat(pref.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True  # unparseable expiry fails closed -> treat as expired
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return at > exp


def _scope_rank(pref: Preference, employer: str, role_id: str) -> int:
    """Higher rank = more specific. -1 = not applicable."""
    scope = pref.scope.lower()
    emp = (employer or "").lower()
    rid = (role_id or "").lower()
    for exc in pref.exceptions:
        e = exc.lower()
        if e.startswith("employer:") and e.split(":", 1)[1] in emp:
            return -1
        if e.startswith("role:") and e.split(":", 1)[1] in rid:
            return -1
    if scope == "global":
        return 0
    if scope.startswith("employer:") and scope.split(":", 1)[1] in emp:
        return 2
    if scope.startswith("role:") and scope.split(":", 1)[1] in rid:
        return 3
    return -1


def match_preference(prefs: list[Preference], family: str,
                     employer: str = "", role_id: str = "",
                     at: Optional[datetime] = None):
    """Return (preference|None, reason). Most-specific live preference wins."""
    cands = []
    for p in prefs:
        if p.family != family or not p.reusable:
            continue
        if is_expired(p, at):
            continue
        rank = _scope_rank(p, employer, role_id)
        if rank >= 0:
            cands.append((rank, p))
    if not cands:
        # distinguish "none exists" from "exists but expired/inapplicable"
        existing = [p for p in prefs if p.family == family]
        if any(is_expired(p, at) for p in existing):
            return None, "preference expired — re-ask required"
        return None, "no standing preference for family"
    cands.sort(key=lambda t: t[0], reverse=True)
    return cands[0][1], "matched scope=%s source=%s" % (cands[0][1].scope, cands[0][1].source)


def seed_preferences() -> list[Preference]:
    """EXAMPLE seed — illustrative families and values, NOT real decisions.

    Mirrors the *mechanism* (scope, exceptions, provenance, expiry). Replace
    every value with the applicant's own standing decisions before use.
    """
    D = "2026-09-16"
    return [
        Preference("office.max_days_per_week", "3 (EXAMPLE — applicant's own cap)",
                   source="example seed; replace with the applicant's own policy",
                   created_at=D),
        Preference("travel.max_required_percentage", "25 on a defined schedule (EXAMPLE)",
                   source="example seed; replace with the applicant's own policy",
                   created_at=D),
        Preference("relocation.general", "Yes — will relocate anywhere (EXAMPLE)",
                   source="example seed; replace with the applicant's own words + date",
                   created_at=D),
        Preference("heard_about_source", "Other",
                   source="example seed; replace with the applicant's own preference",
                   created_at=D),
        Preference("compensation.strategy", "defensible current-market midpoint",
                   source="example seed; replace with the applicant's own preference",
                   created_at=D),
        Preference("start_timeframe", "Next day",
                   source="example seed; replace with the applicant's own fact",
                   created_at=D),
        Preference("email_application.agent_send_authorization",
                   "NOT AUTHORIZED — agents never send under the applicant's identity",
                   source="standing boundary example; agents never send as the applicant",
                   created_at=D),
        # Deliberately ABSENT (no standing decision on file — these are the
        # high-leverage ask-the-applicant items, not auto-resolvable):
        #   interview_recording.metaview / .bright_hire / .general
        #   recruiting.whatsapp / recruiting.sms
        #   legal.arbitration (employer-specific; never blanket)
        #   travel.unspecified_required (one consolidated decision pending)
    ]


# Families the engine may auto-apply vs families that always need the
# applicant. A family listed here as trent_only can NEVER resolve via
# preferences. (Constant name kept for compatibility with blocker.py; it is
# a code identifier, not a reference to any person.)
TRENT_ONLY_FAMILIES = frozenset({
    "legal.arbitration",
    "interview_recording.metaview",
    "interview_recording.bright_hire",
    "interview_recording.general",
    "recruiting.whatsapp",
    "recruiting.sms",
    "travel.unspecified_required",
    "no_ai_unaided_writing",
    "personally_completed_certification",
    "truthfulness_certification",
    "ai_evaluation_consent",
})
