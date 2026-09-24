"""Deterministic human-question scheduling and honest offline policy evaluation.

No probabilities are guessed. Ranking estimates work released *if* an honest,
usable answer resolves its declared blockers; it does not predict that answer.
Approval, attestation and unaided responses are always individual human work.
"""
from __future__ import annotations

import hashlib
import json
import math
import re


class QuestionError(ValueError):
    pass


KINDS = {"fact", "approval", "attestation", "unaided", "system"}
HUMAN_ONLY = {"approval", "attestation", "unaided"}
FLAGS = {"execution_authorized": False, "canonical_writes": 0,
         "answers_generated": 0, "human_decisions_manufactured": 0}


def _check(condition, code):
    if not condition:
        raise QuestionError(code)


def _fields(value, fields):
    _check(type(value) is dict and set(value) == set(fields.split()), "invalid_fields")


def _id(value):
    _check(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value), "invalid_id")


def _stamp(value):
    _check(type(value) is int and 0 <= value <= 253402300799, "invalid_timestamp")


def _number(value, *, positive=False):
    _check(type(value) in (int, float) and math.isfinite(value) and
           (value > 0 if positive else value >= 0), "invalid_number")


def _ids(values, nonempty=False):
    _check(type(values) is list and len(values) <= 2048 and (not nonempty or values), "invalid_id_list")
    for value in values:
        _id(value)
    _check(len(set(values)) == len(values), "duplicate_id")


def _digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise QuestionError("invalid_json") from None
    _check(len(encoded) <= 2_000_000, "input_too_large")
    return hashlib.sha256(encoded.encode()).hexdigest()


def _validate(spec, now):
    _fields(spec, "schema tasks blockers questions")
    _check(spec["schema"] == "keel.loki.question_input.v1", "invalid_schema")
    _stamp(now)
    tables = {}
    for family, id_field in (("tasks", "task_id"), ("blockers", "blocker_id"), ("questions", "question_id")):
        _check(type(spec[family]) is list and len(spec[family]) <= 2048, "invalid_collection")
        table = {}
        for item in spec[family]:
            _check(type(item) is dict and id_field in item, "invalid_record")
            _id(item[id_field])
            _check(item[id_field] not in table, "duplicate_record")
            table[item[id_field]] = item
        tables[family] = table
    tasks, blockers, questions = (tables[key] for key in ("tasks", "blockers", "questions"))
    for question in questions.values():
        _fields(question, "question_id kind scope_ids prompt estimated_minutes reuse_authorized")
        _check(type(question["kind"]) is str and question["kind"] in KINDS - {"system"}, "invalid_question_kind")
        _ids(question["scope_ids"], nonempty=True)
        _check(type(question["prompt"]) is str and 0 < len(question["prompt"].strip()) <= 8000, "invalid_prompt")
        _number(question["estimated_minutes"], positive=True)
        _check(0.001 <= question["estimated_minutes"] <= 1440, "invalid_minutes")
        _check(type(question["reuse_authorized"]) is bool, "invalid_reuse_flag")
        if question["kind"] in HUMAN_ONLY:
            _check(not question["reuse_authorized"] and len(question["scope_ids"]) == 1, "human_decision_not_reusable")
        elif len(question["scope_ids"]) > 1:
            _check(question["reuse_authorized"], "reuse_not_authorized")
    question_blockers = {identity: [] for identity in questions}
    for blocker in blockers.values():
        _fields(blocker, "blocker_id question_id kind scope_id fact_key depends_on state")
        _id(blocker["scope_id"])
        _ids(blocker["depends_on"])
        _check(type(blocker["kind"]) is str and blocker["kind"] in KINDS, "invalid_blocker_kind")
        _check(type(blocker["state"]) is str and blocker["state"] in {"unresolved", "resolved"}, "invalid_blocker_state")
        _check(all(dep in blockers for dep in blocker["depends_on"]), "unknown_dependency")
        if blocker["kind"] == "fact":
            _id(blocker["fact_key"])
        else:
            _check(blocker["fact_key"] is None, "nonfact_key_forbidden")
        qid = blocker["question_id"]
        if blocker["kind"] == "system":
            _check(qid is None, "system_blocker_not_human_question")
        else:
            _id(qid)
            _check(qid in questions, "unknown_question")
            question = questions[qid]
            _check(question["kind"] == blocker["kind"] and
                   blocker["scope_id"] in question["scope_ids"], "question_scope_mismatch")
            question_blockers[qid].append(blocker)
    for qid, linked in question_blockers.items():
        _check(bool(linked), "orphan_question")
        q = questions[qid]
        if q["kind"] in HUMAN_ONLY:
            _check(len(linked) == 1, "human_decision_not_reusable")
        elif len(linked) > 1:
            _check(q["reuse_authorized"] and len({item["fact_key"] for item in linked}) == 1,
                   "fact_reuse_mismatch")
    # Kahn traversal avoids recursion limits and supplies an exact DAG proof.
    remaining = {key: set(value["depends_on"]) for key, value in blockers.items()}
    order = []
    ready = sorted(key for key, deps in remaining.items() if not deps)
    while ready:
        key = ready.pop(0)
        if key not in remaining:
            continue
        del remaining[key]
        order.append(key)
        for other in sorted(remaining):
            remaining[other].discard(key)
            if not remaining[other] and other not in ready:
                ready.append(other)
        ready.sort()
    _check(not remaining, "dependency_cycle")
    ancestors = {}
    resolved = set()
    for key in order:
        blocker = blockers[key]
        ancestors[key] = {key}
        for dep in blocker["depends_on"]:
            ancestors[key].update(ancestors[dep])
        if blocker["state"] == "resolved":
            _check(set(blocker["depends_on"]) <= resolved, "resolved_dependency_inconsistent")
            resolved.add(key)
    task_dependencies = {}
    for task in tasks.values():
        _fields(task, "task_id weight blocker_ids created_at deadline")
        _number(task["weight"], positive=True)
        _check(task["weight"] <= 1000, "invalid_weight")
        _ids(task["blocker_ids"])
        _stamp(task["created_at"])
        _check(task["created_at"] <= now, "future_task")
        if task["deadline"] is not None:
            _stamp(task["deadline"])
            _check(task["deadline"] >= task["created_at"], "invalid_deadline")
        _check(all(key in blockers for key in task["blocker_ids"]), "unknown_blocker")
        task_dependencies[task["task_id"]] = set().union(*(ancestors[key] for key in task["blocker_ids"]))
    return tasks, blockers, questions, question_blockers, order, resolved, task_dependencies


