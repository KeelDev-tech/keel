#!/usr/bin/env python3
"""Regression tests for conv-recal-v4 (ARM 3, keel-octopus-pulse
2026-09-20, blackboard J-20260920-2244-veri-3960).

The defect: conv-recal-v3's unscored_skipped / fastpath_live_n
deductions ERASED REAL PROMOTIONS. The 2026-09-20 12:05/12:14 UTC
verify waves (scanned=17/20, unscored_skipped=83, fastpath_live_n=10,
promoted_ready=7/10 -- 17 real promotions) clamped to (0,0): the waves
vanished WITH their promotions, and the 12h conversion reading (0.000)
was partly a metric artifact.

v4 contract under test:
  1. A wave with real promotions is never clamped to zero
     (numerator>0 survives; the wave still enters windows/EWMA).
  2. The fastpath reuse cohort still contributes 0 -- it leaves the
     denominator and never adds to the numerator.
  3. The unscored_skipped subtraction applies only when
     schema-consistent (unscored <= scanned); a disjoint pool-level skip
     count is not subtracted from the batch.
  4. Legacy rows (no fastpath_live_n) degrade exactly to the v2 formula.
  5. The monotonic-decay regime reporting (conv-recal-v2) is intact:
     decay regime detected, forecast mid capped at min(ewma, 24h),
     range untouched.
  6. Per-batch invariant 0 <= promoted <= scanned always holds
     (ratios can never exceed 1 or go negative).

All fixtures are synthetic in-memory telemetry rows -- no files, no
network, no production state.
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
        "flow_board_pulse_recal_v4_test", PULSE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flow_board_pulse_recal_v4_test"] = mod
    spec.loader.exec_module(mod)
    return mod


P = load_pulse()
NOW = datetime(2026, 9, 20, 12, 30, tzinfo=timezone.utc)


def wave_row(ts, scanned, unscored=0, fastpath=0, promoted=0, legacy=False):
    """One scan_summary row. legacy=True omits the LP-E/LP-F field
    (pre-2026-09-18 producer rows)."""
    d = {"scanned": scanned, "promoted_ready": promoted,
         "unscored_skipped": unscored}
    if not legacy:
        d["fastpath_live_n"] = fastpath
    return {"event_type": "scan_summary", "source": "verify-retry",
            "ts": ts.isoformat(), "details": d}


def test_version_is_v4():
    assert P.RECALIBRATION_VERSION == "conv-recal-v4"


# --- 1. real-promotion waves survive (the 12:05/12:14 UTC defect) ---------

def test_real_promotion_waves_not_clamped_to_zero():
    # exact live shapes from 2026-09-20T12:05:51Z / 12:14:30Z
    rows = [
        wave_row(NOW - timedelta(minutes=25), scanned=17, unscored=83,
                 fastpath=10, promoted=7),
        wave_row(NOW - timedelta(minutes=16), scanned=20, unscored=83,
                 fastpath=10, promoted=10),
    ]
    batches = P.scan_batches_from_rows(rows, NOW)
    assert batches[0][1:] == (7, 7)   # v3: (0, 0) -- promotions erased
    assert batches[1][1:] == (10, 10)  # v3: (0, 0) -- promotions erased


def test_promoted_wave_enters_window_and_ewma():
    rows = [
        wave_row(NOW - timedelta(minutes=25), scanned=17, unscored=83,
                 fastpath=10, promoted=7),
        wave_row(NOW - timedelta(minutes=16), scanned=20, unscored=83,
                 fastpath=10, promoted=10),
    ]
    batches = P.scan_batches_from_rows(rows, NOW)
    w = P.window_ratio(batches, NOW, 12)
    assert w["scanned"] == 17 and w["promoted"] == 17
    assert w["conversion"] == pytest.approx(1.0)
    assert P.ewma_conversion(batches) == pytest.approx(1.0)


def test_numerator_positive_wave_never_yields_zero_scanned():
    # any batch whose attempted pool is NOT entirely the reuse cohort
    # keeps a positive scanned pool when it promotes -- the wave can
    # never vanish from windows/EWMA
    shapes = [(17, 83, 10, 7), (20, 83, 10, 10), (11, 83, 10, 1),
              (100, 0, 10, 3), (140, 100, 5, 4)]
    for scanned, unscored, fastpath, promoted in shapes:
        (ts, s, p), = P.scan_batches_from_rows(
            [wave_row(NOW - timedelta(hours=1), scanned=scanned,
                      unscored=unscored, fastpath=fastpath,
                      promoted=promoted)], NOW)
        assert s > 0 and p > 0


def test_all_reuse_attempted_pool_washes_out_cleanly():
    # attempted pool entirely consumed by the reuse cohort (attempted=10,
    # fastpath=10) with a producer-reported promotion: a contradiction
    # (reuse promotes at 0), so the batch washes out to (0,0) instead of
    # inventing a denominator -- the numerator cap is the principled
    # floor, documented here rather than silently trusted.
    (ts, s, p), = P.scan_batches_from_rows(
        [wave_row(NOW - timedelta(hours=1), scanned=48, unscored=38,
                  fastpath=10, promoted=2)], NOW)
    assert (s, p) == (0, 0)


# --- 2. the reuse cohort still contributes 0 -------------------------------

def test_reuse_cohort_waves_contribute_nothing():
    # the held-cohort wave shape (33 of the last 60 live waves):
    # 10 zero-HTTP re-verifications, 0 promotions possible
    rows = [wave_row(NOW - timedelta(hours=i), scanned=10, unscored=83,
                     fastpath=10, promoted=0) for i in range(33)]
    batches = P.scan_batches_from_rows(rows, NOW)
    assert all(s == 0 and p == 0 for _, s, p in batches)
    w = P.window_ratio(batches, NOW, 24)
    assert w["scanned"] == 0 and w["conversion"] is None
    assert P.ewma_conversion(batches) is None  # no scan evidence


def test_mixed_cohort_only_fresh_promotions_count():
    rows = [
        wave_row(NOW - timedelta(minutes=25), scanned=17, unscored=83,
                 fastpath=10, promoted=7),
        wave_row(NOW - timedelta(minutes=16), scanned=20, unscored=83,
                 fastpath=10, promoted=10),
    ] + [wave_row(NOW - timedelta(hours=i), scanned=10, unscored=83,
                  fastpath=10, promoted=0) for i in range(33)]
    batches = P.scan_batches_from_rows(rows, NOW)
    w = P.window_ratio(batches, NOW, 24)
    # 33 reuse waves add nothing; the 17 fresh promotions all count
    assert w["scanned"] == 17 and w["promoted"] == 17


def test_reuse_cohort_never_inflates_denominator():
    # a batch that is entirely reuse cohort washes out, not negative
    (ts, s, p), = P.scan_batches_from_rows(
        [wave_row(NOW - timedelta(hours=1), scanned=10, unscored=0,
                  fastpath=50, promoted=0)], NOW)
    assert (s, p) == (0, 0)


# --- 3. schema-conditional unscored subtraction ----------------------------

def test_unscored_subtracted_when_schema_consistent():
    # old-producer shape: unscored <= scanned -> v2 subtraction holds
    rows = [wave_row(NOW - timedelta(hours=1), scanned=140, unscored=100,
                     fastpath=5, promoted=4)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (35, 4)


def test_unscored_ignored_when_disjoint_from_batch():
    # live-producer shape: unscored is a pool-level skip count disjoint
    # from the batch; subtracting it would erase the batch (v3 defect)
    rows = [wave_row(NOW - timedelta(hours=1), scanned=10, unscored=83,
                     fastpath=0, promoted=0)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (10, 0)  # v3 clamped attempted to max(0, 10-83) = 0


# --- 4. legacy rows degrade to v2 ------------------------------------------

def test_legacy_row_degrades_to_v2_exactly():
    rows = [wave_row(NOW - timedelta(hours=1), scanned=140, unscored=100,
                     promoted=4, legacy=True)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (40, 4)
    rows = [wave_row(NOW - timedelta(hours=1), scanned=5, promoted=0,
                     legacy=True)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (5, 0)


# --- 5. monotonic-decay regime reporting intact ----------------------------

def decay_wave_set():
    rows = []
    for d in (6, 5.9, 5.8, 5.7):  # healthy past: 50%
        rows.append(wave_row(NOW - timedelta(days=d), scanned=200,
                             promoted=100))
    for h in (20, 19):            # recent: 5%
        rows.append(wave_row(NOW - timedelta(hours=h), scanned=200,
                             promoted=10))
    for m in (90, 60):            # fresh (inside the 120-min stale bound): 0%
        rows.append(wave_row(NOW - timedelta(minutes=m), scanned=200,
                             promoted=0))
    return rows


def test_monotonic_decay_regime_detected_and_capped():
    rows = decay_wave_set()
    batches = P.scan_batches_from_rows(rows, NOW)
    windows = {name: P.window_ratio(batches, NOW, h)
               for name, h in P.WINDOW_DEFS_H.items()}
    assert windows["7d"]["conversion"] > windows["24h"]["conversion"] \
        > windows["12h"]["conversion"]
    assert P.decay_regime(windows)
    block = P.recalibrate_releases(
        [{"release_id": "cooldown-released", "count": 100, "expected": 0}],
        {}, rows, NOW)
    rel = block["releases"][0]
    rng = rel["expected_range"]
    assert rng["regime"] == "monotonic_decay"
    # mid capped at min(ewma, 24h) expected counts; range untouched
    ewma_count = rel["expected_ewma"]
    c24_count = int(100 * windows["24h"]["conversion"])
    assert rng["mid"] == min(ewma_count, c24_count)


def test_monotonic_decay_mid_equals_min_ewma_24h():
    rows = decay_wave_set()
    block = P.recalibrate_releases(
        [{"release_id": "cooldown-released", "count": 1000, "expected": 0}],
        {}, rows, NOW)
    rel = block["releases"][0]
    ewma_count = rel["expected_ewma"]
    c24_count = int(1000 * rel["conversions_used"]["24h"])
    assert rel["expected_range"]["regime"] == "monotonic_decay"
    assert rel["expected_range"]["mid"] == min(ewma_count, c24_count)
    # the range still spans the full 7d..12h spread (not narrowed)
    lo, hi = rel["expected_range"]["lo"], rel["expected_range"]["hi"]
    assert lo == int(1000 * rel["conversions_used"]["12h"])
    assert hi == int(1000 * rel["conversions_used"]["7d"])


def test_non_decay_regime_keeps_raw_ewma_mid():
    rows = []
    for d in (6, 5.9):            # weak past
        rows.append(wave_row(NOW - timedelta(days=d), scanned=200,
                             promoted=10))
    for m in (90, 60):            # strong present (fresh, inside stale bound)
        rows.append(wave_row(NOW - timedelta(minutes=m), scanned=200,
                             promoted=100))
    batches = P.scan_batches_from_rows(rows, NOW)
    windows = {name: P.window_ratio(batches, NOW, h)
               for name, h in P.WINDOW_DEFS_H.items()}
    assert not P.decay_regime(windows)
    block = P.recalibrate_releases(
        [{"release_id": "cooldown-released", "count": 1000, "expected": 0}],
        {}, rows, NOW)
    rel = block["releases"][0]
    assert rel["expected_range"]["regime"] == "stable_or_recovering"
    assert rel["expected_range"]["mid"] == rel["expected_ewma"]


# --- 6. per-batch invariant ------------------------------------------------

def test_invariant_promoted_between_0_and_scanned():
    shapes = [(17, 83, 10, 7), (20, 83, 10, 10), (203, 200, 3, 1),
              (10, 0, 50, 0), (100, 0, 10, 999), (140, 100, 5, 4),
              (0, 0, 0, 0), (48, 38, 10, 0), (11, 83, 10, 1)]
    rows = [wave_row(NOW - timedelta(hours=i + 1), scanned=s, unscored=u,
                     fastpath=f, promoted=p) for i, (s, u, f, p)
            in enumerate(shapes)]
    batches = P.scan_batches_from_rows(rows, NOW)
    for ts, s, p in batches:
        assert 0 <= p <= s
    for name, h in P.WINDOW_DEFS_H.items():
        w = P.window_ratio(batches, NOW, h)
        assert w["conversion"] is None or 0.0 <= w["conversion"] <= 1.0
    ewma = P.ewma_conversion(batches)
    assert ewma is None or 0.0 <= ewma <= 1.0
