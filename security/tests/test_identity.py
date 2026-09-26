import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from security.identity.agent_identity import (
    IdentityRegistry, mint_identity, mint_identity_id,
    register_system_identity)


class TestAgentIdentity(unittest.TestCase):
    def setUp(self):
        IdentityRegistry.clear()

    def tearDown(self):
        IdentityRegistry.clear()

    def test_mint_unique_ids(self):
        a = mint_identity("agent", "worker-a")
        b = mint_identity("agent", "worker-a")
        self.assertNotEqual(a.id, b.id)  # random nonce -> unique

    def test_system_identity_deterministic(self):
        x = register_system_identity("keel-application-engine",
                                     kind="service")
        IdentityRegistry.clear()
        y = register_system_identity("keel-application-engine",
                                     kind="service")
        self.assertEqual(x.id, y.id)  # stable across processes

    def test_id_is_content_addressed(self):
        i1 = mint_identity_id("tool", "scanner", nonce="fixed")
        i2 = mint_identity_id("tool", "scanner", nonce="fixed")
        i3 = mint_identity_id("tool", "scanner", nonce="other")
        self.assertEqual(i1, i2)
        self.assertNotEqual(i1, i3)

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ValueError):
            mint_identity("superuser", "x")

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            mint_identity("agent", "  ")

    def test_parent_linkage(self):
        parent = mint_identity("agent", "parent")
        IdentityRegistry.register(parent)
        child = mint_identity("subagent", "child", parent_id=parent.id)
        self.assertEqual(child.parent_id, parent.id)

    def test_registry_revoke(self):
        ident = mint_identity("worker", "w1")
        IdentityRegistry.register(ident)
        self.assertFalse(IdentityRegistry.is_revoked(ident.id))
        self.assertTrue(IdentityRegistry.revoke(ident.id, "test"))
        self.assertTrue(IdentityRegistry.is_revoked(ident.id))

    def test_revoke_unknown_returns_false(self):
        self.assertFalse(IdentityRegistry.revoke("nope"))


if __name__ == "__main__":
    unittest.main()
