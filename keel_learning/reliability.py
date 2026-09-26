"""Pinned empirical agent/task matrix; no confidence self-reports or execution."""
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import math

from keel_agent.models import ReviewerConfig
from keel_loki.calibration import binomial_upper
from keel_loki.routing import make_policy
from .controller import bindings, canonical, digest, number, require, token
import json


def matrix_plan(*, matrix_id, agents, tasks, source_sha256, calibration_sha256, delta=.05, max_error=.1, min_coverage=.5, min_trials=30, synthetic=False):
    """Freeze the finite agent/task family before labels; host must attest that.

    Each agent entry has config (ReviewerConfig dict), model_sha256, and
    config_sha256. All model identities are explicit operator measurements.
    """
    plan = {"schema": "keel.learning.reliability-plan.v1", "matrix_id": matrix_id,
        "agents": agents, "tasks": tasks, "source_sha256": source_sha256,
        "calibration_sha256": calibration_sha256, "delta": delta, "max_error": max_error,
        "min_coverage": min_coverage, "min_trials": min_trials, "synthetic": synthetic}
    return _plan(plan)


def _plan(plan):
    plan = json.loads(canonical(plan))
    require(type(plan) is dict and set(plan) == {"schema", "matrix_id", "agents", "tasks", "source_sha256", "calibration_sha256", "delta", "max_error", "min_coverage", "min_trials", "synthetic"}, "matrix_plan_fields_invalid")
    require(plan["schema"] == "keel.learning.reliability-plan.v1", "matrix_plan_schema_invalid")
    token(plan["matrix_id"])
    require(type(plan["agents"]) is dict and 1 <= len(plan["agents"]) <= 16, "agent_roster_invalid")
    require(type(plan["tasks"]) is list and 1 <= len(plan["tasks"]) <= 32 and len(set(plan["tasks"])) == len(plan["tasks"]), "task_family_invalid")
    for task in plan["tasks"]:
        token(task)
    for aid, entry in plan["agents"].items():
        token(aid)
        require(type(entry) is dict and set(entry) == {"config", "config_sha256", "model_sha256"}, "agent_fields_invalid")
        try:
            cfg = ReviewerConfig(**entry["config"])
        except (TypeError, ValueError):
            require(False, "agent_config_invalid")
        require(cfg.reviewer_id == aid and digest(asdict(cfg)) == entry["config_sha256"], "agent_config_pin_mismatch")
        bindings({"source_sha256": plan["source_sha256"], "config_sha256": entry["config_sha256"],
            "model_sha256": entry["model_sha256"], "calibration_sha256": plan["calibration_sha256"]})
    number(plan["delta"], 1e-12, .25)
    number(plan["max_error"], 0, 1)
    number(plan["min_coverage"], 0, 1)
    require(type(plan["min_trials"]) is int and 1 <= plan["min_trials"] <= 2000, "min_trials_invalid")
    require(type(plan["synthetic"]) is bool, "synthetic_flag_required")
    return plan


