#!/usr/bin/env python3
"""Tests for cost_tracker.py — cost-per-submission series builder.

Run: python3 -m pytest test_cost_tracker.py
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
import cost_tracker as ct  # noqa: E402


def _sub(rid, day, **kw):
    row = {"role_id": rid, "status": "SUBMITTED",
           "date_submitted": f"{day}T12:00:00+00:00"}
    row.update(kw)
    return row


def test_import_exposes_public_api():
    for name in ("composite_units", "bucket_rows", "least_squares",
                 "trend_verdict", "build_series"):
        assert hasattr(ct, name), name


def test_composite_units_metered_and_unmetered():
    units, basis = ct.composite_units(
        {"cost_wall_min": 10.0, "cost_browser_tasks": 2})
    assert units == 10.0 + 5 * 2, (units, basis)
    assert basis == "cost_browser_tasks+cost_wall_min", basis  # sorted
    units, basis = ct.composite_units({"cost_wall_min": None})
    assert units is None and basis == "unmetered", "fully-unmetered -> None, never 0.0"


def test_bucket_rows_explicit_gaps():
    rows = [_sub("A", "2026-09-14", cost_wall_min=20.0),
            _sub("B", "2026-09-16", cost_wall_min=10.0)]
    buckets, meta = ct.bucket_rows(rows, "daily")
    keys = [b["bucket"] for b in buckets]
    assert keys == ["2026-09-14", "2026-09-15", "2026-09-16"], keys
    mid = buckets[1]
    assert mid.get("gap") is True, "missing day must be an explicit gap"
    assert meta["submissions_total"] == 2
    assert meta["undated_rows"] == 0


def test_bucket_rows_undated_rows_reported():
    rows = [_sub("A", "2026-09-14", cost_wall_min=5.0),
            {"role_id": "B", "status": "SUBMITTED"}]  # no date field at all
    buckets, meta = ct.bucket_rows(rows, "daily")
    assert meta["undated_rows"] == 1, meta
    assert meta["submissions_dated"] == 1


def test_trend_verdict_compounding_and_efficiency():
    # cost down + volume up -> compounding
    buckets = [
        {"bucket": "d1", "submissions": 1, "cost_units_mean": 30.0},
        {"bucket": "d2", "submissions": 2, "cost_units_mean": 20.0},
        {"bucket": "d3", "submissions": 3, "cost_units_mean": 10.0},
    ]
    v = ct.trend_verdict(buckets)
    assert v["verdict"].startswith("compounding"), v
    # cost down + volume down -> efficiency gain, NOT compounding
    buckets = [
        {"bucket": "d1", "submissions": 3, "cost_units_mean": 30.0},
        {"bucket": "d2", "submissions": 2, "cost_units_mean": 20.0},
        {"bucket": "d3", "submissions": 1, "cost_units_mean": 10.0},
    ]
    v = ct.trend_verdict(buckets)
    assert "not compounding" in v["verdict"], v


def test_trend_verdict_insufficient_and_rising():
    assert ct.trend_verdict(
        [{"bucket": "d1", "gap": True}])["verdict"] == "insufficient metered data"
    rising = [
        {"bucket": "d1", "submissions": 1, "cost_units_mean": 10.0},
        {"bucket": "d2", "submissions": 1, "cost_units_mean": 20.0},
        {"bucket": "d3", "submissions": 1, "cost_units_mean": 30.0},
    ]
    assert "rising" in ct.trend_verdict(rising)["verdict"]


def test_build_series_reads_ledger(tmp_path, monkeypatch):
    ledger = [
        _sub("A", "2026-09-14", cost_wall_min=20.0, cost_browser_tasks=1),
        _sub("B", "2026-09-16", cost_wall_min=10.0, cost_browser_tasks=1),
        {"role_id": "C", "status": "PARKED"},  # non-submitted rows excluded
    ]
    lp = tmp_path / "ledger.json"
    lp.write_text(json.dumps(ledger))
    monkeypatch.setattr(ct, "LEDGER", str(lp))
    out = ct.build_series("daily")
    assert out["meta"]["submissions_total"] == 2, out["meta"]
    assert len(out["buckets"]) == 3  # 14th, 15th (gap), 16th
    gaps = [b for b in out["buckets"] if b.get("gap")]
    assert len(gaps) == 1 and gaps[0]["bucket"] == "2026-09-15"
    assert "trend" in out and "compounding_verdict" in out


def test_build_series_empty_ledger(tmp_path, monkeypatch):
    lp = tmp_path / "ledger.json"
    lp.write_text("[]")
    monkeypatch.setattr(ct, "LEDGER", str(lp))
    out = ct.build_series("daily")
    assert out["buckets"] == [] and out["meta"]["submissions_total"] == 0
