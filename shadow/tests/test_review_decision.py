#!/usr/bin/env python3
"""Regression tests: genuine decision capture and SHA-256 binding.

All fixtures synthetic and in-memory; store files live in tmp_path only.
The real review/decision-requests.jsonl and decisions.jsonl are never touched.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

KEEL_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(KEEL_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from review.packet import (  # noqa: E402
    build_packet,
    issue_decision_request,
    write_packet,
)
from review.decision import (  # noqa: E402
    HUMAN_DECISION_REQUIRED,
    DecisionBindingError,
    DecisionError,
    HumanDecisionRequired,
    decision_status,
    list_genuine_decisions,
    record_decision,
    require_genuine_decision,
)
from _fixtures import six_family_records, iso_in  # noqa: E402


class DecisionTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.requests_file = self.tmp_path / "requests.jsonl"
        self.decisions_file = self.tmp_path / "decisions.jsonl"
        self.packet = build_packet("TEST-DEC-001", six_family_records())
        self.packet_path = write_packet(
            self.packet, self.tmp_path / "TEST-DEC-001-review-packet.json")
        self.request = issue_decision_request(
            self.packet, requests_file=self.requests_file)

    def tearDown(self):
        self._tmp.cleanup()

    def _record(self, **kwargs):
        params = {
            "actor": "Trent Wade",
            "authority_ref": "trent-main-chat-2026-09-18T120000Z",
            "reviewed_sha256": self.packet["packet_sha256"],
            "expires_at": iso_in(30),
            "packet_path": self.packet_path,
            "requests_file": self.requests_file,
            "decisions_file": self.decisions_file,
        }
        params.update(kwargs)
        decision = params.pop("decision", "APPROVE")
        return record_decision(self.request["request_id"], decision, **params)


class TestNoGenuineDecision(unittest.TestCase):
    def test_genuine_decision_store_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            decisions = list_genuine_decisions(tmp_path / "decisions.jsonl")
            self.assertEqual(decisions, [])

    def test_status_is_human_decision_required(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            packet = build_packet("TEST-DEC-000", six_family_records())
            request = issue_decision_request(
                packet, requests_file=tmp_path / "requests.jsonl")
            self.assertEqual(
                decision_status(request["request_id"],
                                requests_file=tmp_path / "requests.jsonl",
                                decisions_file=tmp_path / "decisions.jsonl"),
                HUMAN_DECISION_REQUIRED)

    def test_require_genuine_decision_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            packet = build_packet("TEST-DEC-000b", six_family_records())
            request = issue_decision_request(
                packet, requests_file=tmp_path / "requests.jsonl")
            with self.assertRaises(HumanDecisionRequired):
                require_genuine_decision(
                    request["request_id"],
                    requests_file=tmp_path / "requests.jsonl",
                    decisions_file=tmp_path / "decisions.jsonl")

    def test_unknown_request_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with self.assertRaises(DecisionError):
                require_genuine_decision(
                    "keel-decision-request-doesnotexist",
                    requests_file=tmp_path / "requests.jsonl",
                    decisions_file=tmp_path / "decisions.jsonl")


class TestApproveRejectCapture(DecisionTestBase):
    def test_approve_binds_reviewed_sha256(self):
        record = self._record()
        self.assertEqual(record["decision"], "APPROVE")
        self.assertEqual(record["reviewed_sha256"],
                         self.packet["packet_sha256"])
        self.assertEqual(record["request_id"], self.request["request_id"])
        self.assertEqual(record["actor"], "Trent Wade")
        stored = list_genuine_decisions(self.decisions_file)
        self.assertEqual(len(stored), 1)
        self.assertEqual(
            decision_status(self.request["request_id"],
                            requests_file=self.requests_file,
                            decisions_file=self.decisions_file),
            "DECIDED_APPROVE")

    def test_reject_captured_and_request_closed(self):
        record = self._record(decision="REJECT")
        self.assertEqual(record["decision"], "REJECT")
        self.assertEqual(
            decision_status(self.request["request_id"],
                            requests_file=self.requests_file,
                            decisions_file=self.decisions_file),
            "DECIDED_REJECT")

    def test_sha_mismatch_rejected(self):
        with self.assertRaises(DecisionBindingError):
            self._record(reviewed_sha256="f" * 64)
        self.assertEqual(list_genuine_decisions(self.decisions_file), [])

    def test_sha_matches_request_but_not_sealed_packet_rejected(self):
        # reviewed sha equals the request's sha, but the sealed packet on
        # disk was rebuilt with different contents: the binding chain must
        # still fail because the reviewer did not review THESE contents.
        other = build_packet("TEST-DEC-001", six_family_records())
        assert other["packet_sha256"] != self.request["packet_sha256"]
        write_packet(other, self.packet_path)
        with self.assertRaises(DecisionBindingError):
            self._record(reviewed_sha256=self.request["packet_sha256"])

    def test_double_decision_rejected(self):
        self._record()
        with self.assertRaises(DecisionError):
            self._record()

    def test_invalid_decision_value_rejected(self):
        with self.assertRaises(DecisionError):
            self._record(decision="MAYBE")

    def test_missing_actor_rejected(self):
        with self.assertRaises(DecisionError):
            self._record(actor="  ")

    def test_missing_authority_ref_rejected(self):
        # Approval is never inferred: an empty authority reference fails.
        with self.assertRaises(DecisionError):
            self._record(authority_ref="")

    def test_past_expiry_rejected(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with self.assertRaises(DecisionError):
            self._record(expires_at=past)

    def test_tampered_packet_rejected(self):
        raw = json.loads(self.packet_path.read_text(encoding="utf-8"))
        raw["exact_values"]["policy"]["value"] = "forged"
        self.packet_path.write_text(json.dumps(raw, sort_keys=True, indent=2),
                                     encoding="utf-8")
        from review.packet import PacketTampered
        with self.assertRaises(PacketTampered):
            self._record()

    def test_no_synthetic_write_path(self):
        # decision.py must offer no API that persists a synthetic decision:
        # the only writer requires a real OPEN request from the store and a
        # sealed packet on disk.
        import review.decision as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("synthetic", source.lower())


if __name__ == "__main__":
    unittest.main()
