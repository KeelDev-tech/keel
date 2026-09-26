"""Synthetic end-to-end demonstration for the guarded evolution engine."""
from hashlib import sha256

from .core import EvolutionEngine


def _sha(text):
    return sha256(text.encode()).hexdigest()


def run(home):
    engine = EvolutionEngine.create(home)
    first = engine.observe("ev-1", "document-review", "verify-before-use", "PASS",
                           evidence_sha256=_sha("receipt-1"), context={"synthetic": True},
                           utility_milli=900)
    engine.observe("ev-2", "document-review", "verify-before-use", "PASS",
                   evidence_sha256=_sha("receipt-2"), context={"synthetic": True},
                   utility_milli=950)
    lesson = engine.propose_lesson("lesson-verify", "document-review",
        "Verify a source revision before relying on a prior result.", ["ev-1", "ev-2"],
        applicability={"source_revision_required": True}, invalidators=["revision_changed"])
    procedure = engine.propose_procedure("procedure-verify", "document-review",
        [{"operation": "compare_revision", "input": "$revision"}], ["ev-1", "ev-2"],
        required_state={"connector": "local", "schema": 1}, mutable_inputs=["revision"],
        validators=[_sha("validator-v1")])
    cases = [{"case_id": "heldout-" + str(i), "champion": "PASS" if i < 8 else "FAIL",
              "candidate": "PASS"} for i in range(10)]
    receipt = engine.tournament("lesson-verify", cases, harness_sha256=_sha("harness"))
    lesson_eval = engine.evaluate("eval-lesson", "lesson-verify", receipt)
    receipt = engine.tournament("procedure-verify", cases, harness_sha256=_sha("harness"))
    procedure_eval = engine.evaluate("eval-procedure", "procedure-verify", receipt)
    plan = engine.replay_plan("procedure-verify", {"connector": "local", "schema": 1},
                              {"revision": _sha("document")})
    scenario = engine.failure_to_scenario("document-review", {"code": "REVISION_CHANGED"},
        fixture={"old": _sha("old"), "new": _sha("new")},
        invariant={"path": ["status"], "operator": "equals", "expected": "HELD"},
        source_evidence_id="ev-1")
    scenario_result = engine.run_scenario(scenario["scenario_id"], {"status": "HELD"})
    priorities = engine.prioritize([
        {"investigation_id": "shared-blocker", "affected_tasks": 40,
         "information_gain_milli": 900, "success_probability_milli": 800,
         "cost_milli": 100, "deadline_weight_milli": 200, "authority_required": False},
        {"investigation_id": "external-action", "affected_tasks": 100,
         "information_gain_milli": 1000, "success_probability_milli": 900,
         "cost_milli": 50, "deadline_weight_milli": 500, "authority_required": True}],
        budget_milli=100)
    checks = {"evidence_recorded": not first["replay"],
              "lesson_screened_but_not_production_promoted": (lesson_eval["state"] == "SIMULATION"
                                                               and not lesson_eval["production_qualified"]),
              "procedure_screened_for_simulation": procedure_eval["state"] == "SIMULATION",
              "replay_still_unauthorized": (plan["execution_authorized"] is False
                                              and plan["simulation_only"] is True),
              "failure_became_fixed_dsl_scenario": (scenario["executable_code"] is False
                                                     and scenario_result["passed"]),
              "authority_required_not_selected": priorities["selected"] == ["shared-blocker"]}
    return {"schema": "keel.evolution.demo.v1",
            "status": "EVOLUTION_DEMO_PASSED" if all(checks.values()) else "EVOLUTION_DEMO_FAILED",
            "checks": checks, "status_report": engine.status(), "synthetic": True,
            "execution_authorized": False, "paid_services_required": False}
