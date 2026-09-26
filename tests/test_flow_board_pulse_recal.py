#!/usr/bin/env python3
"""Regression tests for flow_board_pulse.py conversion recalibration (ARM 72).

All fixtures are synthetic in-memory telemetry rows -- no files, no network,
no production state. Fail-safe degradations are pinned: nothing is invented
when evidence is absent, and the 7d window must replicate the exporter's flat
input exactly (ARM 70: 557/3838 = 0.14512767066180302).

Fixture rows match the real telemetry schema: top-level ts / source /
event_type, payload in details.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PULSE_PATH = os.path.join(KEEL_DIR, "flow_board_pulse.py")


def load_pulse():
    spec = importlib.util.spec_from_file_location(
        "flow_board_pulse_recal_test", PULSE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flow_board_pulse_recal_test"] = mod
    spec.loader.exec_module(mod)
    return mod


P = load_pulse()
NOW = datetime(2026, 9, 18, 17, 30, tzinfo=timezone.utc)


def scan_row(ts, scanned, promoted, synthetic=False):
    d = {"scanned": scanned, "promoted_ready": promoted}
    if synthetic:
        d["synthetic_test"] = True
    return {"event_type": "scan_summary", "source": "verify-retry",
            "ts": ts.isoformat(), "details": d}


def role_row(event_type, role_id, ts, source="verify-retry"):
    return {"event_type": event_type, "source": source,
            "role_id": role_id, "ts": ts.isoformat(), "details": {}}


def mat_row(event_type, role_id, ts, gate="materials_missing"):
    """materials-owner lane-assignment rows: same per-role schema, but a
    different source and a lane-abstain gate -- NOT a verify verdict."""
    return {"event_type": event_type, "source": "materials-owner",
            "role_id": role_id, "ts": ts.isoformat(),
            "details": {"gate": gate} if gate else {}}


# --- 7d flat input replication ------------------------------------------------

def test_flat_7d_replicates_exporter_exactly():
    # ARM 70: 433 batches summed to 3838 scanned / 557 promoted -> 0.145127...
    rows = []
    for i in range(433):
        rows.append(scan_row(NOW - timedelta(days=6, hours=i % 10),
                             scanned=8, promoted=1))
    rows.append(scan_row(NOW - timedelta(hours=1), scanned=374, promoted=124))
    # totals: scanned = 433*8+374 = 3838; promoted = 433+124 = 557
    w = P.window_ratio(P.scan_batches_from_rows(rows, NOW), NOW, 7 * 24)
    assert w["scanned"] == 3838
    assert w["promoted"] == 557
    assert w["conversion"] == pytest.approx(0.14512767066180302)
    assert w["waves"] == 434
    assert not w["low_confidence"]


def test_synthetic_batches_excluded():
    rows = [scan_row(NOW - timedelta(hours=1), scanned=100, promoted=50),
            scan_row(NOW - timedelta(hours=2), scanned=100, promoted=50,
                     synthetic=True)]
    rows[1] = {**rows[1]}  # keep
    kept = [r for r in rows if not (r.get("details") or {}).get("synthetic_test")]
    w = P.window_ratio(P.scan_batches_from_rows(kept, NOW), NOW, 7 * 24)
    assert w["conversion"] == pytest.approx(0.5)


def test_future_batches_not_counted():
    rows = [scan_row(NOW - timedelta(hours=1), scanned=100, promoted=20),
            scan_row(NOW + timedelta(hours=1), scanned=100, promoted=100)]
    w = P.window_ratio(P.scan_batches_from_rows(rows, NOW), NOW, 7 * 24)
    assert w["conversion"] == pytest.approx(0.2)


# --- windowed ratios ----------------------------------------------------------

def test_windowed_24h_12h_ratios():
    # recent regime collapse: bulk zero waves on top of healthy history
    rows = []
    for d in (5, 4, 3, 2):  # healthy history
        rows.append(scan_row(NOW - timedelta(days=d), scanned=100, promoted=30))
    rows.append(scan_row(NOW - timedelta(hours=20), scanned=200, promoted=0))
    rows.append(scan_row(NOW - timedelta(hours=8), scanned=100, promoted=0))
    batches = P.scan_batches_from_rows(rows, NOW)
    w24 = P.window_ratio(batches, NOW, 24)
    assert w24["scanned"] == 300 and w24["promoted"] == 0
    assert w24["conversion"] == pytest.approx(0.0)
    w12 = P.window_ratio(batches, NOW, 12)
    assert w12["scanned"] == 100 and w12["promoted"] == 0
    w7 = P.window_ratio(batches, NOW, 7 * 24)
    assert w7["conversion"] == pytest.approx(120 / 700)


def test_low_confidence_below_min_waves():
    rows = [scan_row(NOW - timedelta(hours=1), scanned=100, promoted=40),
            scan_row(NOW - timedelta(hours=2), scanned=100, promoted=40)]
    w = P.window_ratio(P.scan_batches_from_rows(rows, NOW), NOW, 24)
    assert w["low_confidence"] and w["waves"] == 2
    assert w["conversion"] == pytest.approx(0.4)  # ratio still reported


# --- EWMA ---------------------------------------------------------------------

def test_ewma_tracks_regime_shift_better_than_flat():
    rows = []
    for d in range(6, 1, -1):  # healthy past, 0.3 each
        rows.append(scan_row(NOW - timedelta(days=d), scanned=100, promoted=30))
    for h in (10, 8, 6, 4, 2):  # collapsed recent, 0.0 each
        rows.append(scan_row(NOW - timedelta(hours=h), scanned=100, promoted=0))
    batches = P.scan_batches_from_rows(rows, NOW)
    flat = P.window_ratio(batches, NOW, 7 * 24)["conversion"]
    ewma = P.ewma_conversion(batches)
    assert flat == pytest.approx(150 / 1000)          # 0.15
    assert ewma is not None and 0.0 <= ewma <= 1.0
    assert ewma < flat  # recent zero waves pull the EWMA below the flat mean


def test_ewma_none_without_scans():
    assert P.ewma_conversion([]) is None
    rows = [scan_row(NOW - timedelta(hours=1), scanned=0, promoted=0)]
    assert P.ewma_conversion(P.scan_batches_from_rows(rows, NOW)) is None


# --- range emission + backward compatibility -----------------------------------

def test_expected_range_and_legacy_untouched():
    rows = [scan_row(NOW - timedelta(hours=h), scanned=100, promoted=p)
            for h, p in ((30, 30), (20, 10), (10, 5), (5, 0), (1, 0))]
    releases = [{"release_id": "cooldown-released", "count": 673,
                 "expected": 97, "estimated_conversion": 0.1451}]
    role_ids = {"cooldown-released": ["r1", "r2"]}
    block = P.recalibrate_releases(releases, role_ids, rows, NOW)
    assert block["ok"] and block["flat_7d_replaced"] is True
    rel = block["releases"][0]
    assert rel["expected_legacy"] == 97          # board point untouched
    rng = rel["expected_range"]
    assert rng["lo"] <= rng["mid"] <= rng["hi"]
    assert rng["lo"] < rng["hi"]                  # spread is the signal
    # conv-recal-v2 (J-20260918-1940-veri-2258): this fixture's windows are
    # monotonically decaying (7d=0.09 -> 24h=0.0375 -> 12h=0.0167), so the
    # mid is capped at min(ewma, 24h) instead of the raw EWMA
    assert rng["regime"] == "monotonic_decay"
    assert rel["expected_ewma"] == 103           # raw EWMA still reported
    assert rng["mid"] == min(rel["expected_ewma"], int(673 * 0.0375))
    assert rng["mid"] < rel["expected_ewma"]
    assert set(rel["conversions_used"]) >= {"7d", "24h", "12h", "ewma"}


# --- fail-safe degradations ----------------------------------------------------

def test_empty_scan_input_is_fail_safe():
    rows = [role_row("lead_verified", "r1", NOW - timedelta(hours=1))]
    releases = [{"release_id": "x", "count": 10, "expected": 1,
                 "estimated_conversion": 0.1}]
    block = P.recalibrate_releases(releases, {}, rows, NOW)
    assert block["ok"] and block["input_available"] is False
    assert block["releases"] == []
    assert block["fail_safe"]["degraded"] is True
    assert block["fail_safe"]["reason"] == "no_scan_batches_7d"


def test_stale_input_falls_back_to_7d():
    # last wave is 3h old: 24h/12h/ewma inputs are stale
    rows = [scan_row(NOW - timedelta(hours=3), scanned=100, promoted=10),
            scan_row(NOW - timedelta(hours=4), scanned=100, promoted=10)]
    releases = [{"release_id": "x", "count": 100, "expected": 10,
                 "estimated_conversion": 0.1}]
    block = P.recalibrate_releases(releases, {}, rows, NOW)
    assert block["telemetry_stale"] is True
    assert block["fail_safe"]["degraded"] is True
    rel = block["releases"][0]
    # mid falls back to the widest-evidence 7d window, never invented
    assert rel["expected_range"]["mid"] == int(100 * block["windows"]["7d"]
                                              ["conversion"])
    assert block["fail_safe"]["primary"].startswith("7d flat")


def test_fresh_input_uses_ewma_primary():
    rows = [scan_row(NOW - timedelta(minutes=m), scanned=100, promoted=p)
            for m, p in ((50, 10), (40, 10), (30, 10), (20, 10), (10, 10),
                         (5, 10))]
    releases = [{"release_id": "x", "count": 100, "expected": 10,
                 "estimated_conversion": 0.1}]
    block = P.recalibrate_releases(releases, {}, rows, NOW)
    assert block["telemetry_stale"] is False
    rel = block["releases"][0]
    assert rel["expected_range"]["mid"] == rel["expected_ewma"]
    assert block["fail_safe"]["degraded"] is False


# --- export-time cohort join ----------------------------------------------------

def test_cohort_join_coverage_and_counts():
    role_ids = ["a", "b", "c", "d"]
    rows = [role_row("lead_verified", "a", NOW - timedelta(hours=2)),
            role_row("gate_blocked", "b", NOW - timedelta(hours=3)),
            role_row("lead_dead", "c", NOW - timedelta(days=5)),  # outside 24h
            role_row("lead_verified", "a", NOW - timedelta(hours=1))]
    j = P.cohort_join(role_ids, rows, NOW, window_h=24)
    assert j["cohort_size"] == 4
    assert j["observed_any"] == 2            # a and b only; c is out of window
    assert j["coverage"] == pytest.approx(0.5)
    assert j["observed_verified"] == 1       # per-role dedupe
    assert j["observed_gate_blocked"] == 1
    assert j["observed_dead"] == 0
    # J-20260918-1922-veri-2248: per-role verify telemetry now covers every
    # scanned lead (verify_attempt rows for the silent verdict classes), so
    # coverage<1.0 means unscanned supply, not verdict silence. The old
    # "ambiguous/no_url" note was rewritten with that finding; the assertion
    # tracks the current honest wording.
    assert "unscanned" in j["note"] and "verdict silence" in j["note"]


def test_cohort_join_empty_cohort():
    j = P.cohort_join([], [], NOW)
    assert j["cohort_size"] == 0 and j["coverage"] == 0.0


# --- P1 source filter (pulse 806 ARM 05) --------------------------------------

def test_cohort_join_materials_blocked_not_verify_outcome():
    # mixed cohort: verify-retry verdicts + materials-owner abstains.
    # outcome counts must admit ONLY verify-retry events; materials
    # rows land in observed_materials_blocked with per-member semantics.
    role_ids = ["v1", "v2", "m1", "m2", "u1"]
    rows = [role_row("lead_verified", "v1", NOW - timedelta(hours=1)),
            role_row("gate_blocked", "v2", NOW - timedelta(hours=2)),
            mat_row("gate_blocked", "m1", NOW - timedelta(hours=3)),
            mat_row("gate_blocked", "m2", NOW - timedelta(hours=4)),
            mat_row("gate_blocked", "m2", NOW - timedelta(hours=5)),
            role_row("verify_attempt", "u1", NOW - timedelta(hours=6)),
            # impossible in prod, but proves the filter is strict on
            # source, not event type: a materials lead_verified never
            # counts as a verify outcome.
            mat_row("lead_verified", "m1", NOW - timedelta(hours=7))]
    j = P.cohort_join(role_ids, rows, NOW, window_h=24)
    assert j["observed_verified"] == 1       # v1 only
    assert j["observed_dead"] == 0
    assert j["observed_gate_blocked"] == 1  # v2 only
    assert j["observed_materials_blocked"] == 2   # m1, m2 (distinct)
    assert j["materials_blocked_gates"] == {"materials_missing": 2}
    assert j["observed_verify_any"] == 3     # v1, v2, u1
    assert j["coverage_verify"] == pytest.approx(0.6)
    assert j["observed_any"] == 5            # any-source observability kept
    assert j["coverage"] == 1.0


def test_cohort_join_materials_only_cohort():
    # pulse-805 shape: 13 materials-blocked, ZERO verify verdicts in
    # the window. The old join would have reported observed_gate_blocked
    # == 13 (misread by the digest as 13 failed verify gates).
    role_ids = [f"m{i}" for i in range(13)]
    rows = [mat_row("gate_blocked", rid, NOW - timedelta(hours=i % 5 + 1))
            for i, rid in enumerate(role_ids)]
    j = P.cohort_join(role_ids, rows, NOW, window_h=24)
    assert j["observed_verified"] == 0
    assert j["observed_dead"] == 0
    assert j["observed_gate_blocked"] == 0   # NOT 13
    assert j["observed_materials_blocked"] == 13
    assert j["materials_blocked_gates"] == {"materials_missing": 13}
    assert j["observed_verify_any"] == 0
    assert j["coverage_verify"] == 0.0


def test_cohort_join_verify_events_still_count():
    # verify-retry events keep their old counts under the source filter.
    role_ids = ["a", "b", "c"]
    rows = [role_row("lead_verified", "a", NOW - timedelta(hours=1)),
            role_row("lead_dead", "b", NOW - timedelta(hours=2)),
            role_row("verify_attempt", "c", NOW - timedelta(hours=3))]
    j = P.cohort_join(role_ids, rows, NOW, window_h=24)
    assert j["observed_verified"] == 1
    assert j["observed_dead"] == 1
    assert j["observed_gate_blocked"] == 0
    assert j["observed_materials_blocked"] == 0
    assert j["observed_verify_any"] == 3
    assert j["materials_blocked_gates"] == {}


def test_cohort_join_materials_gate_missing_defaults_unknown():
    # gate_blocked rows without a details.gate still count, labelled
    # "unknown" -- never dropped, never a verify outcome.
    j = P.cohort_join(["x"], [mat_row("gate_blocked", "x",
                                      NOW - timedelta(hours=1), gate=None)],
                      NOW, window_h=24)
    assert j["observed_gate_blocked"] == 0
    assert j["observed_materials_blocked"] == 1
    assert j["materials_blocked_gates"] == {"unknown": 1}


# --- replicates_exporter_7d sanity --------------------------------------------

def test_replicates_exporter_7d_flag():
    rows = [scan_row(NOW - timedelta(hours=1), scanned=1000, promoted=145)]
    releases = [{"release_id": "x", "count": 10, "expected": 1,
                 "estimated_conversion": 0.145}]
    block = P.recalibrate_releases(releases, {}, rows, NOW)
    assert block["replicates_exporter_7d"] is True


def test_replicates_exporter_7d_false_on_mismatch():
    rows = [scan_row(NOW - timedelta(hours=1), scanned=1000, promoted=145)]
    releases = [{"release_id": "x", "count": 10, "expected": 1,
                 "estimated_conversion": 0.9}]
    block = P.recalibrate_releases(releases, {}, rows, NOW)
    assert block["replicates_exporter_7d"] is False
