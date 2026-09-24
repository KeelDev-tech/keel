"""Regression: counsel packet completeness (every required field present)."""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.expanduser("~/workspace"))

from keel.privacy.counsel_packet import (
    prepare_packet, verify_packet_digest, PACKET_FIELDS,
)


def make_packet_kwargs(**overrides):
    kwargs = dict(
        release_id="PR-20260918-abcdef",
        manifest_digest="m" * 64,
        artifact_inventory=[{"artifact_id": "artifact-001", "name": "a.md",
                             "sha256": "a" * 64}],
        artifact_sha256={"artifact-001": "a" * 64},
        intended_destination="public Keel launch website",
        purpose="publish launch metrics",
        personal_data_inventory=[],
        sensitive_data_inventory=[],
        secrets_scan={"scanned": True, "findings": []},
        derivative_data_inventory=[],
        data_lineage={"records": {}, "completeness": {}},
        telemetry_schema={"not_applicable": True,
                          "reason": "no telemetry in this release"},
        aggregation_methodology={"not_applicable": True,
                                 "reason": "no aggregates in this release"},
        contribution_bounds={"max_per_unit": 1, "bound_method": "dedup"},
        differencing_reidentification_analysis={"risk": "low",
                                                "notes": "single release"},
        retention_behavior={"retention_days": 90, "policy": "rolling delete"},
        deletion_behavior={"method": "crypto-shred keys", "verified": False,
                           "notes": "deletion not yet exercised end-to-end"},
        third_parties=[],
        external_processors=[],
        known_risks=[{"risk": "small-cell disclosure", "severity": "medium"}],
        unresolved_questions=["confirm bucket suppression threshold with counsel"],
        security_controls=[{"control": "egress guard default-deny"}],
        proposed_public_claims=["Keel processed 1,000 applications in testing."],
    )
    kwargs.update(overrides)
    return kwargs


class TestPacketCompleteness(unittest.TestCase):
    def test_all_required_fields_present(self):
        packet = prepare_packet(**make_packet_kwargs())
        for f in PACKET_FIELDS:
            self.assertIn(f, packet, f"missing required field: {f}")
        self.assertEqual(set(packet.keys()), set(PACKET_FIELDS))

    def test_packet_digest_verifies(self):
        packet = prepare_packet(**make_packet_kwargs())
        ok, reason = verify_packet_digest(packet)
        self.assertTrue(ok, reason)

    def test_tampered_packet_fails_digest(self):
        packet = prepare_packet(**make_packet_kwargs())
        packet["purpose"] = "something else entirely"
        ok, _ = verify_packet_digest(packet)
        self.assertFalse(ok)

    def test_missing_field_raises_not_partial(self):
        with self.assertRaises(ValueError) as ctx:
            prepare_packet(**make_packet_kwargs(deletion_behavior=None))
        self.assertIn("deletion_behavior", str(ctx.exception))

    def test_null_field_raises(self):
        with self.assertRaises(ValueError):
            prepare_packet(**make_packet_kwargs(known_risks=None))

    def test_empty_string_destination_raises(self):
        with self.assertRaises(ValueError):
            prepare_packet(**make_packet_kwargs(intended_destination="  "))

    def test_digest_mismatch_between_inventory_and_map_raises(self):
        with self.assertRaises(ValueError) as ctx:
            prepare_packet(**make_packet_kwargs(
                artifact_sha256={"artifact-001": "b" * 64,
                                 "artifact-999": "c" * 64}))
        self.assertIn("artifact-999", str(ctx.exception))

    def test_missing_digest_for_inventoried_artifact_raises(self):
        with self.assertRaises(ValueError):
            prepare_packet(**make_packet_kwargs(artifact_sha256={}))

    def test_bad_digest_format_raises(self):
        with self.assertRaises(ValueError):
            prepare_packet(**make_packet_kwargs(
                artifact_sha256={"artifact-001": "not-a-digest"}))

    def test_not_applicable_requires_reason(self):
        with self.assertRaises(ValueError):
            prepare_packet(**make_packet_kwargs(
                telemetry_schema={"not_applicable": True}))

    def test_empty_claim_string_raises(self):
        with self.assertRaises(ValueError):
            prepare_packet(**make_packet_kwargs(
                proposed_public_claims=["valid claim", "   "]))

    def test_packet_never_contains_decision(self):
        packet = prepare_packet(**make_packet_kwargs())
        blob = str(packet).lower()
        self.assertNotIn("approved", blob)
        self.assertNotIn("privacy_counsel_approved", blob)


if __name__ == "__main__":
    unittest.main()
