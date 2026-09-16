#!/usr/bin/env python3
"""Tests for input_resolution/preferences.py — standing-preference architecture.

Run: python3 -m pytest test_preferences.py
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)

import input_resolution.preferences as prefs  # noqa: E402
import input_resolution.blocker as blocker  # noqa: E402


def test_import_exposes_public_api():
    for name in ("Preference", "is_expired", "match_preference",
                 "seed_preferences", "TRENT_ONLY_FAMILIES"):
        assert hasattr(prefs, name), name


def test_seed_is_example_only_no_personal_data():
    blob = open(prefs.__file__).read()
    # Synthetic stand-ins: the guard scans for PII-shaped leaks without
    # embedding any real personal identifier in the published history.
    for ident in ("alex.applicant1", "Alex Applicant", "APPLICANTCORP",
                  "555", "00000", "Applicant"):
        assert ident not in blob, f"personal identifier leaked: {ident}"
    seeds = prefs.seed_preferences()
    assert len(seeds) >= 5
    assert all("EXAMPLE" in (s.source or "").upper() or
               "example" in (s.source or "") for s in seeds), \
        "example seeds must be marked as examples"


def test_match_most_specific_scope_wins():
    ps = [
        prefs.Preference("f.travel", "global-yes"),
        prefs.Preference("f.travel", "employer-no",
                         scope="employer:acme"),
    ]
    p, reason = prefs.match_preference(ps, "f.travel", employer="Acme Corp")
    assert p.value == "employer-no", reason
    p2, _ = prefs.match_preference(ps, "f.travel", employer="Other Inc")
    assert p2.value == "global-yes"


def test_exceptions_beat_scope():
    ps = [prefs.Preference("f.travel", "global-yes",
                           exceptions=("employer:acme",))]
    p, reason = prefs.match_preference(ps, "f.travel", employer="Acme")
    assert p is None and reason == "no standing preference for family", reason


def test_expired_preference_fails_closed():
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    ps = [prefs.Preference("f.travel", "global-yes", expires_at=yesterday)]
    p, reason = prefs.match_preference(ps, "f.travel")
    assert p is None and "expired" in reason, reason
    # Unparseable expiry also fails closed.
    ps2 = [prefs.Preference("f.travel", "global-yes", expires_at="never")]
    assert prefs.is_expired(ps2[0]) is True


def test_trent_only_families_never_auto_resolve():
    assert "legal.arbitration" in prefs.TRENT_ONLY_FAMILIES
    assert "interview_recording.general" in prefs.TRENT_ONLY_FAMILIES
    assert "no_ai_unaided_writing" in prefs.TRENT_ONLY_FAMILIES


def test_blocker_consumes_preferences_module():
    # blocker.py does `from . import preferences`; this is the contract the
    # repair depended on.
    assert blocker.prefs_mod is prefs
    fam = "legal.arbitration"
    assert fam in blocker.prefs_mod.TRENT_ONLY_FAMILIES
