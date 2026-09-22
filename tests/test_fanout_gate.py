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
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
FG_PATH = os.path.abspath(os.path.join(BASE, "..", "monitors", "fanout_gate.py"))

if not os.path.isfile(FG_PATH):
    # monitors/ is git-ignored (private operational surface); fanout_gate.py
    # exists only in dev checkouts. Skip the whole module under stdlib CI
    # instead of erroring on the missing file.
    raise unittest.SkipTest(
        "monitors/fanout_gate.py not present (git-ignored); skipping fanout-gate tests"
    )


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
              # The production watermark always carries this (build_watermark
              # writes it); the ARM 119 cadence-breach requirement keys off
              # it. Seeded 180 min back so the cadence is breached unless a
              # verify-retry signal in the window re-stamps it to now.
              "last_verify_retry_signal_ts":
                  (now - timedelta(minutes=180)).isoformat(),
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

    # --- 11-14. Workstream 2 (2026-09-17): verify contention is keyed ONLY
    # off true_concurrent_mutation, never off the benign internal
    # scan_apply_derivation_delta. The old combined race metric
    # double-counted verify's own re-derivation and sent an arm chasing a
    # phantom contention bug.
    def make_scan_summary(self, ts, tcm=None, sad=None, role_id="V-SCAN"):
        scan = self.make_event("scan_summary", "verify-retry", ts,
                               role_id=role_id)
        scan["details"] = {"scanned": 25, "apply_races": (tcm or 0) + (sad or 0)}
        if tcm is not None:
            scan["details"]["true_concurrent_mutation"] = tcm
        if sad is not None:
            scan["details"]["scan_apply_derivation_delta"] = sad
        return scan

    def contention_reason(self, reasons):
        return [r for r in reasons if r.startswith("verify_contention:")]

    def test_true_concurrent_mutation_burst_fans_out(self):
        now = datetime.now(timezone.utc)
        scan = self.make_scan_summary(now - timedelta(minutes=5), tcm=3, sad=47)
        reasons, _sig, _extra = self.evaluate([scan], pool_size=5)
        self.assertEqual(self.contention_reason(reasons),
                         ["verify_contention:true_concurrent_mutation_x3"])

    def test_derivation_delta_alone_never_fans_out(self):
        # The phantom-chase regression: 50 internal re-derivations and
        # zero real mutations must NOT trip the contention reason.
        now = datetime.now(timezone.utc)
        scan = self.make_scan_summary(now - timedelta(minutes=5), tcm=0, sad=50)
        reasons, _sig, _extra = self.evaluate([scan], pool_size=5)
        self.assertEqual(self.contention_reason(reasons), [])

    def test_pre_split_summary_without_new_keys_never_fans_out(self):
        # Backward compatibility: summaries emitted before the split carry
        # neither key — no contention reason, no crash.
        now = datetime.now(timezone.utc)
        scan = self.make_scan_summary(now - timedelta(minutes=5))
        reasons, _sig, _extra = self.evaluate([scan], pool_size=5)
        self.assertEqual(self.contention_reason(reasons), [])

    def test_contention_window_returns_four_tuple(self):
        now = datetime.now(timezone.utc)
        scan = self.make_scan_summary(now - timedelta(minutes=5), tcm=2, sad=9)
        queue = [{"role_id": "PV-0", "status": "PARKED-PENDING-VERIFICATION"}]
        with open(self.queue_path, "w") as f:
            json.dump(queue, f)
        with open(self.telemetry_path, "w") as f:
            f.write(json.dumps(scan) + "\n")
        out = self.fg.scan_telemetry_window(now - timedelta(minutes=10))
        self.assertEqual(len(out), 4)
        self.assertEqual(out[3], 2,
                         "contention sums true_concurrent_mutation only, "
                         "not the derivation delta")


