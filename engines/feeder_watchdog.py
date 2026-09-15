#!/usr/bin/env python3
"""Feeder watchdog: keeps the browser application lane fed.

Problem it solves: the browser lane idled on 2026-09-14 behind orphaned
IN-FLIGHT markers and an empty READY queue. This script watches for that
condition and triggers the designed remedy.

Checks (read-only first):
  (a) READY lead count in data/queues/standard-queue.json
      (status in READY / READY-FOR-BROWSER).
  (b) Browser-activity proxy: any IN-FLIGHT entry whose status_updated is
      within STALE_AFTER_HOURS. The queue is the shared state between the
      orchestrator (which marks IN-FLIGHT when it spawns a browser task)
      and this script, so a fresh IN-FLIGHT marker means the browser is
      probably busy. Stale IN-FLIGHT markers are logged as their own
      anomaly — they are NEVER auto-reset here (resetting queue state is
      the orchestrator's judgment call; see the 2026-09-14 incident).

Logic:
  - READY >= READY_FLOOR (2): nothing to do. Silent, read-only.
  - READY < 2 and no fresh IN-FLIGHT (browser idle): run
    `verify_retry.py --live --limit 25` on the pending-verification pool.
    That worker owns verification; it is NOT reimplemented here. Then
    recount READY.
  - Still READY < 2 afterwards: log one gate_encountered event
    (gate=feeder_empty) so the 30-min ping surfaces it as blocked.
    Deduped: at most one feeder_empty event per DEDUPE_HOURS.
  - Stale IN-FLIGHT markers: one gate_encountered event per marker
    (gate=stale_inflight), deduped the same way.

Never invents leads. Never touches the ledger. Never rewrites telemetry
(log_event.py appends only). The only queue writes are verify_retry's own
under --live, which is that worker's designed purpose.

Usage:
    python3 feeder_watchdog.py            # check; act only if the lane is dry
    python3 feeder_watchdog.py --dry-run  # report only, never run verify_retry
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import log_event  # noqa: E402 — append-only telemetry

from keel_paths import HOME as PIPE  # noqa: E402
QUEUE = os.path.join(PIPE, "data", "queues", "standard-queue.json")
EVENTS = os.path.join(PIPE, "data", "telemetry", "events.jsonl")

READY_STATUSES = ("READY", "READY-FOR-BROWSER")
READY_FLOOR = 2
STALE_AFTER_HOURS = 2
DEDUPE_HOURS = 6
VERIFY_LIMIT = 25
VERIFY_TIMEOUT = 600


def load_queue():
    d = json.load(open(QUEUE))
    return d if isinstance(d, list) else d.get("entries", d.get("items", []))


def parse_updated(s):
    """Parse '2026-09-14 21:25 PDT' (or ISO) into an aware datetime."""
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.endswith(" PDT"):
            dt = datetime.strptime(s[:-4], "%Y-%m-%d %H:%M")
            return dt.replace(tzinfo=timezone(timedelta(hours=-7)))
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def recent_gate_event(gate, within_hours):
    """True if a gate_encountered/gate_blocked event with this gate name was
    logged within the window (dedupe so the ping isn't spammed)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    try:
        with open(EVENTS) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event_type") not in ("gate_encountered",
                                                "gate_blocked"):
                    continue
                if (ev.get("details") or {}).get("gate") != gate:
                    continue
                try:
                    ts = datetime.fromisoformat(ev["ts"])
                except (KeyError, ValueError):
                    continue
                if ts >= cutoff:
                    return True
    except FileNotFoundError:
        pass
    return False


def main():
    dry_run = "--dry-run" in sys.argv
    items = load_queue()
    ready = [e for e in items
             if (e.get("status") or "").upper() in READY_STATUSES]
    now = datetime.now(timezone.utc)
    inflight = [e for e in items
                if (e.get("status") or "").upper() == "IN-FLIGHT"]
    fresh, stale = [], []
    for e in inflight:
        ts = parse_updated(e.get("status_updated"))
        (fresh if ts and now - ts < timedelta(hours=STALE_AFTER_HOURS)
         else stale).append(e)

    print(f"READY: {len(ready)} (floor {READY_FLOOR}) | "
          f"IN-FLIGHT fresh: {len(fresh)}, stale: {len(stale)}")

    for e in stale:
        rid = e.get("role_id", "")
        print(f"  STALE IN-FLIGHT: {rid} "
              f"(updated {e.get('status_updated')}) — not auto-resetting")
        if not dry_run and not recent_gate_event("stale_inflight",
                                                 DEDUPE_HOURS):
            log_event.log("gate_encountered", role_id=rid,
                          company=e.get("company", ""), ats="",
                          source="feeder_watchdog",
                          details={"gate": "stale_inflight",
                                   "status_updated": e.get("status_updated"),
                                   "note": "IN-FLIGHT marker older than "
                                           f"{STALE_AFTER_HOURS}h with no "
                                           "fresh activity; orchestrator "
                                           "should inspect before the lane "
                                           "can reuse the browser slot"})

    if len(ready) >= READY_FLOOR:
        print("lane fed — nothing to do")
        return 0

    if fresh:
        print("browser appears busy (fresh IN-FLIGHT) — not triggering "
              "verify_retry; READY refill can wait")
        return 0

    print("lane dry and browser idle — triggering verify_retry --live")
    if dry_run:
        print("dry run — verify_retry NOT executed")
        return 0
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(BASE, "verify_retry.py"),
             "--live", "--limit", str(VERIFY_LIMIT)],
            capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
        print(r.stdout[-1500:] if r.stdout else "")
        if r.returncode != 0:
            print(f"verify_retry exited {r.returncode}: "
                  f"{(r.stderr or '')[-500:]}")
    except subprocess.TimeoutExpired:
        print(f"verify_retry timed out after {VERIFY_TIMEOUT}s")
    except Exception as ex:
        print(f"verify_retry failed to launch: {ex}")

    ready_after = [e for e in load_queue()
                   if (e.get("status") or "").upper() in READY_STATUSES]
    print(f"READY after verify_retry: {len(ready_after)}")
    if len(ready_after) < READY_FLOOR:
        if not recent_gate_event("feeder_empty", DEDUPE_HOURS):
            log_event.log(
                "gate_encountered", role_id="", company="", ats="",
                source="feeder_watchdog",
                details={"gate": "feeder_empty",
                         "ready_count": len(ready_after),
                         "note": "READY queue below floor after "
                                 "verify_retry --live; discovery may need "
                                 "to produce new leads"})
            print("logged gate_encountered feeder_empty")
        else:
            print("feeder_empty already logged recently — not duplicating")
    return 0


if __name__ == "__main__":
    sys.exit(main())
