"""Regression tests for the P3 verify_drought redefinition (ARM 90, 2026-09-15).

The tooling gap: the fan-out gate's verify_drought counter reset on ANY
lead_verified telemetry event. Real telemetry shows lead_verified from
apply_loop (browser-lane pre-verifies, 309), arm4-strategic-recheck,
greenhouse-direct, and hygiene-reverify — all of which masked verify_retry's
actual drought (ARM67 / J-20260915-1429-veri-139).

The fix: the drought counter resets only on verify-retry's OWN signals —
lead_verified with source == "verify-retry", or an actual promotion to READY
(verify-retry gate_cleared on the pending_verification / materials_missing
gates). Everything else is unchanged: 5-min window floor,
VERIFY_DROUGHT_RUNS=3, pool-nonempty requirement, reason string format.

Covers:
  1. other-source lead_verified (incl. the browser lane) does NOT reset the
     counter — a genuine verify-retry drought still trips the reason
  2. verify-retry's own lead_verified resets the counter
  3. verify-retry promotions (gate_cleared pending_verification /
     materials_missing) reset the counter
  4. gate_cleared from other sources does NOT reset the counter
  5. empty pending-verify pool never reports a drought
  6. sub-5-minute windows carry the previous counter value
  7. stale events outside the window are ignored
  8-10 (ARM 107, ARM-102 P2 gate half): verify-retry's own scan_summary
     resets the counter (activity != promotion); other-source scan_summary
     does not; stale scan_summary is ignored

Run: python3 test_fanout_gate.py
"""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
FG_PATH = os.path.abspath(os.path.join(BASE, "..", "monitors", "fanout_gate.py"))


def load_fanout_gate():
    spec = importlib.util.spec_from_file_location("fanout_gate", FG_PATH)
    fg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fg)
    return fg


