"""test_launch_lock_telemetry_guard.py — TELEMETRY_SUBMITTED guard (2026-09-22).

Port: the canonical ledger lags raw telemetry by days, so a guard keyed
only on the ledger can re-queue an already-submitted role to READY. The
guard now also refuses when raw telemetry/events.jsonl holds a
'submitted' / 'submission_claimed' event for the same company+title.
All fixtures use synthetic employers; the ledger path and the events
path are test-local, so the real repo data is never touched.

unittest style, run with:
    python3 -m unittest tests.test_launch_lock_telemetry_guard
"""
import json
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "engines"))
import launch_lock as ll  # noqa: E402


def _event(company, title, etype="submitted"):
    return {"event_type": etype, "company": company,
            "details": {"title": title}}


class TestTelemetrySubmittedMatch(unittest.TestCase):
    def _events(self, rows):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.jsonl")
        with open(p, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return p

    def test_match_on_company_title(self):
        p = self._events([_event("ExampleCo", "Senior Backend Engineer")])
        self.assertEqual(
            ll._telemetry_submitted_match("ExampleCo", "Senior Backend Engineer", p),
            "seniorbackendengineer")

    def test_submission_claimed_alias_counts(self):
        p = self._events([_event("ExampleCo", "Senior Backend Engineer",
                                 etype="submission_claimed")])
        self.assertIsNotNone(
            ll._telemetry_submitted_match("ExampleCo", "Senior Backend Engineer", p))

    def test_no_match_returns_none(self):
        p = self._events([_event("ExampleCo", "Senior Backend Engineer")])
        self.assertIsNone(
            ll._telemetry_submitted_match("OtherCo", "Senior Backend Engineer", p))
        self.assertIsNone(
            ll._telemetry_submitted_match("ExampleCo", "DevOps Engineer", p))

    def test_fail_closed_on_missing_file(self):
        self.assertIsNone(
            ll._telemetry_submitted_match("ExampleCo", "Senior Backend Engineer",
                                          "/nonexistent/events.jsonl"))

    def test_blank_company_or_title_returns_none(self):
        p = self._events([_event("ExampleCo", "Senior Backend Engineer")])
        self.assertIsNone(ll._telemetry_submitted_match("", "Senior Backend Engineer", p))
        self.assertIsNone(ll._telemetry_submitted_match("ExampleCo", "", p))


class TestGuardTelemetryVerdict(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ledger = os.path.join(self.d, "ledger.json")
        with open(self.ledger, "w") as f:
            json.dump({"rows": []}, f)
        self.events = os.path.join(self.d, "events.jsonl")
        with open(self.events, "w") as f:
            f.write(json.dumps(
                _event("ExampleCo", "Senior Backend Engineer")) + "\n")

    def tearDown(self):
        for rid in ("T-RID-TELEMETRY-1", "T-RID-TELEMETRY-2"):
            try:
                ll.release(rid, "T-TASK")
            except Exception:
                pass

    def test_telemetry_submitted_refuses(self):
        ok, info = ll.prelaunch_guard(
            "T-RID-TELEMETRY-1", "T-TASK", "ExampleCo",
            "Senior Backend Engineer", ledger_path=self.ledger,
            telemetry_path=self.events)
        self.assertFalse(ok)
        self.assertEqual(info["status"], "TELEMETRY_SUBMITTED")
        self.assertEqual(info["verdict"], "REFUSE")

    def test_no_telemetry_match_goes(self):
        ok, info = ll.prelaunch_guard(
            "T-RID-TELEMETRY-2", "T-TASK", "OtherCo", "DevOps Engineer",
            ledger_path=self.ledger, telemetry_path=self.events)
        self.assertTrue(ok)
        self.assertEqual(info["status"], "ACQUIRED")

    def test_missing_events_file_fail_closed_go(self):
        ok, info = ll.prelaunch_guard(
            "T-RID-TELEMETRY-2", "T-TASK", "ExampleCo",
            "Senior Backend Engineer", ledger_path=self.ledger,
            telemetry_path="/nonexistent/events.jsonl")
        self.assertTrue(ok)
        self.assertEqual(info["status"], "ACQUIRED")


if __name__ == "__main__":
    unittest.main()
