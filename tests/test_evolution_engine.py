import hashlib
from pathlib import Path
import tempfile
import unittest

from keel_evolve.core import EvolutionEngine
from keel_eval.reliability import Runner, freeze_plan


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def always_pass(subject, config, seed):
    return "PASS"


def always_fail(subject, config, seed):
    return "FAIL"


class EvolutionEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="keel-evolution-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "state"
        self.engine = EvolutionEngine.create(self.home, clock=lambda: 1000)
        for number, outcome in ((1, "PASS"), (2, "PASS"), (3, "FAIL")):
            self.engine.observe("e-" + str(number), "forms", "revision-check", outcome,
                evidence_sha256=sha("evidence-" + str(number)),
                context={"revision": number}, utility_milli=number * 10)

    def lesson(self, name="lesson"):
        return self.engine.propose_lesson(name, "forms", "Check the revision first.",
            ["e-1", "e-2"], applicability={"schema": 1}, invalidators=["revision-change"])

    def procedure(self, name="procedure"):
        return self.engine.propose_procedure(name, "forms",
            [{"operation": "compare", "value": "$revision"}], ["e-1", "e-2"],
            required_state={"site": "fixture", "revision": 1},
            mutable_inputs=["revision"], validators=[sha("validator")])

    def receipt(self, artifact, *, candidate="PASS", champion_passes=8):
        cases = [{"case_id": "case-" + str(i),
                  "champion": "PASS" if i < champion_passes else "FAIL",
                  "candidate": candidate} for i in range(10)]
        return self.engine.tournament(artifact["artifact_id"], cases,
                                      harness_sha256=sha("harness"), repeats=3)

    def test_evidence_is_idempotent_and_conflicts_hold(self):
        repeated = self.engine.observe("e-1", "forms", "revision-check", "PASS",
            evidence_sha256=sha("evidence-1"), context={"revision": 1}, utility_milli=10)
        self.assertTrue(repeated["replay"])
        with self.assertRaisesRegex(ValueError, "evolution_evidence_conflict"):
            self.engine.observe("e-1", "forms", "different", "PASS",
                evidence_sha256=sha("evidence-1"), context={"revision": 1}, utility_milli=10)

    def test_candidate_memory_is_invisible_after_simulation_screen(self):
        artifact = self.lesson()
        self.assertEqual(self.engine.retrieve("forms", {})["lessons"], [])
        result = self.engine.evaluate("eval-lesson", artifact["artifact_id"], self.receipt(artifact))
        self.assertEqual(result["state"], "SIMULATION")
        self.assertFalse(result["production_qualified"])
        retrieved = self.engine.retrieve("forms", {"schema": 1})
        self.assertEqual(retrieved["lessons"], [])
        self.assertFalse(retrieved["execution_authorized"])
        restarted = EvolutionEngine(self.home, clock=lambda: 1000)
        self.assertEqual(restarted.status()["artifacts"], {"LESSON:SIMULATION": 1})

    def test_pinned_nonsynthetic_reliability_run_promotes(self):
        artifact = self.lesson()
        cases = [{"case_id": "qualified-" + str(i), "tags": ["fixture"],
                  "expected_verdict": "FAIL", "label_rationale": "Controlled adjudication fixture.",
                  "subject": {"required_claim_ids": ["claim"],
                    "claims": [{"claim_id": "claim", "text": "Claim."}],
                    "evidence": [{"evidence_id": "evidence", "text": "Contradiction."}]}}
                 for i in range(20)]
        dataset = {"schema": "keel.eval.dataset.v1", "dataset_id": "operator-heldout-fixture",
                   "synthetic": False, "split": "held_out", "label_source": "operator_supplied",
                   "cases": cases}
        adjudications = {row["case_id"]: {"cluster_id": row["case_id"],
            "adjudicator_id": "test-adjudicator", "record_sha256": sha(row["case_id"])}
            for row in cases}
        baseline, candidate = Runner(always_pass, {}), Runner(always_fail, {})
        plan = freeze_plan(dataset, baseline=baseline, candidate=candidate,
                           adjudications=adjudications, repeats=3)
        result = self.engine.qualify("qualified-eval", "lesson", plan, dataset,
            baseline=baseline, candidate=candidate,
            adjudication_validator=lambda frozen_plan, frozen_dataset: True)
        self.assertEqual(result["state"], "PROMOTED")
        self.assertTrue(result["production_qualified"])
        self.assertEqual(self.engine.retrieve("forms", {})["lessons"][0]["artifact_id"], "lesson")

    def test_failed_tournament_cannot_promote(self):
        artifact = self.lesson()
        receipt = self.receipt(artifact, candidate="FAIL")
        result = self.engine.evaluate("eval-failed", "lesson", receipt)
        self.assertEqual(result["decision"], "REJECT")
        self.assertEqual(result["state"], "CANDIDATE")
        self.assertEqual(self.engine.retrieve("forms", {})["lessons"], [])

    def test_false_pass_or_regression_blocks_promotion(self):
        artifact = self.lesson()
        cases = [{"case_id": "safety-" + str(i), "champion": "PASS" if i < 8 else "FAIL",
                  "candidate": "FALSE_PASS" if i == 9 else "PASS"} for i in range(10)]
        receipt = self.engine.tournament("lesson", cases, harness_sha256=sha("harness"))
        self.assertEqual(receipt["false_passes"], 1)
        self.assertEqual(self.engine.evaluate("eval-safety", "lesson", receipt)["decision"], "REJECT")

    def test_replay_requires_promoted_exact_state_and_exact_inputs(self):
        artifact = self.procedure()
        with self.assertRaisesRegex(ValueError, "replay_procedure_not_promoted"):
            self.engine.replay_plan("procedure", {"site": "fixture", "revision": 1},
                                    {"revision": sha("r")})
        self.engine.evaluate("eval-procedure", "procedure", self.receipt(artifact))
        with self.assertRaisesRegex(ValueError, "replay_precondition_mismatch"):
            self.engine.replay_plan("procedure", {"site": "changed", "revision": 1},
                                    {"revision": sha("r")})
        with self.assertRaisesRegex(ValueError, "replay_input_binding_mismatch"):
            self.engine.replay_plan("procedure", {"site": "fixture", "revision": 1}, {})
        plan = self.engine.replay_plan("procedure", {"site": "fixture", "revision": 1},
                                      {"revision": sha("r")})
        self.assertFalse(plan["execution_authorized"])
        self.assertTrue(plan["simulation_only"])
        self.assertTrue(plan["requires_fresh_authority"])

    def test_retired_memory_disappears(self):
        artifact = self.lesson()
        self.engine.evaluate("eval", "lesson", self.receipt(artifact))
        self.engine.retire("lesson", "fixture contradicted")
        self.assertEqual(self.engine.retrieve("forms", {})["lessons"], [])

    def test_failure_scenario_is_deduplicated_and_inert(self):
        kwargs = dict(fixture={"response": "changed"},
                      invariant={"path": ["status"], "operator": "equals", "expected": "HELD"},
                      source_evidence_id="e-3")
        first = self.engine.failure_to_scenario("forms", {"code": "FORM_CHANGED"}, **kwargs)
        second = self.engine.failure_to_scenario("forms", {"code": "FORM_CHANGED"}, **kwargs)
        self.assertEqual(first, second)
        self.assertFalse(first["executable_code"])
        self.assertEqual(self.engine.status()["scenarios"], 1)
        self.assertTrue(self.engine.run_scenario(first["scenario_id"], {"status": "HELD"})["passed"])
        self.assertFalse(self.engine.run_scenario(first["scenario_id"], {"status": "READY"})["passed"])

    def test_information_gain_respects_budget_and_authority(self):
        report = self.engine.prioritize([
            {"investigation_id": "large-shared", "affected_tasks": 100,
             "information_gain_milli": 900, "success_probability_milli": 900,
             "cost_milli": 100, "deadline_weight_milli": 10, "authority_required": False},
            {"investigation_id": "restricted", "affected_tasks": 1000,
             "information_gain_milli": 1000, "success_probability_milli": 1000,
             "cost_milli": 1, "deadline_weight_milli": 1000, "authority_required": True},
            {"investigation_id": "small", "affected_tasks": 1,
             "information_gain_milli": 100, "success_probability_milli": 100,
             "cost_milli": 50, "deadline_weight_milli": 0, "authority_required": False}],
            budget_milli=100)
        self.assertEqual(report["selected"], ["large-shared"])
        self.assertFalse(next(row for row in report["ranked"]
                         if row["investigation_id"] == "restricted")["eligible"])
        self.assertFalse(report["execution_authorized"])

    def test_artifact_revision_binding_and_tournament_bounds(self):
        artifact = self.lesson()
        receipt = self.receipt(artifact)
        receipt["artifact_body_sha256"] = sha("different")
        with self.assertRaisesRegex(ValueError, "evaluation_artifact_revision_mismatch"):
            self.engine.evaluate("wrong-revision", "lesson", receipt)
        with self.assertRaisesRegex(ValueError, "tournament_cases_invalid"):
            self.engine.tournament("lesson", [], harness_sha256=sha("harness"))

    def test_compute_router_is_bounded_and_does_not_call_models(self):
        report = self.engine.route_compute([
            {"task_id": "easy", "ambiguity_milli": 10, "risk_milli": 10,
             "novelty_milli": 10, "deterministic_procedure_available": True},
            {"task_id": "hard-one", "ambiguity_milli": 1000, "risk_milli": 1000,
             "novelty_milli": 1000, "deterministic_procedure_available": False},
            {"task_id": "hard-two", "ambiguity_milli": 900, "risk_milli": 1000,
             "novelty_milli": 1000, "deterministic_procedure_available": False}],
            advanced_budget_units=1)
        assigned = {row["task_id"]: row["assigned_tier"] for row in report["tasks"]}
        self.assertEqual(assigned["easy"], "DETERMINISTIC")
        self.assertEqual(sum(value == "ADVANCED_REVIEW" for value in assigned.values()), 1)
        self.assertEqual(sum(value == "HELD_BUDGET" for value in assigned.values()), 1)
        self.assertEqual(report["model_calls"], 0)

    def test_community_indexes_are_private_and_quarantined_until_local_match(self):
        failure = {"code": "FORM_CHANGED"}
        kwargs = dict(fixture={"response": "changed"},
                      invariant={"path": ["status"], "operator": "equals", "expected": "HELD"},
                      source_evidence_id="e-3")
        self.engine.failure_to_scenario("forms", failure, **kwargs)
        index = self.engine.export_scenario_index()
        self.assertFalse(index["contains_fixture_content"])
        self.assertNotIn("changed", str(index))
        other_home = Path(self.temp.name) / "other"
        other = EvolutionEngine.create(other_home, clock=lambda: 1000)
        first = other.import_scenario_index(index)
        self.assertEqual(first["quarantined"], 1)
        other.observe("other-evidence", "forms", "revision-check", "FAIL",
            evidence_sha256=sha("other"), context={}, utility_milli=0)
        other.failure_to_scenario("forms", failure, fixture={"response": "changed"},
            invariant={"path": ["status"], "operator": "equals", "expected": "HELD"},
            source_evidence_id="other-evidence")
        second = other.import_scenario_index(index)
        self.assertEqual(second["validated_by_exact_local_scenario"], 1)

    def test_action_contract_detects_any_stale_binding(self):
        artifact = self.procedure()
        self.engine.evaluate("eval-procedure", "procedure", self.receipt(artifact))
        plan = self.engine.replay_plan("procedure", {"site": "fixture", "revision": 1},
                                      {"revision": sha("r")})
        contract = self.engine.bind_action_contract(plan, target={"account": "fixture"},
            evidence_bindings={"form": sha("form")}, authority_revision_sha256=sha("approval"))
        current = self.engine.validate_action_contract(contract,
            current_target={"account": "fixture"}, current_evidence_bindings={"form": sha("form")},
            current_authority_revision_sha256=sha("approval"))
        stale = self.engine.validate_action_contract(contract,
            current_target={"account": "changed"}, current_evidence_bindings={"form": sha("form")},
            current_authority_revision_sha256=sha("approval"))
        self.assertEqual(current["status"], "CURRENT_REQUIRES_ACTION_GATE")
        self.assertEqual(stale["status"], "STALE")
        self.assertFalse(current["execution_authorized"])


if __name__ == "__main__":
    unittest.main()
