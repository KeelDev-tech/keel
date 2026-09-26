import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from security.errors import MemoryViolation
from security.injection.content_classifier import Origin, classify_source
from security.memory.provenance import (
    MemoryEntry, Provenance, hash_content)
from security.memory.quarantine import QuarantineStore
from security.memory.validator import enforce, validate


def _entry(content="some fact", source="job_listing", writer="worker-1",
           verification="unverified", confidence=0.8):
    prov = Provenance.create(content, source=source, writer=writer,
                             confidence=confidence,
                             verification=verification)
    return MemoryEntry(key="k1", content=content, provenance=prov)


class TestProvenance(unittest.TestCase):
    def test_create_carries_lineage(self):
        e = _entry()
        d = e.provenance.to_dict()
        for key in ("source", "writer", "timestamp", "confidence",
                    "sensitivity", "verification", "content_hash"):
            self.assertIn(key, d)

    def test_content_hash_matches(self):
        e = _entry(content="hello")
        self.assertEqual(e.provenance.content_hash, hash_content("hello"))

    def test_serialize_roundtrip(self):
        e = _entry()
        e2 = MemoryEntry.from_dict(e.to_dict())
        self.assertEqual(e2.provenance.source, "job_listing")
        self.assertEqual(e2.provenance.content_hash,
                         e.provenance.content_hash)

    def test_verification_states(self):
        e = _entry(verification="verified")
        self.assertEqual(e.provenance.verification, "verified")


class TestValidator(unittest.TestCase):
    def test_valid_entry(self):
        self.assertEqual(validate(_entry()), [])

    def test_tampered_content(self):
        e = _entry()
        e.content = "different fact"
        viols = validate(e)
        self.assertTrue(any("hash" in v for v in viols))

    def test_untrusted_cannot_self_verify(self):
        e = _entry(source="job_listing", verification="verified")
        viols = validate(e)
        self.assertTrue(viols)  # untrusted source must not be verified

    def test_trusted_policy_can_verify(self):
        e = _entry(source="policy_document",
                   writer="keel-security-authority",
                   verification="verified")
        self.assertEqual(validate(e), [])

    def test_enforce_raises(self):
        e = _entry()
        e.content = "tampered"
        with self.assertRaises(MemoryViolation):
            enforce(e)

    def test_enforce_passes(self):
        self.assertIsNotNone(enforce(_entry()))


class TestQuarantine(unittest.TestCase):
    def setUp(self):
        self.q = QuarantineStore()

    def test_untrusted_held(self):
        item = self.q.hold(_entry(), "untrusted source")
        self.assertEqual(item.entry.provenance.verification, "quarantined")
        self.assertEqual(len(self.q.pending()), 1)

    def test_promote_requires_trusted_verifier(self):
        item = self.q.hold(_entry(), "untrusted source")
        with self.assertRaises(MemoryViolation):
            self.q.promote(item.quarantine_id,
                           verifier_origin=classify_source("webpage"),
                           verifier_id="worker-1",
                           evidence="trust me")

    def test_promote_requires_evidence(self):
        item = self.q.hold(_entry(), "untrusted source")
        with self.assertRaises(MemoryViolation):
            self.q.promote(item.quarantine_id,
                           verifier_origin=Origin.TRUSTED_OPERATOR,
                           verifier_id="trent", evidence="  ")

    def test_promote_ok(self):
        item = self.q.hold(_entry(), "untrusted source")
        entry = self.q.promote(item.quarantine_id,
                               verifier_origin=Origin.TRUSTED_OPERATOR,
                               verifier_id="trent",
                               evidence="trent confirmed in tray")
        self.assertEqual(entry.provenance.verification, "verified")
        self.assertEqual(self.q.pending(), [])

    def test_double_promote_refused(self):
        item = self.q.hold(_entry(), "untrusted source")
        self.q.promote(item.quarantine_id,
                       verifier_origin=Origin.TRUSTED_OPERATOR,
                       verifier_id="trent", evidence="e")
        with self.assertRaises(MemoryViolation):
            self.q.promote(item.quarantine_id,
                           verifier_origin=Origin.TRUSTED_OPERATOR,
                           verifier_id="trent", evidence="e")

    def test_unknown_id_refused(self):
        with self.assertRaises(MemoryViolation):
            self.q.promote("q_nonexistent",
                           verifier_origin=Origin.TRUSTED_OPERATOR,
                           verifier_id="trent", evidence="e")


if __name__ == "__main__":
    unittest.main()
