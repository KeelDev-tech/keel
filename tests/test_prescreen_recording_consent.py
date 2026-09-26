#!/usr/bin/env python3
"""Silent-defect sweep (2026-09-19) — prescreen._primary_gate recording-consent clause.

FIX 5: ported ONLY the ARM-72 clause from the live tree's
job-pipeline/engines/application-executor/prescreen.py::_primary_gate
(the "eligibility"/"3-month" clause and everything else were NOT ported).
Rationale: keel's engines/record_outcome.py:237-238 already expects
"recording_consent" from _canonical_primary_gate, and keel's GATE_TYPES
already contains it — but keel's prescreen could never produce it, so
recording-consent parks were silently bucketed as technique_blocked.

The check goes FIRST (as live does); all existing clause order/behavior
is otherwise unchanged. Fixtures synthetic.
"""

import os
import sys

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KEEL_DIR, "engines"))

from prescreen import _primary_gate  # noqa: E402
import record_outcome  # noqa: E402


def test_recording_consent_classified():
    # The ARM-72 clause matches the literal phrase "recording consent"
    # (verbatim port of the live tree's clause); live reason strings are
    # phrased with that exact phrase.
    assert _primary_gate(
        ["Required interview recording consent needs the applicant's "
         "explicit decision (recording consent)"]) == "recording_consent"


def test_recording_consent_first_even_with_other_signals():
    # Live behavior: the clause is checked before essay/travel/attest.
    assert _primary_gate(["recording consent required",
                          "essay required"]) == "recording_consent"


def test_word_order_variant_does_not_match_live_clause():
    # "applicant must consent to interview recording" does NOT contain the
    # literal "recording consent" phrase, so the verbatim-ported live
    # clause does not fire on it — same as the live tree.
    assert _primary_gate(
        ["applicant must consent to interview recording"]) == "needs_input"


def test_existing_essay_behavior_unchanged():
    assert _primary_gate(["essay required"]) == "essay"


def test_existing_travel_behavior_unchanged():
    assert _primary_gate(["relocation required"]) == "travel"


def test_existing_attest_behavior_unchanged():
    assert _primary_gate(["arbitration agreement"]) == "attest"


def test_fallthrough_unchanged():
    assert _primary_gate(["some other reason"]) == "needs_input"


def test_record_outcome_end_to_end_contract():
    """record_outcome's _canonical_primary_gate is prescreen._primary_gate;
    the recording-consent gate value must now be producible end to end."""
    assert record_outcome._canonical_primary_gate is not None
    assert record_outcome._canonical_primary_gate(
        ["Required interview recording consent (recording consent)"
         ]) == "recording_consent"