class FanoutGateCase(unittest.TestCase):
    def setUp(self):
        self.fg = load_fanout_gate()
        self.tmp = tempfile.mkdtemp(prefix="fgtest-")
        self.queue_path = os.path.join(self.tmp, "queue.json")
        self.telemetry_path = os.path.join(self.tmp, "events.jsonl")
        self.fg.QUEUE = self.queue_path
        self.fg.TELEMETRY = self.telemetry_path

    def make_event(self, event_type, source, ts, gate=None, role_id="TEST-1"):
        e = {"ts": ts.isoformat(), "event_type": event_type,
             "role_id": role_id, "source": source}
        if gate is not None:
            e["details"] = {"gate": gate}
        return e

    def evaluate(self, events, pool_size=5, drought_in=0,
                 last_run_ago_min=10):
        """Run evaluate() against a synthetic environment.

        events: list of event dicts (ts as datetime).
        Returns (reasons, sig, extra).
        """
        queue = [{"role_id": "PV-%d" % i, "status": "PARKED-PENDING-VERIFICATION"}
                 for i in range(pool_size)]
        with open(self.queue_path, "w") as f:
            json.dump(queue, f)
        with open(self.telemetry_path, "w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        now = datetime.now(timezone.utc)
        snap = {"ledger_submitted": 83, "ready": 71, "inflight": [],
                "needs_input": 73}
        wm = {"pulse_count": 24,
              "last_run_ts": (now - timedelta(minutes=last_run_ago_min)).isoformat(),
              "hashes": {},
              "signature": {"ledger_submitted": 83, "ready": 71,
                            "inflight": [], "needs_input": 73,
                            "pending_verify": pool_size},
              "known_gates": [],
              "verify_drought_runs": drought_in}
        return self.fg.evaluate(snap, wm, now)

    def drought_reason(self, reasons):
        return [r for r in reasons if r.startswith("verify_drought:")]

    # --- 1. other-source verifications do not reset a genuine drought ---
    def test_other_source_verifications_do_not_reset_drought(self):
        now = datetime.now(timezone.utc)
        ts = now - timedelta(minutes=5)
        events = [
            self.make_event("lead_verified", "apply_loop", ts, role_id="A-1"),
            self.make_event("lead_verified", "apply_loop", ts, role_id="A-2"),
            self.make_event("lead_verified", "apply_loop", ts, role_id="A-3"),
            self.make_event("lead_verified", "arm4-strategic-recheck", ts,
                            role_id="A-4"),
        ]
        reasons, _sig, extra = self.evaluate(events, pool_size=5, drought_in=2)
        self.assertEqual(self.drought_reason(reasons),
                         ["verify_drought:3_runs_no_verification"])
        self.assertEqual(extra["drought"], 3)

    # --- 2. verify-retry's own lead_verified resets the counter ---
    def test_verify_retry_lead_verified_resets_drought(self):
        now = datetime.now(timezone.utc)
        events = [self.make_event("lead_verified", "verify-retry",
                                  now - timedelta(minutes=5), role_id="V-1")]
        reasons, _sig, extra = self.evaluate(events, pool_size=5, drought_in=2)
        self.assertEqual(self.drought_reason(reasons), [])
        self.assertEqual(extra["drought"], 0)

    # --- 3. verify-retry promotions reset the counter ---
    def test_verify_retry_promotions_reset_drought(self):
        now = datetime.now(timezone.utc)
        ts = now - timedelta(minutes=5)
        for gate in ("pending_verification", "materials_missing"):
            events = [self.make_event("gate_cleared", "verify-retry", ts,
                                      gate=gate, role_id="V-" + gate)]
            reasons, _sig, extra = self.evaluate(events, pool_size=5,
                                                drought_in=2)
            self.assertEqual(self.drought_reason(reasons), [],
                             "gate_cleared %s should reset drought" % gate)
            self.assertEqual(extra["drought"], 0,
                             "gate_cleared %s should reset drought" % gate)

    # --- 4. gate_cleared from other sources does not reset ---
    def test_other_source_gate_cleared_does_not_reset_drought(self):
        now = datetime.now(timezone.utc)
        events = [self.make_event("gate_cleared", "pipeline-department",
                                  now - timedelta(minutes=5),
                                  gate="pending_verification", role_id="X-1")]
        reasons, _sig, extra = self.evaluate(events, pool_size=5, drought_in=2)
        self.assertEqual(self.drought_reason(reasons),
                         ["verify_drought:3_runs_no_verification"])
        self.assertEqual(extra["drought"], 3)

    # --- 5. empty pool never reports a drought ---
    def test_empty_pool_never_drought(self):
        reasons, _sig, extra = self.evaluate([], pool_size=0, drought_in=5)
        self.assertEqual(self.drought_reason(reasons), [])
        self.assertEqual(extra["drought"], 0)

    # --- 6. sub-5-minute windows carry the previous counter ---
    def test_short_window_carries_counter(self):
        reasons, _sig, extra = self.evaluate([], pool_size=5, drought_in=2,
                                             last_run_ago_min=1)
        self.assertEqual(self.drought_reason(reasons), [])
        self.assertEqual(extra["drought"], 2)

    # --- 7. stale events outside the window are ignored ---
    def test_stale_events_outside_window_ignored(self):
        now = datetime.now(timezone.utc)
        # last_run was 10 min ago; this verify-retry event is 20 min old
        events = [self.make_event("lead_verified", "verify-retry",
                                  now - timedelta(minutes=20), role_id="V-0")]
        reasons, _sig, extra = self.evaluate(events, pool_size=5, drought_in=2)
        self.assertEqual(self.drought_reason(reasons),
                         ["verify_drought:3_runs_no_verification"])
        self.assertEqual(extra["drought"], 3)

    # --- 8. verify-retry scan_summary resets the counter (ARM 107) ---
    def test_verify_retry_scan_summary_resets_drought(self):
        now = datetime.now(timezone.utc)
        # a healthy --live run over a fully-cooled-down window: zero liveness
        # verdicts, but one scan_summary event — that is worker activity
        scan = self.make_event("scan_summary", "verify-retry",
                               now - timedelta(minutes=5), role_id="V-SCAN")
        scan["details"] = {"scanned": 25, "cooldown_skipped": 25,
                           "verified": 0}
        reasons, _sig, extra = self.evaluate([scan], pool_size=5, drought_in=2)
        self.assertEqual(self.drought_reason(reasons), [])
        self.assertEqual(extra["drought"], 0)

    # --- 9. other-source scan_summary does NOT reset the counter ---
    def test_other_source_scan_summary_does_not_reset_drought(self):
        now = datetime.now(timezone.utc)
        scan = self.make_event("scan_summary", "apply_loop",
                               now - timedelta(minutes=5), role_id="A-SCAN")
        scan["details"] = {"scanned": 3, "verified": 3}
        reasons, _sig, extra = self.evaluate([scan], pool_size=5, drought_in=2)
        self.assertEqual(self.drought_reason(reasons),
                         ["verify_drought:3_runs_no_verification"])
        self.assertEqual(extra["drought"], 3)

    # --- 10. stale scan_summary outside the window is ignored ---
    def test_stale_scan_summary_outside_window_ignored(self):
        now = datetime.now(timezone.utc)
        scan = self.make_event("scan_summary", "verify-retry",
                               now - timedelta(minutes=20), role_id="V-OLD")
        scan["details"] = {"scanned": 25, "cooldown_skipped": 25}
        reasons, _sig, extra = self.evaluate([scan], pool_size=5, drought_in=2)
        self.assertEqual(self.drought_reason(reasons),
                         ["verify_drought:3_runs_no_verification"])
        self.assertEqual(extra["drought"], 3)

    # --- baseline: no signal at all still counts up ---
    def test_no_signal_counts_up_to_trip(self):
        reasons, _sig, extra = self.evaluate([], pool_size=5, drought_in=0)
        self.assertEqual(self.drought_reason(reasons), [])
        self.assertEqual(extra["drought"], 1)
        reasons, _sig, extra = self.evaluate([], pool_size=5, drought_in=2)
        self.assertEqual(self.drought_reason(reasons),
                         ["verify_drought:3_runs_no_verification"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
