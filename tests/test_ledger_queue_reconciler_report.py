#!/usr/bin/env python3
"""Regression tests for the report-only reconciler runner.

Stdlib only (unittest). Every test runs against scratch fixtures; the live
tree is never touched. Conventions: tests are colocated with the engines,
matching the other test_*.py files in this directory.
"""

import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest

_HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines")
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import ledger_queue_reconciler_report as runner  # first: sets up ledger_append path
import ledger_queue_reconciler as lqr

# Real vendored callables, captured BEFORE any guard installation (the
# runner installs guards only inside run_report()).
_REAL_DELIVER_PENDING = lqr._deliver_pending
_REAL_WRITE_QUEUE = lqr._write_queue
_REAL_QUEUE_IO = runner._REAL_QUEUE_IO

PUB = lqr.PUBLICATION_FIELD

LEDGER_ROWS = [
    {"role_id": "R-PUB", "attempt_id": "A-PUB", "status": "SUBMITTED",
     "attempt_id_source": "writer", "date_submitted": "2026-09-20"},
    {"role_id": "R-NOOP", "attempt_id": "A-NOOP", "status": "SUBMITTED",
     "attempt_id_source": "role_id"},
    {"role_id": "R-CONF", "attempt_id": "B-CONF", "status": "SUBMITTED",
     "attempt_id_source": "writer"},
    {"role_id": "R-UNTRUSTED", "attempt_id": "A-UNTRUSTED",
     "status": "SUBMITTED", "attempt_id_source": "company"},
    {"role_id": "R-IDLE", "attempt_id": "A-IDLE", "status": "SUBMITTED",
     "attempt_id_source": "writer"},
    {"role_id": "R-HIST", "attempt_id": "A-HIST", "status": "SUBMITTED",
     "attempt_id_source": "writer"},
]

INTENTS = [
    {"attempt_id": "A-PUB", "role_id": "R-PUB", "state": "SUBMITTED",
     "transport": "api_direct"},
    {"attempt_id": "A-NOOP", "role_id": "R-NOOP", "state": "SUBMITTED"},
    {"attempt_id": "B-CONF", "role_id": "R-CONF", "state": "SUBMITTED"},
    {"attempt_id": "A-UNTRUSTED", "role_id": "R-UNTRUSTED",
     "state": "SUBMITTED"},
    {"attempt_id": "A-IDLE", "role_id": "R-IDLE", "state": "SUBMITTED"},
    {"attempt_id": "A-HIST", "role_id": "R-HIST", "state": "FAILED"},
]

STANDARD = [
    {"role_id": "R-PUB", "status": "READY"},
    {"role_id": "R-CONF", "status": "READY", "attempt_id": "C-CONF"},
    {"role_id": "R-UNTRUSTED", "status": "READY"},
    {"role_id": "R-IDLE", "status": "IN-FLIGHT"},
    {"role_id": "R-HIST", "status": "READY"},
]
NEEDS_INPUT = [
    {"role_id": "R-NOOP", "status": "SUBMITTED", "attempt_id": "A-NOOP"},
]
STRATEGIC = []


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
        fh.write("\n")


def _fingerprint(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return (path, False, None, None)
    st = os.stat(path)
    return (path, True, hashlib.sha256(raw).hexdigest(), st.st_mtime_ns)


class ReportOnlyFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = os.path.join(self.tmp.name, "ledger.json")
        self.qs = os.path.join(self.tmp.name, "standard-queue.json")
        self.qn = os.path.join(self.tmp.name, "needs_input-queue.json")
        self.qg = os.path.join(self.tmp.name, "strategic-queue.json")
        self.wm = os.path.join(self.tmp.name, "watermark.json")
        _write(self.ledger, LEDGER_ROWS)
        _write(self.qs, STANDARD)
        _write(self.qn, NEEDS_INPUT)
        _write(self.qg, STRATEGIC)
        self.queue_paths = {"standard": self.qs, "needs_input": self.qn,
                            "strategic": self.qg}

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, **kw):
        kw.setdefault("intent_records_provider", lambda: INTENTS)
        return runner.run_report(ledger_path=self.ledger,
                                 queue_paths=self.queue_paths,
                                 watermark_path=self.wm, **kw)

    def _sources(self):
        return [_fingerprint(p) for p in
                [self.ledger, self.qs, self.qn, self.qg, self.wm]]