def plan_questions(spec, now):
    """Rank answerable questions; overdue/old tasks get a transparent fair turn.

    Fairness tiers: overdue (2), waiting at least seven days (1), other (0).
    Within tiers use released work/minute, then fractional dependency progress,
    then fewest unresolved blockers, oldest task and stable question ID.
    Counts are distinct task and blocker IDs, never the sum of overlapping rows.
    """
    spec_hash = _digest(spec)
    tasks, blockers, questions, linked, order, resolved, task_deps = _validate(spec, now)
    ranked, deferred = [], []
    unresolved = set(blockers) - resolved
    active_tasks = {key for key, deps in task_deps.items() if deps - resolved}
    for qid in sorted(questions):
        question = questions[qid]
        targets = {item["blocker_id"] for item in linked[qid]} & unresolved
        if not targets:
            continue
        # A answer may cover a chain of same-fact blockers after prerequisites
        # resolve, but does not resolve dependencies belonging to other questions.
        after = set(resolved)
        for key in order:
            if key in targets and set(blockers[key]["depends_on"]) <= after:
                after.add(key)
        released_blockers = after - resolved
        affected = {key for key in active_tasks if task_deps[key] & released_blockers}
        if not released_blockers or not affected:
            deferred.append({"question_id": qid, "reason": "PREREQUISITE_BLOCKED" if not released_blockers
                             else "NO_ACTIVE_TASK", "unresolved_prerequisite_ids": sorted(set().union(
                                 *(set(blockers[key]["depends_on"]) - after for key in targets)))})
            continue
        unlocked = {key for key in affected if task_deps[key] <= after}
        minutes = question["estimated_minutes"]
        weight = sum(tasks[key]["weight"] for key in sorted(unlocked))
        progress = sum(tasks[key]["weight"] * len((task_deps[key] - resolved) & released_blockers)
                       / len(task_deps[key] - resolved) for key in sorted(affected))
        age = max(now - tasks[key]["created_at"] for key in affected)
        deadlines = [tasks[key]["deadline"] for key in affected if tasks[key]["deadline"] is not None]
        overdue = any(deadline <= now for deadline in deadlines)
        tier = 2 if overdue else 1 if age >= 7 * 86400 else 0
        row = {"question_id": qid, "kind": question["kind"], "prompt": question["prompt"],
               "scope_ids": sorted(question["scope_ids"]), "estimated_minutes": minutes,
               "reusable_fact": question["kind"] == "fact" and question["reuse_authorized"],
               "human_only": question["kind"] in HUMAN_ONLY,
               "resolvable_blocker_ids": sorted(released_blockers), "affected_task_ids": sorted(affected),
               "conditionally_unlocked_task_ids": sorted(unlocked), "conditionally_unlocked_weight": weight,
               "conditional_weight_per_minute": weight / minutes,
               "fractional_progress_per_minute": progress / minutes,
               "fewest_remaining_blockers": min(len(task_deps[key] - resolved) for key in affected),
               "oldest_task_age_seconds": age, "earliest_deadline": min(deadlines) if deadlines else None,
               "fairness_tier": tier, "answer_probability": None,
               "approval_reused": False, "execution_authorized": False}
        ranked.append(row)
    ranked.sort(key=lambda row: (-row["fairness_tier"], -row["conditional_weight_per_minute"],
                                 -row["fractional_progress_per_minute"], row["fewest_remaining_blockers"],
                                 -row["oldest_task_age_seconds"], row["question_id"]))
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
    return {"schema": "keel.loki.question_plan.v1", "input_sha256": spec_hash, "as_of": now,
            "policy": "deterministic_dependency_fairness_v1", "learned_policy": False,
            "ranking": ranked, "deferred": deferred,
            "unique_unresolved_blocker_count": len(unresolved), "unique_blocked_task_count": len(active_tasks),
            "unique_ranked_question_count": len(ranked), "conditional_on_usable_answers": True,
            "predicted_interviews": None, **FLAGS}


