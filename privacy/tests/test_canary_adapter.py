"""Regression: canary-evidence adapter.

Uses the REAL KEEL-CANARY-001 evidence files read-only (they are workstream
E's outputs; this suite never writes to keel/canary/). Asserts the exact
schema is consumed precisely and the BLOCKED verdict becomes
BLOCKED-PENDING-EVIDENCE with no approval request issuable.
"""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/workspace"))

from keel.privacy.canary_adapter import (
    load_canary_evidence, evidence_status, assert_evidence_ready,
    summarize_evidence, canary_packet_inputs,
    CanarySchemaError, EvidenceBlocked,
    EVIDENCE_READY, EVIDENCE_BLOCKED,
)

BUNDLE = os.path.expanduser(
    "~/workspace/keel/canary/evidence/keel-canary-001/bundle.json")
VERDICT = os.path.expanduser(
    "~/workspace/keel/canary/evidence/keel-canary-001/verdict.json")


def _synthetic_pass_evidence(tmpdir: str):
    """Build a synthetic all-authentic, all-passing fixture (canary schema
    natively supports synthetic_fixture provenance; here every record is
    authentic and every check passes)."""
    record = {
        "workspace_id": "ws-test", "role_id": "role-1",
        "application_id": "app-1", "action": "observe_public_posting",
        "component": "target", "source_ref": "https://example.invalid/jobs/1",
        "source_version": "v1", "observed_at": "2026-09-18T20:00:00Z",
        "provenance": "authentic_observation", "provenance_reason": "captured",
        "expires_at": None, "revoked": False, "revocation_ref": None,
        "material_digest": "d" * 16, "capture_version": "keel-canary-capture/0.8.0",
        "payload": {"ok": True},
    }
    checks = [{"name": f"check-{i}", "passed": True, "detail": "ok"}
              for i in range(13)]
    bundle = {"schema": "keel.canary.bundle/v1",
              "capture_version": "keel-canary-capture/0.8.0",
              "record_count": 1, "records": [record]}
    verdict = {"schema": "keel.canary.verdict/v1",
               "validator_version": "keel-canary-validate/0.8.0",
               "verdict": "PASS", "role_id": "role-1", "application_id": "app-1",
               "missing_families": [], "families_without_records": [],
               "checks": checks, "failed_checks": [],
               "check_count": 13, "failed_count": 0}
    bpath = str(Path(tmpdir) / "bundle.json")
    vpath = str(Path(tmpdir) / "verdict.json")
    Path(bpath).write_text(json.dumps(bundle))
    Path(vpath).write_text(json.dumps(verdict))
    return bpath, vpath


class TestCanaryAdapterRealEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (os.path.exists(BUNDLE) and os.path.exists(VERDICT)):
            raise unittest.SkipTest("canary evidence files not present")
        cls.evidence = load_canary_evidence(BUNDLE, VERDICT)

    def test_exact_schema_parses(self):
        self.assertEqual(self.evidence.bundle["schema"], "keel.canary.bundle/v1")
        self.assertEqual(self.evidence.verdict["schema"], "keel.canary.verdict/v1")
        self.assertEqual(len(self.evidence.records), 6)

    def test_canary_001_is_blocked_pending_evidence(self):
        self.assertEqual(self.evidence.verdict["verdict"], "BLOCKED")
        self.assertEqual(self.evidence.verdict["failed_count"], 7)
        self.assertEqual(evidence_status(self.evidence), EVIDENCE_BLOCKED)

    def test_no_approval_request_on_blocked_evidence(self):
        with self.assertRaises(EvidenceBlocked) as ctx:
            assert_evidence_ready(self.evidence)
        msg = str(ctx.exception)
        self.assertIn("BLOCKED-PENDING-EVIDENCE", msg)
        self.assertIn("policy_compatibility", msg)

    def test_packet_inputs_record_the_block(self):
        inputs = canary_packet_inputs(self.evidence)
        lineage = inputs["data_lineage"]["canary_evidence"]
        self.assertEqual(lineage["evidence_status"], EVIDENCE_BLOCKED)
        self.assertEqual(lineage["failed_count"], 7)
        # The block is recorded in risks AND questions, not silently dropped.
        risks_blob = json.dumps(inputs["known_risks"])
        self.assertIn("BLOCKED-PENDING-EVIDENCE", risks_blob)
        self.assertIn("policy_compatibility", risks_blob)
        questions_blob = " ".join(inputs["unresolved_questions"])
        for family in ["policy", "form", "answers", "attachments", "route"]:
            self.assertIn(family, questions_blob)
        # Unknown-provenance records are called out as risks.
        self.assertIn("unknown", risks_blob)

    def test_summary_covers_all_records(self):
        s = summarize_evidence(self.evidence)
        self.assertEqual(len(s["records"]), 6)
        provs = {r["provenance"] for r in s["records"]}
        self.assertIn("authentic_observation", provs)
        self.assertIn("unknown", provs)


class TestCanaryAdapterSynthetic(unittest.TestCase):
    def test_clean_evidence_is_ready(self):
        with tempfile.TemporaryDirectory() as d:
            b, v = _synthetic_pass_evidence(d)
            ev = load_canary_evidence(b, v)
            self.assertEqual(evidence_status(ev), EVIDENCE_READY)
            assert_evidence_ready(ev)  # must not raise
            inputs = canary_packet_inputs(ev)
            self.assertEqual(
                inputs["data_lineage"]["canary_evidence"]["evidence_status"],
                EVIDENCE_READY)
            self.assertEqual(inputs["known_risks"], [])
            self.assertEqual(inputs["unresolved_questions"], [])

    def test_revoked_record_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            b, v = _synthetic_pass_evidence(d)
            bundle = json.loads(Path(b).read_text())
            bundle["records"][0]["revoked"] = True
            bundle["records"][0]["revocation_ref"] = "rev-1"
            Path(b).write_text(json.dumps(bundle))
            ev = load_canary_evidence(b, v)
            self.assertEqual(evidence_status(ev), EVIDENCE_BLOCKED)
            with self.assertRaises(EvidenceBlocked):
                assert_evidence_ready(ev)

    def test_schema_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as d:
            b, v = _synthetic_pass_evidence(d)
            bundle = json.loads(Path(b).read_text())
            bundle["schema"] = "keel.canary.bundle/v999"
            Path(b).write_text(json.dumps(bundle))
            with self.assertRaises(CanarySchemaError):
                load_canary_evidence(b, v)

    def test_record_count_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as d:
            b, v = _synthetic_pass_evidence(d)
            bundle = json.loads(Path(b).read_text())
            bundle["record_count"] = 99
            Path(b).write_text(json.dumps(bundle))
            with self.assertRaises(CanarySchemaError):
                load_canary_evidence(b, v)

    def test_record_missing_field_raises(self):
        with tempfile.TemporaryDirectory() as d:
            b, v = _synthetic_pass_evidence(d)
            bundle = json.loads(Path(b).read_text())
            del bundle["records"][0]["material_digest"]
            Path(b).write_text(json.dumps(bundle))
            with self.assertRaises(CanarySchemaError):
                load_canary_evidence(b, v)


if __name__ == "__main__":
    unittest.main()
