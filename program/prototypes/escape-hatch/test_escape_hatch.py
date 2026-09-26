"""Tests for escape_hatch.py (prototype, G-2/B-23).

Covers the B-23 acceptance scenarios:
 1. attribution: auto / assisted / user-only label end-to-end from actor split
 2. no silent dead ends: every lead lands in exactly one visible state (fuzz)
 3. CAPTCHA: second failed auto attempt goes to the hatch, never a 3rd retry
 4. Trent-only blockers always route to TRAY, never to an assisted workaround
 5. explicit skips are recorded, not dropped
"""

import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from escape_hatch import (  # noqa: E402
    AUTO, ASSISTED_ACTION, TRAY, SKIPPED, TRENT_ONLY_REASONS,
    Lead, HatchOutcome, route, complete_step, receipt_label,
)


def test_clean_route_goes_auto():
    o = route(Lead("l1"))
    assert o.state == AUTO
    complete_step(o, "submitted via api lane", "automation")
    assert receipt_label(o) == "auto_verified"
    print("ok test_clean_route_goes_auto")


def test_unsupported_route_gets_bounded_action():
    o = route(Lead("l2", api_lane_available=False, browser_lane_blocked="unsupported_route"))
    assert o.state == ASSISTED_ACTION
    assert o.reason, "hatch must carry a one-sentence reason"
    assert o.action and o.action["kind"] in ("finish_in_browser", "answer_one_question", "skip_route")
    print("ok test_unsupported_route_gets_bounded_action")


def test_captcha_second_failure_goes_to_hatch_not_retry():
    o = route(Lead("l3", browser_lane_blocked="captcha", captcha_attempts=2))
    assert o.state == ASSISTED_ACTION
    assert o.action["kind"] == "finish_in_browser"
    # under the cap, the router leaves the lead alone (auto lane retries itself)
    o2 = route(Lead("l3b", browser_lane_blocked="captcha", captcha_attempts=1))
    assert o2.state == AUTO  # router does not manufacture a retry; policy owns the cap
    print("ok test_captcha_second_failure_goes_to_hatch_not_retry")


def test_trent_only_blockers_always_tray():
    for reason in sorted(TRENT_ONLY_REASONS):
        lead = Lead(
            "lt-" + reason,
            api_lane_available=False,
            browser_lane_blocked="unsupported_route",
            attestation_required=reason,
            manual_step="would have been an assisted action",
        )
        o = route(lead)
        assert o.state == TRAY, f"{reason}: got {o.state}"
        assert o.action is None, f"{reason}: hatch offered a workaround"
    print(f"ok test_trent_only_blockers_always_tray ({len(TRENT_ONLY_REASONS)} reasons)")


def test_attribution_split_labels_assisted():
    o = route(Lead("l5", api_lane_available=False, browser_lane_blocked="unsupported_route"))
    assert o.state == ASSISTED_ACTION
    complete_step(o, "packet prepared", "automation")
    complete_step(o, "CAPTCHA completed by hand", "user")
    complete_step(o, "submission confirmed", "automation")
    assert receipt_label(o) == "assisted"  # never masquerades as auto_verified
    print("ok test_attribution_split_labels_assisted")


def test_user_only_completion_label():
    o = route(Lead("l6", api_lane_available=False, browser_lane_blocked="unsupported_route"))
    complete_step(o, "whole form by hand", "user")
    assert receipt_label(o) == "user_only"
    print("ok test_user_only_completion_label")


def test_skip_is_recorded():
    o = route(Lead("l7", drop_reason="employer on blocklist — route intentionally skipped"))
    assert o.state == SKIPPED
    assert "blocklist" in o.reason
    print("ok test_skip_is_recorded")


def test_no_silent_dead_ends_fuzz():
    rng = random.Random(20260919)
    blockers = [None, "captcha", "verification", "unsupported_route"]
    attestations = [None] + sorted(TRENT_ONLY_REASONS) + ["some_machine_attestation"]
    seen = set()
    for i in range(2000):
        lead = Lead(
            f"f{i}",
            api_lane_available=rng.random() < 0.5,
            browser_lane_blocked=rng.choice(blockers),
            captcha_attempts=rng.randint(0, 3),
            attestation_required=rng.choice(attestations),
            manual_step=rng.choice([None, "needs one manual answer"]),
            drop_reason=rng.choice([None, "user skipped"]) if rng.random() < 0.1 else None,
        )
        o = route(lead)
        assert o.state in (AUTO, ASSISTED_ACTION, TRAY, SKIPPED), o.state
        seen.add(o.state)
        # every non-AUTO outcome carries its reason; nothing vanishes
        if o.state != AUTO:
            assert o.reason, f"{lead.lead_id}: {o.state} without reason"
    assert seen == {AUTO, ASSISTED_ACTION, TRAY, SKIPPED}, f"unreached states: {seen}"
    print("ok test_no_silent_dead_ends_fuzz (2000 leads, all four states reachable)")


def test_manual_step_routes_to_answer_one_question():
    o = route(Lead("l8", api_lane_available=False,
                   browser_lane_blocked="verification",
                   manual_step="confirm years of AI experience"))
    assert o.state == ASSISTED_ACTION
    assert o.action["kind"] == "answer_one_question"
    print("ok test_manual_step_routes_to_answer_one_question")


if __name__ == "__main__":
    test_clean_route_goes_auto()
    test_unsupported_route_gets_bounded_action()
    test_captcha_second_failure_goes_to_hatch_not_retry()
    test_trent_only_blockers_always_tray()
    test_attribution_split_labels_assisted()
    test_user_only_completion_label()
    test_skip_is_recorded()
    test_no_silent_dead_ends_fuzz()
    test_manual_step_routes_to_answer_one_question()
    print("ALL 9 TESTS PASSED")
