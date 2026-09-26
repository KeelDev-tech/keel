"""Finite-model safety, mutation sensitivity and supplied-trace conformance."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from keel_loki.modelcheck import (ACTIONS, INITIAL, MUTANTS, ModelCheckError, check_model,
                                 demo, machine_sha256, replay_trace, transitions)


def test_exhaustive_finite_machine_has_safety_and_bounded_controller_progress():
    result = check_model()
    assert result["status"] == "VERIFIED_FINITE_MODEL"
    assert result["exploration_complete"] is True
    assert result["states_discovered"] > 100
    assert result["transitions_explored"] > result["states_discovered"]
    assert set(result["action_coverage"]) == set(ACTIONS)
    assert all(result["action_coverage"].values())
    assert result["progress_states_checked"] == result["states_discovered"]
    assert result["progress_max_controller_steps"] <= 3
    assert result["progress_failures"] == []
    assert result["tla_plus"]["status"] == "NOT_RUN"
    assert result["production_implementation_proven"] is False
    assert result["human_approval_authenticated"] is False


@pytest.mark.parametrize("mutant", MUTANTS)
def test_every_seeded_broken_control_produces_a_counterexample(mutant):
    result = check_model(mutant=mutant)
    assert result["status"] == "COUNTEREXAMPLE"
    assert result["counterexample"]["violations"]
    trace = result["counterexample"]["trace"]
    assert trace["steps"]
    assert trace["laboratory_mutant"] == mutant
    with pytest.raises(ModelCheckError, match="laboratory_trace"):
        replay_trace(trace)


def test_resource_limit_is_incomplete_never_pass():
    result = check_model(max_states=5)
    assert result["status"] == "INCOMPLETE"
    assert result["exploration_complete"] is False
    assert result["progress_states_checked"] == 0
    assert result["progress_max_controller_steps"] is None


def test_actual_ordered_observations_conform_and_forged_unknown_retry_fails():
    trace = demo()["trace"]
    assert replay_trace(trace)["steps_verified"] == 5
    forged = deepcopy(trace)
    forged["steps"][-1]["action"] = "start"
    forged["steps"][-1]["state"]["phase"] = "ACTIVE"
    checked = replay_trace(forged)
    assert checked["status"] == "NONCONFORMING"
    assert checked["first_invalid_step"] == 4
    assert checked["observations_authenticated"] is False


def test_revision_invalidation_makes_active_attempt_unknown():
    state = INITIAL
    for action in ("approve", "claim_a", "start", "revise"):
        state = next(target for name, target in transitions(state) if name == action)
    assert state.phase == "UNKNOWN"
    assert state.start_revision == 0 and state.revision == 1
    assert "start" not in {name for name, target in transitions(state)}
    assert "finish" not in {name for name, target in transitions(state)}


def test_trace_machine_pin_and_json_types_are_exact():
    trace = demo()["trace"]
    with pytest.raises(ModelCheckError, match="pin_mismatch"):
        replay_trace(trace, expected_machine_sha256="0" * 64)
    trace["initial"]["fence"] = False
    with pytest.raises(ModelCheckError, match="type_invalid"):
        replay_trace(trace)


@pytest.mark.parametrize("value", [0, True, 100001, "5"])
def test_invalid_exploration_bounds_are_rejected(value):
    with pytest.raises(ModelCheckError):
        check_model(max_states=value)
