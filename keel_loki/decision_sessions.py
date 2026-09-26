"""Bounded, dependency-aware human decision sessions; never capabilities.

Exact subset optimization is deliberately small. Oversized inputs return an
explicit planning limit, rather than silently discarding questions or claiming
an approximate answer is optimal. All unlocks are conditional on usable human
answers and current host checks, including individual approvals.
"""
from __future__ import annotations

from fractions import Fraction
import itertools
import math

from .questions import (FLAGS, HUMAN_ONLY, QuestionError, _check, _digest,
                        _validate, plan_questions)


MAX_QUESTIONS = 16
MAX_ENUMERATION_WORK = 10_000_000


def _finite(value):
    if type(value) not in (int, float):
        raise ValueError("expected a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ValueError("expected a representable finite number") from None
    if not math.isfinite(result):
        raise ValueError("expected a finite number")
    return result


def best_question_bundle(questions, packets, max_questions=16, *,
                         minute_budget=None, required_question_ids=()):
    """Maximize unique packet value minus asking cost by exact enumeration.

Original cost/value semantics are retained. Optional ``minutes`` on questions
and ``minute_budget`` add an independent hard effort constraint; minutes and
value must never be conflated. Exact rational sums preserve displayed decimal budgets.
Required question IDs reserve a fair turn; equal objectives prefer lower cost,
then shorter time, fewer questions, and stable IDs. Limits cover actual work.
"""
    if type(max_questions) is not int or not 1 <= max_questions <= 20:
        raise ValueError("max_questions must be an integer from 1 to 20")
    if type(questions) not in (list, tuple) or not questions or len(questions) > max_questions:
        raise ValueError("supply 1 to %d questions" % max_questions)
    if type(packets) not in (list, tuple) or len(packets) > 10000:
        raise ValueError("supply a bounded packet collection")
    if (1 << len(questions)) * max(1, len(packets)) > MAX_ENUMERATION_WORK:
        raise ValueError("question/packet model exceeds exact enumeration budget")

    def identifiers(value):
        if type(value) not in (list, tuple, set, frozenset):
            raise ValueError("requirements must be a collection of ids")
        if any(type(item) is not str or not item.strip() for item in value):
            raise ValueError("requirement ids must be nonblank strings")
        return set(value)

    budget = None
    if minute_budget is not None:
        if _finite(minute_budget) <= 0:
            raise ValueError("minute_budget must be positive")
        budget = Fraction(str(minute_budget))
    qs, pks, seen = [], [], set()
    for q in questions:
        if type(q) is not dict:
            raise ValueError("question must be an object")
        qid = q.get("id")
        if type(qid) is not str or not qid or qid in seen:
            raise ValueError("question ids must be unique nonempty strings")
        seen.add(qid)
        cost = _finite(q.get("cost", 0))
        minutes = q.get("minutes", 0)
        if cost < 0 or _finite(minutes) < 0:
            raise ValueError("asking cost and minutes cannot be negative")
        if budget is not None and _finite(minutes) <= 0:
            raise ValueError("budgeted questions need positive minutes")
        qs.append((qid, cost, identifiers(q.get("resolves", ())), Fraction(str(minutes))))
    required = identifiers(required_question_ids)
    if not required <= seen:
        raise ValueError("unknown required question")
    seen_packets = set()
    for p in packets:
        if type(p) is not dict:
            raise ValueError("packet must be an object")
        pid = p.get("id")
        if type(pid) is not str or not pid or pid in seen_packets:
            raise ValueError("packet ids must be unique nonempty strings")
        seen_packets.add(pid)
        value = _finite(p.get("value", 0))
        if value < 0:
            raise ValueError("unlock value cannot be negative")
        pks.append((pid, value, identifiers(p.get("needs", ()))))
    try:
        _finite(math.fsum(q[1] for q in qs))
        _finite(math.fsum(p[1] for p in pks))
        _finite(float(sum((q[3] for q in qs), Fraction(0))))
    except (OverflowError, ValueError):
        raise ValueError("aggregate cost, value or minutes exceeds finite range") from None
    # Stable order makes equal-value decisions independent of adapter row order.
    qs.sort(key=lambda q: q[0])
    pks.sort(key=lambda p: p[0])
    best = None
    for bits in itertools.product((False, True), repeat=len(qs)):
        subset = [q for q, on in zip(qs, bits) if on]
        selected = [q[0] for q in subset]
        if not required <= set(selected):
            continue
        minutes = sum((q[3] for q in subset), Fraction(0))
        if budget is not None and minutes > budget:
            continue
        resolved = set().union(*(q[2] for q in subset)) if subset else set()
        unlocked_ids = [pid for pid, _, needs in pks if needs and needs <= resolved]
        cost = math.fsum(q[1] for q in subset)
        unlocked = math.fsum(v for _, v, needs in pks if needs and needs <= resolved)
        objective = unlocked - cost
        rank = (objective, -cost, -minutes, -len(subset))
        if best is None or rank > best[0] or (rank == best[0] and selected < best[1]["selected"]):
            best = (rank, {"selected": selected, "cost": cost, "estimated_minutes": float(minutes),
                           "unlocked_packet_ids": unlocked_ids, "unlocked_value": unlocked,
                           "objective": objective, "status": "optimal_for_supplied_toy_model"})
    if best is None:
        raise ValueError("required questions exceed minute budget")
    return best[1]


def plan_decision_session(spec, now, *, minute_budget):
    """Plan a bounded session from the same spec used by ``plan_questions``.

An affordable overdue/old question gets the first reserved turn. The remaining
budget maximizes complete task unlock weight, then minimizes effort. If no task
can unlock, one ranked question may make dependency progress. No answer truth,
approval, successful completion, or observed human effort is inferred.
"""
    try:
        budget = _finite(minute_budget)
    except ValueError:
        raise QuestionError("invalid_session_minutes") from None
    _check(0 < budget <= 1440, "invalid_session_minutes")
    tasks, blockers, questions, linked, order, resolved, task_deps = _validate(spec, now)
    ranking = plan_questions(spec, now)
    active = {key: deps - resolved for key, deps in task_deps.items() if deps - resolved}
    relevant = set().union(*active.values()) if active else set()
    candidate_ids = sorted({blockers[key]["question_id"] for key in relevant
                            if blockers[key]["question_id"] is not None})
    report = {"schema": "keel.loki.decision-session.v1", "input_sha256": _digest(spec), "as_of": now,
              "minute_budget": budget, "estimated_minutes": 0.0, "actual_human_minutes": None,
              "status": "NO_PENDING_QUESTIONS", "selected_question_ids": [], "questions": [],
              "conditionally_unlocked_task_ids": [], "conditionally_unlocked_weight": 0,
              "remaining_blocked_task_ids": sorted(active), "fairness_reserved_question_id": None,
              "candidate_question_count": len(candidate_ids), "max_questions": MAX_QUESTIONS,
              "max_enumeration_work": MAX_ENUMERATION_WORK,
              "conditional_on_usable_answers": True, "all_authorizations_cleared": False,
              "requires_fresh_projection_after_each_response": True, "optimal_for": None, **FLAGS}
    if not candidate_ids:
        return report
    if len(candidate_ids) > MAX_QUESTIONS or (1 << len(candidate_ids)) * max(1, len(active)) > MAX_ENUMERATION_WORK:
        return dict(report, status="SESSION_TOO_LARGE",
                    next_step="Provide a narrower review snapshot; no questions were silently discarded.")
    affordable = [row for row in ranking["ranking"] if row["estimated_minutes"] <= budget]
    reserved = next((row["question_id"] for row in affordable if row["fairness_tier"] > 0), None)
    # The transitive closure includes every prerequisite. System blockers retain
    # their IDs but no question resolves them; they cannot produce a false unlock.
    packets = [{"id": tid, "value": tasks[tid]["weight"], "needs": list(deps)}
               for tid, deps in sorted(active.items())]
    model_questions = [{"id": qid, "cost": 0, "minutes": questions[qid]["estimated_minutes"],
                        "resolves": [b["blocker_id"] for b in linked[qid]]} for qid in candidate_ids]
    result = best_question_bundle(model_questions, packets, minute_budget=minute_budget,
                                  required_question_ids=[reserved] if reserved else [])
    selected = set(result["selected"])
    status = "PLANNED"
    if not selected and affordable:
        selected = {affordable[0]["question_id"]}
        status = "PROGRESS_ONLY"
    if not selected:
        return dict(report, status="NO_AFFORDABLE_ACTIONABLE_QUESTION")
    # Real dependencies determine response order. Rows with prerequisites still
    # unresolved are conditional followups and require a refreshed host review.
    after, sequence, answered = set(resolved), [], set()
    priority = {row["question_id"]: index for index, row in enumerate(ranking["ranking"])}
    while selected - answered:
        ready = [qid for qid in selected - answered if any(
            b["blocker_id"] not in after and set(b["depends_on"]) <= after for b in linked[qid])]
        if not ready:
            # No claim of progress through an unresolved system prerequisite.
            sequence.extend(sorted(selected - answered))
            break
        qid = min(ready, key=lambda q: (q != reserved, priority.get(q, len(questions)), q))
        sequence.append(qid)
        answered.add(qid)
        for key in order:
            if blockers[key]["question_id"] in answered and set(blockers[key]["depends_on"]) <= after:
                after.add(key)
    # The selected solver model is equivalent for complete task unlocks, but
    # this independent DAG walk avoids claiming blocked partial dependencies.
    unlocked = sorted(tid for tid, deps in active.items() if deps <= after)
    if not unlocked:
        status = "PROGRESS_ONLY"
    rows = []
    for qid in sequence:
        question = questions[qid]
        prerequisites = set().union(*(set(b["depends_on"]) - resolved for b in linked[qid]))
        rows.append(dict(question, question_sha256=_digest(question),
                         human_only=question["kind"] in HUMAN_ONLY,
                         reusable_fact=question["kind"] == "fact" and question["reuse_authorized"],
                         conditional_followup=bool(prerequisites),
                         unresolved_prerequisite_ids=sorted(prerequisites),
                         approval_reused=False, execution_authorized=False))
    minutes = float(sum((Fraction(str(questions[qid]["estimated_minutes"])) for qid in selected), Fraction(0)))
    return dict(report, status=status, selected_question_ids=sequence, questions=rows,
                estimated_minutes=minutes, conditionally_unlocked_task_ids=unlocked,
                conditionally_unlocked_weight=sum(tasks[tid]["weight"] for tid in unlocked),
                remaining_blocked_task_ids=sorted(set(active) - set(unlocked)),
                fairness_reserved_question_id=reserved,
                optimal_for="supplied complete-task weight, hard minute budget, and reserved fairness turn"
                if status == "PLANNED" else None)
