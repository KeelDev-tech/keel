#!/usr/bin/env python3
"""Keel mathematical core — stdlib-only, tested primitives adopted from the
2026-09-18 advanced-mathematics research (Keel_Advanced_Mathematics_Research)
and its companion lab (Keel_Math_Lab_2_c0s0.py).

Adopted because each earned its place:
- eligibility_confirmed: fail-closed permission-flag check (identity, not
  truthiness). A string "true" leaking out of a log/fixture must NEVER read
  as authorization.
- empirical_cvar: atom-correct CVaR for finite scenario losses (Rockafellar-
  Uryasev optimization form). Used by risk-aware planning; a property of the
  supplied scenarios, not a population guarantee.
- require_aligned: scenario-column alignment enforcement. Correlated-failure
  reasoning is garbage unless scenario vectors line up.
- invalidate: dependency invalidation WITH alternative derivations. Unlike the
  lab's all-parents-necessary closure, a conclusion survives when ANY one of
  its derivations is fully grounded. Ungrounded cycles can never self-ground.
- conformal_radius: split-conformal absolute-residual quantile with the n+1
  finite-sample correction. Returns None (unbounded) on insufficient data
  instead of clamping into false precision.
- best_question_bundle: exact small-subset VOI-lite for tray triage. Picks the
  question set maximizing expected packet unlock value minus asking cost.
  Complementary requirements (A and B both needed) are handled exactly by
  enumeration, not by a greedy heuristic with no guarantee.

No network, no credentials, no persistence, no execution. Pure functions.
An eligible=True fixture is NOT evidence of permission or factual correctness.
"""

import math


# --------------------------------------------------------------------------
# Strict numeric validation
# --------------------------------------------------------------------------

def finite(value):
    """Return float(value); reject bools, NaN, infinities, non-numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a finite number, got %r" % (value,))
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError("expected a representable finite number") from None
    if not math.isfinite(result):
        raise ValueError("expected a finite number")
    return result


# --------------------------------------------------------------------------
# Fail-closed eligibility
# --------------------------------------------------------------------------

def eligibility_confirmed(flag):
    """True only when flag is the boolean True — identity, not truthiness.

    Permission/authorization flags arriving from fixtures, logs, JSON blobs
    or hand-transcribed rows must never be read as granted on a truthy
    string ("true"), 1, or any other non-True value. Fail closed.
    """
    return flag is True


# --------------------------------------------------------------------------
# CVaR for finite scenario losses
# --------------------------------------------------------------------------

def empirical_cvar(losses, beta=0.9):
    """Exact CVaR for an equally weighted finite loss distribution.

    min_eta eta + mean(max(loss - eta, 0)) / (1 - beta).
    Handles probability atoms at the quantile correctly (fractional tail
    mass); does NOT naively average observations above an empirical quantile.
    """
    values = [finite(v) for v in losses]
    beta = finite(beta)
    if not values or not 0 <= beta < 1:
        raise ValueError("need losses and 0 <= beta < 1")
    # Integrate the upper empirical tail directly. Enumerating every eta was
    # quadratic and v - eta could overflow even when the correct answer was
    # finite (for example [-1e308, 1e308] at beta=0 has mean zero).
    values.sort(reverse=True)
    tail = len(values) * (1 - beta)
    whole = math.floor(tail)
    fractional = tail - whole
    weights = [1 / tail] * whole
    if fractional:
        weights.append(fractional / tail)
    selected = values[:len(weights)]
    try:
        result = math.fsum(value * weight for value, weight in zip(selected, weights))
    except OverflowError:
        # Roundoff at the largest finite float may overflow a convex sum.
        scale = max(abs(value) for value in selected)
        normalized = math.fsum((value / scale) * weight
                               for value, weight in zip(selected, weights))
        result = max(-1.0, min(1.0, normalized)) * scale
    return max(min(selected), min(max(selected), result))


# --------------------------------------------------------------------------
# Scenario alignment
# --------------------------------------------------------------------------

def require_aligned(vectors):
    """Validate that scenario vectors share one length; return that length.

    Correlated-scenario reasoning (CVaR plans, joint outage models) is only
    meaningful when column s means the same scenario for every task.
    Misaligned input is rejected, never silently broadcast.
    """
    vecs = [tuple(finite(v) for v in vec) for vec in vectors]
    if not vecs or any(len(v) == 0 for v in vecs):
        raise ValueError("need at least one non-empty scenario vector")
    sizes = {len(v) for v in vecs}
    if len(sizes) != 1:
        raise ValueError("scenario vectors must align: got lengths %s"
                         % sorted(sizes))
    return sizes.pop()


# --------------------------------------------------------------------------
# Dependency invalidation with alternative derivations
# --------------------------------------------------------------------------

def _normalize_derivations(dependencies):
    """Accept {node: [parents]} (all necessary) or {node: [[p..],[p..],..]}
    (alternative derivations). Returns {node: [frozenset, ...]} where the node
    is valid iff at least one derivation's premises are all valid."""
    if type(dependencies) is not dict:
        raise ValueError("dependencies must be an object")
    norm = {}
    for node, deps in dependencies.items():
        if not isinstance(node, str) or not node:
            raise ValueError("node ids must be nonempty strings")
        if type(deps) not in (list, tuple, set, frozenset):
            raise ValueError("parents must be a collection of ids")
        deps = list(deps)
        if deps and all(isinstance(d, str) for d in deps):
            norm[node] = [frozenset(deps)]          # single necessary set
        else:
            derivs = []
            for d in deps:
                if type(d) not in (list, tuple, set, frozenset):
                    raise ValueError("mixed derivation formats for %r" % node)
                derivs.append(frozenset(d))
            norm[node] = derivs
    # every mentioned premise must be a known node (no dangling deps)
    known = set(norm)
    for node, derivs in norm.items():
        for d in derivs:
            for p in d:
                if not isinstance(p, str) or not p:
                    raise ValueError("premise ids must be nonempty strings")
                if p not in known:
                    raise ValueError("dangling dependency %r of %r" % (p, node))
    return norm


