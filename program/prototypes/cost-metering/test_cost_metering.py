"""Tests for cost_metering.py (prototype, G-5 / PP-11).

Covers the PP-11 acceptance scenarios:
 1. known-cost replay within tolerance on 3 consecutive fabricated runs
    (audited totals computed INDEPENDENTLY via Decimal from the same unit
    observations — the meter uses floats, so tolerance is exercised honestly)
 2. no-double-count on api->browser transport failover: two attempts cost
    separately, the completion counts ONCE
 3. tampered run OUTSIDE tolerance is flagged (the band discriminates)
 4. publish refused when the evidence bar is unmet
 5. published figure carries definition / denominator / exclusions
"""

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cost_metering import (  # noqa: E402
    AttemptCost,
    EvidenceBarNotMet,
    RATE_CARD,
    cost_of_attempt,
    cost_of_completion,
    dollars_per_completion,
    publish_figure,
    replay,
)


def audited(units: dict) -> float:
    """Independent audit: Decimal arithmetic from the same unit observations."""
    total = Decimal("0")
    for unit, qty in units.items():
        total += Decimal(str(RATE_CARD[unit])) * Decimal(str(qty))
    return float(total)


def make_attempt(attempt_id, completion_id, **units):
    full = dict(
        browser_seconds=0.0,
        network_calls=0,
        failed_attempts=0,
        human_taps=0,
        infra_share_units=0.0,
    )
    full.update(units)
    return AttemptCost(attempt_id=attempt_id, completion_id=completion_id, **full)


def test_known_cost_replay_three_consecutive_runs():
    runs = [
        # run 1: two clean api attempts
        [
            make_attempt("r1-a1", "c1", browser_seconds=12.5, network_calls=30,
                         infra_share_units=1.0),
            make_attempt("r1-a2", "c2", browser_seconds=8.0, network_calls=22,
                         infra_share_units=1.0),
        ],
        # run 2: failover + a human tap + a failed sub-attempt
        [
            make_attempt("r2-a1", "c3", browser_seconds=45.0, network_calls=60,
                         failed_attempts=1, infra_share_units=1.0),
            make_attempt("r2-a2", "c3", browser_seconds=90.0, network_calls=41,
                         human_taps=1, infra_share_units=1.0),
            make_attempt("r2-a3", "c4", browser_seconds=5.0, network_calls=12,
                         infra_share_units=0.5),
        ],
        # run 3: dry run — zero activity meters to exactly zero
        [
            make_attempt("r3-a1", "c5"),
        ],
    ]
    for i, attempts in enumerate(runs, start=1):
        audited_total = sum(
            audited({
                "browser_seconds": a.browser_seconds,
                "network_calls": a.network_calls,
                "failed_attempts": a.failed_attempts,
                "human_taps": a.human_taps,
                "infra_share_units": a.infra_share_units,
            })
            for a in attempts
        )
        metered, within, method = replay(attempts, audited_total, tolerance=0.005)
        assert within, f"run {i}: metered={metered} audited={audited_total}"
        assert method and "tolerance" in method.lower()
    print("ok test_known_cost_replay_three_consecutive_runs")


def test_failover_costs_attempts_separately_counts_completion_once():
    api = make_attempt("att-api-1", "c-fail", browser_seconds=40.0,
                       network_calls=55, failed_attempts=1, infra_share_units=1.0)
    browser = make_attempt("att-brw-1", "c-fail", browser_seconds=120.0,
                           network_calls=30, human_taps=1, infra_share_units=1.0)
    result = cost_of_completion([api, browser], "c-fail")
    expected = cost_of_attempt(api) + cost_of_attempt(browser)
    assert abs(result["dollars"] - expected) < 1e-12
    assert result["attempt_count"] == 2, "both transport attempts cost"
    assert result["completion_count"] == 1, "completion counted ONCE"
    assert result["dollars"] > cost_of_attempt(api), "failover is not free"
    print("ok test_failover_costs_attempts_separately_counts_completion_once")


