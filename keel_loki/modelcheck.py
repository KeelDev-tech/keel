"""Executable finite approval/recovery model; no execution capability.

Exploration is exhaustive only for the explicit finite machine below. Progress
checks freeze environmental changes and run the documented controller policy;
they are not a proof of liveness for an arbitrary scheduler or production host.
"""
from collections import deque
from dataclasses import asdict, dataclass, replace
import hashlib
from pathlib import Path

from keel_trust.common import canonical, digest


class ModelCheckError(ValueError):
    pass


@dataclass(frozen=True)
class State:
    revision: int = 0
    approval: int = -1
    fence: int = 0
    owner: str = ""
    phase: str = "IDLE"
    start_revision: int = -1
    start_approval: int = -1
    start_fence: int = 0
    finish_fence: int = 0
    held: bool = False
    ever_held: bool = False
    rate_limited: bool = False
    ever_rate_limited: bool = False
    ever_unknown: bool = False
    escalated: bool = False


INITIAL = State()
MUTANTS = ("unapproved_start", "stale_completion", "retry_unknown", "forget_429", "clear_hold")
ACTIONS = ("approve", "revoke", "revise", "claim_a", "claim_b", "expire", "start",
           "finish", "crash", "hold", "rate_429", "escalate")


def machine_sha256():
    """Bind traces to the actual Python machine source, not caller labels."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _unknown(state, **changes):
    changes.update(phase="UNKNOWN", ever_unknown=True, owner="")
    return replace(state, **changes)


def transitions(state, *, mutant=None):
    """Enumerate enabled transitions; disabled worker messages never mutate."""
    if mutant is not None and mutant not in MUTANTS:
        raise ModelCheckError("unknown_laboratory_mutant")
    out = []
    def add(action, target):
        out.append((action, target))
    if state.phase == "IDLE" and state.approval != state.revision:
        add("approve", replace(state, approval=state.revision))
    if state.approval != -1:
        add("revoke", _unknown(state, approval=-1) if state.phase == "ACTIVE"
            else replace(state, approval=-1))
    if state.revision == 0:
        add("revise", _unknown(state, revision=1) if state.phase == "ACTIVE"
            else replace(state, revision=1))
    if state.phase == "IDLE" and not state.owner and state.fence < 2:
        for owner in ("a", "b"):
            add("claim_" + owner, replace(state, owner=owner, fence=state.fence + 1))
    if state.owner:
        add("expire", _unknown(state) if state.phase == "ACTIVE" else replace(state, owner=""))
    if (state.phase == "IDLE" and state.owner and not state.held and not state.rate_limited
            and (state.approval == state.revision or mutant == "unapproved_start")):
        add("start", replace(state, phase="ACTIVE", start_revision=state.revision,
                              start_approval=state.approval, start_fence=state.fence))
    if state.phase == "ACTIVE":
        add("finish", replace(state, phase="DONE", finish_fence=state.fence, owner=""))
        add("crash", _unknown(state))
        if mutant == "stale_completion":
            add("lab_stale_completion", replace(state, phase="DONE", finish_fence=state.start_fence - 1, owner=""))
    if not state.held:
        add("hold", _unknown(state, held=True, ever_held=True) if state.phase == "ACTIVE"
            else replace(state, held=True, ever_held=True))
    if not state.rate_limited:
        add("rate_429", _unknown(state, rate_limited=True, ever_rate_limited=True) if state.phase == "ACTIVE"
            else replace(state, rate_limited=True, ever_rate_limited=True))
    if state.phase != "DONE" and not state.escalated and _blocked(state):
        add("escalate", replace(state, escalated=True))
    if mutant == "retry_unknown" and state.phase == "UNKNOWN":
        add("lab_retry_unknown", replace(state, phase="IDLE"))
    if mutant == "forget_429" and state.rate_limited:
        add("lab_forget_429", replace(state, rate_limited=False))
    if mutant == "clear_hold" and state.held:
        add("lab_clear_hold", replace(state, held=False))
    return out


def _blocked(state):
    return (state.phase == "UNKNOWN" or state.held or state.rate_limited
            or state.approval != state.revision or state.phase == "IDLE" and not state.owner and state.fence == 2)


def invariant_failures(state):
    failures = []
    if state.phase == "ACTIVE" and (not state.owner or state.approval != state.revision
            or state.start_approval != state.start_revision or state.start_revision != state.revision
            or state.start_fence != state.fence or state.held or state.rate_limited):
        failures.append("active_requires_current_approval_revision_lease_and_clear_gates")
    if state.phase in {"ACTIVE", "DONE"} and state.start_approval != state.start_revision:
        failures.append("attempt_requires_matching_approval")
    if state.phase == "DONE" and state.finish_fence != state.start_fence:
        failures.append("completion_requires_original_fence")
    if state.ever_unknown and state.phase != "UNKNOWN":
        failures.append("unknown_cannot_retry_or_complete")
    if state.ever_rate_limited and not state.rate_limited:
        failures.append("rate_limit_is_durable")
    if state.ever_held and not state.held:
        failures.append("holds_cannot_be_cleared_by_agent")
    return failures


def _trace(state, parents, *, mutant=None):
    steps = []
    while parents[state] is not None:
        before, action = parents[state]
        steps.append({"action": action, "state": asdict(state)})
        state = before
    steps.reverse()
    return {"schema": "keel.loki.trace.v1", "machine_sha256": machine_sha256(),
            "initial": asdict(INITIAL), "laboratory_mutant": mutant, "steps": steps}


def _controller(state):
    """Environment frozen: escalate a block, otherwise claim/start/finish."""
    if state.phase == "DONE" or state.escalated:
        return None
    action = "escalate" if _blocked(state) else "finish" if state.phase == "ACTIVE" else "start" if state.owner else "claim_a"
    return next(((name, target) for name, target in transitions(state) if name == action), None)


def check_model(*, mutant=None, max_states=50000):
    if mutant is not None and mutant not in MUTANTS:
        raise ModelCheckError("unknown_laboratory_mutant")
    if type(max_states) is not int or not 1 <= max_states <= 100000:
        raise ModelCheckError("state_limit_invalid")
    queue, parents = deque([INITIAL]), {INITIAL: None}
    edge_count, action_counts, counterexample = 0, {}, None
    complete, depth = True, {INITIAL: 0}
    while queue:
        state = queue.popleft()
        failures = invariant_failures(state)
        if failures:
            counterexample = {"violations": failures, "trace": _trace(state, parents, mutant=mutant)}
            complete = False
            break
        for action, target in transitions(state, mutant=mutant):
            edge_count += 1
            action_counts[action] = action_counts.get(action, 0) + 1
            if target not in parents:
                if len(parents) >= max_states:
                    complete = False
                    queue.clear()
                    break
                parents[target] = (state, action)
                depth[target] = depth[state] + 1
                queue.append(target)
    progress_failures, longest = [], 0
    if complete and counterexample is None:
        for state in parents:
            current = state
            for steps in range(4):
                if current.phase == "DONE" or current.escalated:
                    longest = max(longest, steps)
                    break
                following = _controller(current)
                if following is None:
                    progress_failures.append(asdict(state))
                    break
                current = following[1]
            else:
                progress_failures.append(asdict(state))
    status = "COUNTEREXAMPLE" if counterexample else "INCOMPLETE" if not complete else "FAIL" if progress_failures else "VERIFIED_FINITE_MODEL"
    return {"schema": "keel.loki.modelcheck.v1", "status": status,
            "machine_sha256": machine_sha256(), "laboratory_mutant": mutant,
            "exploration_complete": complete, "states_discovered": len(parents),
            "transitions_explored": edge_count, "maximum_discovered_depth": max(depth.values()),
            "action_coverage": action_counts, "counterexample": counterexample,
            "safety_invariants": 6, "progress_states_checked": len(parents) if complete else 0,
            "progress_max_controller_steps": longest if complete else None,
            "progress_failures": progress_failures[:8],
            "progress_assumptions": ["environment frozen during controller steps", "controller scheduled fairly",
                "local transitions complete", "escalation is durable reporting, not human resolution"],
            "finite_bounds": {"revisions": 2, "workers": 2, "maximum_fence": 2, "jobs": 1},
            "tla_plus": {"status": "NOT_RUN", "reason": "TLC is a separate optional tool; Python exploration is not TLC"},
            "production_implementation_proven": False, "human_approval_authenticated": False,
            "execution_authorized": False}


def _state(value):
    if type(value) is not dict or set(value) != set(asdict(INITIAL)):
        raise ModelCheckError("trace_state_schema_invalid")
    for key in ("revision", "approval", "fence", "start_revision", "start_approval", "start_fence", "finish_fence"):
        if type(value[key]) is not int:
            raise ModelCheckError("trace_state_type_invalid")
    for key in ("held", "ever_held", "rate_limited", "ever_rate_limited", "ever_unknown", "escalated"):
        if type(value[key]) is not bool:
            raise ModelCheckError("trace_state_type_invalid")
    if value["owner"] not in ("", "a", "b") or value["phase"] not in ("IDLE", "ACTIVE", "UNKNOWN", "DONE"):
        raise ModelCheckError("trace_state_value_invalid")
    return State(**value)


def replay_trace(trace, *, expected_machine_sha256=None):
    """Check supplied full state observations against the current safe machine.

    A matching trace proves contract conformance of these supplied observations,
    not their authenticity or completeness relative to a real running host.
    """
    if type(trace) is not dict or set(trace) != {"schema", "machine_sha256", "initial", "laboratory_mutant", "steps"}:
        raise ModelCheckError("trace_schema_invalid")
    current_pin = machine_sha256()
    if (trace["schema"] != "keel.loki.trace.v1" or trace["machine_sha256"] != current_pin
            or expected_machine_sha256 is not None and expected_machine_sha256 != current_pin):
        raise ModelCheckError("trace_machine_pin_mismatch")
    if trace["laboratory_mutant"] is not None:
        raise ModelCheckError("laboratory_trace_not_production_conformance")
    if type(trace["steps"]) is not list or len(trace["steps"]) > 10000:
        raise ModelCheckError("trace_size_invalid")
    current = _state(trace["initial"])
    if current != INITIAL:
        raise ModelCheckError("trace_initial_state_invalid")
    seen_actions, states = {}, {current}
    for index, step in enumerate(trace["steps"]):
        if type(step) is not dict or set(step) != {"action", "state"} or type(step["action"]) is not str:
            raise ModelCheckError("trace_step_schema_invalid")
        target = _state(step["state"])
        if (step["action"], target) not in transitions(current) or invariant_failures(target):
            return {"schema": "keel.loki.trace-check.v1", "status": "NONCONFORMING",
                    "first_invalid_step": index, "steps_verified": index,
                    "machine_sha256": current_pin, "observations_authenticated": False,
                    "execution_authorized": False}
        seen_actions[step["action"]] = seen_actions.get(step["action"], 0) + 1
        states.add(target)
        current = target
    return {"schema": "keel.loki.trace-check.v1", "status": "CONFORMING",
            "first_invalid_step": None, "steps_verified": len(trace["steps"]),
            "unique_states_observed": len(states), "action_coverage": seen_actions,
            "machine_sha256": current_pin, "trace_sha256": digest(trace),
            "observations_authenticated": False, "execution_authorized": False}


def demo():
    result = check_model()
    mutants = {name: check_model(mutant=name)["status"] for name in MUTANTS}
    state, steps = INITIAL, []
    for action in ("approve", "claim_a", "start", "crash", "escalate"):
        state = next(target for name, target in transitions(state) if name == action)
        steps.append({"action": action, "state": asdict(state)})
    trace = {"schema": "keel.loki.trace.v1", "machine_sha256": machine_sha256(),
             "initial": asdict(INITIAL), "laboratory_mutant": None, "steps": steps}
    return {"schema": "keel.loki.modelcheck-demo.v1", "synthetic": True,
            "model": result, "mutant_results": mutants, "trace": trace,
            "trace_check": replay_trace(trace), "execution_authorized": False}
