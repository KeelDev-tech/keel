#!/usr/bin/env python3
"""Regression tests: review packet immutability and evidence gating.

All fixtures are synthetic and in-memory. Store-touching calls use tmp_path.
"""

import json
import unittest
from pathlib import Path

import sys

KEEL_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(KEEL_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from review.packet import (  # noqa: E402
    FAMILIES,
    NoApprovalRequest,
    PacketError,
    PacketTampered,
    build_packet,
    issue_decision_request,
    load_requests,
    read_packet,
    sha256_hex,
    write_packet,
)
from _fixtures import canary_record, six_family_records, iso_in  # noqa: E402


class TestPacketBuild(unittest.TestCase):
    def test_ready_packet_when_all_six_authentic(self):
        packet = build_packet("TEST-READY-001", six_family_records())
        self.assertEqual(packet["status"], "READY_FOR_REVIEW")
        self.assertEqual(packet["unresolved_warnings"], [])
        self.assertEqual(set(packet["revisions"]), set(FAMILIES))
        self.assertEqual(set(packet["exact_values"]), set(FAMILIES))
        self.assertIsNone(packet["decision_request"])
        self.assertEqual(packet["scope"]["role_id"], "synthetic-role")

    def test_blocked_when_any_family_missing(self):
        records = [r for r in six_family_records()
                   if r["component"] != "route"]
        packet = build_packet("TEST-BLOCKED-001", records)
        self.assertEqual(packet["status"], "BLOCKED-PENDING-EVIDENCE")
        self.assertIsNone(packet["decision_request"])
        warnings = {w["component"]: w["state"]
                    for w in packet["unresolved_warnings"]}
        self.assertEqual(warnings["route"], "MISSING")

    def test_blocked_when_family_unknown(self):
        records = six_family_records()
        for record in records:
            if record["component"] == "target":
                record["provenance"] = "unknown"
                record["provenance_reason"] = "no legitimate observation"
                record["payload"] = None
                from _fixtures import digest_of
                record["material_digest"] = digest_of(None)
        packet = build_packet("TEST-BLOCKED-002", records)
        self.assertEqual(packet["status"], "BLOCKED-PENDING-EVIDENCE")
        self.assertTrue(any(w["component"] == "target" and w["state"] == "UNKNOWN"
                            for w in packet["unresolved_warnings"]))

    def test_synthetic_provenance_is_not_evidence(self):
        records = six_family_records(
            answers={"provenance": "synthetic_fixture"})
        packet = build_packet("TEST-BLOCKED-003", records)
        self.assertEqual(packet["status"], "BLOCKED-PENDING-EVIDENCE")
        self.assertTrue(any(w["component"] == "answers" and w["state"] == "SYNTHETIC"
                            for w in packet["unresolved_warnings"]))

    def test_digest_mismatch_blocks(self):
        records = six_family_records()
        for record in records:
            if record["component"] == "form":
                record["material_digest"] = "0" * 64  # tampered digest
        packet = build_packet("TEST-BLOCKED-004", records)
        self.assertEqual(packet["status"], "BLOCKED-PENDING-EVIDENCE")
        self.assertTrue(any(w["component"] == "form"
                            and w["state"] == "DIGEST_MISMATCH"
                            for w in packet["unresolved_warnings"]))

    def test_expired_record_blocks(self):
        records = six_family_records(policy={"expires_at": iso_in(-1)})
        packet = build_packet("TEST-BLOCKED-005", records)
        self.assertEqual(packet["status"], "BLOCKED-PENDING-EVIDENCE")
        self.assertTrue(any(w["component"] == "policy" and w["state"] == "EXPIRED"
                            for w in packet["unresolved_warnings"]))

    def test_revoked_record_blocks(self):
        records = six_family_records(attachments={"revoked": True})
        packet = build_packet("TEST-BLOCKED-006", records)
        self.assertEqual(packet["status"], "BLOCKED-PENDING-EVIDENCE")
        self.assertTrue(any(w["component"] == "attachments" and w["state"] == "REVOKED"
                            for w in packet["unresolved_warnings"]))

    def test_empty_export_is_blocked_pending_evidence(self):
        packet = build_packet("TEST-BLOCKED-007", [])
        self.assertEqual(packet["status"], "BLOCKED-PENDING-EVIDENCE")
        self.assertIsNone(packet["decision_request"])
        self.assertEqual(len(packet["unresolved_warnings"]), 7)  # scope + 6

    def test_scope_mismatch_raises(self):
        records = six_family_records()
        records[0] = {**records[0], "role_id": "different-role"}
        with self.assertRaises(PacketError):
            build_packet("TEST-ERR-001", records)

    def test_duplicate_family_raises(self):
        records = six_family_records() + [canary_record("policy", {"x": 1})]
        with self.assertRaises(PacketError):
            build_packet("TEST-ERR-002", records)

    def test_unknown_family_raises(self):
        records = six_family_records()
        records.append(canary_record("policy", {"x": 1}))
        records[-1]["component"] = "approval"  # not a pre-approval family
        with self.assertRaises(PacketError):
            build_packet("TEST-ERR-003", records)


class TestPacketImmutability(unittest.TestCase):
    def _sealed(self, tmp_path: Path, packet_id="TEST-SEAL-001"):
        packet = build_packet(packet_id, six_family_records())
        path = write_packet(packet, tmp_path / f"{packet_id}-review-packet.json")
        return packet, path

    def test_hash_deterministic(self):
        # Pin every timestamp so the two builds see identical contents.
        fixed = "2026-09-18T23:30:00+00:00"
        records = six_family_records()
        for record in records:
            record["observed_at"] = fixed
            record["expires_at"] = "2026-10-18T23:30:00+00:00"
        first = build_packet("TEST-DET-001", records)
        second = build_packet("TEST-DET-001", records)
        for packet in (first, second):
            packet.pop("built_at")
        body1 = {k: v for k, v in first.items() if k != "packet_sha256"}
        body2 = {k: v for k, v in second.items() if k != "packet_sha256"}
        self.assertEqual(sha256_hex(body1), sha256_hex(body2))

    def test_read_back_intact(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            packet, path = self._sealed(Path(tmp))
            back = read_packet(path)
            self.assertEqual(back["packet_sha256"], packet["packet_sha256"])

    def test_tamper_detected_payload(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            packet, path = self._sealed(Path(tmp))
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["exact_values"]["policy"]["value"] = "forged"
            path.write_text(json.dumps(raw, sort_keys=True, indent=2),
                            encoding="utf-8")
            with self.assertRaises(PacketTampered):
                read_packet(path)

    def test_tamper_detected_status_flip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # A genuinely BLOCKED packet: attacker flips status to READY.
            packet = build_packet("TEST-SEAL-002", [])
            self.assertEqual(packet["status"], "BLOCKED-PENDING-EVIDENCE")
            path = write_packet(
                packet, tmp_path / "TEST-SEAL-002-review-packet.json")
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["status"] = "READY_FOR_REVIEW"
            path.write_text(json.dumps(raw, sort_keys=True, indent=2),
                            encoding="utf-8")
            with self.assertRaises(PacketTampered):
                read_packet(path)

    def test_tamper_detected_hash_removed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            _, path = self._sealed(Path(tmp), "TEST-SEAL-003")
            raw = json.loads(path.read_text(encoding="utf-8"))
            del raw["packet_sha256"]
            path.write_text(json.dumps(raw, sort_keys=True, indent=2),
                            encoding="utf-8")
            with self.assertRaises(PacketError):
                read_packet(path)


class TestDecisionRequestIssuance(unittest.TestCase):
    def test_issue_on_ready_packet(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            packet = build_packet("TEST-REQ-001", six_family_records())
            request = issue_decision_request(
                packet, requests_file=tmp_path / "requests.jsonl")
            self.assertEqual(request["packet_sha256"], packet["packet_sha256"])
            self.assertEqual(request["packet_id"], "TEST-REQ-001")
            self.assertEqual(request["status"], "OPEN")
            stored = load_requests(tmp_path / "requests.jsonl")
            self.assertEqual(len(stored), 1)

    def test_no_approval_request_on_blocked_packet(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            packet = build_packet("TEST-REQ-002", [])  # BLOCKED
            with self.assertRaises(NoApprovalRequest):
                issue_decision_request(
                    packet, requests_file=tmp_path / "requests.jsonl")
            # Nothing was written: zero genuine approval requests issued.
            self.assertFalse((tmp_path / "requests.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
