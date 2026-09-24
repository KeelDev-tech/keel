import sys
import os
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from security.errors import CapabilityDenied
from security.identity.capabilities import CapabilityManifest
from security.identity.delegation import DelegationRegistry


def _manifest(*caps):
    return CapabilityManifest(caps)


class TestDelegation(unittest.TestCase):
    def setUp(self):
        DelegationRegistry.clear()

    def tearDown(self):
        DelegationRegistry.clear()

    def test_subset_delegation_ok(self):
        d = DelegationRegistry.delegate(
            "orchestrator", _manifest("read_leads", "write_packet"),
            "worker-1", ("read_leads",))
        self.assertEqual(d.capabilities, frozenset({"read_leads"}))
        valid, _ = DelegationRegistry.is_valid(d)
        self.assertTrue(valid)

    def test_escalation_refused(self):
        # Delegator cannot grant what it does not hold.
        with self.assertRaises(CapabilityDenied):
            DelegationRegistry.delegate(
                "orchestrator", _manifest("read_leads"),
                "worker-1", ("read_leads", "browser_submit"))

    def test_empty_set_refused(self):
        with self.assertRaises(ValueError):
            DelegationRegistry.delegate(
                "orchestrator", _manifest("read_leads"), "worker-1", ())

    def test_expiry(self):
        d = DelegationRegistry.delegate(
            "o", _manifest("read_leads"), "w", ("read_leads",),
            ttl_seconds=3600)
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        valid, why = DelegationRegistry.is_valid(d, now=future)
        self.assertFalse(valid)
        self.assertIn("expired", why)

    def test_revoke(self):
        d = DelegationRegistry.delegate(
            "o", _manifest("read_leads"), "w", ("read_leads",))
        self.assertTrue(DelegationRegistry.revoke(d.id, "misbehaving"))
        valid, why = DelegationRegistry.is_valid(d)
        self.assertFalse(valid)
        self.assertIn("revoked", why)

    def test_malformed_expiry_fail_closed(self):
        d = DelegationRegistry.delegate(
            "o", _manifest("read_leads"), "w", ("read_leads",))
        d.expires_at = "not-a-date"
        valid, _ = DelegationRegistry.is_valid(d)
        self.assertFalse(valid)


if __name__ == "__main__":
    unittest.main()
