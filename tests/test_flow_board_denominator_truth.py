"""Denominator-truth tests for the flow board's verify conversion (2026-09-19).

The real denominator artifact (blackboard J-20260918-2350-veri-2389,
proposed -> built): flow_board_pulse.scan_batches_from_rows read
scan_summary's details.scanned straight into the conversion denominator,
but details.scanned is len(scan_order) — which DELIBERATELY includes
unscored_skip rows so every windowed lead is accounted (2026-09-18
blind-wave fix). Those rows get zero HTTP and can never promote, so
conversion read 1.4-2.7x low on real data (7d 6.6%->9.4%, 24h 1.7%->3.6%,
12h 0.8%->2.1% measured 2026-09-19).

The fix: subtract details.unscored_skipped (clamped at 0) at the consumer.
The producer's scan_order accounting invariant is untouched.

A wrong fix was attempted and reverted the same night: subtracting
unscored_skipped in ewma_drift's per-run yield. That double-counted the
partition — verify_retry already removes unscored leads from `eligible`
before counting scanned_n, so the heartbeat's per-run `scanned` was
already the effective pool. The artifact lives ONLY in scan_summary's
scan_order-based scanned. These tests pin that distinction.

Covers:
  1. scan_summary with unscored_skipped -> effective denominator
  2. backward compat: rows without unscored_skipped -> unchanged
  3. clamp: unscored > scanned -> 0, never negative
  4. end-to-end window_ratio: corrected conversion over mixed batches
  5. decay_regime still detects monotonic decay on corrected windows
  6. per-run heartbeat scanned is NOT adjusted here (documents the
     reverted wrong fix: this module must not touch it)

Run: python3 test_flow_board_denominator_truth.py
"""
import importlib.util
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
KEEL_ROOT = os.path.abspath(os.path.join(BASE, ".."))
sys.path.insert(0, KEEL_ROOT)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fb = _load("flow_board_pulse_under_test",
           os.path.join(KEEL_ROOT, "flow_board_pulse.py"))

ANCHOR = datetime(2026, 9, 19, 7, 30, tzinfo=timezone.utc)


def summary_row(ts, scanned, promoted, unscored=None):
    details = {"scanned": scanned, "promoted_ready": promoted}
    if unscored is not None:
        details["unscored_skipped"] = unscored
    return {"event_type": "scan_summary", "source": "verify-retry",
            "ts": ts.isoformat(), "details": details}


class TestDenominatorTruth(unittest.TestCase):
    def test_unscored_subtracted(self):
        rows = [summary_row(ANCHOR - timedelta(hours=1),
                            scanned=207, promoted=7, unscored=200)]
        batches = fb.scan_batches_from_rows(rows, ANCHOR)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][1], 7)   # effective attempted pool
        self.assertEqual(batches[0][2], 7)

    def test_backward_compat_without_field(self):
        rows = [summary_row(ANCHOR - timedelta(hours=1),
                            scanned=100, promoted=60, unscored=None)]
        batches = fb.scan_batches_from_rows(rows, ANCHOR)
        self.assertEqual(batches[0][1], 100)

    def test_clamped_never_negative(self):
        # v4 (J-20260920-2244-veri-3960): unscored > scanned means the
        # producer's fields are disjoint counts (pool-level skip count,
        # not scan_order containment) -- the subtraction is skipped, the
        # batch keeps its own scanned count, and the denominator is
        # still never negative.
        rows = [summary_row(ANCHOR - timedelta(hours=1),
                            scanned=5, promoted=0, unscored=50)]
        batches = fb.scan_batches_from_rows(rows, ANCHOR)
        self.assertEqual(batches[0][1], 5)
        self.assertEqual(batches[0][2], 0)
        self.assertGreaterEqual(batches[0][1], 0)

    def test_window_ratio_corrected(self):
        rows = [
            summary_row(ANCHOR - timedelta(hours=2),
                        scanned=207, promoted=7, unscored=200),
            summary_row(ANCHOR - timedelta(hours=1),
                        scanned=100, promoted=50, unscored=None),
        ]
        batches = fb.scan_batches_from_rows(rows, ANCHOR)
        w = fb.window_ratio(batches, ANCHOR, 24)
        # (7+50) / (7+100) = 57/107, not 57/307
        self.assertAlmostEqual(w["conversion"], 57 / 107)
        self.assertEqual(w["scanned"], 107)
        self.assertEqual(w["promoted"], 57)

    def test_decay_regime_intact(self):
        windows = {"7d": {"conversion": 0.094}, "24h": {"conversion": 0.036},
                   "12h": {"conversion": 0.021}}
        self.assertTrue(fb.decay_regime(windows))
        # Rising conversions are not a decay regime (flat counts as
        # decaying under the >= contract — existing semantics, unchanged).
        rising = {"7d": {"conversion": 0.02}, "24h": {"conversion": 0.04},
                  "12h": {"conversion": 0.09}}
        self.assertFalse(fb.decay_regime(rising))

    def test_zero_scanned_batches_unchanged(self):
        # scan_summary rows from other sources (rescreen/description)
        # carry scanned=None -> 0; they contribute nothing either way.
        rows = [{"event_type": "scan_summary", "source": "rescreen",
                 "ts": ANCHOR.isoformat(), "details": {"scanned": None}}]
        batches = fb.scan_batches_from_rows(rows, ANCHOR)
        self.assertEqual(batches, [])  # unrelated sources are excluded


if __name__ == "__main__":
    unittest.main(verbosity=2)
