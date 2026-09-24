"""Tests for volume_controls.py (prototype, G-8/B-25).

Covers the B-25 acceptance scenarios:
 1. user tightening allowed (below-cap values kept as-is, nothing clamped)
 2. exceeding an operator cap -> clamped to cap AND recorded (never exceeded)
 3. 500-attempt stress run: sequential check_launch, zero overruns
 4. saved-search policy application produces the correct effective policy
 5. DENY cites the reason (and the ceiling that fired)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from volume_controls import (  # noqa: E402
    AGGRESSION_LEVELS,
    OperatorCaps,
    VolumePolicy,
    SavedSearchPolicy,
    effective_policy,
    apply_policy,
    check_launch,
)

CAPS = OperatorCaps(
    max_submissions_per_run=20,
    max_per_day=50,
    cadence_minutes=30,
    max_aggression="bounded_max",
)


def test_user_tightening_is_kept_and_nothing_clamped():
    eff = effective_policy(
        VolumePolicy(max_submissions_per_run=5, max_per_day=10, cadence_minutes=120,
                     aggression="low"),
        CAPS,
    )
    assert eff.policy.max_submissions_per_run == 5
    assert eff.policy.max_per_day == 10
    assert eff.policy.cadence_minutes == 120
    assert eff.policy.aggression == "low"
    assert eff.clamped_fields == {}, eff.clamped_fields
    assert eff.pacing_per_tick == 1
    print("ok test_user_tightening_is_kept_and_nothing_clamped")


def test_exceeding_cap_is_clamped_and_recorded():
    eff = effective_policy(
        VolumePolicy(max_submissions_per_run=999, max_per_day=500,
                     cadence_minutes=5, aggression="bounded_max"),
        CAPS,
    )
    assert eff.policy.max_submissions_per_run == 20, "never exceeded"
    assert eff.policy.max_per_day == 50
    assert eff.policy.cadence_minutes == 30, "cadence floor is a ceiling on speed"
    assert eff.clamped_fields["max_submissions_per_run"] == (999, 20)
    assert eff.clamped_fields["max_per_day"] == (500, 50)
    assert eff.clamped_fields["cadence_minutes"] == (5, 30)
    assert eff.was_clamped("max_submissions_per_run")
    assert not eff.was_clamped("aggression")
    print("ok test_exceeding_cap_is_clamped_and_recorded")


def test_aggression_above_operator_max_is_clamped():
    strict_caps = OperatorCaps(max_aggression="standard")
    eff = effective_policy(VolumePolicy(aggression="bounded_max"), strict_caps)
    assert eff.policy.aggression == "standard"
    assert eff.clamped_fields["aggression"] == ("bounded_max", "standard")
    print("ok test_aggression_above_operator_max_is_clamped")


def test_stress_500_attempts_zero_overruns():
    eff = effective_policy(
        VolumePolicy(max_submissions_per_run=20, max_per_day=50),
        CAPS,
    )
    today = 0
    this_run = 0
    launched = 0
    denied = 0
    for i in range(500):
        if i % 25 == 0 and i > 0:
            this_run = 0  # a new run starts every 25 ticks, independent of allow/deny
        d = check_launch(eff, today, this_run)
        if d.allowed:
            launched += 1
            today += 1
            this_run += 1
        else:
            denied += 1
            assert d.ceiling in ("max_submissions_per_run", "max_per_day"), d.reason
    # Run ceiling: 20 per run; daily ceiling: 50 per day — both hard.
    assert launched == 50, f"launched={launched}"  # daily cap binds
    assert denied == 450
    assert today == 50 and this_run <= 20
    print(f"ok test_stress_500_attempts_zero_overruns (launched=50 denied=450)")


def test_stress_run_ceiling_binds_before_day_ceiling():
    eff = effective_policy(
        VolumePolicy(max_submissions_per_run=7, max_per_day=1000),
        OperatorCaps(max_per_day=1000),  # day cap effectively off
    )
    launched = 0
    ceilings = set()
    for _ in range(100):
        d = check_launch(eff, launched, launched)
        if d.allowed:
            launched += 1
        else:
            ceilings.add(d.ceiling)
    assert launched == 7
    assert ceilings == {"max_submissions_per_run"}
    print("ok test_stress_run_ceiling_binds_before_day_ceiling")


def test_saved_search_policy_application():
    saved = SavedSearchPolicy(
        name="ml-ic-sf",
        filters={"title": "ML engineer", "level": "IC", "location": "SF"},
        aggression="bounded_max",
        max_submissions_per_run=15,
        max_per_day=40,
    )
    eff = apply_policy(saved, {"aggression": "low"}, CAPS)
    # override wins for aggression; saved preset wins where no override
    assert eff.policy.aggression == "low"
    assert eff.policy.max_submissions_per_run == 15
    assert eff.policy.max_per_day == 40
    assert eff.clamped_fields == {}
    assert eff.pacing_per_tick == 1
    print("ok test_saved_search_policy_application")


def test_saved_policy_defaults_fill_gaps():
    saved = SavedSearchPolicy(name="bare", filters={})
    eff = apply_policy(saved, {}, CAPS)
    assert eff.policy.aggression == "standard"
    assert eff.policy.max_submissions_per_run == 10
    assert eff.policy.max_per_day == 25
    assert eff.policy.cadence_minutes == 60
    print("ok test_saved_policy_defaults_fill_gaps")


def test_saved_policy_still_capped():
    saved = SavedSearchPolicy(name="greedy", aggression="bounded_max",
                              max_submissions_per_run=10_000)
    eff = apply_policy(saved, {}, CAPS)
    assert eff.policy.max_submissions_per_run == 20
    assert eff.clamped_fields["max_submissions_per_run"] == (10_000, 20)
    print("ok test_saved_policy_still_capped")


def test_deny_cites_reason_and_ceiling():
    eff = effective_policy(VolumePolicy(max_submissions_per_run=3, max_per_day=50), CAPS)
    d = check_launch(eff, attempts_today=0, attempts_this_run=3)
    assert not d.allowed
    assert "run limit reached" in d.reason
    assert "max_submissions_per_run=3" in d.reason
    assert d.ceiling == "max_submissions_per_run"

    eff2 = effective_policy(VolumePolicy(max_submissions_per_run=20, max_per_day=5), CAPS)
    d2 = check_launch(eff2, attempts_today=5, attempts_this_run=0)
    assert not d2.allowed
    assert "daily limit reached" in d2.reason
    assert d2.ceiling == "max_per_day"

    ok = check_launch(eff, attempts_today=0, attempts_this_run=0)
    assert ok.allowed and ok.ceiling is None
    print("ok test_deny_cites_reason_and_ceiling")


def test_invalid_values_rejected():
    for bad in (0, -3, "ten", True, 4.5):
        try:
            effective_policy(VolumePolicy(max_submissions_per_run=bad), CAPS)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid max_submissions_per_run={bad!r}")
    try:
        effective_policy(VolumePolicy(aggression="turbo"), CAPS)
    except ValueError:
        pass
    else:
        raise AssertionError("accepted unknown aggression level")
    print("ok test_invalid_values_rejected")


def test_operator_caps_are_immutable():
    try:
        CAPS.max_per_day = 999
    except AttributeError:
        pass
    else:
        raise AssertionError("OperatorCaps was mutable")
    assert CAPS.max_per_day == 50
    print("ok test_operator_caps_are_immutable")


if __name__ == "__main__":
    test_user_tightening_is_kept_and_nothing_clamped()
    test_exceeding_cap_is_clamped_and_recorded()
    test_aggression_above_operator_max_is_clamped()
    test_stress_500_attempts_zero_overruns()
    test_stress_run_ceiling_binds_before_day_ceiling()
    test_saved_search_policy_application()
    test_saved_policy_defaults_fill_gaps()
    test_saved_policy_still_capped()
    test_deny_cites_reason_and_ceiling()
    test_invalid_values_rejected()
    test_operator_caps_are_immutable()
    print("ALL 11 TESTS PASSED")