def reliability_matrix(plan, trials, *, expected_pin, current_bindings, validator, proof):
    """Simultaneous time-union exact binomial bounds across the frozen cells.

    Each cell requires IID task units, fixed agent/model/score function and
    truthful independent labels. Abstention is measured separately. Trials may
    be paired across agents, but each unit appears only once in each cell.
    The family union bound does not assume independent agents.
    """
    plan = _plan(plan)
    require(digest(plan) == expected_pin, "matrix_pin_mismatch")
    require(type(trials) is list and len(trials) <= 64000, "trials_limit")
    require(callable(validator), "host_validator_required")
    trials = json.loads(canonical(trials))
    try:
        event = {"kind": "reliability_matrix", "plan": plan, "trials_sha256": digest(trials),
            "current_bindings": current_bindings, "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "required_assertions": ["fresh_nonreplayed_proof", "plan_frozen_before_labels", "complete_trial_stream_in_order", "independent_units_within_cell", "labels_authentic_and_not_used_for_training", "no_revoked_trials", "stable_task_population"]}
        accepted = validator(json.loads(canonical(event)), proof)
    except Exception:
        accepted = False
    require(accepted is True, "host_attestation_rejected")
    groups, seen, observations = defaultdict(list), set(), set()
    require(type(current_bindings) is dict and set(current_bindings) == set(plan["agents"]), "current_agent_bindings_required")
    for row in trials:
        require(type(row) is dict and set(row) == {"trial_id", "unit_id", "agent_id", "task_id", "bindings", "observed", "expected", "latency_seconds", "human_correction_minutes", "observation_sha256", "label_sha256"}, "trial_fields_invalid")
        for key in ("trial_id", "unit_id", "agent_id", "task_id"):
            token(row[key])
        require(row["agent_id"] in plan["agents"] and row["task_id"] in plan["tasks"], "trial_outside_frozen_family")
        require(row["trial_id"] not in observations, "duplicate_trial")
        observations.add(row["trial_id"])
        pair = (row["agent_id"], row["task_id"])
        unit = pair + (row["unit_id"],)
        require(unit not in seen, "repeated_unit_cluster")
        seen.add(unit)
        for hash_key in ("observation_sha256", "label_sha256"):
            require(type(row[hash_key]) is str and len(row[hash_key]) == 64 and all(c in "0123456789abcdef" for c in row[hash_key]), "trial_hash_invalid")
        # Reusing an observation is not an independent repeated trial.
        observed_key = pair + (row["observation_sha256"],)
        require(observed_key not in seen, "repeated_observation")
        seen.add(observed_key)
        require(row["expected"] in ("PASS", "FAIL") and row["observed"] in ("PASS", "FAIL", "ABSTAIN", "ERROR"), "verdict_invalid")
        number(row["latency_seconds"], 0, 86400)
        number(row["human_correction_minutes"], 0, 1000000)
        bindings(row["bindings"])
        groups[pair].append(row)
    cells = []
    family = len(plan["agents"]) * len(plan["tasks"])
    for aid, entry in plan["agents"].items():
        wanted = {"config_sha256": entry["config_sha256"], "model_sha256": entry["model_sha256"], "source_sha256": plan["source_sha256"], "calibration_sha256": plan["calibration_sha256"]}
        bindings(current_bindings[aid])
        for task in plan["tasks"]:
            rows = groups[(aid, task)]
            n = len(rows)
            require(n <= 2000, "cell_trial_limit")
            answered = [r for r in rows if r["observed"] in ("PASS", "FAIL")]
            errors = sum(r["observed"] != r["expected"] for r in answered)
            holds = []
            if current_bindings[aid] != wanted or any(r["bindings"] != wanted for r in rows):
                holds.append("BINDINGS_DRIFTED")
            if plan["synthetic"]:
                holds.append("SYNTHETIC_ONLY")
            if n < plan["min_trials"]:
                holds.append("INSUFFICIENT_TRIALS")
            # Half alpha for selected error, half for coverage; spend over the
            # selected-count index and total-count index independently.
            k = len(answered)
            error_delta = plan["delta"] / (2 * family * max(1, k) * (max(1, k) + 1))
            cover_delta = plan["delta"] / (2 * family * max(1, n) * (max(1, n) + 1))
            upper = binomial_upper(errors, k, error_delta)
            coverage_lower = 1 - binomial_upper(n-k, n, cover_delta) if n else 0.0
            if not k or upper > plan["max_error"]:
                holds.append("ERROR_BOUND_EXCEEDS_LIMIT")
            if coverage_lower < plan["min_coverage"]:
                holds.append("COVERAGE_TOO_LOW")
            cells.append({"agent_id": aid, "task_id": task, "trials": n, "answered": k,
                "errors": errors, "abstentions": sum(r["observed"] == "ABSTAIN" for r in rows),
                "runtime_errors": sum(r["observed"] == "ERROR" for r in rows),
                "error_upper": upper, "coverage_lower": coverage_lower,
                "mean_latency_seconds": math.fsum(r["latency_seconds"] for r in rows)/n if n else None,
                "mean_human_correction_minutes": math.fsum(r["human_correction_minutes"] for r in rows)/n if n else None,
                "status": "HOLD" if holds else "ELIGIBLE_FOR_PROPOSAL", "hold_reasons": holds})
    return {"schema": "keel.learning.reliability-matrix.v1", "plan_pin": expected_pin,
        "trials_sha256": digest(trials), "cells": cells, "synthetic": plan["synthetic"],
        "method": "finite_family_time_union_exact_binomial", "assumptions_independently_proven": False,
        "execution_authorized": False, "production_deployed": False}


def propose_route(plan, trials, *, task_id, expected_pin, current_bindings, validator, proof):
    """Recompute the evidence; generate the existing Loki route policy only."""
    plan = _plan(plan)
    report = reliability_matrix(plan, trials, expected_pin=expected_pin,
        current_bindings=current_bindings, validator=validator, proof=proof)
    require(task_id in plan["tasks"], "unknown_task")
    eligible = [c for c in report["cells"] if c["task_id"] == task_id and c["status"] == "ELIGIBLE_FOR_PROPOSAL"]
    ranked = sorted(eligible, key=lambda c: (c["mean_human_correction_minutes"], c["mean_latency_seconds"], c["error_upper"], c["agent_id"]))
    policy = None
    if ranked:
        selected = ranked[0]["agent_id"]
        policy = make_policy(policy_id=plan["matrix_id"] + ":proposal", source_sha256=plan["source_sha256"],
            small_config=ReviewerConfig(**plan["agents"][selected]["config"]), max_model_calls=1)
    return {"schema": "keel.learning.route-proposal.v1", "task_id": task_id,
        "status": "PROPOSAL_ONLY" if policy else "HOLD", "policy": policy, "matrix": report,
        "human_review_required": True, "execution_authorized": False, "model_calls": 0}
