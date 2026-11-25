"""Synthetic ownership-status refusals; no real worker or host access."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
import feeder_watchdog as watchdog

BAD_STATUSES = ([], {}, False, 0, None, ["PRIVATE_SENTINEL"],
                {"PRIVATE_SENTINEL": "IN-FLIGHT"}, True, 7)


class WatchdogStatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="keel-watchdog-status-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.queue = self.home / "queue.json"
        self.events = self.home / "events.jsonl"
        self.events.write_text("")
        for name, value in {"PIPE": str(self.home), "QUEUE": str(self.queue),
                            "EVENTS": str(self.events),
                            "BLOCKLIST": str(self.home / "blocklist")}.items():
            p = patch.object(watchdog, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(watchdog.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout="", stderr=""))
        self.run = p.start()
        self.addCleanup(p.stop)
        p = patch.object(watchdog.log_event, "log")
        self.log = p.start()
        self.addCleanup(p.stop)

    def invoke(self, entries, dry=False):
        self.queue.write_text(json.dumps(entries))
        before = self.queue.read_bytes()
        out = io.StringIO()
        with patch.object(sys, "argv", ["feeder_watchdog.py"] +
                          (["--dry-run"] if dry else [])), contextlib.redirect_stdout(out):
            result = watchdog.main()
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertEqual(self.events.read_text(), "")
        return result, out.getvalue()

    def test_malformed_status_blocks_before_any_dispatch_or_diagnostic_write(self):
        for status in BAD_STATUSES:
            for dry in (False, True):
                with self.subTest(status=status, dry=dry):
                    self.run.reset_mock()
                    self.log.reset_mock()
                    result, output = self.invoke([
                        {"role_id": "PRIVATE_SENTINEL", "status": status},
                        {"role_id": "tail", "status": "PARKED"}], dry)
                    self.assertNotEqual(result, 0)
                    self.assertIn("BLOCKED", output)
                    self.assertNotIn("PRIVATE_SENTINEL", output)
                    self.assertNotIn("browser idle", output)
                    self.run.assert_not_called()
                    self.log.assert_not_called()

    def test_missing_status_is_unknown_ownership(self):
        result, output = self.invoke([{"role_id": "PRIVATE_SENTINEL"}])
        self.assertNotEqual(result, 0)
        self.assertIn("BLOCKED", output)
        self.run.assert_not_called()
        self.log.assert_not_called()

    def test_status_helpers_refuse_malformed_types(self):
        for status in BAD_STATUSES:
            with self.subTest(status=status):
                for call in (lambda: watchdog.is_inflight_status(status),
                             lambda: watchdog.workable({"status": status}),
                             lambda: watchdog.classify_inflight([{"status": status}])):
                    with self.assertRaisesRegex(ValueError, "malformed queue status"):
                        call()

    def test_canonical_busy_and_timestamp_classes_are_preserved(self):
        now = datetime.now(timezone.utc)
        fresh = {"role_id": "fresh", "status": "IN-FLIGHT-LANE-A",
                 "status_updated": now.isoformat()}
        stale = {"role_id": "stale", "status": "in-flight",
                 "status_updated": (now - timedelta(hours=3)).isoformat()}
        unknown = {"role_id": "unknown", "status": "IN-FLIGHT"}
        self.assertEqual(watchdog.classify_inflight([fresh, stale, unknown], now),
                         ([fresh], [stale], [unknown]))
        for marker in (fresh, unknown):
            for dry in (False, True):
                with self.subTest(marker=marker["role_id"], dry=dry):
                    self.run.reset_mock()
                    self.log.reset_mock()
                    result, output = self.invoke([marker], dry)
                    self.assertEqual(result, 0)
                    self.assertIn("browser appears busy", output)
                    self.run.assert_not_called()
                    if dry:
                        self.log.assert_not_called()

    def test_canonical_idle_and_fed_behavior_is_preserved(self):
        result, output = self.invoke([{"status": "PARKED"}], dry=True)
        self.assertEqual(result, 0)
        self.run.assert_not_called()
        self.log.assert_not_called()
        result, output = self.invoke([{"status": "PARKED"}])
        self.assertEqual(result, 0)
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0][-3:], ["--live", "--limit", "25"])
        self.run.reset_mock()
        self.log.reset_mock()
        (self.home / "resume.txt").write_text("Synthetic")
        fed = [{"status": s, "action_band": "APPLY", "company": "Synthetic",
                "materials": {"resume": "resume.txt"},
                "ats_url": "https://example.invalid/job"}
               for s in ("READY", "READY-FOR-BROWSER")]
        result, output = self.invoke(fed)
        self.assertEqual(result, 0)
        self.assertIn("lane fed", output)
        self.run.assert_not_called()
        self.log.assert_not_called()

    def test_malformed_recount_refuses_success_without_another_dispatch(self):
        with patch.object(watchdog, "load_queue", side_effect=[
                [{"status": "PARKED"}], [{"status": False}]]):
            result, output = self.invoke([])
        self.assertNotEqual(result, 0)
        self.assertIn("BLOCKED", output)
        self.run.assert_called_once()
        self.log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