def invalidate(dependencies, revoked):
    """Nodes invalidated after revoking `revoked`.

    Validity is computed bottom-up (least fixpoint) from ground nodes: a node
    is valid iff it is not revoked AND at least one derivation has every
    premise valid. Consequences:
    - Withdrawing one premise invalidates dependents that have no surviving
      alternative derivation; dependents with another grounded derivation
      stay valid (no overinvalidation).
    - Cycles can never establish their own factual support: a cycle with no
      grounded derivation is invalid even when nothing was revoked.
    Returns the sorted list of invalid node ids.
    """
    norm = _normalize_derivations(dependencies)
    if type(revoked) not in (list, tuple, set, frozenset):
        raise ValueError("revoked must be a collection of ids")
    if any(type(node) is not str or not node for node in revoked):
        raise ValueError("revoked ids must be nonempty strings")
    revoked = set(revoked)
    unknown = revoked - set(norm)
    if unknown:
        raise ValueError("unknown revoked nodes: %s" % sorted(unknown))
    valid = set()
    # ground nodes: no derivations listed (axiomatic) or an empty derivation
    for node, derivs in norm.items():
        if node not in revoked and (not derivs or any(len(d) == 0 for d in derivs)):
            valid.add(node)
    changed = True
    while changed:
        changed = False
        for node, derivs in norm.items():
            if node in valid or node in revoked:
                continue
            if any(d and d <= valid for d in derivs):
                valid.add(node)
                changed = True
    return sorted(set(norm) - valid)


# --------------------------------------------------------------------------
# Split-conformal radius
# --------------------------------------------------------------------------

def conformal_radius(residuals, alpha=0.1):
    """Split-conformal absolute-residual quantile with the n+1 correction.

    rank = ceil((n+1)(1-alpha)); returns the rank-th order statistic, or None
    when the finite-sample rule needs an unbounded interval (insufficient
    calibration data). Never clamps into false precision.
    Coverage is marginal under exchangeability, not a per-item probability.
    """
    alpha = finite(alpha)
    values = sorted(finite(v) for v in residuals)
    if not 0 < alpha < 1 or not values or any(v < 0 for v in values):
        raise ValueError("need nonnegative residuals and 0 < alpha < 1")
    rank = math.ceil((len(values) + 1) * (1 - alpha))
    return values[rank - 1] if rank <= len(values) else None


# --------------------------------------------------------------------------
# Question-bundle triage (VOI-lite, exact for small sets)
# --------------------------------------------------------------------------

def best_question_bundle(questions, packets, max_questions=16):
    """Compatibility entry point for the bounded reusable question optimizer.

    This API retains its original inputs and result fields. Operational decision
    sessions use the same solver with a separate hard human-minute budget.
    """
    from keel_loki.decision_sessions import best_question_bundle as solve
    result = solve(questions, packets, max_questions=max_questions)
    return {key: result[key] for key in
            ("selected", "cost", "unlocked_value", "objective", "status")}
