import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# Isolate the safe-mode flag before importing the module.
_tmp = tempfile.mkdtemp(prefix="keel-safe-test-")
os.environ["KEEL_SAFE_MODE_FILE"] = os.path.join(_tmp, "safe_mode")

from security.actions.classifier import ActionClass
from security.identity.agent_identity import (
    IdentityRegistry, mint_identity)
from security.response.containment import freeze_agent_tree
from security.response.revoke import RevocationRegistry
from security.response.safe_mode import SafeMode


class TestRevocation(unittest.TestCase):
    def setUp(self):
        IdentityRegistry.clear()
        self.auth = RevocationRegistry()

    def tearDown(self):
        IdentityRegistry.clear()

    def test_revoke_identity(self):
        ident = mint_identity("agent", "bad-actor")
        IdentityRegistry.register(ident)
        self.assertTrue(self.auth.revoke_identity(ident.id, "test"))
        self.assertTrue(self.auth.is_identity_revoked(ident.id))
        self.assertTrue(IdentityRegistry.is_revoked(ident.id))

    def test_revoke_token(self):
        self.assertTrue(self.auth.revoke_token("tok-123", "test"))
        self.assertTrue(self.auth.is_token_revoked("tok-123"))
        self.assertFalse(self.auth.is_token_revoked("tok-999"))

    def test_revoke_delegation(self):
        from security.identity.capabilities import CapabilityManifest
        from security.identity.delegation import DelegationRegistry
        DelegationRegistry.clear()
        d = DelegationRegistry.delegate(
            "o", CapabilityManifest(("read_leads",)), "w",
            ("read_leads",))
        self.assertTrue(self.auth.revoke_delegation(d.id, "test"))
        valid, _ = DelegationRegistry.is_valid(d)
        self.assertFalse(valid)
        DelegationRegistry.clear()

    def test_freeze_agent_tree(self):
        parent = mint_identity("agent", "p")
        IdentityRegistry.register(parent)
        child = mint_identity("subagent", "c", parent_id=parent.id)
        IdentityRegistry.register(child)
        unrelated = mint_identity("agent", "u")
        IdentityRegistry.register(unrelated)
        frozen = freeze_agent_tree(parent.id, self.auth, "test")
        self.assertIn(parent.id, frozen)
        self.assertIn(child.id, frozen)
        self.assertNotIn(unrelated.id, frozen)

class TestSafeMode(unittest.TestCase):
    def setUp(self):
        path = os.environ["KEEL_SAFE_MODE_FILE"]
        if os.path.exists(path):
            os.unlink(path)
        self.sm = SafeMode()

    def test_engage_requires_reason(self):
        with self.assertRaises(ValueError):
            self.sm.engage("")

    def test_engage_and_disengage(self):
        self.assertFalse(self.sm.is_engaged())
        self.sm.engage("test")
        self.assertTrue(self.sm.is_engaged())
        with self.assertRaises(ValueError):
            self.sm.disengage("", approver="trent")  # reason required
        with self.assertRaises(ValueError):
            self.sm.disengage("done", approver="")  # approver required
        self.sm.disengage("incident resolved", approver="trent")
        self.assertFalse(self.sm.is_engaged())

    def test_allowed_classes_under_safe_mode(self):
        # Safe mode is enforced by the policy engine via
        # safe_mode_allowed_classes; here we check the flag itself.
        self.sm.engage("test")
        self.assertTrue(self.sm.is_engaged())
        status = self.sm.status()
        self.assertTrue(status["engaged"])
        self.assertEqual(status["reason"], "test")

    def test_env_override(self):
        os.environ["KEEL_SAFE_MODE"] = "1"
        try:
            self.assertTrue(SafeMode().is_engaged())
        finally:
            del os.environ["KEEL_SAFE_MODE"]

    def test_state_roundtrip(self):
        self.sm.engage("test reason")
        status = self.sm.status()
        self.assertTrue(status["engaged"])
        self.assertEqual(status["reason"], "test reason")
        self.assertIn("engaged_at", status)


if __name__ == "__main__":
    unittest.main()
