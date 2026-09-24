#!/usr/bin/env python3
"""Silent-defect sweep (2026-09-19) — rate_limits.py contracts.

FIX 1 (rate_limits.py:88): count_used read ledger.get("applications", [])
while every sibling reader uses d.get("rows", d.get("applications", [])).
A canonical {"rows": [...]} ledger was read as empty -> count_used 0 ->
over-budget submissions silently permitted. Fixed to
ledger.get("rows", ledger.get("applications", [])).

FIX 2 (rate_limits.py:60-66): the "T" not in str(val) check misfired on the
"T" inside "PDT", so the documented "2026-09-14 19:03 PDT" form took the
ISO branch, raised, and the submission counted as undated. Fixed to test
the separator only in the date portion ("T" not in str(val).split(" ")[0]).

All fixtures synthetic; no production state touched.
"""

import json
import os
import sys
from datetime import date

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KEEL_DIR, "engines"))

import rate_limits  # noqa: E402

TODAY = date(2026, 9, 19)


def _write_ledger(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f)


def test_count_used_reads_rows_key(tmp_path):
    """Mirrors the red assertion in test_extended_regressions.py
    FinalBoundaryTests::test_budget_counts_wrapped_claims_and_ignores_similar_employer_name
    (do NOT touch that file): a {"rows": [...]} ledger must be counted."""
    ledger = str(tmp_path / "ledger.json")
    _write_ledger(ledger, {"rows": [
        {"role_id": "R", "company": "Fixture", "status": "SUBMISSION_CLAIMED"},
        {"role_id": "X", "company": "Not Fixture", "status": "SUBMITTED"},
    ]})
    assert rate_limits.count_used("Fixture", ledger, today=TODAY) == 1


def test_count_used_ignores_claimed_and_wrong_employer(tmp_path):
    ledger = str(tmp_path / "ledger.json")
    _write_ledger(ledger, {"rows": [
        {"role_id": "A", "company": "Fixture", "status": "SUBMITTED",
         "date_submitted": "2026-09-01"},
        {"role_id": "B", "company": "OtherCo", "status": "SUBMITTED",
         "date_submitted": "2026-09-01"},
    ]})
    assert rate_limits.count_used("Fixture", ledger, today=TODAY) == 1


def test_count_used_applications_key_still_supported(tmp_path):
    """The legacy {"applications": [...]} shape keeps working as fallback."""
    ledger = str(tmp_path / "ledger.json")
    _write_ledger(ledger, {"applications": [
        {"role_id": "A", "company": "Fixture", "status": "SUBMITTED",
         "date_submitted": "2026-09-01"},
    ]})
    assert rate_limits.count_used("Fixture", ledger, today=TODAY) == 1


def test_submitted_date_pdt_format():
    """The documented "2026-09-14 19:03 PDT" form must parse to its date."""
    assert rate_limits._submitted_date(
        {"submitted_at": "2026-09-14 19:03 PDT"}) == date(2026, 9, 14)


def test_submitted_date_iso_still_works():
    assert rate_limits._submitted_date(
        {"submitted_at": "2026-09-14T19:51:23-07:00"}) == date(2026, 9, 14)


def test_submitted_date_plain_ymd():
    assert rate_limits._submitted_date(
        {"date_submitted": "2026-09-01"}) == date(2026, 9, 1)


def test_count_used_counts_pdt_dated_row_in_window(tmp_path):
    """End-to-end: a PDT-timestamped SUBMITTED row inside the window counts."""
    ledger = str(tmp_path / "ledger.json")
    _write_ledger(ledger, {"rows": [
        {"role_id": "A", "company": "Fixture", "status": "SUBMITTED",
         "submitted_at": "2026-09-14 19:03 PDT"},
    ]})
    assert rate_limits.count_used("Fixture", ledger, today=TODAY,
                                  window_days=180) == 1
