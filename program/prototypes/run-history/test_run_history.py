"""Tests for run_history.py (prototype, G-1/B-22).

Covers the B-22 acceptance scenarios:
 1. history-accuracy: replay of fabricated telemetry matches exactly
 2. zero-activity run renders honestly, is not omitted
 3. ambiguous submissions never counted as confirmed
 4. pause/resume cannot double-count an attempt (F22 identity)
 5. unknown/malformed events are surfaced, never silently dropped
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_history import build_run_history  # noqa: E402


def ev(t, type, run_id, **kw):
    return {"t": t, "type": type, "run_id": run_id, **kw}


def test_happy_path_exact_rows():
    events = [
        ev("2026-09-19T03:00:00", "run_started", "r1", name="nightly"),
        ev("2026-09-19T03:01:00", "scan_completed", "r1", leads_promoted=4),
        ev("2026-09-19T03:02:00", "attempt_recorded", "r1", attempt_id="a1", verdict="confirmed"),
        ev("2026-09-19T03:03:00", "attempt_recorded", "r1", attempt_id="a2", verdict="confirmed"),
        ev("2026-09-19T03:04:00", "lead_parked", "r1", reason="essay"),
        ev("2026-09-19T03:05:00", "run_ended", "r1", outcome="completed"),
    ]
    rows = build_run_history(events)
    assert len(rows) == 1
    r = rows[0]
    assert r["run_id"] == "r1"
    assert r["status"] == "COMPLETED"
    assert r["scans_completed"] == 1
    assert r["leads_promoted"] == 4
    assert r["attempts_confirmed"] == 2
    assert r["attempts_ambiguous"] == 0
    assert r["leads_parked"] == 1
    assert r["honesty_note"] == ""
    print("ok test_happy_path_exact_rows")


def test_zero_activity_run_renders_honestly():
    events = [
        ev("2026-09-19T03:00:00", "run_started", "r2", name="dry-run"),
        ev("2026-09-19T03:05:00", "run_ended", "r2", outcome="completed"),
    ]
    rows = build_run_history(events)
    assert len(rows) == 1  # not omitted
    r = rows[0]
    assert r["leads_promoted"] == 0
    assert r["attempts_confirmed"] == 0
    assert r["honesty_note"] == "0 promotions this run — no activity recorded"
    print("ok test_zero_activity_run_renders_honestly")


def test_ambiguous_never_counts_as_confirmed():
    events = [
        ev("2026-09-19T03:00:00", "run_started", "r3"),
        ev("2026-09-19T03:01:00", "attempt_recorded", "r3", attempt_id="a1", verdict="ambiguous"),
        ev("2026-09-19T03:02:00", "attempt_recorded", "r3", attempt_id="a2", verdict="failed"),
        ev("2026-09-19T03:03:00", "run_ended", "r3", outcome="completed"),
    ]
    rows = build_run_history(events)
    r = rows[0]
    assert r["attempts_confirmed"] == 0
    assert r["attempts_ambiguous"] == 1
    assert r["attempts_failed"] == 1
    print("ok test_ambiguous_never_counts_as_confirmed")


def test_pause_resume_no_double_count():
    events = [
        ev("2026-09-19T03:00:00", "run_started", "r4"),
        ev("2026-09-19T03:01:00", "attempt_recorded", "r4", attempt_id="a1", verdict="confirmed"),
        ev("2026-09-19T03:02:00", "run_paused", "r4"),
        # replay/emission of the same attempt after resume — must count once
        ev("2026-09-19T03:10:00", "attempt_recorded", "r4", attempt_id="a1", verdict="confirmed"),
        ev("2026-09-19T03:11:00", "run_resumed", "r4"),
        ev("2026-09-19T03:12:00", "attempt_recorded", "r4", attempt_id="a2", verdict="confirmed"),
        ev("2026-09-19T03:15:00", "run_ended", "r4", outcome="completed"),
    ]
    rows = build_run_history(events)
    r = rows[0]
    assert r["attempts_confirmed"] == 2, r
    print("ok test_pause_resume_no_double_count")


def test_unknown_and_malformed_events_surfaced():
    events = [
        ev("2026-09-19T03:00:00", "run_started", "r5"),
        ev("2026-09-19T03:01:00", "something_weird", "r5"),
        ev("2026-09-19T03:02:00", "attempt_recorded", "r5", attempt_id=None, verdict="confirmed"),
        ev("2026-09-19T03:03:00", "attempt_recorded", "r5", attempt_id="a9", verdict="maybe"),
        ev("2026-09-19T03:04:00", "run_ended", "r5", outcome="completed"),
    ]
    rows = build_run_history(events)
    r = rows[0]
    assert r["unmapped_events"] == 3, r  # surfaced, not dropped
    assert r["attempts_confirmed"] == 0  # malformed attempts never counted
    print("ok test_unknown_and_malformed_events_surfaced")


def test_multiple_runs_isolated():
    events = [
        ev("2026-09-19T03:00:00", "run_started", "r6"),
        ev("2026-09-19T03:01:00", "scan_completed", "r6", leads_promoted=2),
        ev("2026-09-19T03:05:00", "run_ended", "r6", outcome="completed"),
        ev("2026-09-19T04:00:00", "run_started", "r7"),
        ev("2026-09-19T04:05:00", "run_ended", "r7", outcome="aborted"),
    ]
    rows = {r["run_id"]: r for r in build_run_history(events)}
    assert rows["r6"]["leads_promoted"] == 2
    assert rows["r6"]["status"] == "COMPLETED"
    assert rows["r7"]["status"] == "ABORTED"
    assert rows["r7"]["honesty_note"] != ""
    print("ok test_multiple_runs_isolated")


if __name__ == "__main__":
    test_happy_path_exact_rows()
    test_zero_activity_run_renders_honestly()
    test_ambiguous_never_counts_as_confirmed()
    test_pause_resume_no_double_count()
    test_unknown_and_malformed_events_surfaced()
    test_multiple_runs_isolated()
    print("ALL 6 TESTS PASSED")
