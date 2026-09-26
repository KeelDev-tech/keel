"""Regression tests for the fan-out gate's supply fallback (2026-09-19).

The recurring false-FANOUT: when the pulse worker hand-writes
state-snapshot.json from the abbreviated schema, `pool_supply` is absent and
the gate fired `supply_unverified:missing_observation` even with a fresh
pool-guardian heartbeat (pulses 474/478/479/483/484/490).

The fix (structural, in fanout_gate.resolve_supply_reason): when the snapshot
lacks pool_supply, re-derive the observation from the same pool-guardian
heartbeat stream (telemetry pool_health events, source "pool-guardian") the
canonical writer reads. A fresh observation suppresses the unverified
reason; genuinely missing/stale data still fans out.

Covers:
  1. snapshot without pool_supply + fresh heartbeat -> reason suppressed
  2. snapshot without pool_supply + stale heartbeat -> reason still fires
  3. snapshot without pool_supply + missing/empty telemetry -> reason fires
  4. snapshot with fresh healthy pool_supply -> behavior unchanged (None)
  5. snapshot with stale pool_supply -> refresh_observation unchanged
     (the fallback only touches the missing_observation case)
  6. snapshot with fresh starved pool_supply -> starved reason unchanged
  7. fallback: fresh heartbeat with actionable=0 -> starved reason
  8. fallback: contradictory heartbeat counts -> reason still fires
  9/10. end-to-end main(): hand-written snapshot + fresh heartbeat -> QUIET
        without supply_unverified; + stale heartbeat -> FANOUT with the reason

Run: python3 test_fanout_gate_supply_fallback.py
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
FG_PATH = os.path.abspath(os.path.join(BASE, "..", "monitors", "fanout_gate.py"))
KEEL_ROOT = os.path.abspath(os.path.join(BASE, ".."))
sys.path.insert(0, KEEL_ROOT)
from keel_local.supply import classify_pool  # noqa: E402


def load_fanout_gate():
    spec = importlib.util.spec_from_file_location("fanout_gate", FG_PATH)
    fg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fg)
    return fg


def pool_health_event(ready, actionable, observed_at):
    """A contract-valid pool_health / pool-guardian telemetry event."""
    health = classify_pool(ready, actionable)
    return {
        "ts": observed_at.isoformat(),
        "event_type": "pool_health",
        "source": "pool-guardian",
        "details": {
            "schema_version": 1,
            "state": health["state"],
            "reason": health["reason"],
            "healthy": health["healthy"],
            "ready": ready,
            "actionable": actionable,
            "ready_floor": 5,
            "observed_at": observed_at.isoformat(),
        },
    }


class SupplyFallbackCase(unittest.TestCase):
    def setUp(self):
        self.fg = load_fanout_gate()
        self.tmp = tempfile.mkdtemp(prefix="fgfall-")
        self.telemetry_path = os.path.join(self.tmp, "events.jsonl")
        self._old_telemetry = self.fg.TELEMETRY
        self.fg.TELEMETRY = self.telemetry_path

    def tearDown(self):
        self.fg.TELEMETRY = self._old_telemetry

    def write_telemetry(self, events):
        with open(self.telemetry_path, "w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

    def snap_without_supply(self):
        # The hand-written abbreviated schema: no pool_supply key at all.
        return {"ledger_submitted": 196, "ready": 6, "inflight": [],
                "needs_input": 308}

    def snap_with_supply(self, ready, actionable, observed_at):
        snap = self.snap_without_supply()
        health = classify_pool(ready, actionable)
        snap["pool_supply"] = {
            "schema_version": 1, "state": health["state"],
            "reason": health["reason"], "healthy": health["healthy"],
            "ready": ready, "actionable": actionable, "ready_floor": 5,
            "observed_at": observed_at.isoformat(),
        }
        return snap

    # --- 1. fresh heartbeat suppresses the unverified reason ---
    def test_fresh_heartbeat_suppresses_missing_observation(self):
        now = datetime.now(timezone.utc)
        self.write_telemetry([pool_health_event(6, 100,
                                                now - timedelta(seconds=30))])
        self.assertIsNone(
            self.fg.resolve_supply_reason(self.snap_without_supply(), now))

    # --- 2. stale heartbeat still fans out ---
    def test_stale_heartbeat_keeps_reason(self):
        now = datetime.now(timezone.utc)
        self.write_telemetry([pool_health_event(6, 100,
                                                now - timedelta(seconds=300))])
        self.assertEqual(
            self.fg.resolve_supply_reason(self.snap_without_supply(), now),
            "supply_unverified:missing_observation")

    # --- 3. missing telemetry file still fans out ---
    def test_missing_telemetry_keeps_reason(self):
        now = datetime.now(timezone.utc)
        os.path.exists(self.telemetry_path) and os.remove(self.telemetry_path)
        self.assertEqual(
            self.fg.resolve_supply_reason(self.snap_without_supply(), now),
            "supply_unverified:missing_observation")

    # --- 3b. empty telemetry (no heartbeat ever) still fans out ---
    def test_empty_telemetry_keeps_reason(self):
        now = datetime.now(timezone.utc)
        self.write_telemetry([])
        self.assertEqual(
            self.fg.resolve_supply_reason(self.snap_without_supply(), now),
            "supply_unverified:missing_observation")

    # --- 4. snapshot with fresh healthy pool_supply: unchanged behavior ---
    def test_snapshot_supply_healthy_unchanged(self):
        now = datetime.now(timezone.utc)
        snap = self.snap_with_supply(6, 100, now - timedelta(seconds=30))
        self.assertIsNone(self.fg.resolve_supply_reason(snap, now))

    # --- 5. snapshot with stale pool_supply: refresh path unchanged ---
    def test_snapshot_supply_stale_refresh_unchanged(self):
        now = datetime.now(timezone.utc)
        snap = self.snap_with_supply(6, 100, now - timedelta(seconds=300))
        # Even with a FRESH heartbeat in telemetry, a snapshot that carries a
        # stale pool_supply keeps the refresh_observation reason: the fallback
        # only touches the missing_observation case.
        self.write_telemetry([pool_health_event(6, 100,
                                                now - timedelta(seconds=10))])
        self.assertEqual(self.fg.resolve_supply_reason(snap, now),
                         "supply_unverified:refresh_observation")

    # --- 6. snapshot with fresh starved pool_supply: starved reason unchanged ---
    def test_snapshot_supply_starved_unchanged(self):
        now = datetime.now(timezone.utc)
        snap = self.snap_with_supply(6, 0, now - timedelta(seconds=30))
        self.assertEqual(
            self.fg.resolve_supply_reason(snap, now),
            "supply_starved:actionable=0:AUDIT_REFILL_SUPPLY")

    # --- 7. fallback: fresh heartbeat with actionable=0 -> starved ---
    def test_fallback_fresh_starved_heartbeat_fires_starved(self):
        now = datetime.now(timezone.utc)
        self.write_telemetry([pool_health_event(6, 0,
                                                now - timedelta(seconds=30))])
        self.assertEqual(
            self.fg.resolve_supply_reason(self.snap_without_supply(), now),
            "supply_starved:actionable=0:AUDIT_REFILL_SUPPLY")

    # --- 8. fallback: heartbeat contradicting its counts -> unverified ---
    def test_fallback_contradictory_heartbeat_keeps_reason(self):
        now = datetime.now(timezone.utc)
        ev = pool_health_event(6, 100, now - timedelta(seconds=30))
        ev["details"]["state"] = "HEALTHY"
        ev["details"]["ready"] = 0  # counts now say REFILL_REQUIRED
        ev["details"]["healthy"] = False
        self.write_telemetry([ev])
        self.assertEqual(
            self.fg.resolve_supply_reason(self.snap_without_supply(), now),
            "supply_unverified:missing_observation")


class SupplyFallbackEndToEndCase(unittest.TestCase):
    """main() with a hand-written snapshot (no pool_supply)."""

    def setUp(self):
        self.fg = load_fanout_gate()
        self.tmp = tempfile.mkdtemp(prefix="fge2e-")
        self.paths = {k: os.path.join(self.tmp, k + ".json")
                      for k in ("snap", "wm", "ledger", "queue",
                                "needs_input", "telemetry")}
        self.saved = {n: getattr(self.fg, n)
                      for n in ("SNAPSHOT", "WATERMARK", "LEDGER", "QUEUE",
                                "NEEDS_INPUT", "TELEMETRY", "WATCHED_FILES")}
        self.fg.SNAPSHOT = self.paths["snap"]
        self.fg.WATERMARK = self.paths["wm"]
        self.fg.LEDGER = self.paths["ledger"]
        self.fg.QUEUE = self.paths["queue"]
        self.fg.NEEDS_INPUT = self.paths["needs_input"]
        self.fg.TELEMETRY = self.paths["telemetry"]
        self.fg.WATCHED_FILES = {k: self.paths[k]
                                 for k in ("ledger", "queue",
                                           "needs_input", "telemetry")}

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(self.fg, n, v)

    def stage(self, heartbeat_age_s):
        now = datetime.now(timezone.utc)
        snap = {"ledger_submitted": 196, "ready": 6, "inflight": [],
                "needs_input": 308}  # hand-written: no pool_supply
        for k in ("ledger", "queue", "needs_input"):
            with open(self.paths[k], "w") as f:
                json.dump([], f)
        ev = pool_health_event(6, 100,
                               now - timedelta(seconds=heartbeat_age_s))
        with open(self.paths["telemetry"], "w") as f:
            f.write(json.dumps(ev) + "\n")
        with open(self.paths["snap"], "w") as f:
            json.dump(snap, f)
        hashes = {name: self.fg.sha256_file(path)
                  for name, path in self.fg.WATCHED_FILES.items()}
        wm = {"pulse_count": 1,  # next = 2, not a forced scan
              "last_run_ts": (now - timedelta(minutes=10)).isoformat(),
              "hashes": hashes,
              "signature": {"ledger_submitted": 196, "ready": 6,
                            "inflight": [], "needs_input": 308,
                            "pending_verify": 0},
              "known_gates": [],
              "verify_drought_runs": 0,
              "last_verify_retry_signal_ts": now.isoformat()}
        with open(self.paths["wm"], "w") as f:
            json.dump(wm, f)

    def run_main(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                self.fg.main()
        return json.loads(buf.getvalue())

    # --- 9. fresh heartbeat -> no supply_unverified, QUIET ---
    def test_handwritten_snapshot_fresh_heartbeat_quiet(self):
        self.stage(heartbeat_age_s=5)
        out = self.run_main()
        self.assertEqual(out["verdict"], "QUIET")
        self.assertFalse(any("supply_unverified" in r
                             for r in out["reasons"]))

    # --- 10. stale heartbeat -> FANOUT with the reason ---
    def test_handwritten_snapshot_stale_heartbeat_fanout(self):
        self.stage(heartbeat_age_s=300)
        out = self.run_main()
        self.assertEqual(out["verdict"], "FANOUT")
        self.assertEqual(out["reasons"],
                         ["supply_unverified:missing_observation"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