def check_fact_reuse(answer, *, scope_id, fact_key, now):
    """Check declared reuse metadata only; never authenticate an answer's truth."""
    _fields(answer, "answer_id kind fact_key source_sha256 value_sha256 scope_ids permitted_uses reuse_authorized observed_at expires_at")
    _id(answer["answer_id"])
    _id(answer["fact_key"])
    _id(scope_id)
    _id(fact_key)
    _stamp(now)
    _stamp(answer["observed_at"])
    _stamp(answer["expires_at"])
    for field in ("source_sha256", "value_sha256"):
        _check(type(answer[field]) is str and re.fullmatch(r"[0-9a-f]{64}", answer[field]), "invalid_sha256")
    _ids(answer["scope_ids"], nonempty=True)
    _check(type(answer["permitted_uses"]) is list and
           all(type(use) is str for use in answer["permitted_uses"]) and
           len(set(answer["permitted_uses"])) == len(answer["permitted_uses"]), "invalid_permitted_uses")
    _check(type(answer["reuse_authorized"]) is bool, "invalid_reuse_flag")
    _check(answer["expires_at"] > answer["observed_at"], "invalid_expiry")
    allowed = (answer["kind"] == "fact" and answer["reuse_authorized"] and
               scope_id in answer["scope_ids"] and fact_key == answer["fact_key"] and
               answer["permitted_uses"] == ["application_fact"] and
               answer["observed_at"] <= now < answer["expires_at"])
    return {"schema": "keel.loki.fact_reuse.v1", "status": "METADATA_COMPATIBLE" if allowed else "BLOCKED",
            "metadata_compatible": allowed, "answer_truth_verified": False,
            "source_bytes_verified": False, "approval_reusable": False, **FLAGS}


def evaluate_outcomes(log):
    """Report observed task completions per recorded human minute, not causality."""
    _check(type(log) is list and len(log) <= 10000, "invalid_log")
    event_ids, task_ids, interviews = set(), set(), set()
    minutes = 0.0
    for row in log:
        _fields(row, "event_id question_id human_minutes completed_task_ids interview_event_ids")
        _id(row["event_id"])
        _id(row["question_id"])
        _check(row["event_id"] not in event_ids, "duplicate_event")
        event_ids.add(row["event_id"])
        _number(row["human_minutes"])
        _check(row["human_minutes"] <= 1_000_000, "invalid_minutes")
        _ids(row["completed_task_ids"])
        _ids(row["interview_event_ids"])
        task_ids.update(row["completed_task_ids"])
        interviews.update(row["interview_event_ids"])
        minutes += row["human_minutes"]
    return {"schema": "keel.loki.question_outcomes.v1", "events": len(event_ids),
            "recorded_human_minutes": minutes, "unique_completed_tasks": len(task_ids),
            "tasks_per_recorded_human_minute": len(task_ids) / minutes if minutes else None,
            "reported_unique_interview_events": len(interviews),
            "interview_event_authenticity_verified": False, "causal_improvement_established": False,
            "missing_time_is_not_zero_cost": minutes == 0, **FLAGS}


