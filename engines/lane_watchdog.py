#!/usr/bin/env python3
"""Sustainability red-line watchdog (compounding growth loop).

The operation's standing directive is compound growth: every loop's output
must make the next cycle faster, cheaper, or better — never merely linear.
The sustainability matrix (docs/sustainability-matrix.md when shipped)
names the red lines; this script evaluates them against LIVE data and emits
one deduped `sustainability_redline` gate event per 6h per breached line.

Semantics:
  - Red lines live in the policy file (data/.state/sustainability-policy.json,
    copied to the repo from the operator's own pipeline). Each line is
    {"id", "name", "metric", "operator", "threshold", "window_days",
    "freeze"}. Only the metric names in METRICS below are supported;
    anything else is reported unevaluated — never guessed, never
    evaluated by hand-waving.
  - A red line that cannot be evaluated (missing data source) is listed in
    the stdout report as unevaluated with its reason. It never fires.
  - Breach of a line named with "freeze" in its policy emits the gate with
    the freeze action (e.g. RL-2 supply:burn < 1.0 -> "lane expansion
    frozen") so the status ping surfaces the constraint and the feeder
    lane stands down expansion until the line clears.
  - Events go through the single sanctioned logging path (log_event.py);
    one event per red-line id per DEDUPE_HOURS.

Read-only except the dedupe ledger (telemetry itself). Never writes the
queue, ledger, packets, or costs.

Usage:
    python3 lane_watchdog.py             # evaluate; emit deduped events
    python3 lane_watchdog.py --dry-run   # report only, no event writes
    python3 lane_watchdog.py --explain   # print each red line's definition
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import DATA  # noqa: E402 — repo path convention
import log_event  # noqa: E402 — sanctioned telemetry path

POLICY_PATH = os.path.join(DATA, ".state", "sustainability-policy.json")
QUEUES = [os.path.join(DATA, "queues", "standard-queue.json"),
          os.path.join(DATA, "queues", "needs_input-queue.json"),
          os.path.join(DATA, "queues", "strategic-queue.json")]
LEDGER = os.path.join(DATA, "application-ledger.json")
COST_SERIES = os.path.join(DATA, ".state", "cost-series.json")
EVENTS = log_event.EVENTS

GATE = "sustainability_redline"
DEDUPE_HOURS = 6


# ---------------------------------------------------------------------------
# Live data sources
# ---------------------------------------------------------------------------

def _load_entries():
    items = []
    for q in QUEUES:
        try:
            d = json.load(open(q))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        items.extend(d if isinstance(d, list)
                     else d.get("entries", d.get("items", [])))
    return items


def _load_submitted_rows():
    try:
        d = json.load(open(LEDGER))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    rows = d if isinstance(d, list) else d.get("rows", d.get("applications", []))
    return [r for r in rows
            if str(r.get("status", "")).upper() == "SUBMITTED"]


def _sub_date(row):
    for key in ("date_submitted", "submitted_at", "date_confirmed"):
        v = row.get(key)
        if not v:
            continue
        try:
            s = str(v).replace(" PDT", "")
            return datetime.fromisoformat(s if "T" in s
                                          else s.replace(" ", "T", 1)).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Metric functions: name -> callable returning (value, evaluated, reason)
# ---------------------------------------------------------------------------

def _m_ready_supply(entries, rows, window_days):
    n = sum(1 for e in entries
            if str(e.get("status", "")).upper() == "READY")
    return n, True, f"{n} READY leads across queues"


def _m_burn(entries, rows, window_days):
    cutoff = date.today() - timedelta(days=window_days or 7)
    n = sum(1 for r in rows
            if (_sub_date(r) or date.min) >= cutoff)
    return n, True, f"{n} submissions in trailing {window_days or 7}d"


def _m_supply_burn_ratio(entries, rows, window_days):
    supply, _, _ = _m_ready_supply(entries, rows, window_days)
    burn, _, _ = _m_burn(entries, rows, window_days)
    ratio = supply / burn if burn else float("inf")
    note = (f"supply {supply} / burn {burn} = {ratio:.2f}"
            if burn else f"supply {supply}, burn 0 (ratio undefined)")
    return ratio, True, note


def _m_inflight_starving(entries, rows, window_days):
    """IN-FLIGHT markers older than 45m with no browser_launched claim
    (the packet watchdog's starvation definition, evaluated inline so this
    script stays standalone)."""
    claimed = set()
    try:
        with open(EVENTS) as f:
            for line in f:
                if "browser_launched" not in line:
                    continue
                try:
                    rid = json.loads(line).get("role_id")
                except json.JSONDecodeError:
                    continue
                if rid:
                    claimed.add(rid)
    except (FileNotFoundError, OSError):
        return None, False, "telemetry unreadable — unevaluated"
    now = datetime.now(timezone.utc)
    n = 0
    for e in entries:
        if str(e.get("status", "")).upper() not in ("IN-FLIGHT", "INFLIGHT"):
            continue
        if e.get("role_id") in claimed:
            continue
        ts = None
        for key in ("in_flight_at", "status_updated"):
            v = e.get(key)
            if not v:
                continue
            try:
                s = str(v)
                ts = (datetime.strptime(s[:-4], "%Y-%m-%d %H:%M")
                      .replace(tzinfo=timezone(timedelta(hours=-7)))
                      if s.endswith(" PDT")
                      else datetime.fromisoformat(s))
                break
            except ValueError:
                continue
        if ts is None or now - ts >= timedelta(minutes=45):
            n += 1
    return n, True, f"{n} starving IN-FLIGHT markers"


def _m_cost_trend(entries, rows, window_days):
    try:
        series = json.load(open(COST_SERIES))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, False, "cost-series.json missing — unevaluated"
    t = series.get("trend", {})
    return (t.get("slope_units_per_bucket", 0.0), True,
            "cost trend: %s" % t.get("verdict", "unknown"))


def _m_needs_input_depth(entries, rows, window_days):
    n = sum(1 for e in entries
            if str(e.get("status", "")).upper() == "NEEDS_INPUT")
    return n, True, f"{n} leads parked awaiting input"


METRICS = {
    "ready_supply": _m_ready_supply,
    "burn": _m_burn,
    "supply_burn_ratio": _m_supply_burn_ratio,
    "inflight_starving": _m_inflight_starving,
    "cost_trend_slope": _m_cost_trend,
    "needs_input_depth": _m_needs_input_depth,
}


# ---------------------------------------------------------------------------
# Red-line evaluation
# ---------------------------------------------------------------------------

_OPERATORS = {
    "<": lambda v, t: v < t, "<=": lambda v, t: v <= t,
    ">": lambda v, t: v > t, ">=": lambda v, t: v >= t,
    "==": lambda v, t: v == t,
}


def evaluate_line(line, entries, rows):
    """Return {"id", "evaluated", "breached", "value", "detail"}."""
    lid = line.get("id", "?")
    metric = line.get("metric")
    fn = METRICS.get(metric)
    if fn is None:
        return {"id": lid, "evaluated": False, "breached": False,
                "value": None,
                "detail": f"unknown metric '{metric}' — unevaluated, never guessed"}
    op = _OPERATORS.get(line.get("operator", "<"))
    if op is None:
        return {"id": lid, "evaluated": False, "breached": False,
                "value": None,
                "detail": f"unknown operator '{line.get('operator')}'"}
    window = line.get("window_days") or 7
    try:
        value, evaluated, detail = fn(entries, rows, window)
    except Exception as ex:
        return {"id": lid, "evaluated": False, "breached": False,
                "value": None, "detail": f"metric raised ({ex})"}
    if not evaluated:
        return {"id": lid, "evaluated": False, "breached": False,
                "value": None, "detail": detail}
    threshold = line.get("threshold")
    try:
        breached = bool(op(value, threshold))
    except TypeError:
        return {"id": lid, "evaluated": False, "breached": False,
                "value": value,
                "detail": f"value {value!r} not comparable to {threshold!r}"}
    return {"id": lid, "name": line.get("name"), "evaluated": True,
            "breached": breached, "value": value,
            "threshold": threshold, "operator": line.get("operator"),
            "freeze": line.get("freeze"),
            "detail": f"{detail}; line {lid} {line.get('operator')} {threshold}: "
                      f"{'BREACHED' if breached else 'holding'}"}


def recent_gate_event(gate, rid, within_hours):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    try:
        with open(EVENTS) as f:
            for line in f:
                if "sustainability_redline" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                det = ev.get("details") or {}
                if det.get("gate") != gate or det.get("red_line_id") != rid:
                    continue
                try:
                    ts = datetime.fromisoformat(ev.get("ts", ""))
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    return True
    except FileNotFoundError:
        pass
    return False


def emit_breach(res, dry_run):
    details = {"gate": GATE, "red_line_id": res["id"],
               "name": res.get("name"),
               "value": res["value"],
               "operator": res["operator"], "threshold": res["threshold"],
               "freeze": res.get("freeze"),
               "detail": res["detail"],
               "note": ("sustainability red line breached; "
                        + (res["freeze"] or "operator review required"))}
    if dry_run:
        print(f"  dry-run: would emit sustainability_redline for {res['id']}")
        return False
    if recent_gate_event(GATE, res["id"], DEDUPE_HOURS):
        print(f"  {res['id']}: event within {DEDUPE_HOURS}h — deduped")
        return False
    log_event.log("gate_encountered", role_id="", company="",
                  source="lane_watchdog", details=details)
    print(f"  {res['id']}: emitted sustainability_redline")
    return True


def main(argv):
    dry_run = "--dry-run" in argv
    explain = "--explain" in argv
    try:
        policy = json.load(open(POLICY_PATH))
    except FileNotFoundError:
        print(f"lane_watchdog: no policy file at {POLICY_PATH} — "
              f"nothing to evaluate (copy your sustainability-policy.json "
              f"there first)")
        return 0
    except (json.JSONDecodeError, OSError) as ex:
        print(f"lane_watchdog: policy unreadable ({ex})", file=sys.stderr)
        return 2
    lines = policy.get("red_lines", []) if isinstance(policy, dict) \
        else policy
    if explain:
        for line in lines:
            print(f"{line.get('id')}: {line.get('name')}")
            print(f"    metric={line.get('metric')} "
                  f"{line.get('operator')} {line.get('threshold')} "
                  f"over trailing {line.get('window_days', 7)}d; "
                  f"freeze={line.get('freeze')}")
        return 0
    entries = _load_entries()
    rows = _load_submitted_rows()
    results = [evaluate_line(line, entries, rows) for line in lines]
    breached = [r for r in results if r["evaluated"] and r["breached"]]
    uneval = [r for r in results if not r["evaluated"]]
    print(f"evaluated {len(results) - len(uneval)}/{len(results)} red lines "
          f"({len(entries)} queue entries, {len(rows)} submitted rows): "
          f"{len(breached)} breached, {len(uneval)} unevaluated")
    for r in results:
        flag = "BREACH" if r["breached"] else ("UNEVALUATED"
                                               if not r["evaluated"] else "ok")
        print(f"  [{flag}] {r['id']}: {r['detail']}")
    emitted = 0
    for r in breached:
        if emit_breach(r, dry_run):
            emitted += 1
    if dry_run and breached:
        print("dry-run: no events written")
    elif breached and not emitted:
        print("all breaches already evented within the dedupe window")
    return 0


if __name__ == "__main__":
    main(sys.argv[1:])
