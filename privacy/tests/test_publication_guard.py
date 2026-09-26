"""Regression: publication guard — default DENY, allow only on verified decision."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.expanduser("~/workspace"))

from keel.privacy import _ed25519
from keel.privacy.counsel_decision import (
    register_authority, record_decision, revoke_decision,
    decision_signing_payload,
)
from keel.privacy.publication_guard import (
    check_egress, check_release, assert_egress_allowed, EgressDenied,
)
from keel.privacy.release_manifest import build_release
from keel.privacy.inventory import ArtifactRecord

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
RELEASE = "PR-20260918-guard01"


def _rec(aid, digest):
    return ArtifactRecord(artifact_id=aid, name=aid + ".md", sha256=digest,
                          size_bytes=10, media_type="text/markdown")


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        # Fixture reviewer authority, registered once per test.
        self.seed, pub = _ed25519.generate_keypair()
        self.auth = register_authority({
            "authority_reference": "ENG-GUARD-001",
            "authority_name": "Guard Reviewer",
            "authority_type": "external_privacy_counsel",
            "scope_note": "fixture",
            "engagement_start": "2026-01-01T00:00:00Z",
            "registered_by": "fixture", "registration_evidence": "fixture",
            "reviewer_public_key": pub.hex(),
        }, state_dir=self.state)
        reg_at = datetime.fromisoformat(
            self.auth["registered_at"].replace("Z", "+00:00"))
        self.decided_at = _iso(reg_at + timedelta(microseconds=1))
        self.expires_at = _iso(reg_at + timedelta(days=30))

    def tearDown(self):
        self.tmp.cleanup()

    def _approve(self, release_id=RELEASE, digest=DIGEST_A):
        payload = decision_signing_payload(
            release_id=release_id, artifact_digest=digest, decision="approved",
            actor="Guard Reviewer", authority_reference="ENG-GUARD-001",
            decided_at=self.decided_at, expires_at=self.expires_at,
            conditions=[], packet_digest="p" * 64)
        record_decision({
            "release_id": release_id, "artifact_digest": digest,
            "decision": "approved", "actor": "Guard Reviewer",
            "authority_reference": "ENG-GUARD-001", "conditions": [],
            "decided_at": self.decided_at, "expires_at": self.expires_at,
            "packet_digest": "p" * 64,
            "reviewer_signature": _ed25519.sign(self.seed, payload).hex(),
        }, state_dir=self.state)

    # -- tests -----------------------------------------------------------
    def test_default_deny_unclassified_uncleared(self):
        v = check_egress(RELEASE, DIGEST_A, state_dir=self.state)
        self.assertFalse(v["allowed"])
        self.assertIn("DENY", v["reason"])
        self.assertIsNone(v["decision"])

    def test_default_deny_empty_registry(self):
        with tempfile.TemporaryDirectory() as d:
            v = check_egress(RELEASE, DIGEST_A, state_dir=d)
            self.assertFalse(v["allowed"])

    def test_allow_with_verified_decision(self):
        self._approve()
        v = check_egress(RELEASE, DIGEST_A, destination="example.invalid",
                         state_dir=self.state)
        self.assertTrue(v["allowed"])
        self.assertEqual(v["decision"]["decision"], "approved")
        self.assertEqual(v["decision"]["authority_reference"], "ENG-GUARD-001")

    def test_deny_for_unapproved_artifact_in_same_release(self):
        self._approve(digest=DIGEST_A)
        v = check_egress(RELEASE, DIGEST_B, state_dir=self.state)
        self.assertFalse(v["allowed"])

    def test_assert_raises_on_deny(self):
        with self.assertRaises(EgressDenied):
            assert_egress_allowed(RELEASE, DIGEST_A, state_dir=self.state)

    def test_assert_returns_verdict_on_allow(self):
        self._approve()
        v = assert_egress_allowed(RELEASE, DIGEST_A, state_dir=self.state)
        self.assertTrue(v["allowed"])

    def test_check_release_denies_on_single_bad_artifact(self):
        manifest = build_release([_rec("artifact-001", DIGEST_A),
                                  _rec("artifact-002", DIGEST_B)])
        self._approve(release_id=manifest.release_id, digest=DIGEST_A)  # only A
        result = check_release(manifest, state_dir=self.state)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["artifacts_denied"], 1)
        self.assertEqual(result["denied_artifact_ids"], ["artifact-002"])

    def test_check_release_allows_when_all_verified(self):
        manifest = build_release([_rec("artifact-001", DIGEST_A),
                                  _rec("artifact-002", DIGEST_B)])
        self._approve(release_id=manifest.release_id, digest=DIGEST_A)
        self._approve(release_id=manifest.release_id, digest=DIGEST_B)
        result = check_release(manifest, state_dir=self.state)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["artifacts_checked"], 2)

    def test_revocation_re_denies(self):
        self._approve()
        self.assertTrue(check_egress(RELEASE, DIGEST_A,
                                     state_dir=self.state)["allowed"])
        revoke_decision(RELEASE, DIGEST_A, actor="Guard Reviewer",
                        authority_reference="ENG-GUARD-001",
                        reason="new analysis", state_dir=self.state)
        v = check_egress(RELEASE, DIGEST_A, state_dir=self.state)
        self.assertFalse(v["allowed"])
        self.assertIn("DENY", v["reason"])


if __name__ == "__main__":
    unittest.main()