def evaluate_policy(log, candidate_probabilities):
    """Offline IPS/SNIPS for externally supplied, logged finite-action policies.

    Requires the full logger distribution at every decision, full support for
    the candidate, and the logged selected action probability. No fitting,
    deployment, causal identification or confidence guarantee is performed.
    Rewards must be measured task utility in [0,1]; unobserved outcomes stay
    absent. A propensity is an asserted input, not proof of randomization.
    """
    _check(type(log) is list and 0 < len(log) <= 10000, "invalid_policy_log")
    _check(type(candidate_probabilities) is dict, "invalid_candidate")
    identifiers = set()
    contributions, weights = [], []
    for row in log:
        _fields(row, "decision_id selected_action logging_probabilities propensity reward outcome_observed")
        did = row["decision_id"]
        _id(did)
        _id(row["selected_action"])
        _check(did not in identifiers, "duplicate_decision")
        identifiers.add(did)
        _check(row["outcome_observed"] is True, "outcome_unobserved")
        _number(row["reward"])
        _check(row["reward"] <= 1, "reward_out_of_range")
        _check(did in candidate_probabilities, "candidate_missing_decision")
        logger = row["logging_probabilities"]
        candidate = candidate_probabilities[did]
        for distribution in (logger, candidate):
            _check(type(distribution) is dict and 0 < len(distribution) <= 2048, "invalid_distribution")
            for action, probability in distribution.items():
                _id(action)
                _number(probability)
                _check(probability <= 1, "invalid_probability")
            _check(math.isclose(sum(distribution.values()), 1.0, rel_tol=0, abs_tol=1e-9), "probabilities_not_normalized")
        _check(set(logger) == set(candidate), "action_space_mismatch")
        _check(all(logger[action] > 0 for action in candidate if candidate[action] > 0), "insufficient_policy_support")
        _check(row["selected_action"] in logger and logger[row["selected_action"]] > 0, "impossible_logged_action")
        _number(row["propensity"], positive=True)
        _check(row["propensity"] == logger[row["selected_action"]], "propensity_mismatch")
        weight = candidate[row["selected_action"]] / row["propensity"]
        _check(math.isfinite(weight), "unstable_weight")
        weights.append(weight)
        contributions.append(weight * row["reward"])
    _check(set(candidate_probabilities) == identifiers, "candidate_extra_decision")
    try:
        mass = math.fsum(weights)
        weighted_reward = math.fsum(contributions)
    except OverflowError:
        raise QuestionError("unstable_weight") from None
    _check(all(math.isfinite(value) for value in (mass, weighted_reward)), "unstable_weight")
    # ESS is scale-invariant. Squaring the unscaled total can overflow even
    # when both that total and sum(w*w) are finite; tiny weights can underflow.
    maximum_weight = max(weights)
    normalized = [weight / mass for weight in weights] if mass else []
    normalized_squares = math.fsum(weight * weight for weight in normalized)
    effective_sample_size = 1 / normalized_squares if normalized_squares else 0
    return {"schema": "keel.loki.offline_policy_estimate.v1", "status": "ESTIMATED" if mass else "NO_MATCHING_LOGGED_ACTIONS",
            "decision_count": len(log), "ips_mean": weighted_reward / len(log),
            "snips_mean": weighted_reward / mass if mass else None,
            "effective_sample_size": effective_sample_size,
            "maximum_importance_weight": maximum_weight, "full_declared_support": True,
            "logged_propensities_authenticated": False, "randomization_verified": False,
            "independent_outcomes_verified": False, "confidence_interval": None,
            "candidate_trained_on_log": None, "learned_policy": False,
            "deployment_recommended": False, "causal_improvement_established": False, **FLAGS}


def demo():
    now = 10 * 86400
    spec = {"schema": "keel.loki.question_input.v1", "tasks": [
        {"task_id": "synthetic-role-a", "weight": 2, "blocker_ids": ["fact-a"], "created_at": now - 60, "deadline": None},
        {"task_id": "synthetic-role-b", "weight": 1, "blocker_ids": ["fact-b", "approval-b"], "created_at": now - 60, "deadline": None}],
        "blockers": [
            {"blocker_id": "fact-a", "question_id": "synthetic-preference", "kind": "fact", "scope_id": "scope-a", "fact_key": "geography", "depends_on": [], "state": "unresolved"},
            {"blocker_id": "fact-b", "question_id": "synthetic-preference", "kind": "fact", "scope_id": "scope-b", "fact_key": "geography", "depends_on": [], "state": "unresolved"},
            {"blocker_id": "approval-b", "question_id": "synthetic-approval", "kind": "approval", "scope_id": "scope-b", "fact_key": None, "depends_on": ["fact-b"], "state": "unresolved"}],
        "questions": [
            {"question_id": "synthetic-preference", "kind": "fact", "scope_ids": ["scope-a", "scope-b"], "prompt": "Which locations are currently acceptable?", "estimated_minutes": 1, "reuse_authorized": True},
            {"question_id": "synthetic-approval", "kind": "approval", "scope_ids": ["scope-b"], "prompt": "Review this exact application separately when its packet is ready.", "estimated_minutes": 2, "reuse_authorized": False}]}
    report = plan_questions(spec, now)
    return {"schema": "keel.loki.questions_demo.v1", "synthetic": True, "plan": report,
            "shared_fact_question_ranked_first": report["ranking"][0]["question_id"] == "synthetic-preference",
            "individual_approval_deferred": any(row["question_id"] == "synthetic-approval" for row in report["deferred"]),
            **FLAGS}
