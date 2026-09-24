#!/usr/bin/env python3
"""Egress regressions for the release gate (bypass-3 fix).

check_egress()/gate_release pin the default-DENY contract:

  1. missing counsel decision      -> DENY
  2. digest mismatch               -> DENY
  3. expired decision              -> DENY
  4. revoked decision              -> DENY (covered in test_publication_guard)
  5. tampered decision ledger      -> DENY (seal mismatch)
  6. exact (release_id, digest) approval -> ALLOW
  7. gate_release.main() with no approval -> exit 1, allowed:false JSON
  8. gate_release.main() with approval   -> exit 0

Uses fixture Ed25519 reviewer keys; state dir is a tmp path.
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.expanduser("~/workspace"))

from keel.privacy import _ed25519  # noqa: E402
from keel.privacy.counsel_decision import (  # noqa: E402
    register_authority, record_decision, decision_signing_payload)
from keel.privacy.publication_guard import (  # noqa: E402
    check_egress, EgressDenied, assert_egress_allowed)
from keel.privacy import gate_release  # noqa: E402

RELEASE = "keel-0.7.0-test"
DIGEST = hashlib.sha256(b"synthetic release artifact").hexdigest()


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class EgressRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.seed, pub = _ed25519.generate_keypair()
        self.auth = register_authority({
            "authority_reference": "ENG-EGRESS-001",
            "authority_name": "Egress Reviewer",
            "authority_type": "external_privacy_counsel",
            "scope_note": "fixture",
            "engagement_start": "2026-01-01T00:00:00Z",
            "registered_by": "fixture", "registration_evidence": "fixture",
            "reviewer_public_key": pub.hex(),
        }, state_dir=self.state)
        reg_at = datetime.fromisoformat(
            self.auth["registered_at"].replace("Z", "+00:00"))
        # The record_decision tripwire refuses decided_at <= registered_at
        # (a review takes nonzero time), so all fixture timestamps derive
        # from the authority's registration.
        self.reg_at = reg_at
        self.decided_at = _iso(reg_at + timedelta(microseconds=1))
        self.expires_at = _iso(reg_at + timedelta(days=30))

    def tearDown(self):
        self.tmp.cleanup()

    def _approve(self, release_id=RELEASE, digest=DIGEST,
                 decided_at=None, expires_at=None):
        decided_at = decided_at or self.decided_at
        expires_at = expires_at or self.expires_at
        payload = decision_signing_payload(
            release_id=release_id, artifact_digest=digest, decision="approved",
            actor="Egress Reviewer", authority_reference="ENG-EGRESS-001",
            decided_at=decided_at, expires_at=expires_at,
            conditions=[], packet_digest="p" * 64)
        record_decision({
            "release_id": release_id, "artifact_digest": digest,
            "decision": "approved", "actor": "Egress Reviewer",
            "authority_reference": "ENG-EGRESS-001", "conditions": [],
            "decided_at": decided_at, "expires_at": expires_at,
            "packet_digest": "p" * 64,
            "reviewer_signature": _ed25519.sign(self.seed, payload).hex(),
        }, state_dir=self.state)

    def test_missing_decision_denies(self):
        v = check_egress(RELEASE, DIGEST, state_dir=self.state)
        self.assertFalse(v["allowed"])
        self.assertIn("DENY", v["reason"])
        with self.assertRaises(EgressDenied):
            assert_egress_allowed(RELEASE, DIGEST, state_dir=self.state)

    def test_digest_mismatch_denies(self):
        self._approve()
        v = check_egress(RELEASE, "0" * 64, state_dir=self.state)
        self.assertFalse(v["allowed"])

    def test_expired_decision_denies(self):
        self._approve()
        # Well-formed and valid right now ...
        self.assertTrue(check_egress(
            RELEASE, DIGEST, state_dir=self.state)["allowed"])
        # ... but expired 40 days out (verify_decision takes an explicit now).
        from keel.privacy.counsel_decision import verify_decision
        future = _iso(self.reg_at + timedelta(days=40))
        self.assertFalse(verify_decision(RELEASE, DIGEST,
                                         state_dir=self.state, now=future))

    def test_tampered_ledger_denies(self):
        self._approve()
        self.assertTrue(check_egress(
            RELEASE, DIGEST, state_dir=self.state)["allowed"])
        # Flip the digest inside the sealed decision body (seal unchanged).
        path = os.path.join(self.state, "decisions.jsonl")
        with open(path) as f:
            rec = json.loads(f.readline())
        rec["body"]["artifact_digest"] = "f" * 64
        with open(path, "w") as f:
            f.write(json.dumps(rec) + "\n")
        v = check_egress(RELEASE, DIGEST, state_dir=self.state)
        self.assertFalse(v["allowed"])
        self.assertIn("DENY", v["reason"])

    def test_exact_approval_allows(self):
        self._approve()
        v = check_egress(RELEASE, DIGEST, state_dir=self.state)
        self.assertTrue(v["allowed"])
        self.assertEqual(v["decision"]["decision"], "approved")

    def _artifact(self, content=b"synthetic release artifact"):
        p = os.path.join(self.tmp.name, "keel-test.zip")
        with open(p, "wb") as f:
            f.write(content)
        return p

    def test_gate_release_denies_without_approval(self):
        art = self._artifact()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = gate_release.main(["--release-id", RELEASE,
                                    "--artifact", art,
                                    "--state-dir", self.state])
        self.assertEqual(rc, 1)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["allowed"])
        self.assertEqual(out["artifact_digest"], DIGEST)

    def test_gate_release_allows_with_exact_approval(self):
        art = self._artifact()
        self._approve()  # binds the exact digest of the artifact bytes
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = gate_release.main(["--release-id", RELEASE,
                                    "--artifact", art,
                                    "--state-dir", self.state])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["allowed"])

    def test_gate_release_missing_artifact_denies(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = gate_release.main(["--release-id", RELEASE,
                                    "--artifact",
                                    os.path.join(self.tmp.name, "nope.zip"),
                                    "--state-dir", self.state])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(buf.getvalue())["allowed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
