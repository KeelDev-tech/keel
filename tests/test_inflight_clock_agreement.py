"""Synthetic producer/watchdog clock agreement; no external work is dispatched."""
import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
import feeder_watchdog as watchdog
import inflight_marker as marker
import launch_lock
import queue_io
import submit_intent
import apply_loop
import input_tray_digest


class Clock(datetime):
    instant = datetime(2026, 11, 4, 17, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


class InflightClockAgreementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="keel-clock-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.queue = self.home / "queue.json"
        self.events = self.home / "events.jsonl"
        self.row = {"role_id": "synthetic-clock", "company": "Synthetic", "title": "Role",
                    "status": "IN-FLIGHT", "browser_task_id": "synthetic-owner"}
        self.mark = datetime(2026, 11, 4, 17, tzinfo=timezone.utc)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(watchdog, "QUEUE", str(self.queue)))
        self.stack.enter_context(patch.object(watchdog, "EVENTS", str(self.events)))
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))

    def stamps(self, instant):
        with patch.object(Clock, "instant", instant), patch.object(marker, "datetime", Clock):
            return marker._utc_now(), marker._utc_now()

    def run_watchdog(self, row, now, dry=False, write_fixture=True):
        if write_fixture:
            self.queue.write_text(json.dumps([row]))
        before = self.queue.read_bytes()
        with patch.object(Clock, "instant", now), patch.object(watchdog, "datetime", Clock), \
                patch.object(sys, "argv", ["feeder_watchdog.py"] + (["--dry-run"] if dry else [])), \
                patch.object(watchdog.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as dispatch, \
                patch.object(watchdog.log_event, "log") as log, contextlib.redirect_stdout(io.StringIO()):
            result = watchdog.main()
        self.assertEqual(result, 0)
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertFalse(self.events.exists())
        return dispatch.call_count, [call.kwargs.get("details", {}).get("gate") for call in log.call_args_list]

    def test_producer_round_trips_winter_summer_dst_with_legacy_consumers(self):
        instants = ["2026-01-15T17:00:12+00:00", "2026-07-14T17:00:12+00:00",
                    "2026-03-08T09:30:00+00:00", "2026-03-08T10:30:00+00:00",
                    "2026-11-01T08:30:00+00:00", "2026-11-01T09:30:00+00:00"]
        for text in instants:
            with self.subTest(instant=text):
                instant = datetime.fromisoformat(text)
                stamp, utc = self.stamps(instant)
                self.assertEqual(watchdog.parse_updated(stamp), instant)
                with patch.object(Clock, "instant", instant), patch.object(marker, "datetime", Clock):
                    rollback_stamp = marker._pdt_now()
                rounded = instant.replace(second=0, microsecond=0)
                self.assertEqual(apply_loop._parse_status_ts(rollback_stamp), rounded)
                with patch.object(Clock, "instant", rounded + timedelta(minutes=90)), \
                        patch.object(input_tray_digest, "datetime", Clock):
                    self.assertEqual(input_tray_digest.parse_age_h(rollback_stamp), 1.5)
                self.assertEqual(watchdog.parse_updated(utc), instant)

    def test_generated_winter_markers_remain_busy_until_two_hours(self):
        status, utc = self.stamps(self.mark)
        row = {**self.row, "status_updated": status, "in_flight_at": utc}
        for minutes in (61, 90, 119):
            with self.subTest(minutes=minutes):
                self.assertEqual(self.run_watchdog(row, self.mark + timedelta(minutes=minutes)), (0, []))
                self.assertEqual(self.run_watchdog(row, self.mark + timedelta(minutes=minutes), dry=True), (0, []))

    def test_existing_mislabeled_winter_status_cannot_hide_fresh_utc_mark(self):
        row = {**self.row, "status_updated": "2026-11-04 09:00 PDT",
               "in_flight_at": self.mark.isoformat()}
        for minutes in (61, 90, 119):
            with self.subTest(minutes=minutes):
                self.assertEqual(self.run_watchdog(row, self.mark + timedelta(minutes=minutes)), (0, []))

    def test_newest_aware_timestamp_and_future_ownership_remain_busy(self):
        now = self.mark + timedelta(hours=4)
        old, fresh, future = self.mark.isoformat(), (now - timedelta(minutes=15)).isoformat(), (now + timedelta(hours=1)).isoformat()
        for status, utc in ((fresh, old), (old, fresh), (future, old), (old, future)):
            with self.subTest(status=status, utc=utc):
                row = {**self.row, "status_updated": status, "in_flight_at": utc}
                self.assertEqual(self.run_watchdog(row, now), (0, []))

    def test_previously_unknown_or_naive_timestamps_never_become_stale(self):
        now = self.mark + timedelta(hours=4)
        for status, utc in ((None, None), ("bad", "bad"),
                            ("2026-11-04 17:00 UTC", None),
                            ("2026-11-04 17:00 UTC", "2026-11-04T17:00:00"),
                            (None, "2026-11-04 17:00 UTC"),
                            (self.mark.replace(tzinfo=None).isoformat(), self.mark.isoformat()),
                            (None, self.mark.replace(tzinfo=None).isoformat())):
            with self.subTest(status=status, utc=utc):
                row = {**self.row, "status_updated": status, "in_flight_at": utc}
                fresh, stale, unknown = watchdog.classify_inflight([row], now)
                self.assertEqual((fresh, stale, unknown), ([], [], [row]))
                calls, gates = self.run_watchdog(row, now)
                self.assertEqual(calls, 0)
                self.assertEqual(gates, [watchdog.MARKER_TIMESTAMP_MISSING])
                self.assertEqual(self.run_watchdog(row, now, dry=True), (0, []))

    def test_exact_threshold_stale_and_dry_run_semantics_are_preserved(self):
        row = {**self.row, "status_updated": self.mark.isoformat(), "in_flight_at": self.mark.isoformat()}
        self.assertEqual(self.run_watchdog(row, self.mark + timedelta(hours=2) - timedelta(microseconds=1)), (0, []))
        for hours in (2, 3):
            with self.subTest(hours=hours):
                calls, gates = self.run_watchdog(row, self.mark + timedelta(hours=hours))
                self.assertEqual(calls, 1)
                self.assertEqual(gates, ["stale_inflight", "feeder_empty"])
                self.assertEqual(self.run_watchdog(row, self.mark + timedelta(hours=hours), dry=True), (0, []))

    def test_legacy_summer_and_missing_primary_fallback_are_preserved(self):
        summer = datetime(2026, 7, 14, 17, tzinfo=timezone.utc)
        for status in ("2026-07-14 10:00 PDT", None, "bad"):
            row = {**self.row, "status_updated": status, "in_flight_at": summer.isoformat()}
            self.assertEqual(self.run_watchdog(row, summer + timedelta(minutes=90)), (0, []))

    def test_real_claim_and_rollback_preserve_clock_and_consumer_contracts(self):
        self.check_real_transaction("claim")

    def test_real_spawn_marker_and_rollback_preserve_clock_and_consumer_contracts(self):
        self.check_real_transaction("spawn")

    def check_real_transaction(self, phase):
        data = self.home / "data"
        data.mkdir()
        (data / "employer-blocklist.md").write_text("# Blocked employers\n")
        ledger = data / "application-ledger.json"
        ledger.write_text("[]")
        locks, intents = self.home / "locks", self.home / "intents"
        locks.mkdir(); intents.mkdir()
        ready = {**self.row, "status": "READY", "fit_score": 80, "action_band": "APPLY"}
        ready.pop("browser_task_id")
        self.queue.write_text(json.dumps({"leads": [ready], "revision": 7}))
        prior_paths = marker._PATHS_OVERRIDE
        marker.set_queue_paths({"standard": str(self.queue)})
        self.addCleanup(marker.set_queue_paths, prior_paths)
        for key, value in {"_store_dir_override": str(intents),
                           "STORE_PATH": str(intents / "submit-intents.json"),
                           "LOCK_PATH": str(intents / "submit-intents.json.lock")}.items():
            self.stack.enter_context(patch.object(submit_intent, key, value))
        with patch.object(marker, "HOME", str(self.home)), patch.object(launch_lock, "LOCK_DIR", str(locks)), \
                patch.object(launch_lock, "LEDGER_PATH", str(ledger)), \
                patch.object(queue_io, "_LOCK_PATH", str(self.home / "queue.lock")), \
                patch.object(Clock, "instant", self.mark), patch.object(marker, "datetime", Clock):
            task_id = "synthetic-owner" if phase == "spawn" else None
            result = marker.mark_inflight("synthetic-clock", task_id=task_id, phase=phase, owner="synthetic-test")
            self.assertTrue(result["ok"], result)
            saved = json.loads(self.queue.read_text())
            self.assertEqual(saved["revision"], 7)
            row = saved["leads"][0]
            self.assertEqual(row["status"], "IN-FLIGHT")
            self.assertEqual(watchdog.parse_updated(row["status_updated"]), self.mark)
            self.assertEqual(watchdog.parse_updated(row["in_flight_at"]), self.mark)
            owner = launch_lock.check("synthetic-clock")
            self.assertEqual(owner["task_id"], task_id or "claim:synthetic-clock")
            lock_bytes = {p: p.read_bytes() for p in locks.iterdir() if p.is_file()}
            self.assertEqual(self.run_watchdog(row, self.mark + timedelta(minutes=90), write_fixture=False), (0, []))
            self.assertEqual(lock_bytes, {p: p.read_bytes() for p in locks.iterdir() if p.is_file()})
            result = marker.rollback_inflight("synthetic-clock", owner["task_id"], reason="synthetic cancellation")
            self.assertTrue(result["ok"], result)
            saved = json.loads(self.queue.read_text())
            self.assertEqual(saved["revision"], 7)
            rolled_back = saved["leads"][0]
            self.assertEqual(rolled_back["status"], "READY")
            self.assertIsNone(rolled_back["browser_task_id"])
            self.assertIsNone(rolled_back["in_flight_at"])
            self.assertIsNone(launch_lock.check("synthetic-clock"))
            self.assertEqual(apply_loop._parse_status_ts(rolled_back["status_updated"]), self.mark)
            for delta, blocked in ((-1, False), (1, True)):
                event = (self.mark + timedelta(hours=delta)).isoformat()
                with patch.object(apply_loop, "_tripwire_blocked_map", return_value={"synthetic-clock": event}):
                    self.assertEqual(apply_loop._tripwire_skip(rolled_back), blocked)


if __name__ == "__main__":
    unittest.main()
