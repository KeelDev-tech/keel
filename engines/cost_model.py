#!/usr/bin/env python3
"""cost_model.py — per-submission cost model (item 5/10, 2026-09-16).

Cost model v1. Four dimensions, metered independently:

  wall_min        minutes from launch (browser_launched or
                  api_direct_submit_started) to the SUBMITTED record.
                  Metered via telemetry pairing; None when unpairable.
  browser_tasks   count of browser_launched events for the role_id inside
                  the launch->submit cycle. Duplicate launches are the
                  signal (a double-fire shows up as 2, not 1).
  http_requests   HTTP requests spent on THIS submission. The loop's
                  per-run counter (api-direct-http-count.jsonl) has no
                  role attribution, so per-submission HTTP is UNMETERED
                  in v1. The window-level average is already reported by
                  cost_tracker.py — no per-row value is invented here.
  taps            human approval cards / tray answers attributable to this
                  submission. api-direct: 0 by construction (no platform
                  card is ever spawned on that transport). browser:
                  UNMETERED in v1 — the platform batches approvals and no
                  per-submission tap signal exists; browser_tasks is a
                  proxy, never recorded as taps.

cost_composite_units v1 =
    wall_min + 5*browser_tasks + 10*taps + 0.25*http_requests
Unmetered dimensions contribute 0 and cost_units_basis names exactly
which dimensions were metered, so the composite is never read as
fully-metered when it isn't. Weights are lane-relative conventions
(documented in docs/cost-model.md), not physics — reviewed when the
metering matures.

Provenance: every attribution carries cost_attributed_at (ISO UTC) and
cost_source ('instrumented' | 'backfill'). The ledger stays append-only:
stamp_row() ADDS cost_* keys and REFUSES to overwrite any existing key,
so attribution can never rewrite ledger history — only extend it.
"""

import math
from datetime import datetime, timedelta, timezone

LAUNCH_EVENTS = ("browser_launched", "api_direct_submit_started")

# cost_* keys this module is allowed to write. stamp_row() refuses anything
# else, so a caller typo can't smuggle a non-cost field onto a ledger row.
COST_KEYS = (
    "cost_wall_min",
    "cost_browser_tasks",
    "cost_http_requests",
    "cost_http_status",
    "cost_taps",
    "cost_taps_status",
    "cost_composite_units",
    "cost_units_basis",
    "cost_attributed_at",
    "cost_source",
)

# Composite weights v1 (see docs/cost-model.md for rationale).
W_TASKS = 5.0    # one browser task ~= 5 min of machine time
W_TAPS = 10.0    # one human tap ~= 10 min of attention
W_HTTP = 0.25    # one HTTP request ~= 15 s of budget-weighted time

UNMETERED = "unmetered"
METERED_ZERO = "metered-zero"


