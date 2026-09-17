#!/usr/bin/env python3
"""learning_leak_audit.py — per-cycle learning-loop capture audit.

The reinforcement loop is only as good as its capture: every submission
should leave BOTH a submitted telemetry event AND an evidence-gate record.
This audit measures the capture rate every cycle so the leak stays visible
instead of drifting.

Join design (all counts are measured joins — nothing is fabricated):
  ledger SUBMITTED rows (role_id-bearing)
    -> submitted telemetry events   (join key: role_id)
    -> evidence-gate decisions      (join key: role_id; a row counts as
       evidence-backed when the evidence gate has a verified or pending
       decision for its role_id)

  capture_rate = rows with BOTH telemetry and evidence / SUBMITTED rows.

FLOOR = 0.70 (documented here): below this the loop is losing more than it
keeps. When below the floor the audit emits ONE learning_leak cycle gate
via the canonical log_event logger (gate_encountered, details.gate =
learning_leak, cycle_event=True) — deduped to at most one emission per
24h by scanning recent telemetry (same gate_recent pattern as
lane_watchdog). The event carries the measured numbers, never invented
ones. NOTE: "learning_leak" is not in the install's GATE_TYPES vocabulary;
the logger warns and still records it (fail-safe).

Read-only: never writes the ledger, telemetry, or evidence decisions. The
report goes to <DATA>/hidden_files/outcome-tracking/learning-leak-audit-
<stamp>.json (plus a -latest copy), mirroring learning_join.py.

Usage:
  python3 learning_leak_audit.py            # dry run: report only, no emission
  python3 learning_leak_audit.py --emit     # report + emit learning_leak when below floor
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINES = os.path.dirname(_HERE)
if _ENGINES not in sys.path:
    sys.path.insert(0, _ENGINES)
from keel_paths import DATA, TELEMETRY  # noqa: E402

import log_event  # noqa: E402
from outcome_tracking.evidence_gate import load_decisions  # noqa: E402

LEDGER = os.path.join(DATA, "ledger", "application-ledger.json")
EVENTS = os.path.join(TELEMETRY, "events.jsonl")
OUT_DIR = os.path.join(DATA, "hidden_files", "outcome-tracking")

FLOOR = 0.70
DEDUP_HOURS = 24


def _row_date(r):
    for k in ("date_submitted", "date_prepared", "submitted_at"):
        v = r.get(k)
        if v:
            return str(v)[:10]
    return ""


def load_ledger_submitted(path=None):
    try:
        rows = json.load(open(path or LEDGER))
    except (FileNotFoundError, ValueError):
        return []
    if isinstance(rows, dict):
        rows = rows.get("rows", rows.get("applications", []))
    return [r for r in rows if (r.get("status") or "").upper() == "SUBMITTED"
            and r.get("role_id")]


def load_submitted_telemetry(path=None):
    hits = set()
    try:
        f = open(path or EVENTS)
    except FileNotFoundError:
        return hits
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event_type") == "submitted" and e.get("role_id"):
            hits.add(e["role_id"])
    return hits


def load_evidence_role_ids(path=None):
    """role_ids with >=1 evidence-gate decision (verified or pending)."""
    ids = set()
    for d in load_decisions(path) if path else load_decisions():
        rid = d.get("role_id") if isinstance(d, dict) else None
        if rid:
            ids.add(rid)
    return ids


def _parse_ts(ts):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def gate_recent(gate, within_hours, now, path=None):
    cutoff = now - timedelta(hours=within_hours)
    try:
        f = open(path or EVENTS)
    except FileNotFoundError:
        return False
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event_type") not in ("gate_encountered", "gate_blocked"):
            continue
        if (ev.get("details") or {}).get("gate") != gate:
            continue
        ts = _parse_ts(ev.get("ts"))
        if ts is not None and ts >= cutoff:
            return True
    return False


def audit(ledger_path=None, events_path=None, decisions_path=None):
    rows = load_ledger_submitted(ledger_path)
    tel = load_submitted_telemetry(events_path)
    ev_ids = load_evidence_role_ids(decisions_path)
    per_row = []
    for r in rows:
        rid = r["role_id"]
        t_hit = rid in tel
        e_hit = rid in ev_ids
        per_row.append({"role_id": rid, "date": _row_date(r),
                        "telemetry_hit": t_hit, "evidence_hit": e_hit,
                        "captured": t_hit and e_hit})
    total = len(per_row)
    tel_hits = sum(1 for x in per_row if x["telemetry_hit"])
    ev_hits = sum(1 for x in per_row if x["evidence_hit"])
    captured = sum(1 for x in per_row if x["captured"])
    rate = (captured / total) if total else 1.0
    return {"ts": datetime.now(timezone.utc).isoformat(),
            "submitted_rows": total,
            "telemetry_hits": tel_hits,
            "evidence_hits": ev_hits,
            "captured": captured,
            "capture_rate": round(rate, 4),
            "floor": FLOOR,
            "below_floor": rate < FLOOR,
            "leak_rows": [x["role_id"] for x in per_row if not x["captured"]],
            "rows": per_row}


def main(argv):
    emit = "--emit" in argv
    report = audit()
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = os.path.join(OUT_DIR, f"learning-leak-audit-{stamp}.json")
    with open(out, "w") as f:
        json.dump(report, f, indent=1)
    with open(os.path.join(OUT_DIR, "learning-leak-audit-latest.json"),
              "w") as f:
        json.dump(report, f, indent=1)

    print(f"learning-leak audit {report['ts']}")
    print(f"  submitted ledger rows : {report['submitted_rows']}")
    print(f"  telemetry hits        : {report['telemetry_hits']}")
    print(f"  evidence hits         : {report['evidence_hits']}")
    print(f"  captured (both)       : {report['captured']}")
    print(f"  CAPTURE RATE          : {report['capture_rate']:.1%} "
          f"(floor {report['floor']:.0%})")
    print(f"  below floor           : {report['below_floor']}")
    print(f"  report                : {out}")

    emitted = False
    if report["below_floor"]:
        now = datetime.now(timezone.utc)
        if gate_recent("learning_leak", DEDUP_HOURS, now):
            print(f"  learning_leak suppressed: emitted within {DEDUP_HOURS}h")
        elif emit:
            log_event.log(
                "gate_encountered", role_id="", company="", ats="",
                source="learning-leak-audit",
                details={"gate": "learning_leak",
                         "cycle_event": True,
                         "capture_rate": report["capture_rate"],
                         "floor": report["floor"],
                         "submitted_rows": report["submitted_rows"],
                         "captured": report["captured"],
                         "reason": (
                             f"evidence capture rate "
                             f"{report['capture_rate']:.1%} below documented "
                             f"floor {report['floor']:.0%}: "
                             f"{report['submitted_rows'] - report['captured']}"
                             f"/{report['submitted_rows']} submitted rows "
                             f"lack telemetry and/or evidence-gate records")})
            emitted = True
            print("  learning_leak EMITTED via canonical logger")
        else:
            print("  learning_leak NOT emitted (dry run — pass --emit)")
    return report, emitted


if __name__ == "__main__":
    main(sys.argv[1:])
