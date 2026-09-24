#!/usr/bin/env python3
"""Regression tests for conv-recal-v3 fastpath-reuse exclusion (ARM 2,
keel-octopus-pulse 2026-09-19), updated to the v4 contract (ARM 3,
keel-octopus-pulse 2026-09-20, J-20260920-2244-veri-3960). ARM 516
(arm03-yield-forensics) showed the verify conversion collapse is partly
compositional: ~220/day zero-HTTP fastpath re-verifications of the same
5 held leads (promotable=0) ride the windowed-conversion denominator
with 0 promotion possible. The consumer
(flow_board_pulse.scan_batches_from_rows) subtracts fastpath_live_n
from the attempted pool. v4 corrected the v3 numerator over-deduction
(the 12:05/12:14 UTC waves promoted 17 real leads and were clamped to
(0,0)): the reuse cohort promotes at 0, so the numerator is never
reduced by fastpath_live_n -- it is the observed promoted_ready capped
at the denominator. The unscored_skipped subtraction applies only when
schema-consistent (unscored <= scanned); a disjoint pool-level skip
count is not subtracted from the batch.

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
        "flow_board_pulse_recal_fastpath_test", PULSE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flow_board_pulse_recal_fastpath_test"] = mod
    spec.loader.exec_module(mod)
    return mod


P = load_pulse()
NOW = datetime(2026, 9, 19, 12, 36, tzinfo=timezone.utc)


def wave_row(ts, scanned, unscored=0, fastpath=0, promoted=0, legacy=False):
    """One scan_summary row. legacy=True omits the LP-E/LP-F field
    (pre-2026-09-18 producer rows)."""
    d = {"scanned": scanned, "promoted_ready": promoted,
         "unscored_skipped": unscored}
    if not legacy:
        d["fastpath_live_n"] = fastpath
    return {"event_type": "scan_summary", "source": "verify-retry",
            "ts": ts.isoformat(), "details": d}


# --- per-source classification fixtures -------------------------------------
# Four cohort classes a batch can carry; each maps to a known (scanned,
# promoted) contribution after scan_batches_from_rows.

def test_cohort_classification_fresh_only():
    rows = [wave_row(NOW - timedelta(hours=1), scanned=40, promoted=4)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (40, 4)  # untouched


def test_cohort_classification_unscored_skip():
    rows = [wave_row(NOW - timedelta(hours=1), scanned=140, unscored=100,
                     promoted=4)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (40, 4)  # v2 convention: zero-HTTP rows never promote


def test_cohort_classification_fastpath_reuse():
    # the ARM 516 held-cohort shape: 5 reuses, 0 promotions possible
    rows = [wave_row(NOW - timedelta(hours=1), scanned=140, unscored=100,
                     fastpath=5, promoted=0)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (35, 0)  # phantom supply leaves the denominator


def test_cohort_classification_fastpath_promotion_bounded():
    # observed live edge (2026-09-18 23:24Z): attempted=3, all fastpath,
    # one promoted through the reuse path. The batch washes out instead
    # of pushing the ratio above 1.
    rows = [wave_row(NOW - timedelta(hours=1), scanned=203, unscored=200,
                     fastpath=3, promoted=1)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (0, 0)


def test_cohort_classification_legacy_row_degrades_to_v2():
    # pre-LP-E/LP-F rows lack fastpath_live_n -> exactly the v2 formula
    rows = [wave_row(NOW - timedelta(hours=1), scanned=140, unscored=100,
                     promoted=4, legacy=True)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (40, 4)


def test_version_is_v4():
    assert P.RECALIBRATION_VERSION == "conv-recal-v4"


# --- synthetic fastpath-heavy wave set replay --------------------------------
# 12 waves in the trailing 24h: half carry the held fastpath cohort
# (promoted=0 by design), half carry fresh supply that promotes. The
# exclusion must remove the phantom scans from the denominator while the
# fresh promotions still move the metric.

def fastpath_heavy_waves():
    rows = []
    for i in range(6):  # held cohort: 5 reuses/wave, 0 promotions
        rows.append(wave_row(NOW - timedelta(hours=1 + i * 2),
                             scanned=140, unscored=100, fastpath=5,
                             promoted=0))
    for i in range(6):  # fresh supply: no reuses, promotes 4/wave
        rows.append(wave_row(NOW - timedelta(hours=2 + i * 2),
                             scanned=140, unscored=100, fastpath=0,
                             promoted=4))
    return rows


def test_wave_set_denominator_excludes_phantom_scans():
    rows = fastpath_heavy_waves()
    batches = P.scan_batches_from_rows(rows, NOW)
    w = P.window_ratio(batches, NOW, 24)
    # denominator: 6*(140-100-5) + 6*(140-100) = 210 + 240 = 450
    # (was 480 under v2: 6*5 phantom scans removed)
    assert w["scanned"] == 450
    # numerator: fresh promotions all still count (24); the lower bound
    # subtracts at most fastpath_live_n per batch (0 here)
    assert w["promoted"] == 24
    assert w["conversion"] == pytest.approx(24 / 450)


def test_wave_set_matches_predicted_shift():
    # v2 on the same rows: denominator 480, conversion 24/480 = 0.05.
    # v3 must raise the conversion by exactly the phantom-scan removal.
    rows = fastpath_heavy_waves()
    batches = P.scan_batches_from_rows(rows, NOW)
    w = P.window_ratio(batches, NOW, 24)
    v2_scanned = sum((d["scanned"] - d["unscored_skipped"])
                     for d in (r["details"] for r in rows))
    v2_promoted = sum(d["promoted_ready"] for d in (r["details"] for r in rows))
    phantom = sum(d["fastpath_live_n"] for d in (r["details"] for r in rows))
    assert v2_scanned == 480 and v2_promoted == 24 and phantom == 30
    assert w["conversion"] == pytest.approx(v2_promoted / (v2_scanned - phantom))
    assert w["conversion"] > v2_promoted / v2_scanned


def test_wave_set_ewma_stays_bounded():
    rows = fastpath_heavy_waves()
    # adversarial batch: fastpath-only attempted pool with a promotion
    rows.append(wave_row(NOW - timedelta(minutes=30), scanned=203,
                         unscored=200, fastpath=3, promoted=1))
    batches = P.scan_batches_from_rows(rows, NOW)
    for name, h in P.WINDOW_DEFS_H.items():
        w = P.window_ratio(batches, NOW, h)
        assert w["conversion"] is None or 0.0 <= w["conversion"] <= 1.0
    ewma = P.ewma_conversion(batches)
    assert ewma is None or 0.0 <= ewma <= 1.0
    # per-batch invariant: 0 <= promoted <= scanned, always
    for ts, s, p in batches:
        assert 0 <= p <= s


def test_numerator_survives_when_fresh_supply_promotes():
    # v4 (J-20260920-2244-veri-3960): the reuse cohort promotes at 0
    # (held: materials-abstain/D1/low_fit), so every observed promotion
    # came from the attempted pool. The numerator is no longer reduced
    # by fastpath_live_n; it is capped at the denominator. v3 zeroed
    # these waves via max(0, 3-10) = 0 -- the 12:05/12:14 UTC defect.
    rows = [wave_row(NOW - timedelta(hours=1), scanned=100, unscored=0,
                     fastpath=10, promoted=3)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (90, 3)
    rows = [wave_row(NOW - timedelta(hours=1), scanned=100, unscored=0,
                     fastpath=2, promoted=3)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (98, 3)
    # promoter cap: promoted_ready above the denominator is clipped so
    # the ratio can never exceed 1 (never negative, never > denom)
    rows = [wave_row(NOW - timedelta(hours=1), scanned=100, unscored=0,
                     fastpath=10, promoted=999)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (90, 90)


def test_fastpath_cannot_drive_denominator_negative():
    # corrupt producer row: fastpath > attempted. Clamp, never negative.
    rows = [wave_row(NOW - timedelta(hours=1), scanned=10, unscored=0,
                     fastpath=50, promoted=0)]
    (ts, s, p), = P.scan_batches_from_rows(rows, NOW)
    assert (s, p) == (0, 0)
