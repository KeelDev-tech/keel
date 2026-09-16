#!/usr/bin/env python3
"""Tests for cost_model.py — per-submission cost attribution.

Run: python3 -m pytest test_cost_model.py
"""
import os
import sys

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
import cost_model as cm  # noqa: E402

RID = "TEST-COST-1"


def _ev(ts, et, rid=RID):
    return {"ts": ts, "event_type": et, "role_id": rid}


def test_import_exposes_public_api():
    for name in ("attribute_cost", "stamp_row", "composite",
                 "trend_verdict", "linregress", "compounding_verdict",
                 "COST_KEYS"):
        assert hasattr(cm, name), name


def test_attribution_browser_fixture():
    # Duplicate browser_launched (the double-fire signal) + submitted.
    events = [
        _ev("2026-09-16T10:00:00+00:00", "browser_launched"),
        _ev("2026-09-16T10:20:00+00:00", "browser_launched"),
        _ev("2026-09-16T10:45:00+00:00", "submitted"),
    ]
    c = cm.attribute_cost(RID, "2026-09-16T10:45:00+00:00",
                          events, transport="browser")
    assert c["cost_wall_min"] == 25.0, c          # latest launch -> submit
    assert c["cost_browser_tasks"] == 2, c        # duplicate counted, not 1
    assert c["cost_taps"] is None, c
    assert c["cost_taps_status"] == "unmetered", c
    assert c["cost_http_requests"] is None, c
    assert c["cost_http_status"] == "unmetered", c
    assert c["cost_composite_units"] == 25.0 + 5 * 2, c
    assert c["cost_units_basis"] == "wall+tasks", c
    assert c["cost_source"] == "instrumented", c
    assert set(c) == set(cm.COST_KEYS), set(c)


def test_attribution_api_direct_metered_zero():
    events = [_ev("2026-09-16T10:40:00+00:00", "api_direct_submit_started")]
    c = cm.attribute_cost(RID, "2026-09-16T10:45:00+00:00",
                          events, transport="api-direct")
    assert c["cost_wall_min"] == 5.0, c
    assert c["cost_browser_tasks"] == 0, c
    assert c["cost_taps"] == 0, c
    assert c["cost_taps_status"] == "metered-zero", c
    assert c["cost_units_basis"] == "wall+tasks+taps", c


def test_unmetered_row_never_zero():
    # No launch events at all: fully unmetered -> composite None, not 0.0.
    c = cm.attribute_cost(RID, "2026-09-16T10:45:00+00:00", [],
                          transport="browser")
    assert c["cost_wall_min"] is None
    assert c["cost_browser_tasks"] is None
    assert c["cost_composite_units"] is None, c
    assert c["cost_units_basis"] == "none", c


def test_no_submitted_timestamp_fails_closed():
    c = cm.attribute_cost(RID, "not-a-date", [_ev("2026-09-16T10:00:00+00:00",
                                                  "browser_launched")])
    assert c["cost_composite_units"] is None
    assert c["cost_units_basis"] == "none"


def test_stamp_row_additive_only():
    row = {"role_id": RID, "status": "SUBMITTED", "cost_wall_min": 9.9}
    cost = {"cost_wall_min": 1.1, "cost_browser_tasks": 2,
            "cost_composite_units": 19.1, "cost_units_basis": "wall+tasks"}
    stamped, added = cm.stamp_row(row, cost)
    assert stamped is True
    assert row["cost_wall_min"] == 9.9, "existing key must not be overwritten"
    assert row["cost_browser_tasks"] == 2
    assert "cost_browser_tasks" in added
    assert "cost_wall_min" not in added
    # Second pass with overwrite=True is the only way to change a key.
    cm.stamp_row(row, cost, overwrite=True)
    assert row["cost_wall_min"] == 1.1


def test_trend_verdict_directions():
    assert cm.trend_verdict([10.0, 8.0, 6.0, 4.0]) == "down"
    assert cm.trend_verdict([4.0, 6.0, 8.0, 10.0]) == "up"
    assert cm.trend_verdict([5.0, 5.1, 4.9, 5.0]) == "flat"
    assert cm.trend_verdict([1.0, 2.0]) == "insufficient"
    assert cm.trend_verdict([5.0, None, 4.0, None, 3.0]) == "down"  # gaps dropped


def test_compounding_verdict_strict():
    ok, reason = cm.compounding_verdict([1, 2, 3, 4], [10.0, 8.0, 6.0, 4.0])
    assert ok is True, reason
    ok, reason = cm.compounding_verdict([4, 3, 2, 1], [10.0, 8.0, 6.0, 4.0])
    assert ok is False and "efficiency gain" in reason, reason
    ok, reason = cm.compounding_verdict([1], [1.0])
    assert ok is False and "insufficient" in reason


def test_composite_weights():
    c = {"cost_wall_min": 10.0, "cost_browser_tasks": 2,
         "cost_taps": 1, "cost_http_requests": 4}
    assert cm.composite(c) == 10.0 + 5 * 2 + 10 * 1 + 0.25 * 4
    # Unmetered dims contribute 0.
    assert cm.composite({"cost_wall_min": 10.0}) == 10.0
