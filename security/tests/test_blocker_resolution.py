"""Regression tests for Trent's Blocker Resolution Directive §9.

Six deterministic policies in rules/blocker_resolution.json, enforced by
policy/engine.py as a deny-only pre-layer. No LLM may override these.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from security.actions.classifier import ActionClass
from security.identity.agent_identity import (
    IdentityRegistry, register_system_identity)
from security.identity.capabilities import CapabilityManifest
from security.policy import engine
from security.policy.engine import Decision, _validate_blocker_rules

FACTS_6 = {"authentic_sources": 6, "cross_record_validation": "PASS",
           "genuine_operator_decision": "PRESENT", "assurance": "PASS",
           "trust": "PASS", "consent": "PASS"}


def _agent(*caps):
    IdentityRegistry.clear()
    ident = register_system_identity("blocker-test-agent", kind="agent")
    return {"identity": ident, "manifest": CapabilityManifest(caps),
            "delegation": None}


def _eval(action_name, facts=None, caps=("deploy", "browser_navigate",
                                        "approve_high_impact")):
    engine._reset_caches()
    return engine.evaluate(
        agent=_agent(*caps),
        action={"name": action_name},
        resource={"type": "t", "sensitivity": "INTERNAL"},
        context={"facts": facts or {}})


class TestBlockerAlways(unittest.TestCase):
    def test_api_direct_write_deny_always(self):
        e = _eval("api_direct_write")
        self.assertEqual(e.decision, Decision.DENY)
        self.assertTrue(any("api_direct_write" in r for r in e.reasons))

    def test_synthetic_to_live_deny_always(self):
        e = _eval("synthetic_to_live")
        self.assertEqual(e.decision, Decision.DENY)
        self.assertTrue(any("synthetic_to_live" in r for r in e.reasons))

    def test_human_decision_impersonation_deny_always(self):
        e = _eval("human_decision_impersonation")
        self.assertEqual(e.decision, Decision.DENY)
        self.assertTrue(any("human_decision_impersonation" in r
                            for r in e.reasons))

    def test_deny_always_holds_with_full_capabilities(self):
        # Even a fully-capabled agent cannot pass a DENY_ALWAYS policy.
        e = _eval("api_direct_write",
                  caps=tuple(c for c in
                             __import__("security.identity.capabilities",
                                        fromlist=["KNOWN_CAPABILITIES"])
                             .KNOWN_CAPABILITIES))
        self.assertEqual(e.decision, Decision.DENY)

    def test_blocker_reason_leads(self):
        e = _eval("synthetic_to_live")
        self.assertIn("blocker-resolution", e.reasons[0])


class TestExternalEgress(unittest.TestCase):
    def test_denied_without_g1(self):
        e = _eval("browser_navigate", facts={})
        self.assertEqual(e.decision, Decision.DENY)
        self.assertTrue(any("external_egress" in r for r in e.reasons))

    def test_denied_with_false_g1(self):
        e = _eval("browser_navigate",
                  facts={"exact_g1_authorization_exists": False})
        self.assertEqual(e.decision, Decision.DENY)

    def test_g1_true_abstains_then_host_binding_holds(self):
        # Blocker abstains; the vendor's host-binding hold then DENies
        # external egress via the generic action API (2026-09-21).
        e = _eval("browser_navigate",
                  facts={"exact_g1_authorization_exists": True})
        self.assertEqual(e.decision, Decision.DENY)
        self.assertTrue(any("abstaining" in r for r in e.reasons))
        self.assertTrue(any("binding absent" in r for r in e.reasons))

    def test_bool_is_type_strict(self):
        # Truthy-coercion must not authorize: 1 is not True here.
        e = _eval("browser_navigate",
                  facts={"exact_g1_authorization_exists": 1})
        self.assertEqual(e.decision, Decision.DENY)


class TestPublication(unittest.TestCase):
    def test_denied_without_release_hash(self):
        e = _eval("publication", facts={})
        self.assertEqual(e.decision, Decision.DENY)

    def test_authorized_hash_abstains_then_host_binding_holds(self):
        # Publication is also external egress: needs BOTH the release-hash
        # flag and the G1 flag. Blocker abstains on both; the vendor's
        # host-binding hold then DENies (2026-09-21).
        e = _eval("publication",
                  facts={"exact_release_hash_authorized": True,
                         "exact_g1_authorization_exists": True})
        self.assertEqual(e.decision, Decision.DENY)

    def test_release_hash_alone_still_denied(self):
        # Without G1, the egress policy still denies.
        e = _eval("publication",
                  facts={"exact_release_hash_authorized": True})
        self.assertEqual(e.decision, Decision.DENY)


class TestKeel08Promotion(unittest.TestCase):
    def test_denied_with_no_facts(self):
        e = _eval("keel_0_8_promotion", facts={})
        self.assertEqual(e.decision, Decision.DENY)

    def test_denied_with_five_of_six(self):
        facts = dict(FACTS_6)
        facts["trust"] = "FAIL"
        e = _eval("keel_0_8_promotion", facts=facts)
        self.assertEqual(e.decision, Decision.DENY)

    def test_denied_with_wrong_count(self):
        facts = dict(FACTS_6)
        facts["authentic_sources"] = 5
        e = _eval("keel_0_8_promotion", facts=facts)
        self.assertEqual(e.decision, Decision.DENY)

    def test_all_six_abstains_to_base_rules(self):
        e = _eval("keel_0_8_promotion", facts=FACTS_6)
        self.assertEqual(e.decision, Decision.REQUIRE_APPROVAL)
        self.assertTrue(any("keel_0_8_promotion" in r and "abstaining" in r
                            for r in e.reasons))

    def test_missing_single_fact_denied(self):
        for key in FACTS_6:
            facts = {k: v for k, v in FACTS_6.items() if k != key}
            e = _eval("keel_0_8_promotion", facts=facts)
            self.assertEqual(e.decision, Decision.DENY,
                             f"missing {key} must deny")


class TestExactHashConditions(unittest.TestCase):
    """The engine supports exact-hash equality conditions (task: 'exact-hash
    G1 authorization match, exact release-hash publication match'). Tested
    via a synthetic rules file with hash-valued conditions."""

    def _run_with_rules(self, policies):
        engine._reset_caches()
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json",
                                          delete=False)
        json.dump({"version": "1.0.0", "source": "test",
                   "policies": policies}, tmp)
        tmp.close()
        old = engine._BLOCKER_RULES_PATH
        engine._BLOCKER_RULES_PATH = tmp.name
        try:
            return engine.evaluate(
                agent=_agent("deploy"),
                action={"name": "publication"},
                resource={"type": "t", "sensitivity": "INTERNAL"},
                context={"facts": {"release_hash": "abc123",
                                   "exact_g1_authorization_exists": True}})
        finally:
            engine._BLOCKER_RULES_PATH = old
            engine._reset_caches()
            os.unlink(tmp.name)

    def test_exact_hash_match_abstains_then_host_binding_holds(self):
        e = self._run_with_rules([{
            "id": "publication", "effect": "CONDITIONAL_DENY",
            "conditions": ["release_hash == abc123"]}])
        # publication also matches external_egress (real file absent here —
        # synthetic file has only the publication policy), so abstain ->
        # vendor host-binding hold -> DENY (2026-09-21).
        self.assertEqual(e.decision, Decision.DENY)

    def test_wrong_hash_denied(self):
        engine._reset_caches()
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json",
                                          delete=False)
        json.dump({"version": "1.0.0", "source": "test", "policies": [{
            "id": "publication", "effect": "CONDITIONAL_DENY",
            "conditions": ["release_hash == abc123"]}]}, tmp)
        tmp.close()
        old = engine._BLOCKER_RULES_PATH
        engine._BLOCKER_RULES_PATH = tmp.name
        try:
            e = engine.evaluate(
                agent=_agent("deploy"),
                action={"name": "publication"},
                resource={"type": "t", "sensitivity": "INTERNAL"},
                context={"facts": {"release_hash": "WRONG"}})
            self.assertEqual(e.decision, Decision.DENY)
        finally:
            engine._BLOCKER_RULES_PATH = old
            engine._reset_caches()
            os.unlink(tmp.name)


class TestBlockerFileRobustness(unittest.TestCase):
    def test_invalid_effect_rejected(self):
        with self.assertRaises(ValueError):
            _validate_blocker_rules({
                "version": "1", "source": "t", "policies": [{
                    "id": "x", "effect": "ALLOW_ALWAYS", "conditions": []}]})

    def test_conditional_without_conditions_rejected(self):
        with self.assertRaises(ValueError):
            _validate_blocker_rules({
                "version": "1", "source": "t", "policies": [{
                    "id": "x", "effect": "CONDITIONAL_DENY",
                    "conditions": []}]})

    def test_malformed_condition_rejected(self):
        with self.assertRaises(ValueError):
            _validate_blocker_rules({
                "version": "1", "source": "t", "policies": [{
                    "id": "x", "effect": "CONDITIONAL_DENY",
                    "conditions": ["not a condition"]}]})

    def test_duplicate_id_rejected(self):
        with self.assertRaises(ValueError):
            _validate_blocker_rules({
                "version": "1", "source": "t", "policies": [
                    {"id": "x", "effect": "DENY", "conditions": []},
                    {"id": "x", "effect": "DENY", "conditions": []}]})

    def test_missing_file_fallback_denies_named_actions(self):
        old = engine._BLOCKER_RULES_PATH
        engine._BLOCKER_RULES_PATH = "/nonexistent/blocker.json"
        engine._reset_caches()
        try:
            e = engine.evaluate(
                agent=_agent("deploy"),
                action={"name": "synthetic_to_live"},
                resource={"type": "t", "sensitivity": "INTERNAL"},
                context={})
            self.assertEqual(e.decision, Decision.DENY)
            self.assertTrue(any("fail-closed" in r or "fail closed" in r
                                for r in e.reasons))
        finally:
            engine._BLOCKER_RULES_PATH = old
            engine._reset_caches()


if __name__ == "__main__":
    unittest.main()
