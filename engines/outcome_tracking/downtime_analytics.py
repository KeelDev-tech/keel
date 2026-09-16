#!/usr/bin/env python3
"""Downtime-vs-edge-case analytics for the Western Application Pipeline.

The applicant's directive (2026-09-15): measure pipeline downtime/idle and push it
against edge cases to drive optimization.

What it computes, per 10-minute bucket over the rolling 24h:
  1. browser-lane idle minutes while workable-READY was nonempty
     (the expensive kind of idle)
  2. apply_loop stall events: cycles with zero launches despite READY
     supply and visible loop activity (gates/briefs)
  3. feeder dry events (feeder_empty) and their duration
  4. queue drain rate (READY consumed per hour) vs submission rate

Edge-case correlation layer: every expensive-idle bucket is attributed to
its dominant edge-case class from telemetry (prescreen PARK clusters,
lever_preflight refusals, technique_blocked, travel gates, stale_inflight,
Ashby anti-spam, dead-posting demotions, materials demotions, ...).
Output: edge-case classes ranked by idle-minutes-attributed, so the next
optimization targets the class costing the most lane time.

FAIL-CLOSED CONVENTIONS (mirroring outcome_analytics.py):
  - Idle minutes count ONLY for buckets with telemetry coverage.
  - Historical workable-READY is a REVERSE-RECONSTRUCTED UPPER BOUND
    between hard supply anchors (the exact current count from
    feeder_watchdog.workable, plus feeder_empty events with ready_count=0
    which pin workable-READY at exactly 0). Buckets older than the oldest
    anchor have UNKNOWN supply (None) — the walk cannot see past the last
    hard observation and must not invent supply there.
  - A bucket counts as expensive idle only when supply is evidenced
    (proxy >= 1 OR a supply-signal event in/near the bucket). Idle with
    unknown supply is reported as idle_unknown_supply, never as expensive
    idle — never overclaim.
  - Rates report only with denominator >= 5 (MIN_N), else
    "insufficient outcome data".
  - Timestamps that don't parse are skipped, never guessed.

Read-only against live queue/ledger/telemetry. Never writes queues,
ledger, or telemetry.

Outputs (data/hidden_files/downtime-edge-analysis/):
  downtime-edge-<YYYYMMDD-HHMM>.json / .md
  downtime-edge-latest.json / .md   (copies of the newest run)

CLI: python3 downtime_analytics.py [--out DIR]
Prints "SURFACE: ..." lines for the top-3 downtime drivers.
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
_ENGINES = os.path.dirname(BASE)
for _p in (BASE, _ENGINES):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from keel_paths import DATA, TELEMETRY  # noqa: E402

OUT_DIR = os.path.join(DATA, "hidden_files", "downtime-edge-analysis")
EVENTS = os.path.join(TELEMETRY, "events.jsonl")
QUEUE = os.path.join(DATA, "queues", "standard-queue.json")
LEDGER = os.path.join(DATA, "ledger", "application-ledger.json")

from outcome_analytics import parse_ts  # noqa: E402 — canonical ts parse

# outcome_analytics has no PDT label; hard-code Pacific for display only.
PDT = timezone(timedelta(hours=-7))

WINDOW_MIN = 10
ROLLING_HOURS = 24
BROWSER_CAP_HOURS = 3     # cap a launch window; beyond that needs evidence
MIN_N = 5                 # minimum denominator before a rate is reported

# ---------------------------------------------------------------- edge classes

# Gate-name -> edge-case class. Gate names come from engines' GATE_TYPES
# (application-executor/log_event.py) and the prescreen/verify vocabulary.
EDGE_CLASSES = {
    "prescreen_parks": {
        "needs_input", "essay", "attest", "fabrication", "account",
        "captcha", "recaptcha_enterprise", "email_verification_code",
        "verification_code", "location", "relocation_commitment",
    },
    "travel_gates": {"travel"},
    "lever_preflight": {"lever_preflight"},
    "technique_blocked": {"technique_blocked"},
    "eligibility_dedupe": {
        "eligibility", "duplicate_of_submitted", "discarded_by_applicant",
    },
    "materials": {"materials_missing", "materials_demoted"},
    "inflight_cap": {"inflight_cap"},
    "dead_postings": {"unverifiable", "malformed_ready"},
    "stale_inflight": {
        "stale_inflight", "stale_inflight_reset", "marker_timestamp_missing",
    },
    "ashby_anti_spam": {"ashby_spam_flag"},
}

# Gates whose PARK verdict provably removed a lead from workable-READY.
# NOTE: most gate_blocked events do NOT consume workable supply:
#   - materials_missing / malformed_ready parks remove leads that were never
#     workable (no resume on disk / malformed);
#   - discarded_by_applicant removes parked-queue leads (e.g. the captcha
#     queue discard);
#   - lead_dead removals come from the pending-verification pool;
#   - lever_preflight / inflight_cap are non-destructive skips;
#   - needs_input from the browser lane was IN-FLIGHT (already left workable
#     at launch) — ambiguous, excluded fail-closed.
# Only prescreen (which screens workable-READY leads pre-launch, any gate)
# and real submissions provably consume workable supply.
def consumes_workable(e):
    t = e.get("event_type")
    if t == "submitted" and e.get("role_id"):
        return True
    if t == "gate_blocked" and e.get("source") == "prescreen":
        return True
    return False

SUPPLY_IN_TYPES = {"staged_ingested", "lead_verified"}
OUTCOME_SOURCES = {"record_outcome", "browser_task", "apply_loop"}

# ---------------------------------------------------------------- loading


def load_events(path=None):
    """Parse telemetry jsonl -> list of (aware dt, event dict). Skips bad lines."""
    path = path or EVENTS
    out = []
    try:
        fh = open(path)
    except OSError:
        return out
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        dt = parse_ts(e.get("ts"))
        if dt is None:
            continue
        out.append((dt, e))
    fh.close()
    out.sort(key=lambda t: t[0])
    return out


def gate_of(event):
    d = event.get("details") or {}
    return d.get("gate") or event.get("gate")


def edge_class_of_gate(gate):
    for cls, gates in EDGE_CLASSES.items():
        if gate in gates:
            return cls
    return None


def load_queue():
    d = json.load(open(QUEUE))
    return d if isinstance(d, list) else d.get("entries", d.get("items", []))


def supply_now(entries=None):
    """Exact current workable-READY via the feeder's own workable() mirror.

    Fail-closed: when feeder_watchdog exposes no workable() (the repo's
    feeder_watchdog does not), the current supply anchor is None (unknown)
    — buckets then report idle_unknown_supply, never expensive idle.
    """
    import feeder_watchdog
    entries = entries if entries is not None else load_queue()
    workable = getattr(feeder_watchdog, "workable", None)
    if workable is None:
        return None
    return sum(1 for e in entries if workable(e))


# ---------------------------------------------------------------- browser busy


def load_ledger_submissions():
    """Ledger SUBMITTED rows -> {role_id: [aware dt]}. The ledger is the
    canonical submission record and carries role_ids; telemetry `submitted`
    events from record_outcome often have an empty role_id."""
    try:
        rows = json.load(open(LEDGER))
    except Exception:
        return {}
    rows = rows if isinstance(rows, list) else rows.get("rows", [])
    out = defaultdict(list)
    for r in rows:
        if r.get("status") != "SUBMITTED" or not r.get("role_id"):
            continue
        dt = parse_ts(r.get("date_submitted") or r.get("submitted_at"))
        if dt is not None:
            out[r["role_id"]].append(dt)
    for v in out.values():
        v.sort()
    return out


def browser_windows(events, now=None, cap_hours=BROWSER_CAP_HOURS,
                    ledger_subs=None):
    """Busy windows of the single browser lane from browser_launched events.

    A window runs from launch until the first of:
      - the ledger SUBMITTED timestamp for the same role_id (canonical
        outcome; telemetry submitted events often lack role_id),
      - a same-role outcome event (submitted / gate_blocked from an
        outcome source),
      - the next launch of a different role at least 10 minutes later
        (one-at-a-time lane: a later launch proves the previous one ended;
        launches < 10 min apart are concurrent packet-build bursts, not
        real handoffs, and must not truncate the window).
    Capped at cap_hours; open windows end at `now`.
    Returns merged [(start, end)] aware-datetime pairs.
    """
    now = now or datetime.now(timezone.utc)
    ledger_subs = ledger_subs if ledger_subs is not None \
        else load_ledger_submissions()
    launches = [(dt, e) for dt, e in events
                if e.get("event_type") == "browser_launched" and e.get("role_id")]
    launches.sort(key=lambda t: t[0])

    outcomes = defaultdict(list)  # role_id -> [dt]
    for dt, e in events:
        if e.get("event_type") in ("submitted", "gate_blocked") and e.get("role_id"):
            if e.get("source") in OUTCOME_SOURCES or e.get("event_type") == "submitted":
                outcomes[e["role_id"]].append(dt)
    for role, dts in ledger_subs.items():
        outcomes[role].extend(dts)
    for v in outcomes.values():
        v.sort()

    wins = []
    for i, (dt, e) in enumerate(launches):
        role = e["role_id"]
        end = dt + timedelta(hours=cap_hours)
        # Next launch of a *different* role at least 10 min later proves
        # this one ended. Launches < 10 min apart are concurrent
        # packet-build bursts (multi-arm), not real lane handoffs.
        for j in range(i + 1, len(launches)):
            dt2, e2 = launches[j]
            if e2.get("role_id") != role and dt2 - dt >= timedelta(minutes=10):
                end = min(end, dt2)
                break
        # first outcome at-or-after launch (allow 5 min clock skew)
        for odt in outcomes.get(role, []):
            if odt >= dt - timedelta(minutes=5):
                end = min(end, odt)
                break
        end = min(end, now)
        if end > dt:
            wins.append((dt, end))

    # merge overlaps
    wins.sort()
    merged = []
    for s, e_ in wins:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e_))
        else:
            merged.append((s, e_))
    return merged


# ---------------------------------------------------------------- supply proxy


def supply_anchors(events, now, current):
    """Hard supply observations from telemetry, ascending by time.

    - (now, current): exact workable-READY via feeder_watchdog.workable.
    - feeder_empty with ready_count == 0: exact workable-READY of 0
      (raw READY 0 implies workable 0). Nonzero ready_counts are NOT
      usable as workable anchors (workable <= raw READY, unknown gap),
      so they are ignored — fail-closed.
    Buckets older than the oldest anchor have UNKNOWN supply (None):
    the reverse walk cannot see past the last hard observation, and
    must not invent supply there.
    """
    anchors = []
    for dt, e in events:
        if dt > now:
            continue
        if gate_of(e) == "feeder_empty":
            rc = (e.get("details") or {}).get("ready_count")
            if rc == 0:
                anchors.append((dt, 0))
    anchors.append((now, current))
    anchors.sort(key=lambda t: t[0])
    # dedupe identical timestamps, keep the last
    out = []
    for dt, v in anchors:
        if out and out[-1][0] == dt:
            out[-1] = (dt, v)
        else:
            out.append((dt, v))
    return out


def reconstruct_supply(events, anchors, now=None):
    """Reverse-walk events between hard supply anchors.

    Going backwards within a segment: a `submitted` event or a consuming
    gate_blocked means the lead was in workable supply just before -> +1;
    a staged_ingested means supply entered staging -> -1; lead_dead
    removals -> +1. Non-consuming gates (inflight_cap/lever_preflight
    skips, stale_inflight diagnostics, eligibility dedupe) never touched
    supply and are excluded.

    At each older anchor the level resets to the anchor's hard value.
    Buckets older than the oldest anchor -> None (unknown supply).

    APPROXIMATE by design: backfilled date-only timestamps are coarse and
    not every queue mutation has a telemetry event. Values are an upper
    bound within each segment, never a claim of exactness. Clamped at 0.
    """
    now = now or datetime.now(timezone.utc)
    deltas = Counter()
    for dt, e in events:
        if dt > now:
            continue
        b = _bucket(dt)
        t = e.get("event_type")
        if consumes_workable(e):
            deltas[b] += 1
        elif t in SUPPLY_IN_TYPES:
            deltas[b] -= 1

    oldest = anchors[0][0]
    proxy = {}
    # walk back segment by segment: (prev_anchor, this_anchor]
    segments = list(zip(anchors[:-1], anchors[1:]))
    for (a_dt, a_val), (b_dt, b_val) in segments:
        cur = b_val
        b = _bucket(b_dt)
        seg_start = _bucket(a_dt)
        while b > seg_start:
            proxy[b] = max(0, cur)
            cur = max(0, cur + deltas.get(b, 0))
            b -= timedelta(minutes=WINDOW_MIN)
        # the anchor bucket itself carries the hard value
        proxy[seg_start] = a_val
    # Launch-implied floor: a browser_launched event proves workable
    # supply >= 1 in its bucket (the lane only launches leads that passed
    # every workability gate). The walk can understate where supply entered
    # through paths with no per-lead event (batch materials builds); the
    # floor corrects the boolean without inventing levels.
    for dt, e in events:
        if dt > now:
            continue
        if e.get("event_type") == "browser_launched":
            b = _bucket(dt)
            if b in proxy and proxy[b] is not None:
                proxy[b] = max(1, proxy[b])
    # buckets older than the oldest anchor: unknown
    b = _bucket(oldest) - timedelta(minutes=WINDOW_MIN)
    start = _bucket(now - timedelta(hours=ROLLING_HOURS))
    while b >= start:
        proxy[b] = None
        b -= timedelta(minutes=WINDOW_MIN)
    return proxy


def _bucket(dt):
    m = (dt.minute // WINDOW_MIN) * WINDOW_MIN
    return dt.replace(minute=m, second=0, microsecond=0)


# ---------------------------------------------------------------- buckets


def bucket_metrics(events, windows, supply_proxy, now=None):
    """Per-10-min bucket metrics over the rolling window."""
    now = now or datetime.now(timezone.utc)
    start = _bucket(now - timedelta(hours=ROLLING_HOURS))
    buckets = {}
    b = start
    while b <= _bucket(now):
        buckets[b] = {
            "launches": 0, "submissions": 0, "briefs": 0, "consuming": 0,
            "gates": Counter(), "supply_proxy": supply_proxy.get(b, None),
            "supply_signal": False, "busy_min": 0.0,
        }
        b += timedelta(minutes=WINDOW_MIN)

    for dt, e in events:
        if dt < start or dt > now:
            continue
        b = _bucket(dt)
        if b not in buckets:
            continue
        bk = buckets[b]
        t = e.get("event_type")
        if t == "browser_launched":
            bk["launches"] += 1
        elif t == "submitted":
            bk["submissions"] += 1
        elif t == "brief_built":
            bk["briefs"] += 1
        elif t in ("gate_blocked", "gate_encountered"):
            g = gate_of(e)
            if g:
                bk["gates"][g] += 1
        if t in SUPPLY_IN_TYPES or t == "lead_verified":
            bk["supply_signal"] = True
        # A browser launch proves workable supply >= 1 in this bucket —
        # the lane only launches leads that passed every workability gate.
        if t == "browser_launched":
            bk["supply_signal"] = True
        if consumes_workable(e):
            bk["consuming"] += 1

    for s, e_ in windows:
        b = _bucket(max(s, start))
        while b < min(e_, now):
            if b in buckets:
                seg_end = min(b + timedelta(minutes=WINDOW_MIN), e_, now)
                seg_start = max(b, s)
                buckets[b]["busy_min"] += max(
                    0.0, (seg_end - seg_start).total_seconds() / 60.0)
            b += timedelta(minutes=WINDOW_MIN)

    for b, bk in buckets.items():
        idle = WINDOW_MIN - min(WINDOW_MIN, bk["busy_min"])
        bk["idle_min"] = round(idle, 1)
        supply_ok = (bk["supply_proxy"] or 0) >= 1 or bk["supply_signal"]
        bk["supply_known"] = bk["supply_proxy"] is not None
        bk["expensive_idle_min"] = round(idle, 1) if (idle > 0 and supply_ok) else 0.0
        bk["idle_unknown_supply_min"] = (
            round(idle, 1) if (idle > 0 and not supply_ok and bk["supply_known"]) else 0.0
        )
    return buckets


def stall_windows(buckets):
    """30-min windows with loop activity but zero launches and zero
    submissions while supply was evidenced -> stall events."""
    bs = sorted(buckets)
    stalls = []
    for i in range(0, len(bs) - 2):
        trio = [buckets[bs[i + k]] for k in range(3)]
        activity = sum(b["briefs"] for b in trio) + sum(
            sum(b["gates"].values()) for b in trio)
        launches = sum(b["launches"] for b in trio)
        subs = sum(b["submissions"] for b in trio)
        supply = max((b["supply_proxy"] or 0) for b in trio)
        idle = sum(b["idle_min"] for b in trio)
        if activity >= 2 and launches == 0 and subs == 0 and supply >= 1 and idle > 0:
            stalls.append({
                "window_start": bs[i].isoformat(),
                "idle_min": round(idle, 1),
                "activity_events": activity,
                "supply_proxy": supply,
                "top_gates": _top_gates(trio, 3),
            })
    return stalls


def feeder_dry_periods(events, buckets, now=None):
    """feeder_empty events and how long the lane stayed dry after each."""
    now = now or datetime.now(timezone.utc)
    empties = [(dt, e) for dt, e in events
               if gate_of(e) == "feeder_empty"]
    periods = []
    for dt, e in empties:
        end = now
        for dt2, e2 in events:
            if dt2 <= dt:
                continue
            t2 = e2.get("event_type")
            if t2 in ("browser_launched", "submitted") or t2 in SUPPLY_IN_TYPES:
                end = dt2
                break
            if dt2 - dt > timedelta(hours=6):
                end = dt + timedelta(hours=6)  # cap: still dry, stop looking
                break
        # Fail-closed: never claim dry longer than the feeder's own 6h
        # re-fire horizon without a second feeder_empty as evidence.
        end = min(end, dt + timedelta(hours=6), now)
        periods.append({
            "ts": dt.isoformat(),
            "dry_min": round((end - dt).total_seconds() / 60.0, 1),
            "ready_count": (e.get("details") or {}).get("ready_count"),
        })
    return periods


def drain_vs_submit(buckets):
    """Hourly: workable-READY consumed (submissions + provable workable
    consumption: prescreen parks) vs submissions. Rates only with
    denominator >= MIN_N."""
    hours = defaultdict(lambda: {"consumed": 0, "submitted": 0})
    for b, bk in sorted(buckets.items()):
        h = b.replace(minute=0)
        hours[h]["submitted"] += bk["submissions"]
        hours[h]["consumed"] += bk["submissions"] + bk.get("consuming", 0)
    rows = []
    for h in sorted(hours):
        c, s = hours[h]["consumed"], hours[h]["submitted"]
        rows.append({
            "hour": h.isoformat(),
            "consumed": c,
            "submitted": s,
            "submit_share": (round(s / c, 3) if c >= MIN_N
                             else "insufficient outcome data"),
        })
    return rows


def _top_gates(trio, n):
    c = Counter()
    for b in trio:
        c.update(b["gates"])
    return [{"gate": g, "count": n_} for g, n_ in c.most_common(n)]


# ---------------------------------------------------------------- attribution


def attribute_idle(buckets, events=None):
    """Attribute each expensive-idle bucket to its dominant edge-case class.
    Buckets with no gate signal are split by lane-activity evidence:
      - unattributed_post_outcome: a launch/submission in the prior 30 min
        (cooldown/handoff gap after an outcome),
      - unattributed_pre_launch: next lane activity within 60 min
        (scheduling gap before the next launch),
      - unattributed_dead_air: no lane activity within +/-60 min (true dead
        time with supply evidenced — the real waste).
    Returns ranked [(class, idle_minutes, bucket_count)].
    """
    activity = []
    if events:
        activity = sorted(
            dt for dt, e in events
            if e.get("event_type") in ("browser_launched", "submitted"))

    def split_unattributed(b):
        if not activity:
            return "unattributed"
        if any(b - timedelta(minutes=30) <= a <= b for a in activity):
            return "unattributed_post_outcome"
        if any(b < a <= b + timedelta(minutes=60) for a in activity):
            return "unattributed_pre_launch"
        if not any(b - timedelta(minutes=60) <= a <= b + timedelta(minutes=60)
                   for a in activity):
            return "unattributed_dead_air"
        return "unattributed"

    totals = Counter()
    nbuckets = Counter()
    for b, bk in buckets.items():
        mins = bk["expensive_idle_min"]
        if mins <= 0:
            continue
        cls_counts = Counter()
        for g, n_ in bk["gates"].items():
            cls = edge_class_of_gate(g)
            if cls:
                cls_counts[cls] += n_
        if cls_counts:
            dom = cls_counts.most_common(1)[0][0]
        else:
            dom = split_unattributed(b)
        totals[dom] += mins
        nbuckets[dom] += 1
    ranked = [(cls, round(totals[cls], 1), nbuckets[cls])
              for cls in sorted(totals, key=lambda c: -totals[c])]
    return ranked


def ledger_submitted_total():
    try:
        rows = json.load(open(LEDGER))
        rows = rows if isinstance(rows, list) else rows.get("rows", [])
        return sum(1 for r in rows if r.get("status") == "SUBMITTED")
    except Exception:
        return None


# ---------------------------------------------------------------- report


def build_report(events=None, entries=None, now=None):
    now = now or datetime.now(timezone.utc)
    events = events if events is not None else load_events()
    entries = entries if entries is not None else load_queue()
    current = supply_now(entries)
    anchors = supply_anchors(events, now, current)
    windows = browser_windows(events, now=now)
    supply_proxy = reconstruct_supply(events, anchors, now=now)
    buckets = bucket_metrics(events, windows, supply_proxy, now=now)

    total_idle = round(sum(b["idle_min"] for b in buckets.values()), 1)
    total_expensive = round(sum(b["expensive_idle_min"] for b in buckets.values()), 1)
    total_unknown = round(sum(b["idle_unknown_supply_min"] for b in buckets.values()), 1)
    busy_min = round(sum(b["busy_min"] for b in buckets.values()), 1)
    stalls = stall_windows(buckets)
    # stall windows slide every 10 min and overlap; report unique minutes too
    stall_buckets = set()
    for s in stalls:
        bs = datetime.fromisoformat(s["window_start"])
        for k in range(3):
            stall_buckets.add(bs + timedelta(minutes=WINDOW_MIN * k))
    stall_unique_idle = round(
        sum(buckets[b]["idle_min"] for b in stall_buckets if b in buckets), 1)
    dry = feeder_dry_periods(events, buckets, now=now)
    drain = drain_vs_submit(buckets)
    ranked = attribute_idle(buckets, events)
    stall_idle = round(sum(s["idle_min"] for s in stalls), 1)

    drivers = [
        {"edge_class": cls, "idle_min_attributed": mins,
         "buckets": nb,
         "share_of_expensive": (round(mins / total_expensive, 3)
                                if total_expensive else 0.0)}
        for cls, mins, nb in ranked
    ]

    rep = {
        "generated_utc": now.isoformat(),
        "window_hours": ROLLING_HOURS,
        "bucket_minutes": WINDOW_MIN,
        "browser": {
            "busy_min": busy_min,
            "idle_min": total_idle,
            "expensive_idle_min": total_expensive,
            "idle_unknown_supply_min": total_unknown,
            "utilization": (round(busy_min / (busy_min + total_idle), 3)
                            if (busy_min + total_idle) else "insufficient outcome data"),
        },
        "supply": {
            "workable_ready_now": current,
            "anchors": [{"ts": dt.isoformat(), "workable": v}
                        for dt, v in anchors],
            "proxy_note": ("Historical workable-READY is a reverse-"
                           "reconstructed UPPER BOUND between hard supply "
                           "anchors (exact current count; feeder_empty with "
                           "ready_count=0). Buckets older than the oldest "
                           "anchor have unknown supply and are never counted "
                           "as expensive idle."),
        },
        "stalls": {"count": len(stalls), "idle_min": stall_idle,
                   "unique_idle_min": stall_unique_idle,
                   "windows": stalls},
        "feeder_dry": {"count": len(dry), "periods": dry},
        "drain_vs_submit_hourly": drain,
        "downtime_drivers_ranked": drivers,
        "ledger_submitted_total": ledger_submitted_total(),
        "telemetry_events_scanned": len(events),
    }
    rep["top5_proposed_fixes"] = proposed_fixes(rep["downtime_drivers_ranked"])
    return rep


def render_markdown(rep):
    L = []
    L.append("# Downtime vs Edge-Case Analysis")
    L.append("")
    L.append(f"Generated (UTC): {rep['generated_utc']} | "
             f"window: rolling {rep['window_hours']}h in "
             f"{rep['bucket_minutes']}-min buckets | "
             f"events scanned: {rep['telemetry_events_scanned']}")
    L.append("")
    b = rep["browser"]
    L.append("## Browser lane")
    L.append(f"- Busy: **{b['busy_min']} min** | idle: **{b['idle_min']} min** "
             f"| utilization: **{b['utilization']}**")
    L.append(f"- Expensive idle (lane idle while supply evidenced): "
             f"**{b['expensive_idle_min']} min**")
    L.append(f"- Idle with unknown supply (not claimed as expensive): "
             f"**{b['idle_unknown_supply_min']} min**")
    L.append(f"- Workable-READY right now: **{rep['supply']['workable_ready_now']}**")
    L.append("")
    s = rep["stalls"]
    L.append(f"## Stalls: {s['count']} windows, {s['unique_idle_min']} unique "
             f"idle min (loop active, zero launches, supply evidenced)")
    for w in s["windows"][:10]:
        gates = ", ".join(f"{g['gate']}x{g['count']}" for g in w["top_gates"])
        L.append(f"- {w['window_start']}: {w['idle_min']} idle min, "
                 f"activity={w['activity_events']}, top gates: {gates or 'none'}")
    L.append("")
    d = rep["feeder_dry"]
    L.append(f"## Feeder dry events: {d['count']}")
    for p in d["periods"]:
        L.append(f"- {p['ts']}: dry {p['dry_min']} min "
                 f"(ready_count={p['ready_count']})")
    L.append("")
    L.append("## Downtime drivers (edge-case classes by idle minutes attributed)")
    for i, dr in enumerate(rep["downtime_drivers_ranked"][:8], 1):
        share = dr["share_of_expensive"]
        share_s = f"{share:.1%}" if isinstance(share, float) else share
        L.append(f"{i}. **{dr['edge_class']}** — {dr['idle_min_attributed']} min "
                 f"({share_s} of expensive idle, {dr['buckets']} buckets)")
    L.append("")
    L.append("## Hourly drain vs submissions")
    for row in rep["drain_vs_submit_hourly"]:
        L.append(f"- {row['hour']}: consumed={row['consumed']} "
                 f"submitted={row['submitted']} submit_share={row['submit_share']}")
    L.append("")
    L.append(f"Ledger SUBMITTED total (all time): {rep['ledger_submitted_total']}")
    L.append("")
    L.append("## Sanctioned-path fix proposals (not implemented — owner approval required)")
    for f in rep["top5_proposed_fixes"]:
        L.append(f"- **{f['edge_class']}** ({f['idle_min_attributed']} idle min): "
                 f"{f['proposed_fix']}")
    L.append("")
    L.append("")
    L.append("_Method: reverse-reconstructed supply UPPER BOUND between hard "
             "anchors (exact current workable-READY; feeder_empty "
             "ready_count=0 pins). Buckets older than the oldest anchor have "
             "unknown supply and never count as expensive idle. See module "
             "docstring for fail-closed rules._")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- fixes (proposals only)


def proposed_fixes(drivers):
    """Sanctioned-path fix proposals per top driver. Proposals only —
    nothing is wired by this module."""
    catalog = {
        "prescreen_parks": (
            "Extend prescreen.py to detect sourcing/'how did you hear' "
            "questions with no Other-equivalent and park pre-launch (burns "
            "browser launches today). Tests on recorded form-intel fixtures; "
            "SOC log + FIM re-baseline."),
        "travel_gates": (
            "Keep the relocation/travel standing-auth as a first-class "
            "answer_bank entry so travel-gated leads re-verify instead of "
            "re-parking; add a 'travel-auth-on-file' prescreen rule with "
            "unit tests."),
        "lever_preflight": (
            "Cache lever_preflight refusal verdicts per board+question-set "
            "with a cooldown so the same refusal doesn't burn a cycle "
            "repeatedly; tests on recorded fixtures."),
        "technique_blocked": (
            "Record technique_blocked patterns into edge_case_registry and "
            "pre-skip those employers at selection time instead of at the "
            "browser step; tests with mocked registry."),
        "materials": (
            "Run the materials-build sweep on the same cadence as discovery "
            "merges so READY leads never sit material-less (the census3x "
            "pattern); alert when workable-READY diverges from raw READY."),
        "inflight_cap": (
            "No fix needed: inflight_cap now aggregates per cycle and is a "
            "correctness gate, not downtime — keep monitoring its share."),
        "dead_postings": (
            "verify_retry already demotes dead postings; ensure dead "
            "detection runs before packet build, not after, to avoid wasted "
            "briefs."),
        "stale_inflight": (
            "Shipped (ARM 79): null-timestamp markers can never be called "
            "stale. Monitor marker_timestamp_missing diagnostics."),
        "ashby_anti_spam": (
            "Add IP-reputation backoff rule: no auto-resubmit on "
            "outcome-unknown after anti-spam flags; park with evidence and "
            "surface. Record via the ATS radar path."),
        "eligibility_dedupe": (
            "Dedupe earlier: check ledger/blocklist at discovery staging so "
            "duplicate candidates never reach selection cycles."),
        "unattributed": (
            "Idle with no gate signal — investigate via launch-packet "
            "timestamps and cron run logs before proposing a fix."),
        "unattributed_post_outcome": (
            "Cooldown/handoff gap after a lane outcome. Consider tightening "
            "the apply_loop cadence or pre-building the next packet while "
            "the browser finishes the current application."),
        "unattributed_pre_launch": (
            "Scheduling gap before the next launch. The 15-min apply_loop "
            "cadence plus packet-build time is the structural floor; "
            "measure packet-build latency before proposing changes."),
        "unattributed_dead_air": (
            "True dead time: lane idle, supply evidenced, no lane activity "
            "within the hour. Highest-value target — check whether the "
            "apply_loop cron actually ran (scheduler body caching caused "
            "stale runs before) and whether prescreen silently skipped the "
            "whole READY pool."),
    }
    out = []
    for dr in drivers[:5]:
        cls = dr["edge_class"]
        out.append({"edge_class": cls,
                    "idle_min_attributed": dr["idle_min_attributed"],
                    "proposed_fix": catalog.get(cls, "No cataloged fix; investigate.")})
    return out


# ---------------------------------------------------------------- main


def main(argv):
    out_dir = argv[1] if len(argv) > 1 and argv[1] != "--out" else OUT_DIR
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 < len(argv):
            out_dir = argv[i + 1]
    os.makedirs(out_dir, exist_ok=True)

    rep = build_report()
    # top5_proposed_fixes already included by build_report()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    base = f"downtime-edge-{stamp}"
    json_path = os.path.join(out_dir, base + ".json")
    md_path = os.path.join(out_dir, base + ".md")
    json.dump(rep, open(json_path, "w"), indent=2)
    open(md_path, "w").write(render_markdown(rep))
    for suffix, src in ((".json", json_path), (".md", md_path)):
        dst = os.path.join(out_dir, "downtime-edge-latest" + suffix)
        with open(src) as fsrc, open(dst, "w") as fdst:
            fdst.write(fsrc.read())

    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    for dr in rep["downtime_drivers_ranked"][:3]:
        print(f"SURFACE: {dr['edge_class']} cost "
              f"{dr['idle_min_attributed']} expensive-idle min "
              f"({dr['buckets']} buckets)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
