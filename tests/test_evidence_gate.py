#!/usr/bin/env python3
"""stdlib unittest for the public evidence gate.

Synthetic temp data only — no private ledger, no real role IDs, no
production counts. Covers the repo's outcome_tracking.evidence_gate API:
load_ledger tolerance, find_submitted_rows, record_gate_decision /
load_decisions round-trip, coverage() verdict buckets, and fail-closed
handling of unparseable input.
"""
import json
import os
import sys
import tempfile
import unittest

_ENGINES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engines")
_ENGINES = os.path.normpath(_ENGINES)
if _ENGINES not in sys.path:
    sys.path.insert(0, _ENGINES)

from outcome_tracking import evidence_gate as eg


def _tmp(name, payload):
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(payload)
    return p


class LoadLedgerTest(unittest.TestCase):
    def test_list_form(self):
        p = _tmp("ledger.json", json.dumps([{"role_id": "R1"}]))
        self.assertEqual(eg.load_ledger(p), [{"role_id": "R1"}])

    def test_rows_form(self):
        p = _tmp("ledger.json", json.dumps({"rows": [{"role_id": "R2"}]}))
        self.assertEqual(eg.load_ledger(p), [{"role_id": "R2"}])

    def test_applications_form(self):
        p = _tmp("ledger.json", json.dumps({"applications": [{"role_id": "R3"}]}))
        self.assertEqual(eg.load_ledger(p), [{"role_id": "R3"}])

    def test_missing_file_returns_empty(self):
        self.assertEqual(eg.load_ledger("/nonexistent/x.json"), [])

    def test_invalid_json_returns_empty(self):
        p = _tmp("ledger.json", "{not json")
        self.assertEqual(eg.load_ledger(p), [])


class SubmittedRowsTest(unittest.TestCase):
    def test_picks_submitted_only(self):
        rows = [
            {"role_id": "A", "status": "SUBMITTED"},
            {"role_id": "B", "status": "submitted"},
            {"role_id": "C", "status": "PARKED"},
            {"role_id": "D"},
        ]
        got = eg.find_submitted_rows(rows)
        self.assertEqual({r["role_id"] for r in got}, {"A", "B"})


class DecisionRoundTripTest(unittest.TestCase):
    def test_record_and_load(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "decisions.json")
        rec = eg.record_gate_decision("R9", "Acme", "submitted",
                                      '"confirmation ABC123"',
                                      ts="2026-09-16T00:00:00+00:00",
                                      path=path)
        self.assertEqual(rec["role_id"], "R9")
        back = eg.load_decisions(path)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["evidence"], '"confirmation ABC123"')
        # second append keeps the first
        eg.record_gate_decision("R10", "Beta", "submitted", "pending",
                                path=path)
        self.assertEqual(len(eg.load_decisions(path)), 2)

    def test_load_missing_returns_empty(self):
        self.assertEqual(eg.load_decisions("/nonexistent/d.json"), [])


class CoverageTest(unittest.TestCase):
    def _events(self, lines):
        return _tmp("events.jsonl", "\n".join(lines) + "\n")

    def test_verified_pending_unevidenced(self):
        rows = [
            {"role_id": "RV", "status": "SUBMITTED",
             "date_submitted": "2026-09-16T09:00:00+00:00"},
            {"role_id": "RP", "status": "SUBMITTED",
             "date_submitted": "2026-09-16T09:00:00+00:00"},
            {"role_id": "RU", "status": "SUBMITTED",
             "date_submitted": "2026-09-16T09:00:00+00:00"},
            {"role_id": "RN", "status": "PARKED",
             "date_submitted": "2026-09-16T09:00:00+00:00"},
        ]
        ev = self._events([
            json.dumps({"event_type": "submitted", "role_id": "RV",
                        "ts": "2026-09-16T10:00:00+00:00",
                        "details": {"confirmation": '"Your application ABC12345 was received"'}}),
            json.dumps({"event_type": "submitted", "role_id": "RP",
                        "ts": "2026-09-16T10:00:00+00:00",
                        "details": {}}),
            "not-json {{{",
        ])
        v, p, u = eg.coverage(rows, events_path=ev)
        self.assertEqual((v, p, u), (1, 1, 1))

    def test_quote_outside_window_is_not_verified(self):
        rows = [{"role_id": "RW", "status": "SUBMITTED",
                 "date_submitted": "2026-09-16T09:00:00+00:00"}]
        ev = self._events([
            json.dumps({"event_type": "submitted", "role_id": "RW",
                        "ts": "2026-09-20T10:00:00+00:00",  # >24h later
                        "details": {"confirmation": '"confirmation ABC12345 received"'}}),
        ])
        v, p, u = eg.coverage(rows, events_path=ev)
        self.assertEqual((v, p, u), (0, 1, 0))

    def test_missing_events_file_all_unevidenced(self):
        rows = [{"role_id": "RX", "status": "SUBMITTED",
                 "date_submitted": "2026-09-16T09:00:00+00:00"}]
        v, p, u = eg.coverage(rows, events_path="/nonexistent/e.jsonl")
        self.assertEqual((v, p, u), (0, 0, 1))


class ConstantsTest(unittest.TestCase):
    def test_status_vocabulary(self):
        self.assertEqual(eg.STATUS_VERIFIED, "verified")
        self.assertEqual(eg.STATUS_PENDING, "pending")
        self.assertEqual(eg.STATUS_UNEVIDENCED, "unevidenced")


if __name__ == "__main__":
    unittest.main()