class TestVendoredPin(ReportOnlyFixture):
    def test_vendored_sha_pinned(self):
        with open(os.path.join(_HERE, "ledger_queue_reconciler.py"),
                  "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(digest, runner.VENDORED_SHA256)
        self.assertEqual(digest,
                         "7ce8a9bd43b09a63ade4f395ba661a80734252d7d199e31de35c582579d44f23")

    def test_no_live_flag(self):
        parser = runner._build_parser()
        option_strings = [s for a in parser._actions for s in a.option_strings]
        self.assertNotIn("--live", option_strings)


class TestReportOnlyZeroWrites(ReportOnlyFixture):
    def test_zero_writes_to_sources(self):
        before = self._sources()
        report = self._run()
        after = self._sources()
        self.assertEqual(before, after)
        self.assertTrue(report["dry_run"])
        self.assertIsNone(report["fatal"])
        self.assertFalse(report["aborted"])
        # The would-publish entry was NOT mutated on disk.
        with open(self.qs, encoding="utf-8") as fh:
            entries = json.load(fh)
        pub = next(e for e in entries if e["role_id"] == "R-PUB")
        self.assertNotIn(PUB, pub)
        self.assertEqual(pub["status"], "READY")
        # Runner metadata records the enforcement + exclusions.
        meta = report["report_only_runner"]
        self.assertEqual(meta["vendored_sha256"], runner.VENDORED_SHA256)
        self.assertEqual(meta["mode"], "report_only")
        self.assertIn("lane_retirement_policy", meta["excluded"])
        self.assertIn("timestamp_format_migration", meta["excluded"])
        self.assertIn("write_authority", meta["excluded"])

    def test_report_path_protection(self):
        with self.assertRaises(runner.ReportOnlyViolation):
            self._run(report_path=self.ledger)
        existing = os.path.join(self.tmp.name, "r.json")
        _write(existing, {})
        with self.assertRaises(FileExistsError):
            self._run(report_path=existing)


class TestOutboxMarkerSemantics(ReportOnlyFixture):
    def _markers(self):
        report = self._run()
        self.assertEqual(len(report["published"]), 1)
        self.assertEqual(len(report["computed_outbox_markers"]), 1)
        return report["computed_outbox_markers"][0]

    def test_would_publish_decision_and_marker(self):
        item = self._markers()
        decision = item["decision"]
        self.assertEqual(decision["kind"], "publish")  # plan() kind; run() tags would_publish
        self.assertEqual(decision["role_id"], "R-PUB")
        self.assertEqual(decision["attempt_id"], "A-PUB")
        self.assertEqual(decision["queue_home"], "standard")
        self.assertTrue(item["note"].startswith("computed from the report-only plan"))
        marker = item["marker"]
        self.assertEqual(marker["schema"], 1)
        self.assertEqual(marker["state"], "pending")
        self.assertEqual(marker["persistence"], "computed_not_persisted")
        self.assertTrue(marker["report_only"])
        payload = marker["payload"]
        self.assertEqual(payload["type"], "ledger_queue_published")
        self.assertEqual(payload["role_id"], "R-PUB")
        self.assertEqual(payload["attempt_id"], "A-PUB")
        self.assertEqual(payload["queue_home"], "standard")
        self.assertEqual(payload["transport"], "api_direct")
        self.assertEqual(payload["evidence"], lqr.EVIDENCE_REASON)
        self.assertRegex(payload["ledger_row_sha256"], r"^[0-9a-f]{64}$")
        # Stable event_id: idempotent-sink dedup base.
        expected = "lqr-" + hashlib.sha256(
            json.dumps(["ledger_queue_published", "R-PUB", "A-PUB"],
                       sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False).encode("utf-8")
        ).hexdigest()
        self.assertEqual(marker["event_id"], expected)
        self.assertEqual(payload["event_id"], expected)
        # The marker core satisfies the vendored validation contract.
        core = {k: marker[k]
                for k in ("schema", "event_id", "payload", "state")}
        lqr._validate_marker(core, {"role_id": "R-PUB"})

    def test_event_id_deterministic_across_runs(self):
        first = self._markers()["marker"]["event_id"]
        second = self._markers()["marker"]["event_id"]
        self.assertEqual(first, second)

    def test_pending_events_complete_semantics(self):
        report = self._run()
        # No pending markers in fixtures: verified zero, not uncertain.
        self.assertEqual(report["pending_events"], [])
        self.assertTrue(report["pending_events_complete"])
        self.assertEqual(report["pending_events_status"], "planning_snapshot")


class TestCommitOutcomeUnknown(ReportOnlyFixture):
    def _pending_marker_fixture(self):
        rid, aid = "R-PEND", "A-PEND"
        event_id = lqr._event_id(rid, aid)
        payload = {"event_id": event_id, "type": "ledger_queue_published",
                   "role_id": rid, "attempt_id": aid, "queue_home": "standard",
                   "transport": None, "ts": "2026-09-21T09:00:00+00:00",
                   "evidence": lqr.EVIDENCE_REASON, "ledger_index": 0,
                   "ledger_row_sha256": "ab" * 32}
        marker = {"schema": 1, "event_id": event_id, "payload": payload,
                  "state": "pending"}
        _write(self.qs, [{"role_id": rid, "attempt_id": aid,
                          "status": "SUBMITTED", PUB: marker}])
        _write(self.ledger, [])
        return rid, aid, event_id

    def test_commit_outcome_unknown_aborts_delivery(self):
        rid, aid, event_id = self._pending_marker_fixture()

        class _RaisingQueueIO:
            @contextlib.contextmanager
            def queue_lock(self, *a, **k):
                yield

            def atomic_write_json(self, *a, **k):
                raise RuntimeError("simulated writer failure after replace")

        saved = (lqr._write_queue, lqr.queue_io, lqr.submit_intent)
        lqr.set_test_paths(ledger_path=self.ledger,
                           queue_paths=self.queue_paths,
                           watermark_path=self.wm)
        try:
            # Real vendored delivery path; only the writer raises.
            lqr._write_queue = _REAL_WRITE_QUEUE
            lqr.queue_io = _RaisingQueueIO()
            lqr.submit_intent = runner._ReadOnlySubmitIntent(
                runner._REAL_SUBMIT_INTENT, lambda: [])
            report = {"pending_events": [], "pending_events_complete": True,
                      "pending_events_status": "delivery_snapshot",
                      "errors": [], "events": [], "delivery_attempts": [],
                      "delivered_events": [], "aborted": False}
            _REAL_DELIVER_PENDING(report, lambda payload: None)
        finally:
            lqr._write_queue, lqr.queue_io, lqr.submit_intent = saved
            lqr.reset_test_paths()

        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["kind"], "commit_outcome_unknown")
        self.assertEqual(report["errors"][0]["event_id"], event_id)
        self.assertTrue(report["aborted"])
        # Uncertain: verified-zero is NOT claimed after the abort.
        self.assertFalse(report["pending_events_complete"])
        self.assertEqual(report["pending_events_status"],
                         "unknown_after_commit_error")
        # The failed ack did not reach the file.
        with open(self.qs, encoding="utf-8") as fh:
            entry = json.load(fh)[0]
        self.assertEqual(entry[PUB]["state"], "pending")
        self.assertNotIn("delivered_ts", entry[PUB])

    def test_report_only_blocks_delivery_path(self):
        # The runner's own guard blocks _deliver_pending outright.
        runner._install_report_only_guards(lambda: [])
        with self.assertRaises(runner.ReportOnlyViolation):
            lqr._deliver_pending({}, None)


