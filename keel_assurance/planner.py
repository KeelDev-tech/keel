"""Bounded exact reviewer-subset planning, with no dispatch or authorization.

Enumerates every subset of at most 16 available registry entries. A feasible
subset covers every required capability, fits the declared cost and parallel
latency limits, and contains a pair differing simultaneously in family, method
and independence group. Marginal diversity alone is insufficient.

The objective is declared total cost, then declared parallel latency, then the
lexicographically sorted reviewer-ID tuple. The selected subset is optimal
only among these enumerated candidates under this objective and these supplied
constraints, not optimal in quality, true independence, or actual expense.
Registry metadata does not establish independence or measured performance.
Parallel latency assumes unconstrained simultaneous dispatch and excludes
queueing, coordination, retries and other overhead. No workers are dispatched.
"""
from __future__ import annotations

import unicodedata

MAX_REVIEWERS = 16
MAX_CAPABILITIES = 32
MAX_TEXT_LENGTH = 128
MAX_SAFE_INTEGER = (1 << 53) - 1
_FIELDS = {"reviewer_id", "independence_group", "family", "method", "capabilities",
           "available", "cost_units", "latency_ms"}


class PlannerError(ValueError):
    """Invalid registry or planning constraint, without silent coercion."""


def _text(value, name):
    if (type(value) is not str or not value or value != value.strip()
            or len(value) > MAX_TEXT_LENGTH
            or any(unicodedata.category(c).startswith("C") for c in value)):
        raise PlannerError(f"{name}: bounded nonempty string without control characters required")
    return value


def _integer(value, name):
    if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
        raise PlannerError(f"{name}: nonnegative JSON-safe integer required")
    return value


def _strings(value, name):
    if type(value) not in (list, tuple) or len(value) > MAX_CAPABILITIES:
        raise PlannerError(f"{name}: list or tuple of at most {MAX_CAPABILITIES} strings required")
    result = [_text(item, name) for item in value]
    if len(set(result)) != len(result):
        raise PlannerError(f"{name}: duplicate values are not permitted")
    return result


def _diversity_label(value):
    # Labels are still assertions, but cosmetic case/spacing/Unicode variants
    # must not make two identical assertions look independently sourced.
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def select_reviewers(reviewers, *, required_capabilities, budget_units, latency_limit_ms):
    """Return a deterministic plan or explicit INFEASIBLE, never approval.

    Every reviewer has exactly these fields:
      reviewer_id, independence_group, family, method: bounded strings;
      capabilities: <=32 distinct strings; available: an actual boolean;
      cost_units, latency_ms: nonnegative JSON-safe integers.
    The registry is a list/tuple of <=16 entries with unique reviewer IDs.
    All entries, even unavailable ones, are validated. Capability matching is
    exact and case-sensitive; diversity labels are NFKC/case/space normalized.
    Empty required capabilities still require a genuinely diverse declared
    pair. Empty/unavailable registries therefore never produce a feasible plan.

    All 2**available_count subsets, including empty and infeasible subsets,
    count toward combinations_examined. On INFEASIBLE, selected_reviewer_ids
    is [] and cost_units/parallel_latency_ms are null rather than invented zero.
    Zero-cost reviewers can satisfy a zero budget without skipping any gate.
    """
    if type(reviewers) not in (list, tuple) or len(reviewers) > MAX_REVIEWERS:
        raise PlannerError(f"reviewers: list or tuple with at most {MAX_REVIEWERS} entries required")
    required = _strings(required_capabilities, "required_capabilities")
    budget = _integer(budget_units, "budget_units")
    latency_limit = _integer(latency_limit_ms, "latency_limit_ms")
    seen, available = set(), []
    for row in reviewers:
        if type(row) is not dict or row.keys() != _FIELDS:
            raise PlannerError("reviewer: exact registry fields required")
        identity = _text(row["reviewer_id"], "reviewer_id")
        if identity in seen:
            raise PlannerError(f"duplicate reviewer_id {identity!r}")
        seen.add(identity)
        dimensions = tuple(_diversity_label(_text(row[key], key))
                           for key in ("independence_group", "family", "method"))
        capabilities = set(_strings(row["capabilities"], "capabilities"))
        if type(row["available"]) is not bool:
            raise PlannerError("available: actual boolean required")
        cost = _integer(row["cost_units"], "cost_units")
        latency = _integer(row["latency_ms"], "latency_ms")
        if row["available"]:
            available.append((identity, dimensions, capabilities, cost, latency))
    available.sort(key=lambda row: row[0])
    n = len(available)
    required_set = set(required)
    # Each mask represents a pair diverse in ALL dimensions at once.
    diverse_pairs = [(1 << first) | (1 << second)
                     for first in range(n) for second in range(first + 1, n)
                     if all(a != b for a, b in zip(available[first][1], available[second][1]))]
    best, feasible_count = None, 0
    for mask in range(1 << n):
        if not any(mask & pair == pair for pair in diverse_pairs):
            continue
        chosen = [available[index] for index in range(n) if mask & (1 << index)]
        cost = sum(row[3] for row in chosen)
        latency = max(row[4] for row in chosen)
        if cost > budget or latency > latency_limit:
            continue
        covered = set().union(*(row[2] for row in chosen))
        if not required_set <= covered:
            continue
        feasible_count += 1
        candidate = (cost, latency, tuple(row[0] for row in chosen))
        if best is None or candidate < best:
            best = candidate
    return {
        "schema_version": 1,
        "method": "bounded-exact-reviewer-subsets.v1",
        "status": "FEASIBLE" if best is not None else "INFEASIBLE",
        "selected_reviewer_ids": list(best[2]) if best is not None else [],
        "cost_units": best[0] if best is not None else None,
        "parallel_latency_ms": best[1] if best is not None else None,
        "combinations_examined": 1 << n,
        "feasible_subsets": feasible_count,
        "registered_count": len(reviewers), "available_count": n,
        "required_capabilities": sorted(required),
        "budget_units": budget, "latency_limit_ms": latency_limit,
        "execution_authorized": False,
        "selection_basis": ["minimum declared total cost", "minimum declared parallel latency",
                            "lexicographically sorted reviewer IDs"],
        "limitations": [
            "Metadata does not prove reviewer independence, capabilities, availability or actual costs.",
            "Parallel latency is max declared latency; scheduling, queueing and coordination are not modeled.",
            "Optimality is limited to enumerated candidate subsets and the stated objective, not quality.",
            "A feasible plan does not dispatch workers, confer authority, or satisfy downstream review gates.",
        ],
    }
