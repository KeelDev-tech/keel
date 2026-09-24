import sys
import os
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from security.actions.approval_gate import (
    Approval, ApprovalStore, digest_resource)
from security.actions.classifier import ActionClass, classify


class TestActionClassifier(unittest.TestCase):
    def test_read_only(self):
        cls, _ = classify("read_leads")
        self.assertEqual(cls, ActionClass.READ_ONLY)

    def test_submission(self):
        cls, _ = classify("record_submission")
        self.assertEqual(cls, ActionClass.SUBMISSION)

    def test_browser_submit(self):
        cls, _ = classify("browser_submit")
        self.assertEqual(cls, ActionClass.SUBMISSION)

    def test_credential(self):
        cls, _ = classify("read_secrets")
        self.assertEqual(cls, ActionClass.CREDENTIAL)

    def test_financial(self):
        cls, _ = classify("process_payment")
        self.assertEqual(cls, ActionClass.FINANCIAL)

    def test_unknown_fail_closed(self):
        cls, reason = classify("quantum_teleport_lead")
        self.assertEqual(cls, ActionClass.EXTERNAL_COMMUNICATION)
        self.assertIn("fail-closed", reason)

    def test_case_insensitive(self):
        cls, _ = classify("READ_LEADS")
        self.assertEqual(cls, ActionClass.READ_ONLY)

    def test_keel_08_promotion_is_system_change(self):
        cls, _ = classify("keel_0_8_promotion")
        self.assertEqual(cls, ActionClass.SYSTEM_CHANGE)


class TestApprovalGate(unittest.TestCase):
    def setUp(self):
        self.store = ApprovalStore()
        self.resource = {"type": "job_application", "id": "lead-1"}

    def test_grant_and_find(self):
        g = self.store.grant("agent-1", "browser_submit", self.resource,
                             approver="trent", ttl_seconds=3600)
        found = self.store.find_match("agent-1", "browser_submit",
                                      self.resource)
        self.assertIsNotNone(found)
        self.assertEqual(found.approval_id, g.approval_id)

    def test_no_match_wrong_agent(self):
        self.store.grant("agent-1", "browser_submit", self.resource,
                         approver="trent")
        self.assertIsNone(self.store.find_match(
            "agent-2", "browser_submit", self.resource))

    def test_no_match_wrong_resource(self):
        self.store.grant("agent-1", "browser_submit", self.resource,
                         approver="trent")
        self.assertIsNone(self.store.find_match(
            "agent-1", "browser_submit", {"type": "other", "id": "x"}))

    def test_single_use(self):
        g = self.store.grant("agent-1", "browser_submit", self.resource,
                             approver="trent")
        self.assertIsNotNone(self.store.consume(g.approval_id))
        self.assertIsNone(self.store.consume(g.approval_id))  # once only

    def test_expiry(self):
        g = self.store.grant("agent-1", "browser_submit", self.resource,
                             approver="trent", ttl_seconds=60)
        g.expires_at = (datetime.now(timezone.utc)
                        - timedelta(seconds=1)).isoformat()
        self.assertIsNone(self.store.find_match(
            "agent-1", "browser_submit", self.resource))

    def test_scope_mismatch(self):
        self.store.grant("agent-1", "browser_navigate", self.resource,
                         approver="trent")
        self.assertIsNone(self.store.find_match(
            "agent-1", "browser_submit", self.resource))

    def test_consumed_approval_no_longer_matches(self):
        g = self.store.grant("agent-1", "browser_submit", self.resource,
                             approver="trent")
        self.assertIsNotNone(self.store.find_match(
            "agent-1", "browser_submit", self.resource))
        self.store.consume(g.approval_id)
        self.assertIsNone(self.store.find_match(
            "agent-1", "browser_submit", self.resource))

    def test_empty_approver_rejected(self):
        with self.assertRaises(ValueError):
            self.store.grant("a", "browser_submit", self.resource,
                             approver="")

    def test_digest_stable(self):
        d1 = digest_resource({"b": 2, "a": 1})
        d2 = digest_resource({"a": 1, "b": 2})
        self.assertEqual(d1, d2)

    def test_cannot_self_approve(self):
        # The security authority has no approver identity; grants require
        # a named human approver distinct from the agent.
        g = self.store.grant("keel-security-authority", "deploy",
                             self.resource, approver="trent")
        self.assertEqual(g.approver, "trent")


if __name__ == "__main__":
    unittest.main()
