#!/usr/bin/env python3
"""Inter-launch latency metrics for the browser lane.

Computes the p90 inter-launch gap from the REPAIRED browser_launched event
stream in the telemetry event log. Repair rules (documented, deterministic):

  1. Only event_type == "browser_launched" counts as a launch.
  2. Sort by the best-available launch instant: details.start_ts when
     present and parseable, else the event ts.
  3. Dedupe: the loop-side launch watcher skips emission when ANY
     browser_launched already exists for the role_id, but the lane itself
     sometimes emits its own too (source=lane-continue / orchestrator),
     so the same role can appear twice minutes apart. Keep only the FIRST
     browser_launched per role_id in any rolling 60-minute window.
  4. Gaps = minutes between consecutive kept events (cross-role: the lane
     is single-driver, so any launch-to-launch gap is lane time).
  5. p50 / p90 via nearest-rank on the sorted gaps
     (index = ceil(q*n) - 1, 0-based).

The script also lists the largest gaps (the stall drivers) so a p90 miss
can be attributed, never narrated.

Usage:
    python3 interlaunch_metrics.py                      # last 24h
    python3 interlaunch_metrics.py --hours 48
    python3 interlaunch_metrics.py --since 2026-09-16T00:10:00+00:00 \
        --until 2026-09-17T00:10:00+00:00
    python3 interlaunch_metrics.py --json                 # machine-readable
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import DATA  # noqa: E402 — repo path convention

EVENTS = os.path.join(DATA, "telemetry", "events.jsonl")

DEDUPE_WINDOW_MIN = 60  # same-role double-emission collapse window


def parse_ts(s):
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.endswith(" PDT"):
            dt = datetime.strptime(s[:-4], "%Y-%m-%d %H:%M")
            return dt.replace(tzinfo=timezone(timedelta(hours=-7)))
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def launch_ts(ev):
    """Best-available launch instant for a browser_launched event.

    Prefer details.start_ts (the launch-lock acquisition instant, a
    launch-time anchor) over the event's ts (detection time, up to a
    refresh interval late). Falls back to ts when start_ts is absent or
    unparseable — never invented.
    """
    details = ev.get("details") or {}
    ts = parse_ts(details.get("start_ts")) or parse_ts(ev.get("ts"))
    return ts


def repaired_launches(events_path=None, since=None, until=None):
    """browser_launched events, repaired per the module docstring.

    Returns [(ts, role_id, source), ...] sorted by ts, where ts is the
    best-available launch instant (details.start_ts preferred).
    """
    path = events_path or EVENTS
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or "browser_launched" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event_type") != "browser_launched":
                    continue
                ts = launch_ts(ev)
                if ts is None:
                    continue
                if since and ts < since:
                    continue
                if until and ts >= until:
                    continue
                rows.append((ts, ev.get("role_id") or "",
                             ev.get("source") or ""))
    except FileNotFoundError:
        return []
    rows.sort(key=lambda r: r[0])
    kept = []
    last_kept = {}  # role_id -> ts of last kept event
    for ts, rid, src in rows:
        prev = last_kept.get(rid)
        if prev is not None and (ts - prev) < timedelta(
                minutes=DEDUPE_WINDOW_MIN):
            continue  # same-role double emission; keep the first
        kept.append((ts, rid, src))
        last_kept[rid] = ts
    return kept


def nearest_rank(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = int(math.ceil(q * len(sorted_vals))) - 1
    return sorted_vals[max(0, min(idx, len(sorted_vals) - 1))]


def gaps_minutes(launches):
    return [(b[0] - a[0]).total_seconds() / 60
            for a, b in zip(launches, launches[1:])]


def summarize(launches):
    gaps = sorted(gaps_minutes(launches))
    out = {
        "n_launches": len(launches),
        "n_gaps": len(gaps),
        "p50_min": round(nearest_rank(gaps, 0.50), 2) if gaps else None,
        "p90_min": round(nearest_rank(gaps, 0.90), 2) if gaps else None,
        "max_gap_min": round(max(gaps), 2) if gaps else None,
        "repair": {
            "dedupe_window_min": DEDUPE_WINDOW_MIN,
            "percentile": "nearest-rank ceil(q*n)-1",
        },
    }
    # Stall drivers: the 10 largest gaps, with their bounding launches,
    # so a p90 miss is attributable to real events.
    if launches:
        paired = sorted(
            zip(gaps_minutes(launches), launches[1:]),
            key=lambda p: p[0], reverse=True)[:10]
        out["largest_gaps"] = [
            {"gap_min": round(g, 2),
             "after_ts": ts.isoformat(), "after_role": rid,
             "after_source": src}
            for g, (ts, rid, src) in paired
        ]
    return out


def main(argv):
    since = until = None
    hours = 24
    as_json = "--json" in argv
    i = 0
    while i < len(argv):
        if argv[i] == "--hours" and i + 1 < len(argv):
            hours = float(argv[i + 1]); i += 2
        elif argv[i] == "--since" and i + 1 < len(argv):
            since = parse_ts(argv[i + 1]); i += 2
        elif argv[i] == "--until" and i + 1 < len(argv):
            until = parse_ts(argv[i + 1]); i += 2
        else:
            i += 1
    now = datetime.now(timezone.utc)
    if since is None and until is None:
        since = now - timedelta(hours=hours)
        until = now
    launches = repaired_launches(since=since, until=until)
    summary = summarize(launches)
    summary["window"] = {
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
    }
    if as_json:
        print(json.dumps(summary, indent=1))
    else:
        print(f"window: {summary['window']['since']} .. "
              f"{summary['window']['until']}")
        print(f"launches: {summary['n_launches']}  "
              f"gaps: {summary['n_gaps']}")
        print(f"p50: {summary['p50_min']} min   "
              f"p90: {summary['p90_min']} min   "
              f"max gap: {summary['max_gap_min']} min")
        for g in summary.get("largest_gaps", [])[:5]:
            print(f"  {g['gap_min']:>8.1f} min  -> {g['after_ts']} "
                  f"{g['after_role'][:60]}")
    return summary


if __name__ == "__main__":
    main(sys.argv[1:])