def parse_ts(s):
    """Parse the mixed timestamp formats the pipeline writes (ISO-Z, PDT)."""
    if not s:
        return None
    t = str(s).strip()
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        d = None
    if d is None:
        for fmt, off in (("%Y-%m-%d %H:%M PDT", -7),
                         ("%Y-%m-%d %H:%M PST", -8)):
            try:
                d = datetime.strptime(t, fmt)
                d = d.replace(tzinfo=timezone(timedelta(hours=off)))
                break
            except ValueError:
                continue
        if d is None:
            try:
                d = datetime.strptime(t[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def attribute_cost(role_id, submitted_ts, events, transport=None,
                   source="instrumented"):
    """Attribute cost dimensions to one submission.

    events: iterable of telemetry dicts with ts / event_type / role_id.
    submitted_ts: datetime (or parseable string) of the SUBMITTED record.
    transport: 'browser' | 'api-direct' | None (unknown -> taps unmetered).

    Never invents: any dimension that cannot be derived from the supplied
    evidence lands as None with a *_status marker. Returns a dict of
    cost_* keys (see COST_KEYS).
    """
    submitted_ts = parse_ts(submitted_ts) if not isinstance(
        submitted_ts, datetime) else submitted_ts
    if submitted_ts is None:
        return _unmetered_row(source, reason="no submitted timestamp")

    launches = []
    task_spawns = 0
    for ev in events or []:
        if ev.get("role_id") != role_id:
            continue
        ts = parse_ts(ev.get("ts"))
        if ts is None or ts > submitted_ts:
            continue
        et = ev.get("event_type")
        if et in LAUNCH_EVENTS:
            launches.append(ts)
        if et == "browser_launched":
            task_spawns += 1

    wall_min = None
    browser_tasks = 0
    if launches:
        launch_ts = max(launches)
        wall_min = round((submitted_ts - launch_ts).total_seconds() / 60.0, 1)
        if wall_min < 0:
            wall_min = 0.0
        browser_tasks = task_spawns  # spawns counted within the paired cycle
    else:
        browser_tasks = None if transport != "api-direct" else 0

    transport = (transport or "").lower()
    if transport == "api-direct":
        taps, taps_status = 0, METERED_ZERO
        if browser_tasks is None:
            browser_tasks = 0
    elif transport == "browser":
        taps, taps_status = None, UNMETERED
    else:
        taps, taps_status = None, UNMETERED

    cost = {
        "cost_wall_min": wall_min,
        "cost_browser_tasks": browser_tasks,
        "cost_http_requests": None,
        "cost_http_status": UNMETERED,
        "cost_taps": taps,
        "cost_taps_status": taps_status,
    }
    basis = [k for k, v in (("wall", wall_min), ("tasks", browser_tasks),
                            ("taps", taps)) if v is not None]
    # Honesty: a fully-unmetered row gets composite None, never 0.0 — a
    # zero would read as "free submission" and corrupt the trend line.
    cost["cost_composite_units"] = round(composite(cost), 2) if basis else None
    cost["cost_units_basis"] = "+".join(basis) if basis else "none"
    cost["cost_attributed_at"] = datetime.now(timezone.utc).isoformat()
    cost["cost_source"] = source
    return cost


def _unmetered_row(source, reason=""):
    cost = {
        "cost_wall_min": None,
        "cost_browser_tasks": None,
        "cost_http_requests": None,
        "cost_http_status": UNMETERED,
        "cost_taps": None,
        "cost_taps_status": UNMETERED,
        "cost_composite_units": None,
        "cost_units_basis": "none",
        "cost_attributed_at": datetime.now(timezone.utc).isoformat(),
        "cost_source": source,
    }
    return cost


def composite(cost):
    """Composite cost units v1. Unmetered dims contribute 0."""
    wall = _num(cost.get("cost_wall_min")) or 0.0
    tasks = _num(cost.get("cost_browser_tasks")) or 0.0
    taps = _num(cost.get("cost_taps")) or 0.0
    http = _num(cost.get("cost_http_requests")) or 0.0
    return wall + W_TASKS * tasks + W_TAPS * taps + W_HTTP * http


def stamp_row(row, cost, overwrite=False):
    """Add cost_* fields to a ledger row. Additive-only by default.

    Returns (stamped: bool, added: list[str]).
    Refuses to overwrite any existing key unless overwrite=True — a second
    attribution pass can never silently rewrite the first.
    """
    added = []
    for k in COST_KEYS:
        if k in cost:
            if k in row and not overwrite:
                continue
            row[k] = cost[k]
            added.append(k)
    return (len(added) > 0, added)


def trend_verdict(values, min_points=3, band=0.05):
    """Least-squares verdict on a series with None gaps dropped.

    Returns 'down' | 'flat' | 'up' | 'insufficient'.
    'down'/'up' require |slope| > band * mean(|y|); otherwise 'flat'.
    """
    ys = [float(v) for v in values if _num(v) is not None]
    if len(ys) < min_points:
        return "insufficient"
    n = len(ys)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return "flat"
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    scale = sum(abs(y) for y in ys) / n
    if scale == 0:
        return "flat"
    rel = slope / scale
    if rel < -band:
        return "down"
    if rel > band:
        return "up"
    return "flat"


def linregress(values):
    """Least-squares (slope, intercept) on None-dropped values; None if <2."""
    ys = [float(v) for v in values if _num(v) is not None]
    if len(ys) < 2:
        return None
    n = len(ys)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return (0.0, my)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return (slope, my - slope * mx)


def compounding_verdict(volume_series, cost_series):
    """Is cost-per-submission going down as volume goes up?

    Returns (compounding: bool, reason: str). compounding=True requires
    volume trending up AND cost trending down on the same buckets — the
    COMPOUND GROWTH directive names both conditions. Cost falling while
    volume falls is an efficiency gain, reported honestly as not-compounding.
    """
    v = trend_verdict(volume_series)
    c = trend_verdict(cost_series)
    if v == "insufficient" or c == "insufficient":
        return (False, f"insufficient data (volume={v}, cost={c})")
    if v == "up" and c == "down":
        return (True, "volume up, cost-per-submission down — compounding")
    if c == "down":
        return (False, f"cost-per-submission down but volume is {v} — "
                       "efficiency gain, not compounding yet")
    return (False, f"not compounding (volume={v}, cost={c})")
