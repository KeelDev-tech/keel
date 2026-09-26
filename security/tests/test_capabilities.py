import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from security.errors import CapabilityDenied
from security.identity.capabilities import (
    KNOWN_CAPABILITIES, CapabilityManifest, required_capability)


class TestCapabilities(unittest.TestCase):
    def test_default_deny_unlisted(self):
        m = CapabilityManifest(("read_leads",))
        self.assertTrue(m.has("read_leads"))
        self.assertFalse(m.has("browser_submit"))  # not granted -> denied
        with self.assertRaises(CapabilityDenied):
            m.check("browser_submit")

    def test_empty_manifest_denies_everything(self):
        m = CapabilityManifest(())
        for cap in KNOWN_CAPABILITIES:
            self.assertFalse(m.has(cap))

    def test_unknown_capability_cannot_be_granted(self):
        with self.assertRaises(ValueError):
            CapabilityManifest(("become_root",))

    def test_check_action_mapped(self):
        m = CapabilityManifest(("record_submission",))
        cap = m.check_action("record_submission")
        self.assertEqual(cap, "record_submission")

    def test_check_action_unmapped_denied(self):
        m = CapabilityManifest(tuple(KNOWN_CAPABILITIES))  # everything
        with self.assertRaises(CapabilityDenied):
            m.check_action("totally_unknown_action_xyz")

    def test_check_action_ungranted_denied(self):
        m = CapabilityManifest(("read_leads",))
        with self.assertRaises(CapabilityDenied):
            m.check_action("browser_submit")

    def test_required_capability_none_for_unknown(self):
        self.assertIsNone(required_capability("nope_nothing"))

    def test_blocker_actions_mapped(self):
        for action in ("api_direct_write", "synthetic_to_live",
                       "human_decision_impersonation", "publication",
                       "keel_0_8_promotion"):
            self.assertIsNotNone(required_capability(action),
                                 f"{action} should map to a capability")


if __name__ == "__main__":
    unittest.main()