class TestConflictBlockingR1(ReportOnlyFixture):
    def _conflict_report(self, status):
        _write(self.qs, [{"role_id": "R-CONF", "status": status,
                          "attempt_id": "C-CONF"}])
        return self._run()

    def test_conflict_blocks_regardless_of_queue_status(self):
        for status in ("READY", "PARKED", "IN-FLIGHT", "SUBMITTED"):
            with self.subTest(status=status):
                report = self._conflict_report(status)
                decisions = [d for d in report["report_only"]
                             if d["attempt_id"] == "B-CONF"]
                self.assertEqual(len(decisions), 1)
                self.assertEqual(decisions[0]["reasons"],
                                 ["attempt_id_mismatch"])
                self.assertFalse(
                    [p for p in report["published"]
                     if p["attempt_id"] == "B-CONF"])
                with open(self.qs, encoding="utf-8") as fh:
                    entry = json.load(fh)[0]
                self.assertEqual(entry["attempt_id"], "C-CONF")
                self.assertNotIn(PUB, entry)

    def test_idless_active_attempt_blocked(self):
        report = self._run()
        decisions = [d for d in report["report_only"]
                     if d["attempt_id"] == "A-IDLE"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["reasons"],
                         ["active_attempt_id_missing"])


class TestHistoricalDivergencesReportOnly(ReportOnlyFixture):
    def test_untrusted_source_never_backfilled(self):
        report = self._run()
        decisions = [d for d in report["report_only"]
                     if d["attempt_id"] == "A-UNTRUSTED"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["reasons"],
                         ["untrusted_attempt_id_source"])
        self.assertFalse(
            [p for p in report["published"]
             if p["attempt_id"] == "A-UNTRUSTED"])
        # The READY queue entry was not backfilled to SUBMITTED.
        with open(self.qs, encoding="utf-8") as fh:
            entries = json.load(fh)
        untrusted = next(e for e in entries
                         if e["role_id"] == "R-UNTRUSTED")
        self.assertEqual(untrusted["status"], "READY")

    def test_nonterminal_intent_stays_report_only(self):
        report = self._run()
        decisions = [d for d in report["report_only"]
                     if d["attempt_id"] == "A-HIST"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["reasons"],
                         ["no_terminal_submitted_intent"])

    def test_no_op_already_published(self):
        report = self._run()
        self.assertIn(1, report["no_ops"])


class TestGuardsFailClosed(ReportOnlyFixture):
    def test_all_write_paths_blocked(self):
        runner._install_report_only_guards(lambda: [])
        with self.assertRaises(runner.ReportOnlyViolation):
            lqr._commit_publish({})
        with self.assertRaises(runner.ReportOnlyViolation):
            lqr._write_queue({})
        with self.assertRaises(runner.ReportOnlyViolation):
            lqr._deliver_pending({}, None)
        with self.assertRaises(runner.ReportOnlyViolation):
            lqr._sync_watermark({})
        with self.assertRaises(runner.ReportOnlyViolation):
            lqr.write_watermark({})
        with self.assertRaises(runner.ReportOnlyViolation):
            lqr.queue_io.atomic_write_json("x", {})
        with self.assertRaises(runner.ReportOnlyViolation):
            lqr.queue_io.queue_lock()
        with self.assertRaises(runner.ReportOnlyViolation):
            lqr.submit_intent.flag_inconsistent("a", "b", "c")
        # The read-only API still works.
        self.assertEqual(lqr.submit_intent.all_records(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
