"""Verification accounting tests for verify_retry (gap-plan item c).

Pure accounting only: every selected verification candidate ends a wave
as attempted with a typed result or skipped with a specific reason, and
the attempt records carry wave_id, per-candidate typed verdicts (including
cooldown-skipped and url_bearing_only-filtered candidates), the selection
policy revision, actual active processing time, evidence, and
next-eligible time.

No live scan, no network, no queue writes — targets the pure builder
(build_attempt_record) and the record-assembly helper
(assemble_attempt_record) only.

Run: python3 -m pytest tests/test_verify_wave_accounting.py
"""
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "engines"))
import verify_retry

PDT = ZoneInfo("America/Los_Angeles")

REQUIRED_FIELDS = {"attempt_id", "wave_id", "role_id", "queue",
                   "discovery_source", "ats", "posting_identity", "verdict",
                   "skip_reason", "active_ms", "evidence_detail",
                   "next_eligible_at", "selection_policy"}


def _entry(**kw):
    e = {"role_id": "rid-1", "source": "greenhouse-enumerate",
         "ats": "greenhouse",
         "ats_url": "https://job-boards.greenhouse.io/acme/jobs/123"}
    e.update(kw)
    return e


def _run_dt():
    return datetime(2026, 9, 19, 6, 30, tzinfo=PDT)


# --- 1. builder emits every required field; verdict domain enforced ---------

@pytest.mark.parametrize("verdict", ["live", "dead", "ambiguous", "no_url",
                                     "skipped"])
def test_build_attempt_record_all_fields(verdict):
    rec = verify_retry.build_attempt_record(
        "rid-1", "standard", _entry(), verdict, "detail text", 250,
        "verify-20260919-063000")
    assert set(rec.keys()) == REQUIRED_FIELDS
    assert rec["wave_id"] == "verify-20260919-063000"
    assert rec["attempt_id"] == "verify-20260919-063000:rid-1"
    assert rec["role_id"] == "rid-1"
    assert rec["queue"] == "standard"
    assert rec["discovery_source"] == "greenhouse-enumerate"
    assert rec["ats"] == "greenhouse"
    assert rec["posting_identity"] == \
        "https://job-boards.greenhouse.io/acme/jobs/123"
    assert rec["verdict"] == verdict
    assert rec["skip_reason"] == "none"
    assert rec["active_ms"] == 250
    assert rec["evidence_detail"] == "detail text"
    assert rec["selection_policy"] == "window-cursor-v1"


def test_build_attempt_record_unknown_verdict_raises():
    # Documented fail-closed choice: an out-of-domain verdict raises
    # ValueError rather than being recorded as a real result.
    with pytest.raises(ValueError):
        verify_retry.build_attempt_record(
            "rid-1", "standard", _entry(), "maybe_live", "d", 0,
            "verify-20260919-063000")


def test_build_attempt_record_unknown_skip_reason_raises():
    with pytest.raises(ValueError):
        verify_retry.build_attempt_record(
            "rid-1", "standard", _entry(), "skipped", "d", 0,
            "verify-20260919-063000", skip_reason="nope")


def test_posting_url_prefers_ats_url():
    rec = verify_retry.build_attempt_record(
        "rid-2", "needs_input",
        _entry(ats_url="https://jobs.lever.co/acme/abc",
               application_url="https://other.example/j"), "live", "d", 1,
        "verify-20260919-063000")
    assert rec["posting_identity"] == "https://jobs.lever.co/acme/abc"


def test_posting_url_empty_when_url_less():
    rec = verify_retry.build_attempt_record(
        "rid-3", "standard", {"role_id": "rid-3"}, "skipped", "d", 0,
        "verify-20260919-063000", skip_reason="url_bearing_only_filter")
    assert rec["posting_identity"] == ""


# --- 2. cooldown-skipped candidate ------------------------------------------

def test_cooldown_skipped_record():
    last = (_run_dt() - timedelta(hours=2)).isoformat()
    rec = verify_retry.assemble_attempt_record(
        _entry(last_verify_attempt=last), "needs_input",
        "verify-20260919-063000", "skipped",
        "in verify cooldown — not scanned", 0, "in_cooldown", _run_dt())
    assert rec["verdict"] == "skipped"
    assert rec["skip_reason"] == "in_cooldown"
    assert rec["active_ms"] == 0
    # next eligible = last attempt + the verify cooldown (24h)
    expected = (datetime.fromisoformat(last) +
                timedelta(hours=verify_retry.VERIFY_COOLDOWN_H)).isoformat()
    assert rec["next_eligible_at"] == expected
    assert set(rec.keys()) == REQUIRED_FIELDS


