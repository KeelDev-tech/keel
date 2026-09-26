"""Regression tests for title_triage.py.

Covers: the TRIAGE_HITS keep-family vocabulary (incl. the 2026-09-20
implementation-family addition, ARM 569-2 / blackboard
J-20260920-0232-meth-3340), the excluded-keyword fail-safe ordering
(HITS before MISS), the location-signal gate, the pre-staging choke-point
gate sharing the same regexes, and deferral audit annotation.
"""
import os
import sys

import pytest

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines")
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import title_triage as tt  # noqa: E402


# The 11 genuine on-lane roles rejected in the 24h before the 2026-09-20 fix
# (measured: 12/174 title-family-miss rejects in the 2026-09-19 LinkedIn
# staging sweep were implementation-family, 11 genuine). Every one of these
# was deferred with reason title-family-miss before the implementation token.
IMPLEMENTATION_FAMILY_TITLES = [
    "Implementation Manager, Global Payroll (NAMER)",   # Rippling
    "Manager, Implementation (Benefits)",              # Rippling
    "Strategic Implementation Manager",                # DailyPay
    "Senior Mid-Market Implementation Manager",        # Canary
    "Implementation Manager",                          # Courier Health
    "Implementation Manager",                          # Exiger
    "Manager, Implementation",                         # FIS
    "Manager, Implementation",                         # Profound
    "Implementation Manager - Moveworks",               # Moveworks (TalentHop)
    "Implementation Manager",                          # PreSales Collective
    "Implementation Manager",                          # RemoteHunter
]


@pytest.mark.parametrize("title", IMPLEMENTATION_FAMILY_TITLES)
def test_implementation_family_kept(title):
    decision, reason = tt.triage_lead(title, "Remote")
    assert decision == "keep"
    assert reason == tt.REASON_KEEP


@pytest.mark.parametrize("title", IMPLEMENTATION_FAMILY_TITLES)
def test_implementation_family_kept_at_staging_gate(title):
    keep, reason = tt.pre_staging_title_gate(title, "Remote")
    assert keep is True
    assert reason == tt.REASON_KEEP


def test_implementation_engineer_still_deferred_by_excluded_keyword():
    # HITS runs before MISS: the engineering title enters on the
    # implementation token but must still defer on excluded-keyword.
    decision, reason = tt.triage_lead("Implementation Engineer", "Remote")
    assert decision == "defer"
    assert reason == tt.REASON_EXCLUDED_KEYWORD
    keep, reason = tt.pre_staging_title_gate("Implementation Engineer",
                                             "Remote")
    assert keep is False
    assert reason == tt.REASON_EXCLUDED_KEYWORD


def test_offlane_control_still_defers_title_family_miss():
    decision, reason = tt.triage_lead("Cashier", "Remote")
    assert decision == "defer"
    assert reason == tt.REASON_TITLE_FAMILY_MISS


def test_location_gate_still_applies_to_implementation_titles():
    # Implementation titles with no keep-signal location still defer on
    # location (empty location is an emitter defect only when an
    # evidence-scored fit>=75 is already in hand).
    decision, reason = tt.triage_lead("Implementation Manager", "")
    assert decision == "defer"
    assert reason == tt.REASON_LOCATION
    decision, reason = tt.triage_lead("Implementation Manager", "", fit_score=88)
    assert decision == "keep"
    assert reason == tt.REASON_KEEP


def test_annotate_deferred_stamps_reason_and_status():
    entry = {"title": "Cashier", "status": "PARKED-PENDING-VERIFICATION"}
    out = tt.annotate_deferred(entry, tt.REASON_TITLE_FAMILY_MISS,
                               "2026-09-20T03:00:00Z")
    assert out["status"] == tt.TRIAGE_DEFERRED_STATUS
    assert out["action_band"] == "PARKED"
    assert out["triage_reason"] == tt.REASON_TITLE_FAMILY_MISS
    assert tt.REASON_TITLE_FAMILY_MISS in out["queue_notes"]
