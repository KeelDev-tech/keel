"""Authority regressions: disposable state, no host modules or outbound calls.

PORTED 2026-09-21 from Keel_Security_Repair_2026-09-21 (vendor handoff).
3 vendor tests EXCLUDED (not ported) — they assert the vendor's lane-retirement
policy (api_direct -> api_direct_write DENY; browser_batch held), which would
retire the API-direct submission lane. Trent's 2026-09-21 push-on-value directive
keeps the human-gated lane: our request_dispatch_authorization routes via the
record_submission action (engine identity holds the capability). Excluded:
  - test_api_retirement_is_not_recording_authority
  - test_browser_dispatch_has_no_bootstrap_submit_grant
  - test_unknown_transport_is_refused
sys.path adjusted: parents[2] resolves to the candidate's keel/ dir.
"""
import dataclasses
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.actions.approval_gate import ApprovalStore
from security.actions.classifier import ActionClass
from security.actions.interceptor import request, request_dispatch_authorization, request_submission_authorization
from security.data import dlp
from security.data.classifier import Sensitivity
from security.errors import PolicyDenied
from security.identity.agent_identity import Identity, IdentityRegistry, mint_identity, register_system_identity
from security.identity.capabilities import CapabilityManifest
from security.policy.engine import Decision, evaluate


class AuthorityRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="keel-authority-test-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = str(Path(self.tmp.name) / "events.jsonl")
        self.flag = str(Path(self.tmp.name) / "safe_mode")
        self.env = patch.dict(os.environ, {"KEEL_SECURITY_LEDGER": self.ledger,
                             "KEEL_SAFE_MODE_FILE": self.flag, "KEEL_SAFE_MODE": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        IdentityRegistry.clear()
        self.addCleanup(IdentityRegistry.clear)
        self.ident = register_system_identity("regression-test")

    def req(self, action="read_leads", caps=("read_leads",), **kw):
        return request(self.ident, action, manifest=CapabilityManifest(caps), **kw)

    def test_unregistered_identity_is_refused(self):
        forged = Identity(id="not-registered", kind="service", name="keel-application-engine")
        with self.assertRaises(PolicyDenied):
            request(forged, "update_queue")

    def test_revoked_identity_is_refused(self):
        IdentityRegistry.revoke(self.ident.id)
        with self.assertRaises(PolicyDenied):
            self.req()

    def test_modified_registered_identity_is_refused(self):
        with self.assertRaises(PolicyDenied):
            request(dataclasses.replace(self.ident, name="keel-application-engine"), "update_queue")

    def test_same_name_does_not_inherit_engine_capabilities(self):
        alias = IdentityRegistry.register(mint_identity("service", "keel-application-engine"))
        self.assertFalse(request(alias, "update_queue").allowed)

    def test_revoked_bootstrap_cannot_reregister_to_bypass(self):
        engine = register_system_identity("keel-application-engine")
        IdentityRegistry.revoke(engine.id)
        with self.assertRaises(PolicyDenied):
            request("keel-application-engine", "update_queue")

    def test_policy_direct_call_rejects_unknown_identity(self):
        result = evaluate({"identity": Identity("forged", "service", "x"),
                           "manifest": CapabilityManifest(("read_leads",))},
                          {"name": "read_leads"}, {}, {})
        self.assertEqual(result.decision, Decision.DENY)




    def test_actual_outcome_recording_remains_allowed(self):
        self.assertTrue(request_submission_authorization(note="Application received.", role_id="synthetic").allowed)

    def test_dlp_violation_cannot_be_overridden_by_approval(self):
        store = ApprovalStore()
        store.grant(self.ident.id, "read_leads", None, approver="synthetic-operator")
        result = self.req(content=[("api_key=AKIAIOSFODNN7EXAMPLE", "job_listing")],
                          destination="PROMPT", approvals=store)
        self.assertTrue(result.dlp_violations)
        self.assertEqual(result.decision, Decision.DENY)
        self.assertEqual(result.evaluation.decision, Decision.DENY)

    def test_mixed_sensitivity_cannot_hide_a_secret(self):
        # Deterministic order exposes the old last-value-wins defect.
        with patch("security.actions.interceptor.classify_text", return_value=[Sensitivity.CREDENTIAL, Sensitivity.PUBLIC]):
            result = self.req(content=[("synthetic", "job_listing")], destination="PROMPT")
        self.assertFalse(result.allowed)

    def test_unknown_destination_denied_even_for_plain_text(self):
        self.assertFalse(self.req(content=[("hello", "job_listing")], destination="typo").allowed)

    def test_sensitive_dlp_exceptions_require_explicit_true(self):
        for sens, dest, key in [(Sensitivity.CREDENTIAL, "BROWSER_FORM", "credential_action_approved"),
                                (Sensitivity.CREDENTIAL, "SECURITY_LEDGER", "hashed"),
                                (Sensitivity.PII, "PROMPT", "pii_in_prompt_justified"),
                                (Sensitivity.FINANCIAL, "INTERNAL_LOG", "redacted")]:
            for flag in [None, False, 1, "true"]:
                with self.subTest(sensitivity=sens, destination=dest, flag=flag):
                    self.assertFalse(dlp.may_travel(sens, dest, {key: flag})[0])
            self.assertTrue(dlp.may_travel(sens, dest, {key: True})[0])

    def test_publication_boolean_claim_cannot_authorize_release(self):
        store = ApprovalStore()
        resource = {"type": "artifact", "id": "synthetic"}
        store.grant(self.ident.id, "publication", resource, approver="synthetic-operator")
        result = self.req("publication", ("deploy",), resource=resource, approvals=store,
                          facts={"exact_g1_authorization_exists": True,
                                 "exact_release_hash_authorized": True})
        self.assertEqual(result.decision, Decision.DENY)

    def test_action_class_override_cannot_demote_submission(self):
        result = self.req("browser_submit", ("browser_submit",), action_class=ActionClass.READ_ONLY,
                          evidence={"note": "unverified"})
        self.assertEqual(result.decision, Decision.DENY)

    def test_false_override_cannot_disable_engaged_safe_mode(self):
        Path(self.flag).write_text("synthetic kill switch")
        self.assertFalse(self.req("update_queue", ("manage_queue",), safe_mode=False).allowed)

    def test_medium_injection_cannot_bypass_risk_ceiling(self):
        result = evaluate({"identity": self.ident, "manifest": CapabilityManifest(("deploy",))},
                          {"name": "deploy"}, {"sensitivity": "CREDENTIAL"},
                          {"injection_severity": "MEDIUM"})
        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(any("risk ceiling" in reason for reason in result.reasons))

    def test_authorization_does_not_claim_execution(self):
        self.assertTrue(self.req().allowed)
        event = json.loads(Path(self.ledger).read_text().splitlines()[-1])
        self.assertEqual(event["body"]["resulting_action"], "authorized")

    def test_generic_browser_submit_is_held_without_host_binding(self):
        store = ApprovalStore()
        resource = {"type": "application", "id": "synthetic"}
        store.grant(self.ident.id, "browser_submit", resource, approver="synthetic-operator")
        self.assertFalse(self.req("browser_submit", ("browser_submit",), resource=resource,
                                 evidence={"unverified": True}, approvals=store).allowed)

    def test_preparation_still_works(self):
        self.assertTrue(self.req("write_packet", ("write_packet",)).allowed)


if __name__ == "__main__":
    unittest.main()
