#!/usr/bin/env python3
"""Workstream 2 (2026-09-17) — pulse_dashboard verify-contention tile.

verify_contention_stats() reads the latest verify-retry scan_summary and
returns the honest split: (true_concurrent_mutation,
scan_apply_derivation_delta, ts). The dashboard tile keys amber ONLY off
true_concurrent_mutation (real concurrent-writer signal); the benign
internal derivation delta never trips it.

Run: python3 test_pulse_dashboard.py
"""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
PD_PATH = os.path.abspath(os.path.join(BASE, "..", "monitors",
                                        "pulse_dashboard.py"))


def load_pulse_dashboard():
    spec = importlib.util.spec_from_file_location("pulse_dashboard", PD_PATH)
    pd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pd)
    return pd


class VerifyContentionStatsTest(unittest.TestCase):
    def setUp(self):
        self.pd = load_pulse_dashboard()
        self.tmp = tempfile.mkdtemp(prefix="pdtest-")
        self.tel_dir = os.path.join(self.tmp, "telemetry")
        os.makedirs(self.tel_dir)
        # Redirect the module's JP root at the telemetry file it reads.
        self.orig_jp = self.pd.JP

        class FakeJP:
            def __init__(self, root):
                self._root = root

            def __truediv__(self, other):
                from pathlib import Path
                return Path(self._root) / other

        self.pd.JP = FakeJP(self.tmp)

    def tearDown(self):
        self.pd.JP = self.orig_jp

    def write_events(self, events):
        with open(os.path.join(self.tel_dir, "events.jsonl"), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def scan_summary(self, ts, tcm=None, sad=None):
        e = {"ts": ts, "event_type": "scan_summary", "source": "verify-retry",
             "role_id": "", "details": {"scanned": 10}}
        if tcm is not None:
            e["details"]["true_concurrent_mutation"] = tcm
        if sad is not None:
            e["details"]["scan_apply_derivation_delta"] = sad
        return e

    def test_latest_summary_split_returned(self):
        ts = datetime.now(timezone.utc).isoformat()
        self.write_events([
            self.scan_summary(ts, tcm=1, sad=20),
            self.scan_summary(ts, tcm=2, sad=50),
        ])
        tcm, sad, rts = self.pd.verify_contention_stats()
        self.assertEqual((tcm, sad), (2, 50),
                         "latest scan_summary wins")

    def test_no_summary_yet(self):
        self.write_events([])
        self.assertEqual(self.pd.verify_contention_stats(),
                         (None, None, None))

    def test_pre_split_summary(self):
        ts = datetime.now(timezone.utc).isoformat()
        self.write_events([self.scan_summary(ts)])
        tcm, sad, rts = self.pd.verify_contention_stats()
        self.assertIsNone(tcm)
        self.assertIsNone(sad)
        self.assertTrue(rts,
                        "timestamp still returned for a pre-split summary")

    def test_other_source_summary_ignored(self):
        ts = datetime.now(timezone.utc).isoformat()
        e = self.scan_summary(ts, tcm=9, sad=9)
        e["source"] = "apply_loop"
        self.write_events([e])
        self.assertEqual(self.pd.verify_contention_stats(),
                         (None, None, None))

    def test_never_raises_on_garbage(self):
        with open(os.path.join(self.tel_dir, "events.jsonl"), "w") as f:
            f.write("not json at all\n")
            f.write(json.dumps({"event_type": "scan_summary"}) + "\n")
        tcm, sad, rts = self.pd.verify_contention_stats()
        self.assertEqual((tcm, sad, rts), (None, None, None))


class EscNoneTest(unittest.TestCase):
    """Regression 2026-09-18: pulse 444 crashed with AttributeError.

    formation.json arm records are written by another loop; the
    'emergency-refill' arm carried an explicit null "started" field.
    a.get("started", "") returned None (not the default), and
    html.escape(None) raised AttributeError, killing the dashboard
    render. The fix: _esc() coerces None to "" before escaping.
    This test fails on the pre-fix module (no _esc attribute) and
    passes on the fixed one.
    """

    def test_esc_none_returns_empty(self):
        pd = load_pulse_dashboard()
        self.assertEqual(pd._esc(None), "",
                         "None arm field must render as empty, not crash")

    def test_esc_string_still_escapes(self):
        pd = load_pulse_dashboard()
        self.assertEqual(pd._esc("<b>&"), "&lt;b&gt;&amp;")

    def test_esc_missing_key_default_still_safe(self):
        pd = load_pulse_dashboard()
        arm = {"name": "emergency-refill", "started": None}
        # the exact pre-fix expression: html.escape(a.get("started",""))
        # raised AttributeError on this record
        self.assertEqual(pd._esc(arm.get("started", "")), "")


if __name__ == "__main__":
    unittest.main(verbosity=1)
