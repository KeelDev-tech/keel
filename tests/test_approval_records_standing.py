"""Regression tests: standing records are grounded authority, never examples.

Covers the 2026-09-19 repair (prompted by the Keel 0.2.0 external audit's
approval-truth quarantine): the keel approval_records module previously
minted three EXAMPLE standing records at import — fabricated evidence text
("Operator standing directive (EXAMPLE)...", fake 2026-09-18 timestamps)
that any presenter could use to pass validation. The defs are now grounded
in Trent's recorded directives; _build_standing() refuses example text.
"""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engines"))
import approval_records
from approval_records import (
    _STANDING_DEFS,
    _build_standing,
    _STANDING,
    issue_record,
    standing_names,
    standing_record,
    validate_record,
)


def test_no_example_names_survive():
    names = standing_names()
    assert names == ["account_creation", "comp_bands", "form_attestations"]
    assert not any(n.startswith("example_") for n in names)


def test_no_example_evidence_text():
    for d in _STANDING_DEFS:
        text = d["source_event"]["text"]
        assert "EXAMPLE" not in text.upper()
        assert "placeholder" not in text.lower()
        assert d["source_event"].get("ref")
        assert "(example)" not in d["source_event"]["ref"].lower()


def test_evidence_dates_are_real_directive_dates():
    ats = {d["name"]: d["source_event"]["at"] for d in _STANDING_DEFS}
    # The fabricated 2026-09-18T00:00:00+00:00 stamp must be gone.
    assert all(at != "2026-09-18T00:00:00+00:00" for at in ats.values())
    assert ats["form_attestations"].startswith("2026-09-16")
    assert ats["account_creation"].startswith("2026-09-14")
    assert ats["comp_bands"].startswith("2026-09-13")


def test_scopes_not_widened():
    rec = standing_record("form_attestations")
    assert rec["scope"]["action"] == "form_attestation"
    assert rec["scope"]["keys"] == [
        "arbitration_agreement", "background_check_consent",
        "at_will_acknowledgment", "information_truthfulness_attestation",
        "data_privacy_consent"]
    rec = standing_record("account_creation")
    assert rec["scope"] == {"action": "account_creation",
                           "for": "job_applications"}
    rec = standing_record("comp_bands")
    assert rec["scope"] == {"action": "answer_compensation",
                           "rule": "market_median_midpoint"}


def test_standing_record_returns_copy():
    rec = standing_record("comp_bands")
    rec["scope"]["rule"] = "top_1_percent"
    assert standing_record("comp_bands")["scope"]["rule"] == \
        "market_median_midpoint"


def test_unknown_standing_name_fails_closed():
    with pytest.raises(KeyError):
        standing_record("example_form_attestations")
    with pytest.raises(KeyError):
        standing_record("nope")


def test_grant_still_enforces_scope():
    rec = standing_record("form_attestations")
    ok, _ = validate_record(rec, {"action": "form_attestation",
                                  "keys": rec["scope"]["keys"]})
    assert ok is True
    ok, reason = validate_record(rec, {"action": "form_attestation",
                                       "keys": ["no_ai_attestation"]})
    assert ok is False
    ok, _ = validate_record(rec, {"action": "account_creation"})
    assert ok is False


def test_build_standing_refuses_example_text():
    bad = copy.deepcopy(_STANDING_DEFS[0])
    bad["name"] = "bad_example"
    bad["source_event"]["text"] = "Operator standing directive (EXAMPLE): ..."
    saved_defs, saved = approval_records._STANDING_DEFS, dict(_STANDING)
    try:
        approval_records._STANDING_DEFS = [bad]
        _STANDING.clear()
        with pytest.raises(ValueError, match="example/placeholder"):
            _build_standing()
    finally:
        approval_records._STANDING_DEFS = saved_defs
        _STANDING.clear()
        _STANDING.update(saved)


def test_build_standing_refuses_missing_ref():
    bad = copy.deepcopy(_STANDING_DEFS[0])
    bad["name"] = "bad_no_ref"
    bad["source_event"]["ref"] = ""
    saved_defs, saved = approval_records._STANDING_DEFS, dict(_STANDING)
    try:
        approval_records._STANDING_DEFS = [bad]
        _STANDING.clear()
        with pytest.raises(ValueError, match="no evidence ref"):
            _build_standing()
    finally:
        approval_records._STANDING_DEFS = saved_defs
        _STANDING.clear()
        _STANDING.update(saved)