class ReadyFloorCase(unittest.TestCase):
    """READY floor (Trent P0, 2026-09-17): the launchable pool must never
    starve. Regression for the 2026-09-17 19:42Z incident — the digest
    showed "zero cushion" (READY 3 vs 17 launches/2h) and nothing acted on
    it, because the content-hash pre-filter QUIETed the "nothing changed"
    state that IS the starvation failure mode."""

    def setUp(self):
        self.fg = load_fanout_gate()
        self.tmp = tempfile.mkdtemp(prefix="fgfloor-")

    def floor_reason(self, ready):
        return self.fg.ready_floor_reason({"ready": ready})

    # --- 1. below the floor breaches ---
    def test_zero_ready_breaches(self):
        r = self.floor_reason(0)
        self.assertIsNotNone(r)
        self.assertIn("EMERGENCY_REFILL", r)
        self.assertIn("ready=0", r)

    def test_four_ready_breaches(self):
        r = self.floor_reason(4)
        self.assertIsNotNone(r)
        self.assertIn("EMERGENCY_REFILL", r)

    # --- 2. at/above the floor does not ---
    def test_at_floor_no_breach(self):
        self.assertIsNone(self.floor_reason(5))

    def test_above_floor_no_breach(self):
        self.assertIsNone(self.floor_reason(47))

    # --- 3. unreadable ready fails toward breach, never toward silence ---
    def test_garbage_ready_fails_toward_breach(self):
        for bad in (None, "garbage", {"x": 1}):
            r = self.fg.ready_floor_reason({"ready": bad})
            self.assertIsNotNone(r, "bad ready %r must breach, not silence"
                                 % (bad,))
        # missing key is equally unreadable -> breach, not silence
        self.assertIsNotNone(self.fg.ready_floor_reason({}))

    # --- 4. the exact incident: unchanged files + starving pool must FANOUT ---
    def test_floor_bypass_prefilter_on_unchanged_files(self):
        now = datetime.now(timezone.utc)
        snap_path = os.path.join(self.tmp, "state-snapshot.json")
        wm_path = os.path.join(self.tmp, "materiality-watermark.json")
        snap = {"ledger_submitted": 195, "ledger_rows": 254,
                "standard_queue": 2717, "needs_input": 148,
                "ready": 0, "strategic_ready": 0, "strategic_firing": 0,
                "inflight": [],
                "latest_telemetry_ts": now.isoformat()}
        with open(snap_path, "w") as f:
            json.dump(snap, f)
        # Watermark whose hashes and signature MATCH the snapshot: the
        # pre-filter sees "nothing changed" and would QUIET without the
        # floor bypass.
        hashes = {name: self.fg.sha256_file(path)
                  for name, path in self.fg.WATCHED_FILES.items()}
        wm = {"pulse_count": 320,  # next = 321, not a forced scan (every 6th)
              "last_run_ts": (now - timedelta(minutes=10)).isoformat(),
              "hashes": hashes,
              "signature": {"ledger_submitted": 195, "ready": 0,
                            "inflight": [], "needs_input": 148,
                            "pending_verify": 0},
              "known_gates": [],
              "verify_drought_runs": 0,
              "last_verify_retry_signal_ts": now.isoformat()}
        with open(wm_path, "w") as f:
            json.dump(wm, f)
        # Point main() at the synthetic files (QUEUE/TELEMETRY stay on the
        # real paths so the hashes above stay valid).
        old_snap, old_wm = self.fg.SNAPSHOT, self.fg.WATERMARK
        self.fg.SNAPSHOT, self.fg.WATERMARK = snap_path, wm_path
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit):
                    self.fg.main()
        finally:
            self.fg.SNAPSHOT, self.fg.WATERMARK = old_snap, old_wm
        out = json.loads(buf.getvalue())
        self.assertEqual(out["verdict"], "FANOUT")
        self.assertEqual(out["reasons"],
                         ["ready_floor_breach:ready=0_floor=5:EMERGENCY_REFILL"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
