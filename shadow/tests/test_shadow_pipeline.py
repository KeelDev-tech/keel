#!/usr/bin/env python3
"""Regression tests: shadow pipeline hard rules.

Hard rules under test:
  1. execution_authorized is False without a COMPLETELY SEPARATE valid
     execution authorization -- even with all six sources + a decision.
  2. Source completeness alone NEVER authorizes execution.
  3. A synthetic fixture NEVER grants consent and NEVER authorizes execution.
  4. Every shadow run honestly reports SHADOW_VALIDATED=false.

All fixtures synthetic and in-memory; the real review stores are snapshotted
before/after to prove the shadow run persists nothing.
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

from review.packet import build_packet, write_packet  # noqa: E402
from review.decision import HUMAN_DECISION_REQUIRED  # noqa: E402
from shadow.pipeline import (  # noqa: E402
    AUTHZ_STORE,
    REVIEW_DIR,
    consent_stage,
    execution_policy_stage,
    export_stage,
    run_shadow,
    validate_execution_authorization,
)
from _fixtures import (  # noqa: E402
    SCOPE,
    iso_in,
    six_family_records,
    synthetic_authz_fixture,
    synthetic_decision_fixture,
    synthetic_shadow_fixture,
)


def _ready_packet(tmp_path: Path, packet_id="TEST-SHD-001"):
    packet = build_packet(packet_id, six_family_records())
    path = write_packet(packet, tmp_path / f"{packet_id}-review-packet.json")
    return packet, path


def _granted_consent(decision_id="synthetic-decision"):
    return {"consent_granted": True, "synthetic": False,
            "decision_id": decision_id, "reason": "mechanism test"}


class TestExecutionBlocked(unittest.TestCase):
    def test_blocked_without_separate_authorization(self):
        """All six sources + a decision-shaped consent, but NO separate
        execution authorization -> execution_authorized False."""
        with tempfile.TemporaryDirectory() as tmp:
            packet, _ = _ready_packet(Path(tmp))
            policy = execution_policy_stage(
                consent=_granted_consent(),
                execution_authorization=None,
                packet=packet,
                decision={"decision_id": "synthetic-decision"},
                attested_genuine=True,
                now=datetime.now(timezone.utc))
            self.assertFalse(policy["execution_authorized"])
            self.assertIn("source completeness alone", policy["reason"])

    def test_source_completeness_alone_never_authorizes(self):
        """The revision adapter's envelope_inputs_ready (structural only)
        must not leak into authorization."""
        with tempfile.TemporaryDirectory() as tmp:
            packet, _ = _ready_packet(Path(tmp))
            export = export_stage(
                synthetic_families={r["component"]: r
                                    for r in six_family_records()})
            from shadow.pipeline import revision_adapter_stage
            adapted = revision_adapter_stage(export)
            self.assertTrue(adapted["envelope_inputs_ready"])
            policy = execution_policy_stage(
                consent=_granted_consent(),
                execution_authorization=None,  # complete but unauthorized
                packet=packet,
                decision={"decision_id": "synthetic-decision"},
                attested_genuine=True,
                now=datetime.now(timezone.utc))
            self.assertFalse(policy["execution_authorized"])

    def test_unattested_artifacts_never_authorize(self):
        """attested_genuine=False (every run_shadow path) -> denied, even
        with a perfect separate authorization."""
        with tempfile.TemporaryDirectory() as tmp:
            packet, _ = _ready_packet(Path(tmp))
            authz = synthetic_authz_fixture(
                decision_id="synthetic-decision",
                packet_sha256=packet["packet_sha256"], scope=packet["scope"])
            policy = execution_policy_stage(
                consent=_granted_consent(),
                execution_authorization=authz,
                packet=packet,
                decision={"decision_id": "synthetic-decision"},
                attested_genuine=False,
                now=datetime.now(timezone.utc))
            self.assertFalse(policy["execution_authorized"])
            self.assertIn("not attested genuine", policy["reason"])

    def test_synthetic_decision_never_grants_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, _ = _ready_packet(Path(tmp))
            export = export_stage(
                synthetic_families={r["component"]: r
                                    for r in six_family_records()})
            consent = consent_stage(
                packet, export,
                synthetic_decision=synthetic_decision_fixture(
                    packet["packet_sha256"]),
                now=datetime.now(timezone.utc))
            self.assertFalse(consent["consent_granted"])
            self.assertTrue(consent["synthetic"])


class TestPolicyMechanism(unittest.TestCase):
    """Pure-function mechanism tests with clearly-labeled synthetic inputs
    (never persisted). Proves the policy is a real gate, not hardcoded False."""

    def _valid_chain(self, tmp_path):
        packet, _ = _ready_packet(tmp_path, "TEST-SHD-MECH")
        decision = {"decision_id": "synthetic-decision"}
        authz = synthetic_authz_fixture(
            decision_id="synthetic-decision",
            packet_sha256=packet["packet_sha256"], scope=packet["scope"])
        return packet, decision, authz

    def test_complete_genuine_chain_authorizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, decision, authz = self._valid_chain(Path(tmp))
            policy = execution_policy_stage(
                consent=_granted_consent(),
                execution_authorization=authz,
                packet=packet, decision=decision,
                attested_genuine=True, now=datetime.now(timezone.utc))
            self.assertTrue(policy["execution_authorized"])

    def test_expired_authz_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, decision, authz = self._valid_chain(Path(tmp))
            authz["expires_at"] = (datetime.now(timezone.utc)
                                   - timedelta(days=1)).isoformat()
            validation = validate_execution_authorization(
                authz, packet=packet, decision=decision,
                now=datetime.now(timezone.utc))
            self.assertFalse(validation["valid"])
            policy = execution_policy_stage(
                consent=_granted_consent(),
                execution_authorization=authz,
                packet=packet, decision=decision,
                attested_genuine=True, now=datetime.now(timezone.utc))
            self.assertFalse(policy["execution_authorized"])

    def test_revoked_authz_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, decision, authz = self._valid_chain(Path(tmp))
            authz["revoked"] = True
            validation = validate_execution_authorization(
                authz, packet=packet, decision=decision,
                now=datetime.now(timezone.utc))
            self.assertFalse(validation["valid"])
            self.assertIn("authorization_revoked", validation["reasons"])

    def test_packet_sha_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, decision, authz = self._valid_chain(Path(tmp))
            authz["packet_sha256"] = "0" * 64
            validation = validate_execution_authorization(
                authz, packet=packet, decision=decision,
                now=datetime.now(timezone.utc))
            self.assertFalse(validation["valid"])
            self.assertIn("packet_sha256_mismatch", validation["reasons"])

    def test_scope_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, decision, authz = self._valid_chain(Path(tmp))
            authz["scope"] = {**packet["scope"], "role_id": "other-role"}
            validation = validate_execution_authorization(
                authz, packet=packet, decision=decision,
                now=datetime.now(timezone.utc))
            self.assertFalse(validation["valid"])
            self.assertIn("scope_mismatch", validation["reasons"])

    def test_decision_id_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, decision, authz = self._valid_chain(Path(tmp))
            authz["decision_id"] = "different-decision"
            validation = validate_execution_authorization(
                authz, packet=packet, decision=decision,
                now=datetime.now(timezone.utc))
            self.assertFalse(validation["valid"])
            self.assertIn("decision_id_mismatch", validation["reasons"])

    def test_missing_authority_ref_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, decision, authz = self._valid_chain(Path(tmp))
            authz["authority_ref"] = " "
            validation = validate_execution_authorization(
                authz, packet=packet, decision=decision,
                now=datetime.now(timezone.utc))
            self.assertFalse(validation["valid"])

    def test_no_consent_no_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, decision, authz = self._valid_chain(Path(tmp))
            policy = execution_policy_stage(
                consent={"consent_granted": False, "synthetic": False,
                         "reason": HUMAN_DECISION_REQUIRED},
                execution_authorization=authz,
                packet=packet, decision=decision,
                attested_genuine=True, now=datetime.now(timezone.utc))
            self.assertFalse(policy["execution_authorized"])


class TestShadowRunHonest(unittest.TestCase):
    def _snapshot_stores(self):
        return {name: (REVIEW_DIR / name).exists() for name in
                ("decisions.jsonl", "decision-requests.jsonl",
                 "execution-authorizations.jsonl")}

    def test_real_run_against_blocked_packet(self):
        """The genuine KEEL-CANARY-001 packet is BLOCKED-PENDING-EVIDENCE:
        the shadow run must report execution blocked and SHADOW_VALIDATED=false."""
        before = self._snapshot_stores()
        packet_path = (REVIEW_DIR / "KEEL-CANARY-001-review-packet.json")
        self.assertTrue(packet_path.exists())
        result = run_shadow(packet_path)
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["shadow_validated"])
        self.assertFalse(result["synthetic_fixture"])
        self.assertEqual(result["packet_status"], "BLOCKED-PENDING-EVIDENCE")
        self.assertIn("pending", result["shadow_validated_reason"].lower())
        stages = result["stages"]
        self.assertFalse(stages["assurance"]["passed"])
        self.assertEqual(stages["consent"]["reason"], HUMAN_DECISION_REQUIRED)
        self.assertFalse(stages["trust"]["source_authenticity_verified"])
        # The shadow run persisted nothing to the genuine stores.
        self.assertEqual(self._snapshot_stores(), before)

    def test_synthetic_run_never_persists_and_never_validates(self):
        before = self._snapshot_stores()
        with tempfile.TemporaryDirectory() as tmp:
            packet, path = _ready_packet(Path(tmp), "TEST-SHD-SYN")
            families = {r["component"]: r for r in six_family_records()}
            fixture = synthetic_shadow_fixture(packet, families)
            result = run_shadow(path, synthetic_fixture=fixture)
            self.assertTrue(result["synthetic_fixture"])
            self.assertFalse(result["execution_authorized"])
            self.assertFalse(result["shadow_validated"])
            self.assertIn("SYNTHETIC", result["synthetic_label"])
            self.assertTrue(result["stages"]["consent"]["synthetic"])
            self.assertFalse(
                result["stages"]["execution_policy"]["execution_authorized"])
        # Nothing leaked into the genuine stores.
        self.assertEqual(self._snapshot_stores(), before)
        self.assertFalse(AUTHZ_STORE.exists())

    def test_result_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, path = _ready_packet(Path(tmp), "TEST-SHD-SCH")
            result = run_shadow(path)
            for key in ("pipeline", "packet_id", "packet_sha256",
                        "packet_status", "synthetic_fixture", "stages",
                        "execution_authorized", "execution_authorized_reason",
                        "shadow_validated", "shadow_validated_reason"):
                self.assertIn(key, result)
            for stage in ("export", "revision_adapter", "assurance", "trust",
                          "consent", "execution_policy"):
                self.assertIn(stage, result["stages"])


if __name__ == "__main__":
    unittest.main()
