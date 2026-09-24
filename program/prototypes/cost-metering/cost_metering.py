"""Attempt-level dollar metering (prototype, G-5 / PP-11).

PROTOTYPE ONLY — never wired to production, never a pricing claim.
Pipeline Performance owns the real rate card, the T3 evidence bar, and G1.

Model
-----
Every attempt carries cost attribution against a STABLE attempt ID (F22
identity: one durable attempt across transports). Retries across transports
(api -> browser failover) are SEPARATE attempts (each costs its own units)
grouped under one completion_id; the completion itself is counted ONCE.

Units metered per attempt:
  browser_seconds      — time spent in the browser lane
  network_calls        — HTTP calls issued (verification, ATS APIs, discovery)
  failed_attempts      — failed sub-attempts inside this attempt (0 or 1 typical)
  human_taps           — human recovery actions attributed to this attempt
  infra_share_units    — share of shared infra (scheduler, storage, telemetry)

Published figures are DOLLARS. Composite internal units may be instrumented
alongside, but the publication path accepts only dollars.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Rate card — PLACEHOLDER RATES. These are not real prices, not vendor quotes,
# not a pricing proposal. They exist so the metering pipeline is testable
# before Pipeline Performance supplies audited real rates.
# ---------------------------------------------------------------------------
RATE_CARD: Dict[str, float] = {
    "browser_seconds": 0.00040,     # PLACEHOLDER $/browser-second
    "network_calls": 0.00080,       # PLACEHOLDER $/HTTP call
    "failed_attempts": 0.02000,    # PLACEHOLDER $/failed sub-attempt
    "human_taps": 0.15000,         # PLACEHOLDER $/human recovery tap
    "infra_share_units": 0.01000,  # PLACEHOLDER $/infra share unit
}

RATE_CARD_VERSION = "placeholder-v0"

# Documented default tolerance band for known-cost replay (relative).
DEFAULT_TOLERANCE = 0.005  # +/-0.5% of the audited total

METHOD_NOTE = (
    "method: metered_total = sum over attempts of "
    "sum(units * RATE_CARD[unit]) using RATE_CARD placeholder-v0; "
    "audited_total is computed independently (Decimal) from the same unit "
    "observations; within_tolerance = abs(metered - audited) <= "
    "tolerance * audited. Tolerance covers float accumulation and per-attempt "
    "rounding only — not model error in the rate card."
)


class EvidenceBarNotMet(Exception):
    """Raised when publication is attempted before the T3 evidence bar."""


@dataclass(frozen=True)
class AttemptCost:
    """Cost attribution for ONE attempt, keyed by its stable attempt ID."""

    attempt_id: str
    browser_seconds: float = 0.0
    network_calls: int = 0
    failed_attempts: int = 0
    human_taps: int = 0
    infra_share_units: float = 0.0
    completion_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("attempt_id is required (F22 stable identity)")
        for name in (
            "browser_seconds",
            "network_calls",
            "failed_attempts",
            "human_taps",
            "infra_share_units",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")


def cost_of_attempt(attempt: AttemptCost) -> float:
    """Dollars for one attempt: units x placeholder rate card."""
    return (
        attempt.browser_seconds * RATE_CARD["browser_seconds"]
        + attempt.network_calls * RATE_CARD["network_calls"]
        + attempt.failed_attempts * RATE_CARD["failed_attempts"]
        + attempt.human_taps * RATE_CARD["human_taps"]
        + attempt.infra_share_units * RATE_CARD["infra_share_units"]
    )


def cost_of_completion(
    attempts: Sequence[AttemptCost], completion_id: str
) -> Dict[str, object]:
    """Dollars for one completion across transport failover.

    Retries across transports (api attempt + browser attempt) cost their
    attempts SEPARATELY, but the completion is counted ONCE.
    """
    group = [a for a in attempts if a.completion_id == completion_id]
    if not group:
        raise ValueError(f"no attempts recorded for completion_id {completion_id!r}")
    attempt_ids = {a.attempt_id for a in group}
    if len(attempt_ids) != len(group):
        raise ValueError(
            f"duplicate attempt_id inside completion {completion_id!r}: "
            "F22 identity violated — each attempt must be unique"
        )
    return {
        "completion_id": completion_id,
        "dollars": sum(cost_of_attempt(a) for a in group),
        "attempt_count": len(group),
        "completion_count": 1,  # counted once, however many transports it took
    }


def replay(
    run_costs: Sequence[AttemptCost],
    audited_total: float,
    tolerance: float = DEFAULT_TOLERANCE,
) -> Tuple[float, bool, str]:
    """Known-cost replay: meter a fabricated run against an audited total.

    Returns (metered_total, within_tolerance, method_note). The tolerance and
    method travel with the numbers — a metered figure without them is not
    publishable.
    """
    if tolerance < 0:
        raise ValueError("tolerance cannot be negative")
    metered_total = sum(cost_of_attempt(a) for a in run_costs)
    if audited_total == 0:
        within = metered_total == 0
    else:
        within = abs(metered_total - audited_total) <= tolerance * abs(audited_total)
    return metered_total, within, METHOD_NOTE


def publish_figure(
    *,
    total_dollars: float,
    verified_eligible_completions: int,
    total_attempts: int,
    definition: str,
    maturity_window: str,
    exclusions: Sequence[str],
    method: str,
    tolerance: float,
    evidence_bar_met: bool = False,
) -> Dict[str, object]:
    """Publication format: dollars per verified eligible completion.

    REFUSES unless evidence_bar_met=True (the T3 evidence bar, plus G1
    privacy-counsel sign-off, both owned by Pipeline Performance).
    """
    if not evidence_bar_met:
        raise EvidenceBarNotMet(
            "publication refused: T3 evidence bar not met (and G1 privacy-counsel "
            "sign-off required). No price/value claim may leave the workspace."
        )
    if verified_eligible_completions <= 0:
        raise ValueError("verified_eligible_completions must be positive")
    if total_dollars < 0:
        raise ValueError("total_dollars cannot be negative")
    return {
        "dollars_per_verified_eligible_completion": (
            total_dollars / verified_eligible_completions
        ),
        "definition": definition,
        "denominator": {
            "total_attempts": total_attempts,
            "verified_eligible_completions": verified_eligible_completions,
        },
        "maturity_window": maturity_window,
        "exclusions": list(exclusions),
        "method": method,
        "tolerance": tolerance,
    }


def dollars_per_completion(
    completion_costs: Sequence[Dict[str, object]],
    verified_eligible_count: int,
) -> float:
    """Helper: total completion dollars / verified eligible completions."""
    if verified_eligible_count <= 0:
        raise ValueError("verified_eligible_count must be positive")
    total = sum(float(c["dollars"]) for c in completion_costs)
    return total / verified_eligible_count
