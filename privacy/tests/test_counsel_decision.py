"""Regression: counsel decisions — binding, signatures, expiry, revocation.

The fixture keypair below plays the reviewer EXPLICITLY (test-only): in
production the private key never exists on this machine. Every fabrication
an agent could attempt through the controller's API is shown to fail.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.expanduser("~/workspace"))

from keel.privacy import _ed25519
from keel.privacy.counsel_decision import (
    verify_decision, record_decision, revoke_decision,
    register_authority, get_authority, list_authorities,
    decision_signing_payload, DecisionError, AuthorityNotRegistered,
    _ledgers,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
PACKET_DIGEST = "p" * 64
RELEASE = "PR-20260918-test01"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class DecisionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        # Fixture reviewer: keypair generated in-test, public key registered.
        # This models the owner ceremony; production keys never live here.
        self.seed, self.pub = _ed25519.generate_keypair()
        self.auth = register_authority({
            "authority_reference": "ENG-TEST-001",
            "authority_name": "Test Reviewer",
            "authority_type": "external_privacy_counsel",
            "scope_note": "test fixture authority",
            "engagement_start": "2026-01-01T00:00:00Z",
            "registered_by": "test fixture (owner ceremony stand-in)",
            "registration_evidence": "fixture",
            "reviewer_public_key": self.pub.hex(),
        }, state_dir=self.state)
        reg_at = datetime.fromisoformat(
            self.auth["registered_at"].replace("Z", "+00:00"))
        self.decided_at = _iso(reg_at + timedelta(microseconds=1))
        self.expires_at = _iso(reg_at + timedelta(days=90))

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _signed(self, digest=DIGEST_A, decision="approved",
                release_id=RELEASE, decided_at=None, expires_at=None,
                seed=None, authority_reference="ENG-TEST-001",
                interval_days=None):
        decided_at = decided_at or self.decided_at
        body = {
            "release_id": release_id,
            "artifact_digest": digest,
            "decision": decision,
            "actor": "Test Reviewer",
            "authority_reference": authority_reference,
            "conditions": [],
            "decided_at": decided_at,
            "packet_digest": PACKET_DIGEST,
        }
        if expires_at:
            body["expires_at"] = expires_at
        elif interval_days:
            body["review_interval_days"] = interval_days
        else:
            body["expires_at"] = self.expires_at
        resolved_expiry = body.get("expires_at") or _iso(
            datetime.fromisoformat(decided_at.replace("Z", "+00:00"))
            + timedelta(days=interval_days))
        payload = decision_signing_payload(
            release_id=release_id, artifact_digest=digest, decision=decision,
            actor="Test Reviewer", authority_reference=authority_reference,
            decided_at=decided_at, expires_at=resolved_expiry,
            conditions=[], packet_digest=PACKET_DIGEST)
        body["reviewer_signature"] = _ed25519.sign(
            seed or self.seed, payload).hex()
        return record_decision(body, state_dir=self.state)

    # -- tests -----------------------------------------------------------
    def test_no_decision_verifies_false(self):
        self.assertFalse(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))

    def test_empty_registry_verifies_false(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(verify_decision(RELEASE, DIGEST_A, state_dir=d))

    def test_unregistered_authority_refused(self):
        with self.assertRaises(AuthorityNotRegistered):
            self._signed(authority_reference="ENG-NOPE")

    def test_genuine_signed_decision_verifies(self):
        self._signed()
        self.assertTrue(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))

    def test_approved_with_conditions_verifies(self):
        rec = self._signed(decision="approved_with_conditions")
        self.assertEqual(rec["decision"], "approved_with_conditions")
        self.assertTrue(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))

    def test_denied_decision_never_verifies(self):
        self._signed(decision="denied")
        self.assertFalse(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))

    def test_needs_information_never_verifies(self):
        self._signed(decision="needs_information")
        self.assertFalse(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))

    def test_wrong_key_signature_refused_at_record(self):
        other_seed, _ = _ed25519.generate_keypair()
        with self.assertRaises(DecisionError) as ctx:
            self._signed(seed=other_seed)
        self.assertIn("signature", str(ctx.exception).lower())

    def test_missing_signature_refused(self):
        body = {"release_id": RELEASE, "artifact_digest": DIGEST_A,
                "decision": "approved", "actor": "X",
                "authority_reference": "ENG-TEST-001", "conditions": [],
                "decided_at": self.decided_at, "packet_digest": PACKET_DIGEST,
                "expires_at": self.expires_at}
        with self.assertRaises(DecisionError):
            record_decision(body, state_dir=self.state)

    def test_fabricated_raw_json_never_verifies(self):
        # An agent drops a plausible "approved" JSON straight into the store.
        path = os.path.join(self.state, "decisions.jsonl")
        fake = {"seq": 1, "prev_seal": "0" * 64, "seal": "f" * 64,
                "body": {"record_type": "decision", "release_id": RELEASE,
                         "artifact_digest": DIGEST_A, "decision": "approved",
                         "actor": "Mallory", "authority_reference": "ENG-FAKE",
                         "conditions": [], "decided_at": self.decided_at,
                         "expires_at": self.expires_at,
                         "packet_digest": PACKET_DIGEST,
                         "reviewer_signature": "0" * 128}}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(fake) + "\n")
        self.assertFalse(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))

    def test_forged_seal_chain_without_private_key_fails(self):
        # Strongest API-level forgery: valid seal chain, registered authority,
        # but no valid reviewer signature (the agent lacks the private key).
        forged_body = {
            "record_type": "decision", "release_id": RELEASE,
            "artifact_digest": DIGEST_A, "decision": "approved",
            "actor": "Mallory", "authority_reference": "ENG-TEST-001",
            "conditions": [], "decided_at": self.decided_at,
            "expires_at": self.expires_at, "packet_digest": PACKET_DIGEST,
            "reviewer_signature": "ab" * 64,  # well-formed hex, wrong signature
            "recorded_at": self.decided_at,
        }
        _, decisions = _ledgers(self.state)
        decisions.append(forged_body)  # seals are valid; signature is not
        self.assertFalse(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))

    def test_tampered_ledger_body_fails_closed(self):
        self._signed()
        self.assertTrue(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))
        path = os.path.join(self.state, "decisions.jsonl")
        lines = open(path, encoding="utf-8").read().splitlines()
        rec = json.loads(lines[0])
        rec["body"]["decision"] = "approved"  # no-op edit still breaks the seal
        rec["body"]["notes"] = "tampered"
        lines[0] = json.dumps(rec)
        open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        self.assertFalse(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))

    def test_mutation_invalidates_authorization(self):
        # Approval binds DIGEST_A. The artifact is mutated -> DIGEST_B.
        self._signed(digest=DIGEST_A)
        self.assertTrue(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))
        self.assertFalse(verify_decision(RELEASE, DIGEST_B, state_dir=self.state))

    def test_decision_bound_to_wrong_digest_fails(self):
        self._signed(digest=DIGEST_A)
        self.assertFalse(verify_decision(RELEASE, "c" * 64, state_dir=self.state))
        self.assertFalse(verify_decision("PR-OTHER", DIGEST_A, state_dir=self.state))

    def test_revocation_kills_approval(self):
        self._signed()
        self.assertTrue(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))
        revoke_decision(RELEASE, DIGEST_A, actor="Test Reviewer",
                        authority_reference="ENG-TEST-001",
                        reason="superseded analysis", state_dir=self.state)
        self.assertFalse(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))

    def test_revoke_nonexistent_raises(self):
        with self.assertRaises(DecisionError):
            revoke_decision(RELEASE, DIGEST_A, actor="X",
                            authority_reference="ENG-TEST-001", reason="y",
                            state_dir=self.state)

    def test_expired_decision_fails(self):
        reg_at = datetime.fromisoformat(
            self.auth["registered_at"].replace("Z", "+00:00"))
        decided = _iso(reg_at + timedelta(microseconds=1))
        expires = _iso(reg_at + timedelta(seconds=120))
        self._signed(decided_at=decided, expires_at=expires)
        # Inside the window: valid.
        self.assertTrue(verify_decision(
            RELEASE, DIGEST_A, state_dir=self.state,
            now=_iso(reg_at + timedelta(seconds=90))))
        # After expiry: denied.
        self.assertFalse(verify_decision(
            RELEASE, DIGEST_A, state_dir=self.state,
            now=_iso(reg_at + timedelta(seconds=3600))))

    def test_missing_expiry_refused(self):
        body = {"release_id": RELEASE, "artifact_digest": DIGEST_A,
                "decision": "approved", "actor": "X",
                "authority_reference": "ENG-TEST-001", "conditions": [],
                "decided_at": self.decided_at, "packet_digest": PACKET_DIGEST,
                "reviewer_signature": "ab" * 64}
        with self.assertRaises(DecisionError) as ctx:
            record_decision(body, state_dir=self.state)
        self.assertIn("expiry", str(ctx.exception).lower())

    def test_review_interval_days_accepted(self):
        self._signed(interval_days=30)
        self.assertTrue(verify_decision(RELEASE, DIGEST_A, state_dir=self.state))

    def test_instant_self_registration_plus_approval_refused(self):
        # decided_at at-or-before the authority's registered_at is refused.
        with self.assertRaises(DecisionError):
            self._signed(decided_at=self.auth["registered_at"])

    def test_duplicate_authority_reference_refused(self):
        with self.assertRaises(DecisionError):
            register_authority({
                "authority_reference": "ENG-TEST-001",
                "authority_name": "Someone Else",
                "authority_type": "external_privacy_counsel",
                "scope_note": "x", "engagement_start": "2026-01-01T00:00:00Z",
                "registered_by": "y", "registration_evidence": "z",
                "reviewer_public_key": self.pub.hex(),
            }, state_dir=self.state)

    def test_bad_public_key_refused(self):
        with self.assertRaises(DecisionError):
            register_authority({
                "authority_reference": "ENG-TEST-002",
                "authority_name": "Bad Key",
                "authority_type": "external_privacy_counsel",
                "scope_note": "x", "engagement_start": "2026-01-01T00:00:00Z",
                "registered_by": "y", "registration_evidence": "z",
                "reviewer_public_key": "not-hex",
            }, state_dir=self.state)

    def test_authority_lookup(self):
        self.assertIsNotNone(get_authority("ENG-TEST-001", state_dir=self.state))
        self.assertIsNone(get_authority("ENG-NOPE", state_dir=self.state))
        self.assertEqual(len(list_authorities(state_dir=self.state)), 1)

    def test_malformed_digest_never_verifies(self):
        self._signed()
        self.assertFalse(verify_decision(RELEASE, "not-a-digest",
                                         state_dir=self.state))
        self.assertFalse(verify_decision("", DIGEST_A, state_dir=self.state))


if __name__ == "__main__":
    unittest.main()
