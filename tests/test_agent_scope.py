"""Integration tests use explicitly synthetic receipts and verdicts only."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from keel_agent.scope import material_context_revisions, prepare_candidate, preflight_candidate
from keel_assurance.core import evaluate, proposal_scope
from keel_flow.common import digest
from keel_workflow.delivery import build_bundle
from keel_workflow.integration import evaluate_candidate
from keel_workflow.reviews import ReviewStore
from tools.make_workflow_demo import NOW, make_fixture


def fixture():
    bundle, flow, assurance, trust = make_fixture()
    for action in assurance["actions"]:
        action["reviews"] = []
    return rebundle(bundle, assurance, trust), flow, assurance, trust


def rebundle(bundle, assurance, trust, application=None):
    document = bundle.document
    application = deepcopy(application or document["content"]["application"])
    return build_bundle(**{key: document[key] for key in
                          ("workspace_id", "role_id", "action", "destination", "account_id")},
                        revisions=material_context_revisions(application, assurance, trust, document["role_id"]),
                        content={"application": application}, attachments=dict(bundle.attachments))


def observe_again(flow, assurance, trust, now):
    """TEST FIXTURE ONLY: emulate genuinely repeated unchanged observations."""
    flow, assurance, trust = deepcopy((flow, assurance, trust))
    flow["observed_at"] = flow["pool"]["observed_at"] = now.isoformat()
    flow["capacity"]["observed_at"] = now.isoformat()
    for lead in flow["leads"]:
        lead["observed_at"] = now.isoformat()
    for source in flow["discovery"]["sources"]:
        source["observed_at"] = now.isoformat()
    trust["observed_at"] = trust["evidence_export"]["observed_at"] = now.isoformat()
    trust["flow_export"] = deepcopy(flow)
    assurance["observed_at"] = now.isoformat()
    assurance["export_sha256"] = digest(flow)
    return flow, assurance, trust


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.bundle, self.flow, self.assurance, self.trust = fixture()

    def prepare(self, *, now=NOW, lead_anchor=None, reviewer_config_sha256=None):
        return prepare_candidate(self.bundle, flow_export=self.flow, assurance_export=self.assurance,
                                 trust_export=self.trust, now=now, lead_anchor=lead_anchor,
                                 reviewer_config_sha256=reviewer_config_sha256)

    def test_new_blind_review_requires_no_synthetic_legacy_pass(self):
        original = deepcopy((self.flow, self.assurance, self.trust))
        self.assertIn("reviewer_diversity_unestablished", evaluate(self.assurance, now=NOW)["rows"][0]["reasons"])
        result = self.prepare()
        self.assertEqual(result["state"], "READY_FOR_BLIND_REVIEW")
        self.assertTrue(result["blind_review_required"])
        self.assertFalse(result["legacy_opinions_used_as_approval"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(original, (self.flow, self.assurance, self.trust))

    def test_refresh_preserves_scope_but_changes_observation_digest(self):
        original = self.prepare()
        self.flow, self.assurance, self.trust = observe_again(self.flow, self.assurance, self.trust,
                                                            NOW + timedelta(seconds=120))
        fresh = self.prepare(now=NOW + timedelta(seconds=120), lead_anchor=original["lead_anchor"])
        self.assertEqual(fresh["state"], "READY_FOR_BLIND_REVIEW")
        self.assertEqual(fresh["subject_sha256"], original["subject_sha256"])
        self.assertNotEqual(fresh["observation_sha256"], original["observation_sha256"])

    def test_refresh_needs_original_anchor_never_rewrites_receipt(self):
        original_authority = deepcopy(self.assurance["actions"][0]["authority"])
        self.flow, self.assurance, self.trust = observe_again(self.flow, self.assurance, self.trust,
                                                            NOW + timedelta(seconds=120))
        with self.assertRaisesRegex(ValueError, "anchor digest"):
            self.prepare(now=NOW + timedelta(seconds=120))
        self.assertEqual(original_authority, self.assurance["actions"][0]["authority"])

    def test_changed_policy_lead_field_cannot_hide_behind_anchor(self):
        original = self.prepare()
        self.flow["leads"][0]["posting_verified"] = False
        self.trust["flow_export"] = deepcopy(self.flow)
        self.assurance["export_sha256"] = digest(self.flow)
        with self.assertRaisesRegex(ValueError, "lead material changed"):
            self.prepare(lead_anchor=original["lead_anchor"])

    def test_changed_lead_expiry_invalidates_anchor(self):
        original = self.prepare()
        self.flow["leads"][0]["approval_expires_at"] = (NOW + timedelta(hours=2)).isoformat()
        self.trust["flow_export"] = deepcopy(self.flow)
        self.assurance["export_sha256"] = digest(self.flow)
        with self.assertRaisesRegex(ValueError, "lead material changed"):
            self.prepare(lead_anchor=original["lead_anchor"])

    def test_stale_snapshot_is_blocked(self):
        result = self.prepare(now=NOW + timedelta(seconds=91))
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("FLOW_UNVERIFIED", result["reasons"])
        self.assertIn("assurance:assurance_export_stale_or_future", result["reasons"])

    def test_future_snapshot_is_blocked(self):
        # Full original validation may reject before readiness, e.g. an outcome
        # follow-up is in the future. Either way no runnable candidate exists.
        with self.assertRaises(ValueError):
            self.prepare(now=NOW - timedelta(seconds=1))

    def test_revoked_evidence_blocks_and_changes_material(self):
        original = self.prepare()
        self.assurance["evidence"][0]["status"] = "REVOKED"
        self.bundle = rebundle(self.bundle, self.assurance, self.trust)
        current = self.prepare()
        self.assertEqual(current["state"], "BLOCKED")
        self.assertIn("assurance:evidence_dependency_invalid", current["reasons"])
        self.assertNotEqual(original["subject_sha256"], current["subject_sha256"])

    def test_evidence_expiry_and_source_observation_are_material(self):
        original = self.prepare()
        for key in ("expires_at", "observed_at"):
            bundle, flow, assurance, trust = fixture()
            assurance["evidence"][0][key] = (NOW + timedelta(seconds=1) if key == "expires_at"
                                            else NOW - timedelta(seconds=1)).isoformat()
            candidate = prepare_candidate(rebundle(bundle, assurance, trust), flow_export=flow,
                                          assurance_export=assurance, trust_export=trust, now=NOW)
            self.assertNotEqual(original["subject_sha256"], candidate["subject_sha256"])

    def test_receipt_denial_remains_blocked(self):
        self.assurance["actions"][0]["authority"]["status"] = "DENIED"
        self.bundle = rebundle(self.bundle, self.assurance, self.trust)
        result = self.prepare()
        self.assertIn("assurance:authority_denied", result["reasons"])
        self.assertEqual(result["state"], "BLOCKED")

    def test_human_rejection_remains_blocked(self):
        self.assurance["actions"][0]["human_review"]["status"] = "REJECT"
        self.bundle = rebundle(self.bundle, self.assurance, self.trust)
        self.assertIn("assurance:human_rejected", self.prepare()["reasons"])

    def test_receipt_expiry_is_not_extended_by_new_observation(self):
        for field in ("authority", "human_review"):
            self.assurance["actions"][0][field]["expires_at"] = (NOW + timedelta(seconds=95)).isoformat()
        self.bundle = rebundle(self.bundle, self.assurance, self.trust)
        original = self.prepare()
        self.flow, self.assurance, self.trust = observe_again(self.flow, self.assurance, self.trust,
                                                            NOW + timedelta(seconds=120))
        result = self.prepare(now=NOW + timedelta(seconds=120), lead_anchor=original["lead_anchor"])
        self.assertIn("assurance:authority_stale_or_future", result["reasons"])
        self.assertEqual(result["state"], "BLOCKED")

    def test_malformed_legacy_opinion_is_validated_before_projection(self):
        self.assurance["actions"][0]["reviews"] = [{"verdict": "PASS"}]
        with self.assertRaises(ValueError):
            self.prepare()

    def test_adverse_legacy_opinions_are_not_silently_superseded(self):
        for verdict, findings, expected in (
                ("FAIL", [], "legacy_assurance:review_fail"),
                ("ABSTAIN", [], "legacy_assurance:review_abstain"),
                ("PASS", ["unresolved source contradiction"], "legacy_assurance:unresolved_review_findings")):
            _bundle, _flow, old_assurance, _trust = make_fixture()
            self.assurance["actions"][0]["reviews"] = deepcopy(old_assurance["actions"][0]["reviews"])
            self.assurance["actions"][0]["reviews"][0]["verdict"] = verdict
            self.assurance["actions"][0]["reviews"][0]["findings"] = findings
            candidate = self.prepare()
            self.assertEqual(candidate["state"], "BLOCKED")
            self.assertIn(expected, candidate["reasons"])
            self.assertTrue(candidate["subject"]["legacy_adverse_review_sha256s"])

    def test_review_registry_changes_material_and_nondisjoint_pair_blocks(self):
        original = self.prepare()
        self.assurance["reviewers"][1]["method"] = self.assurance["reviewers"][0]["method"]
        changed = self.prepare()
        self.assertIn("REVIEWER_DIVERSITY_UNESTABLISHED", changed["reasons"])
        self.assertNotEqual(original["subject_sha256"], changed["subject_sha256"])

    def test_no_receipt_opinions_in_blind_subject(self):
        subject_action = self.prepare()["subject"]["assurance_material"]["action"]
        self.assertNotIn("reviews", subject_action)
        for field in ("authority", "human_review"):
            self.assertEqual(set(subject_action[field]), {"bound_receipt_sha256"})
            self.assertEqual(subject_action[field]["bound_receipt_sha256"],
                             digest(self.assurance["actions"][0][field]))

    def test_reviewer_configuration_is_exactly_bound_when_supplied(self):
        config = {row["reviewer_id"]: digest({"model": row["family"], "revision": "1"})
                  for row in self.assurance["reviewers"]}
        original = self.prepare(reviewer_config_sha256=config)
        self.assertTrue(original["reviewer_configuration_bound"])
        config[self.assurance["reviewers"][0]["reviewer_id"]] = digest("changed-model-file")
        changed = self.prepare(reviewer_config_sha256=config)
        self.assertNotEqual(original["subject_sha256"], changed["subject_sha256"])
        self.assertFalse(self.prepare()["reviewer_configuration_bound"])
        self.assertNotEqual(original["subject_sha256"], self.prepare()["subject_sha256"])

    def test_reviewer_configuration_missing_extra_invalid_hash_rejected(self):
        config = {row["reviewer_id"]: "a" * 64 for row in self.assurance["reviewers"]}
        for invalid in ({}, {**config, "extra-reviewer": "b" * 64},
                        {**config, next(iter(config)): "A" * 64},
                        {**config, next(iter(config)): "not-a-digest"}):
            with self.assertRaises(ValueError):
                self.prepare(reviewer_config_sha256=invalid)

    def test_v07_fixture_requires_new_review_and_uses_material_revisions(self):
        from tools.selfhost_fixture import make_fixture
        bundle, flow, assurance, trust = make_fixture()
        self.assertTrue(all(not row["reviews"] for row in assurance["actions"]))
        candidate = prepare_candidate(bundle, flow_export=flow, assurance_export=assurance,
                                      trust_export=trust, now=NOW)
        self.assertEqual(candidate["state"], "READY_FOR_BLIND_REVIEW")

    def test_document_content_mismatch_fails(self):
        application = self.bundle.document["content"]["application"]
        application["answers"]["name"] = "Different Applicant"
        self.bundle = rebundle(self.bundle, self.assurance, self.trust, application)
        with self.assertRaisesRegex(ValueError, "payload differs"):
            self.prepare()

    def test_claim_drift_remains_blocked(self):
        self.assurance["facts"][0]["field"] = "a_different_field"
        self.bundle = rebundle(self.bundle, self.assurance, self.trust)
        self.assertIn("assurance:identity_claim_drift", self.prepare()["reasons"])

    def test_original_contract_unchanged(self):
        bundle, flow, assurance, trust = make_fixture()
        self.assertEqual(evaluate_candidate(bundle, flow_export=flow, assurance_export=assurance,
                                            trust_export=trust, now=NOW)["state"], "READY_FOR_BLIND_REVIEW")
        for action in assurance["actions"]:
            action["reviews"] = []
        self.assertEqual(evaluate_candidate(bundle, flow_export=flow, assurance_export=assurance,
                                            trust_export=trust, now=NOW)["state"], "BLOCKED")

    def test_real_stored_review_over_90_seconds_with_fresh_preflight(self):
        config = {row["reviewer_id"]: digest({"model": row["family"], "revision": "1"})
                  for row in self.assurance["reviewers"]}
        candidate = self.prepare(reviewer_config_sha256=config)
        with tempfile.TemporaryDirectory() as directory:
            store = ReviewStore(Path(directory) / "reviews.sqlite")
            subject = candidate["subject_sha256"]
            store.create_round("synthetic-long-review", candidate["subject"], candidate["reviewer_ids"],
                               NOW + timedelta(minutes=10), now=NOW)
            later = NOW + timedelta(seconds=120)
            for reviewer in candidate["reviewer_ids"]:
                store.phase_a("synthetic-long-review", reviewer, subject, now=later)
                store.commit("synthetic-long-review", reviewer, subject, verdict="PASS",
                             covered_claim_ids=candidate["subject"]["required_claim_ids"], now=later)
            store.seal("synthetic-long-review", subject, now=later)
            for reviewer in candidate["reviewer_ids"]:
                store.phase_b("synthetic-long-review", reviewer, subject, now=later)
                store.audit("synthetic-long-review", reviewer, subject, verdict="PASS", now=later)
            kwargs = dict(now=later, lead_anchor=candidate["lead_anchor"], reviews=store,
                          round_id="synthetic-long-review", reviewer_config_sha256=config)
            stale = preflight_candidate(self.bundle, flow_export=self.flow, assurance_export=self.assurance,
                                        trust_export=self.trust, **kwargs)
            self.assertEqual(stale["state"], "BLOCKED")
            self.flow, self.assurance, self.trust = observe_again(self.flow, self.assurance, self.trust, later)
            fresh = preflight_candidate(self.bundle, flow_export=self.flow, assurance_export=self.assurance,
                                        trust_export=self.trust, **kwargs)
            self.assertEqual(fresh["state"], "PREFLIGHT_CHECKS_PASSED")
            self.assertFalse(fresh["execution_authorized"])
            changed_configs = dict(config)
            changed_configs[next(iter(config))] = digest("replacement-model")
            reconfigured = preflight_candidate(self.bundle, flow_export=self.flow, assurance_export=self.assurance,
                                               trust_export=self.trust,
                                               **{**kwargs, "reviewer_config_sha256": changed_configs})
            self.assertEqual(reconfigured["state"], "BLOCKED")
            self.assertTrue(any("subject_changed" in reason for reason in reconfigured["reasons"]))
            self.assurance["evidence"][0]["status"] = "REVOKED"
            self.bundle = rebundle(self.bundle, self.assurance, self.trust)
            revoked = preflight_candidate(self.bundle, flow_export=self.flow, assurance_export=self.assurance,
                                          trust_export=self.trust, **kwargs)
            self.assertEqual(revoked["state"], "BLOCKED")
            self.assertIn("BLIND_REVIEW_NOT_PASSED", revoked["reasons"])
            self.assertTrue(any("subject_changed" in reason for reason in revoked["reasons"]))


if __name__ == "__main__":
    unittest.main()