def test_cooldown_skipped_bad_stamp_falls_back_to_run_ts():
    # Fail-open: an unparseable stamp never strands the candidate —
    # next eligibility is computed from this run instead.
    rec = verify_retry.assemble_attempt_record(
        _entry(last_verify_attempt="not-a-time"), "standard",
        "verify-20260919-063000", "skipped", "in cooldown", 0,
        "in_cooldown", _run_dt())
    expected = (_run_dt() +
                timedelta(hours=verify_retry.VERIFY_COOLDOWN_H)).isoformat()
    assert rec["next_eligible_at"] == expected


def test_missing_stamp_also_falls_back():
    rec = verify_retry.assemble_attempt_record(
        _entry(), "standard", "verify-20260919-063000", "skipped",
        "in cooldown", 0, "in_cooldown", _run_dt())
    expected = (_run_dt() +
                timedelta(hours=verify_retry.VERIFY_COOLDOWN_H)).isoformat()
    assert rec["next_eligible_at"] == expected


# --- 3. url_bearing_only filter ---------------------------------------------

def test_url_bearing_only_filter_record():
    rec = verify_retry.assemble_attempt_record(
        _entry(ats_url="", application_url=""), "standard",
        "verify-20260919-063000", "skipped",
        "url_bearing_only filter — no posting URL; enrichment skipped",
        0, "url_bearing_only_filter", _run_dt())
    assert rec["verdict"] == "skipped"
    assert rec["skip_reason"] == "url_bearing_only_filter"
    assert rec["next_eligible_at"] is None  # per-run filter, not a time park
    assert set(rec.keys()) == REQUIRED_FIELDS


# --- next_eligible_at semantics per verdict ----------------------------------

@pytest.mark.parametrize("verdict", ["ambiguous", "no_url"])
def test_parked_verdicts_get_next_eligible_from_this_attempt(verdict):
    rec = verify_retry.assemble_attempt_record(
        _entry(last_verify_attempt="2026-09-10T00:00:00-07:00"),
        "standard", "verify-20260919-063000", verdict,
        "stays parked", 1200, "none", _run_dt())
    expected = (_run_dt() +
                timedelta(hours=verify_retry.VERIFY_COOLDOWN_H)).isoformat()
    assert rec["next_eligible_at"] == expected


@pytest.mark.parametrize("verdict", ["live", "dead"])
def test_resolved_verdicts_have_no_next_eligible(verdict):
    rec = verify_retry.assemble_attempt_record(
        _entry(), "standard", "verify-20260919-063000", verdict,
        "resolved", 1200, "none", _run_dt())
    assert rec["next_eligible_at"] is None


# --- 5. JSONL-safe round-trip ------------------------------------------------

def test_attempts_list_round_trips_through_json():
    attempts = [
        verify_retry.assemble_attempt_record(
            _entry(), "standard", "verify-20260919-063000", "live",
            "greenhouse: Engineer", 312, "none", _run_dt()),
        verify_retry.assemble_attempt_record(
            _entry(last_verify_attempt=(
                _run_dt() - timedelta(hours=1)).isoformat()),
            "needs_input", "verify-20260919-063000", "skipped",
            "in verify cooldown — not scanned", 0, "in_cooldown",
            _run_dt()),
        verify_retry.assemble_attempt_record(
            _entry(ats_url=""), "standard", "verify-20260919-063000",
            "no_url", "NO URL — stays parked", 640, "none", _run_dt()),
    ]
    line = json.dumps({"status": "scanned", "attempts": attempts})
    back = json.loads(line)
    assert back["attempts"] == attempts
    assert [a["verdict"] for a in back["attempts"]] == \
        ["live", "skipped", "no_url"]
    assert all(set(a.keys()) == REQUIRED_FIELDS for a in back["attempts"])


# --- entry is never mutated ---------------------------------------------------

def test_builder_does_not_mutate_entry():
    entry = _entry()
    snapshot = dict(entry)
    verify_retry.assemble_attempt_record(
        entry, "standard", "verify-20260919-063000", "ambiguous",
        "stays parked", 100, "none", _run_dt())
    assert entry == snapshot
