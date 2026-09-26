"""ARM 556-G: event-age + live-skew cross-check in pulse_snapshot.py.

Regression for the 556-C supply-gauge pre-mortem: pool_supply (replayed
guardian event) and flow_board.supply_state (live compute) disagreed over a
47s timing skew. The writer now stamps the replayed observation's honest age
and attaches a skew_note when the replayed ready count differs from a live
queue read at snapshot time. All observations and records are synthetic.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_local.supply import pool_health


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "monitors/pulse_snapshot.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def guardian_event(now, ready, actionable, age_seconds=0):
    """Synthetic pool_health event that passes health_from_event validation."""
    observed = now - timedelta(seconds=age_seconds)
    details = dict(pool_health(ready, actionable, observed_at=observed.isoformat(),
                               now=now))
    return {"event_type": "pool_health", "source": "pool-guardian",
            "ts": now.isoformat(), "details": details}


class PulseSupplySkewTests(unittest.TestCase):
    def build(self, snap, events, live_pool_state=None, standard=None):
        """Run build_snapshot with staged telemetry and a faked live read."""
        with tempfile.TemporaryDirectory() as tmp:
            telemetry = Path(tmp) / "events.jsonl"
            telemetry.write_text("".join(json.dumps(e) + "\n" for e in events))
            rows = standard if standard is not None else []
            patchers = [
                patch.object(snap, "TELEMETRY", str(telemetry)),
                patch.object(snap, "_entries", side_effect=[rows, rows, [], []]),
                patch.object(snap, "_api_direct_stats", return_value={}),
            ]
            if live_pool_state is not None:
                patchers.append(patch.object(snap, "_live_pool_state", live_pool_state))
            for p in patchers:
                p.start()
            try:
                return snap.build_snapshot()
            finally:
                for p in reversed(patchers):
                    p.stop()

    def test_event_age_seconds_present_and_numeric(self):
        snap = module("skew_age_snapshot")
        now = datetime.now(timezone.utc)
        live = lambda now=None: (4, 126)
        result = self.build(snap, [guardian_event(now, 4, 126, age_seconds=47)],
                            live_pool_state=live)
        age = result["pool_supply"].get("event_age_seconds")
        self.assertIsInstance(age, (int, float))
        self.assertNotIsInstance(age, bool)
        self.assertAlmostEqual(age, 47, delta=15)
        # replayed values untouched by the annotation
        self.assertEqual(result["pool_supply"]["ready"], 4)
        self.assertEqual(result["pool_supply"]["state"], "REFILL_REQUIRED")

    def test_skew_note_present_on_staged_ready_delta(self):
        snap = module("skew_delta_snapshot")
        now = datetime.now(timezone.utc)
        # 556-C scenario: event said 4, queue flipped to 6 before the export
        live = lambda now=None: (6, 126)
        result = self.build(snap, [guardian_event(now, 4, 126, age_seconds=47)],
                            live_pool_state=live)
        note = result["pool_supply"].get("skew_note")
        self.assertIsInstance(note, str)
        self.assertIn("4", note)
        self.assertIn("6", note)
        # the replay itself is never overwritten
        self.assertEqual(result["pool_supply"]["ready"], 4)

    def test_skew_note_absent_when_counts_agree(self):
        snap = module("skew_agree_snapshot")
        now = datetime.now(timezone.utc)
        live = lambda now=None: (4, 126)
        result = self.build(snap, [guardian_event(now, 4, 126, age_seconds=47)],
                            live_pool_state=live)
        self.assertNotIn("skew_note", result["pool_supply"])
        self.assertIn("event_age_seconds", result["pool_supply"])

    def test_snapshot_still_writes_when_cross_check_fails(self):
        snap = module("skew_fail_snapshot")
        now = datetime.now(timezone.utc)

        def boom(now=None):
            raise RuntimeError("queue read exploded")

        standard = [{"role_id": str(i), "status": "READY"} for i in range(5)]
        result = self.build(snap, [guardian_event(now, 4, 126, age_seconds=47)],
                            live_pool_state=boom, standard=standard)
        # pinned counts intact; cross-check silently skipped
        self.assertEqual(result["ready"], 5)
        self.assertNotIn("skew_note", result["pool_supply"])
        self.assertIsInstance(result["pool_supply"].get("event_age_seconds"), (int, float))

    def test_missing_live_module_never_breaks_snapshot(self):
        snap = module("skew_nomodule_snapshot")
        now = datetime.now(timezone.utc)
        standard = [{"role_id": str(i), "status": "READY"} for i in range(6)]
        result = self.build(snap, [guardian_event(now, 4, 126, age_seconds=47)],
                            live_pool_state=None, standard=standard)
        # unpatched real _live_pool_state may or may not exist here; either
        # way the snapshot writes with pinned counts intact.
        self.assertEqual(result["ready"], 6)
        self.assertIn("pool_supply", result)

    def test_no_event_yields_no_age_claim(self):
        # no guardian observation: no age is claimed (never an explicit null),
        # and the cross-check has nothing to compare against
        snap = module("skew_noevent_snapshot")
        live = lambda now=None: (6, 126)
        result = self.build(snap, [{"event_type": "noise", "ts": "2026-09-19T00:00:00Z"}],
                            live_pool_state=live)
        self.assertEqual(result["pool_supply"]["state"], "UNVERIFIED")
        self.assertNotIn("event_age_seconds", result["pool_supply"])
        self.assertNotIn("skew_note", result["pool_supply"])


if __name__ == "__main__":
    unittest.main()
