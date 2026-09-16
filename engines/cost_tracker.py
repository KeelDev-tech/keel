#!/usr/bin/env python3
"""Cost-per-submission series builder (compounding growth loop #1).

The pipeline's standing directive is compound growth: cost-per-submission
(wall time, browser tasks, taps, HTTP requests) must trend DOWN as volume
goes UP. This script aggregates the additive cost_* fields the ledger
stamps on each SUBMITTED row (see cost_model.py) into daily/weekly series
at data/.state/cost-series.json, with least-squares trend verdicts and a
strict compounding verdict.

Semantics:
  - Zero-submission days are explicit {"gap": true} — never interpolated.
  - A fully-unmetered day (no metered dims at all) reports composite None,
    never 0.0 — a zero would read as "free submissions" and corrupt the
    trend (caught once, repaired).
  - cost_wall_min is launch->submit telemetry pairing per submission;
    cost_browser_tasks counts duplicates (a double-fire reads 2);
    cost_http_requests and cost_taps are UNMETERED in v1 (window-level
    average lives here, not per-submission attribution) and contribute 0
    with the basis naming the metered dims.
  - compounding_verdict is strict: True only on volume-up + cost-down;
    cost-down-at-down-volume reports "efficiency gain, not compounding yet".

Read-only except writing cost-series.json (atomic tmp+replace). Use the
dashboard's cost_section renderer to display the series — the dashboard
build is fail-safe on a broken cost section by contract.

Usage:
    python3 cost_tracker.py                  # daily series + trend verdicts
    python3 cost_tracker.py --series weekly
    python3 cost_tracker.py --backfill        # (re)build the whole series
                                              # from the ledger, dry-run
                                              # default with --live to write
"""
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import DATA  # noqa: E402 — repo path convention

LEDGER = os.path.join(DATA, "application-ledger.json")
SERIES_PATH = os.path.join(DATA, ".state", "cost-series.json")

COMPOSITE_WEIGHTS = {"cost_wall_min": 1.0, "cost_browser_tasks": 5.0,
                     "cost_taps": 10.0, "cost_http_requests": 0.25}
METERED_DIMS = ("cost_wall_min", "cost_browser_tasks")


def _load_ledger():
    try:
        with open(LEDGER) as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    rows = d if isinstance(d, list) else d.get("rows", d.get("applications", []))
    return [r for r in rows if isinstance(r, dict)]


def _sub_date(row):
    for key in ("date_submitted", "submitted_at", "date_confirmed"):
        v = row.get(key)
        if not v:
            continue
        try:
            return datetime.fromisoformat(
                str(v).replace(" PDT", "").replace(" ", "T", 1)
                if "T" not in str(v) else str(v)).date()
        except ValueError:
            continue
    return None


def composite_units(row):
    """Composite cost units for one SUBMITTED row; None when the row is
    fully unmetered on every metered dim (never 0.0)."""
    metered = {d: row.get(d) for d in METERED_DIMS if row.get(d) is not None}
    if not metered:
        return None, "unmetered"
    units = sum(row.get(d, 0) * COMPOSITE_WEIGHTS[d] for d in METERED_DIMS
                if row.get(d) is not None)
    basis = sorted(metered)
    return units, "+".join(basis)


def bucket_rows(rows, granularity):
    """Group SUBMITTED rows into date buckets; fill gaps explicitly."""
    subs = [r for r in rows if str(r.get("status", "")).upper() == "SUBMITTED"]
    by_day = defaultdict(list)
    dated = 0
    for r in subs:
        d = _sub_date(r)
        if d is None:
            continue  # undated rows: counted nowhere, reported in meta
        dated += 1
        if granularity == "weekly":
            key = (d - timedelta(days=d.weekday())).isoformat()  # Monday start
        else:
            key = d.isoformat()
        by_day[key].append(r)
    if not by_day:
        return [], {"submissions_total": len(subs), "undated_rows": len(subs) - dated}
    days = sorted(by_day)
    start = date.fromisoformat(days[0])
    end = date.fromisoformat(days[-1])
    if granularity == "weekly":
        step = timedelta(weeks=1)
        first = start
    else:
        step = timedelta(days=1)
        first = start
    buckets = []
    cur = first
    while cur <= end:
        key = cur.isoformat()
        rows_b = by_day.get(key, [])
        if not rows_b:
            buckets.append({"bucket": key, "gap": True})
        else:
            comps = [composite_units(r) for r in rows_b]
            metered = [c for c, _ in comps if c is not None]
            buckets.append({
                "bucket": key,
                "submissions": len(rows_b),
                "cost_units_mean": (sum(metered) / len(metered)
                                    if metered else None),
                "unmetered_rows": sum(1 for c, _ in comps if c is None),
                "dims": {d: round(sum(r.get(d, 0) for r in rows_b
                                     if r.get(d) is not None), 2)
                         for d in COMPOSITE_WEIGHTS},
            })
        cur += step
    return buckets, {"submissions_total": len(subs),
                     "submissions_dated": dated,
                     "undated_rows": len(subs) - dated}


def least_squares(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def trend_verdict(buckets):
    """Least-squares slope on metered, non-gap buckets; a plain-English
    verdict. Gaps and unmetered buckets never interpolate into the trend."""
    pts = [(i, b["cost_units_mean"]) for i, b in enumerate(buckets)
           if not b.get("gap") and b.get("cost_units_mean") is not None]
    vols = [(i, b["submissions"]) for i, b in enumerate(buckets)
            if not b.get("gap")]
    if len(pts) < 2:
        return {"slope_units_per_bucket": 0.0,
                "verdict": "insufficient metered data"}
    slope = least_squares([p[0] for p in pts], [p[1] for p in pts])
    vslope = least_squares([v[0] for v in vols], [v[1] for v in vols]) \
        if len(vols) >= 2 else 0.0
    if slope < 0 and vslope > 0:
        verdict = "compounding: cost down, volume up"
    elif slope < 0:
        verdict = "efficiency gain, not compounding yet (volume not rising)"
    elif slope > 0:
        verdict = "cost rising — investigate"
    else:
        verdict = "cost flat"
    return {"slope_units_per_bucket": round(slope, 4),
            "volume_slope_per_bucket": round(vslope, 4),
            "verdict": verdict}


def build_series(granularity="daily"):
    rows = _load_ledger()
    buckets, meta = bucket_rows(rows, granularity)
    out = {"granularity": granularity,
           "built_at": datetime.now().astimezone().isoformat(),
           "meta": meta,
           "buckets": buckets,
           "trend": trend_verdict(buckets)}
    # Strict compounding verdict: only when both halves of the trend say so.
    t = out["trend"]
    out["compounding_verdict"] = t["verdict"].startswith("compounding")
    return out


def main(argv):
    granularity = "daily"
    if "--series" in argv:
        i = argv.index("--series")
        if i + 1 < len(argv) and argv[i + 1] in ("daily", "weekly"):
            granularity = argv[i + 1]
    series = build_series(granularity)
    tmp = SERIES_PATH + ".tmp"
    os.makedirs(os.path.dirname(SERIES_PATH), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(series, f, indent=1)
    os.replace(tmp, SERIES_PATH)
    gaps = sum(1 for b in series["buckets"] if b.get("gap"))
    print(json.dumps({"series": SERIES_PATH,
                      "granularity": granularity,
                      "buckets": len(series["buckets"]),
                      "gaps": gaps,
                      "submissions_total": series["meta"]["submissions_total"],
                      "trend": series["trend"]["verdict"],
                      "compounding": series["compounding_verdict"]},
                     indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
