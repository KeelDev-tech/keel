import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from security.actions.classifier import ActionClass
from security.identity.agent_identity import (
    IdentityRegistry, register_system_identity)
from security.identity.capabilities import CapabilityManifest
from security.identity.delegation import DelegationRegistry
from security.policy.engine import Decision, evaluate


def _agent(*caps, delegation=None):
    IdentityRegistry.clear()
    ident = register_system_identity("test-agent", kind="agent")
    return {"identity": ident, "manifest": CapabilityManifest(caps),
            "delegation": delegation}


def _eval(agent, action_name, action_class=None, resource=None,
          context=None):
    return evaluate(
        agent=agent,
        action={"name": action_name, "action_class": action_class},
        resource=resource or {"type": "t", "sensitivity": "INTERNAL"},
        context=context or {})


class TestPolicyEngine(unittest.TestCase):
    def setUp(self):
        DelegationRegistry.clear()

    def test_read_only_allow_with_capability(self):
        e = _eval(_agent("read_leads"), "read_leads",
                  action_class=ActionClass.READ_ONLY)
        self.assertEqual(e.decision, Decision.ALLOW)

    def test_default_deny_missing_capability(self):
        e = _eval(_agent("read_leads"), "write_packet",
                  action_class=ActionClass.REVERSIBLE_WRITE)
        self.assertEqual(e.decision, Decision.DENY)
        self.assertTrue(any("not granted" in r for r in e.reasons))

    def test_no_manifest_deny(self):
        IdentityRegistry.clear()
        ident = register_system_identity("m", kind="agent")
        e = evaluate({"identity": ident, "manifest": None,
                      "delegation": None},
                     {"name": "read_leads",
                      "action_class": ActionClass.READ_ONLY},
                     {"type": "t"}, {})
        self.assertEqual(e.decision, Decision.DENY)

    def test_unmapped_action_deny(self):
        e = _eval(_agent("read_leads"), "definitely_not_a_real_action",
                  action_class=ActionClass.READ_ONLY)
        self.assertEqual(e.decision, Decision.DENY)

    def test_submission_requires_evidence(self):
        agent = _agent("record_submission")
        e = _eval(agent, "record_submission",
                  action_class=ActionClass.SUBMISSION, context={})
        self.assertEqual(e.decision, Decision.DENY)  # no evidence
        e2 = _eval(agent, "record_submission",
                   action_class=ActionClass.SUBMISSION,
                   context={"evidence": {"note": "confirmed"}})
        self.assertEqual(e2.decision, Decision.ALLOW)

    def test_safe_mode_denies_writes(self):
        agent = _agent("write_packet")
        e = _eval(agent, "write_packet",
                  action_class=ActionClass.REVERSIBLE_WRITE,
                  context={"safe_mode": True})
        self.assertEqual(e.decision, Decision.DENY)
        e2 = _eval(_agent("read_leads"), "read_leads",
                   action_class=ActionClass.READ_ONLY,
                   context={"safe_mode": True})
        self.assertEqual(e2.decision, Decision.ALLOW)

    def test_injection_high_quarantine(self):
        e = _eval(_agent("record_submission"), "record_submission",
                  action_class=ActionClass.SUBMISSION,
                  context={"evidence": {"note": "x"},
                           "injection_severity": "HIGH"})
        self.assertEqual(e.decision, Decision.QUARANTINE)

    def test_external_comm_held_without_host_binding(self):
        # Vendor hardening (2026-09-21): the generic action API holds
        # EXTERNAL_COMMUNICATION until a trusted host execution/egress
        # binding exists. The G1 flag fact no longer reaches the base
        # class rule — external egress via request() is DENY, not
        # REQUIRE_APPROVAL. Dispatch goes through
        # request_dispatch_authorization (SUBMISSION/record_submission).
        e = _eval(_agent("browser_navigate"), "browser_navigate",
                  context={"facts": {"exact_g1_authorization_exists": True}})
        self.assertEqual(e.decision, Decision.DENY)
        self.assertTrue(any("host" in r and "binding" in r
                            for r in e.reasons))

    def test_destructive_never_auto(self):
        # Vendor hardening (2026-09-21): base rule is now
        # "deny_unless_approval" — without an approval a destructive-class
        # action is DENY, stricter than the old REQUIRE_APPROVAL.
        e = _eval(_agent("deploy"), "deploy",
                  action_class=ActionClass.DESTRUCTIVE)
        self.assertEqual(e.decision, Decision.DENY)

    def test_invalid_delegation_deny(self):
        DelegationRegistry.clear()
        d = DelegationRegistry.delegate(
            "o", CapabilityManifest(("read_leads",)), "w", ("read_leads",))
        DelegationRegistry.revoke(d.id, "test")
        e = _eval(_agent("read_leads", delegation=d), "read_leads",
                  action_class=ActionClass.READ_ONLY)
        self.assertEqual(e.decision, Decision.DENY)

    def test_risk_ceiling(self):
        # Vendor hardening (2026-09-21): the classifier canonically maps
        # "deploy" to SYSTEM_CHANGE, and an explicit weaker class is
        # denied ("action class differs from canonical classification").
        # SYSTEM_CHANGE (70) + CREDENTIAL sensitivity (+25) = 95 ->
        # 95 >= 85 threshold -> DENY on the risk ceiling.
        e = _eval(_agent("deploy"), "deploy",
                  action_class=ActionClass.SYSTEM_CHANGE,
                  resource={"type": "t", "sensitivity": "CREDENTIAL"})
        self.assertEqual(e.decision, Decision.DENY)
        self.assertTrue(any("risk ceiling" in r for r in e.reasons))

    def test_policy_version_stamped(self):
        e = _eval(_agent("read_leads"), "read_leads",
                  action_class=ActionClass.READ_ONLY)
        self.assertEqual(e.policy_version, "2026-09-21.1")


if __name__ == "__main__":
    unittest.main()
