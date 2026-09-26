"""Regression tests for application-deduped outcome rates.

Rates must count distinct applications, not raw response events: three
AUTO_ACK messages on one application are one acknowledged application.
Raw event volumes stay visible separately in the per-outcome counters.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "engines"))
import outcome_analytics as oa


def app_events(outcomes):
    return [{"outcome": o} for o in outcomes]


def fixture():
    # 8 applications in one lane; some carry repeated messages.
    rows = [{"resume_lane": "ops", "transport": "browser", "ats": "greenhouse",
             "source": "company site"} for _ in range(8)]
    linked = {
        0: app_events(["AUTO_ACK"] * 3),
        1: app_events(["AUTO_ACK"]),
        2: app_events(["REJECTION"] * 2),
        3: app_events(["REJECTION"]),
        4: app_events(["REJECTION"]),
        5: app_events(["INTERVIEW_INVITE"]),
        6: app_events(["OFFER"]),
        7: [],
    }
    return rows, linked


def test_repeated_messages_count_once_per_application():
    rows, linked = fixture()
    lane = oa.lane_metrics(rows, linked)["ops"]
    # ack: apps 0 and 1 -> 2/8, not 4 raw events / 8.
    assert lane["ack_rate"]["value"] == pytest.approx(2 / 8)
    assert lane["ack_rate"]["n"] == 2
    assert lane["ack_rate"]["denominator"] == 8


def test_decisive_rates_use_distinct_applications():
    rows, linked = fixture()
    lane = oa.lane_metrics(rows, linked)["ops"]
    # 5 decisive applications (2,3,4,5,6); app 2's double rejection is one.
    assert lane["decisive_applications"] == 5
    assert lane["rejection_rate"]["value"] == pytest.approx(3 / 5)
    assert lane["rejection_rate"]["denominator"] == 5
    assert lane["invite_rate"]["value"] == pytest.approx(1 / 5)
    assert lane["offer_rate"]["value"] == pytest.approx(1 / 5)


def test_event_volumes_stay_raw_and_separate():
    rows, linked = fixture()
    lane = oa.lane_metrics(rows, linked)["ops"]
    assert lane["outcomes"]["AUTO_ACK"] == 4
    assert lane["outcomes"]["REJECTION"] == 4
    # decisive_outcomes keeps its event-volume meaning.
    assert lane["decisive_outcomes"] == 6


def test_tier_metrics_dedupe_the_same_way():
    rows, linked = fixture()
    tier = oa.tier_metrics(rows, linked)["employer_site"]
    assert tier["ack_rate"]["value"] == pytest.approx(2 / 8)
    assert tier["rejection_rate"]["value"] == pytest.approx(3 / 5)
    assert tier["outcomes"]["AUTO_ACK"] == 4


def test_small_denominators_stay_insufficient():
    rows = [{"resume_lane": "ops"} for _ in range(2)]
    linked = {0: app_events(["AUTO_ACK"]), 1: []}
    lane = oa.lane_metrics(rows, linked)["ops"]
    assert lane["ack_rate"]["value"] is None
    assert lane["ack_rate"]["note"] == "insufficient outcome data"