def test_tampered_run_outside_tolerance_flagged():
    attempts = [
        make_attempt("t-a1", "c1", browser_seconds=60.0, network_calls=40,
                     infra_share_units=1.0),
    ]
    honest_audit = audited({
        "browser_seconds": 60.0, "network_calls": 40, "failed_attempts": 0,
        "human_taps": 0, "infra_share_units": 1.0,
    })
    tampered_audit = honest_audit * 0.90  # someone shaved 10% off the books
    metered, within, _ = replay(attempts, tampered_audit, tolerance=0.005)
    assert not within, f"tampered audit accepted: metered={metered}"
    print("ok test_tampered_run_outside_tolerance_flagged")


def test_publish_refused_when_evidence_bar_unmet():
    try:
        publish_figure(
            total_dollars=12.40,
            verified_eligible_completions=8,
            total_attempts=11,
            definition="verified eligible completion: submission confirmed by provider-correlated receipt, lead passed all READY gates",
            maturity_window="60 days",
            exclusions=["Trent's own time", "mailbox-derived outcome inference"],
            method="placeholder",
            tolerance=0.005,
            evidence_bar_met=False,
        )
    except EvidenceBarNotMet:
        print("ok test_publish_refused_when_evidence_bar_unmet")
        return
    raise AssertionError("publish_figure did not refuse without the evidence bar")


def test_publish_refused_by_default():
    try:
        publish_figure(
            total_dollars=1.0, verified_eligible_completions=1, total_attempts=1,
            definition="x", maturity_window="60 days", exclusions=[],
            method="m", tolerance=0.005,
        )
    except EvidenceBarNotMet:
        print("ok test_publish_refused_by_default")
        return
    raise AssertionError("default evidence_bar_met must be False")


def test_published_figure_carries_definition_denominator_exclusions():
    fig = publish_figure(
        total_dollars=12.40,
        verified_eligible_completions=8,
        total_attempts=11,
        definition="verified eligible completion: provider-correlated receipt + all READY gates passed",
        maturity_window="60 days",
        exclusions=["Trent's own time", "third-party mailbox inference", "sunk infra build cost"],
        method="metered sum of attempt costs, placeholder-v0 rate card",
        tolerance=0.005,
        evidence_bar_met=True,
    )
    assert fig["dollars_per_verified_eligible_completion"] == 12.40 / 8
    assert "provider-correlated receipt" in fig["definition"]
    assert fig["denominator"] == {"total_attempts": 11, "verified_eligible_completions": 8}
    assert fig["maturity_window"] == "60 days"
    assert "Trent's own time" in fig["exclusions"]
    assert fig["method"] and fig["tolerance"] == 0.005
    print("ok test_published_figure_carries_definition_denominator_exclusions")


def test_dollars_per_completion_helper():
    c1 = cost_of_completion([make_attempt("h-a1", "hc1", browser_seconds=10.0)], "hc1")
    c2 = cost_of_completion([make_attempt("h-a2", "hc2", browser_seconds=20.0)], "hc2")
    per = dollars_per_completion([c1, c2], verified_eligible_count=2)
    assert abs(per - (c1["dollars"] + c2["dollars"]) / 2) < 1e-12
    print("ok test_dollars_per_completion_helper")


def test_negative_units_rejected():
    try:
        AttemptCost(attempt_id="neg", browser_seconds=-1.0)
    except ValueError:
        print("ok test_negative_units_rejected")
        return
    raise AssertionError("negative units must be rejected")


def test_duplicate_attempt_id_in_completion_rejected():
    a = make_attempt("dup", "cd", browser_seconds=5.0)
    b = make_attempt("dup", "cd", browser_seconds=6.0)
    try:
        cost_of_completion([a, b], "cd")
    except ValueError:
        print("ok test_duplicate_attempt_id_in_completion_rejected")
        return
    raise AssertionError("duplicate attempt_id must violate F22 identity")


if __name__ == "__main__":
    test_known_cost_replay_three_consecutive_runs()
    test_failover_costs_attempts_separately_counts_completion_once()
    test_tampered_run_outside_tolerance_flagged()
    test_publish_refused_when_evidence_bar_unmet()
    test_publish_refused_by_default()
    test_published_figure_carries_definition_denominator_exclusions()
    test_dollars_per_completion_helper()
    test_negative_units_rejected()
    test_duplicate_attempt_id_in_completion_rejected()
    print("ALL 9 TESTS PASSED")
