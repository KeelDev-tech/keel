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

In-flight classification (classify_inflight): IN-FLIGHT markers split
into three buckets —
  fresh:      ageable marker, marked within STALE_AFTER_HOURS -> browser
              probably busy.
  stale:      ageable marker, older than STALE_AFTER_HOURS -> anomaly,
              never auto-reset.
  unknown_ts: no parseable timestamp at all (missing status_updated AND
              missing/unparseable in_flight_at, or a naive timestamp that
              cannot be safely compared). ARM 79: NULL aged as infinitely
              old here and produced the Vercel false-stale_inflight.
              Fail-closed — a marker we cannot age is never called stale.
              Timestamp-less markers log a marker_timestamp_missing
              diagnostic (deduped per role_id) and count toward "browser
              busy" so the watchdog never fires verify_retry --live on a
              marker it cannot age.

Honest fed-signal (workable): raw READY counts test artifacts and
material-less leads (2026-09-15: 7/8 READY were FIRETEST rows with no
resumes). The floor check uses workable() — a READY lead the lane can
actually work right now: APPLY band, employer not on the operator's
blocklist (data/employer-blocklist.md), tailored resume on disk, posting
URL present. Liveness is NOT checked here (the browser re-verifies at
task time).

Logic:
  - workable READY >= READY_FLOOR (2): nothing to do. Silent, read-only.
  - workable READY < 2 and no fresh/unknown-ts IN-FLIGHT (browser idle):
    run `verify_retry.py --live --limit 25` on the pending-verification
    pool. That worker owns verification; it is NOT reimplemented here.
    Then recount.
  - Still below floor afterwards: log one gate_encountered event
    (gate=feeder_empty) so the 30-min ping surfaces it as blocked.
    Deduped: at most one feeder_empty event per DEDUPE_HOURS.
  - Stale IN-FLIGHT markers: one gate_encountered event per marker
    (gate=stale_inflight), deduped the same way.
  - UNKNOWN-timestamp IN-FLIGHT markers: one gate_encountered event per
    marker (gate=marker_timestamp_missing), deduped per role_id — the
    orchestrator owns the timestamp backfill, never this script.

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
import queue_entries  # noqa: E402 — shared queue-entry loader

from keel_paths import HOME as PIPE  # noqa: E402
QUEUE = os.path.join(PIPE, "data", "queues", "standard-queue.json")
EVENTS = os.path.join(PIPE, "data", "telemetry", "events.jsonl")
# Operator-owned employer blocklist (setup.sh writes a starter file at this
# path). The real production blocklist is personal data and is never
# committed — populate this file with your own never-apply employers, one
# per line, plain text.
BLOCKLIST = os.path.join(PIPE, "data", "employer-blocklist.md")

READY_STATUSES = ("READY", "READY-FOR-BROWSER")
READY_FLOOR = 2
STALE_AFTER_HOURS = 2
DEDUPE_HOURS = 6
VERIFY_LIMIT = 25
VERIFY_TIMEOUT = 600
# Diagnostic gate for IN-FLIGHT markers that cannot be aged (ARM 79).
MARKER_TIMESTAMP_MISSING = "marker_timestamp_missing"


def load_queue():
    d = json.load(open(QUEUE))
    return queue_entries.load_queue_entries(d)


def _blocklist_txt():
    """Operator blocklist as lowercase text; missing file -> empty (no-op)."""
    try:
        return open(BLOCKLIST).read().lower()
    except FileNotFoundError:
        return ""


def workable(entry):
    """A READY lead the lane can actually work right now — APPLY band,
    employer not blocklisted, tailored resume on disk, posting URL present.
    Liveness is NOT checked here (the browser re-verifies at task time);
    this is the honest 'fed' signal. 2026-09-15: 7 of 8 READY leads were
    FIRETEST artifacts with no materials — raw READY count said 'fed'
    while the lane was starving."""
    if (entry.get("status") or "").upper() not in READY_STATUSES:
        return False
    if entry.get("action_band") != "APPLY":
        return False
    company = entry.get("company") or ""
    if company and company.lower() in _blocklist_txt():
        return False
    m = (entry.get("materials") or {}).get("resume")
    if not m or not os.path.exists(os.path.join(PIPE, m)):
        return False
    if not (entry.get("ats_url") or entry.get("application_url")):
        return False
    return True


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
    return _gate_event_recent(gate, None, within_hours)


def recent_gate_event_for_role(gate, role_id, within_hours):
    """Role-scoped variant: True if an event with this gate name was logged
    for this role_id within the window. Used for per-marker diagnostics
    (marker_timestamp_missing) so one broken marker can't spam, while a
    different broken marker still gets its own diagnostic."""
    return _gate_event_recent(gate, role_id, within_hours)


def _gate_event_recent(gate, role_id, within_hours):
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
                if role_id is not None and ev.get("role_id") != role_id:
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


def inflight_ts(e):
    """Marking-time of an IN-FLIGHT entry: status_updated first, then the
    in_flight_at stamp the orchestrator writes at marking time (ARM 22)."""
    return (parse_updated(e.get("status_updated"))
            or parse_updated(e.get("in_flight_at")))


def is_inflight_status(status):
    """IN-FLIGHT or a two-lane test marker (IN-FLIGHT-LANE-A/B).

    2026-09-15: the authorized two-lane test marks queue entries
    IN-FLIGHT-LANE-A / IN-FLIGHT-LANE-B. Both count as in-flight so the
    feeder never mistakes a live lane for an idle one.
    """
    s = (status or "").upper()
    return s == "IN-FLIGHT" or s.startswith("IN-FLIGHT-LANE-")


def classify_inflight(items, now=None):
    """Split IN-FLIGHT entries into (fresh, stale, unknown_ts).

    fresh: ageable marker, marked within STALE_AFTER_HOURS -> browser
        probably busy.
    stale: ageable marker, older than STALE_AFTER_HOURS -> anomaly, never
        auto-reset.
    unknown_ts: no parseable timestamp at all (missing status_updated AND
        missing/unparseable in_flight_at, or a naive timestamp that cannot
        be safely compared). ARM 79: NULL aged as infinitely old here and
        produced the Vercel false-stale_inflight. Fail-closed — a marker we
        cannot age is never called stale.
    """
    now = now or datetime.now(timezone.utc)
    fresh, stale, unknown_ts = [], [], []
    for e in items:
        if not is_inflight_status(e.get("status")):
            continue
        ts = inflight_ts(e)
        if ts is None or ts.tzinfo is None:
            unknown_ts.append(e)
        elif now - ts < timedelta(hours=STALE_AFTER_HOURS):
            fresh.append(e)
        else:
            stale.append(e)
    return fresh, stale, unknown_ts


def main():
    dry_run = "--dry-run" in sys.argv
    items = load_queue()
    ready = [e for e in items
             if (e.get("status") or "").upper() in READY_STATUSES]
    # Honest fed-signal: READY leads the lane can actually work right now.
    # Raw READY counts test artifacts and material-less leads (2026-09-15:
    # 7/8 READY were FIRETEST rows with no resumes).
    workable_ready = [e for e in ready if workable(e)]
    now = datetime.now(timezone.utc)
    inflight = [e for e in items
                if is_inflight_status(e.get("status"))]
    fresh, stale, unknown_ts = classify_inflight(inflight, now)

    print(f"READY: {len(ready)} (floor {READY_FLOOR}) | "
          f"workable: {len(workable_ready)} | "
          f"IN-FLIGHT fresh: {len(fresh)}, stale: {len(stale)}, "
          f"unknown-ts: {len(unknown_ts)}")

    for e in unknown_ts:
        # ARM 79: a marker we cannot age is NEVER called stale. Log the
        # diagnostic for the orchestrator to backfill the timestamp;
        # treat the marker as evidence the browser may be busy so we
        # don't fire verify_retry --live over a slot we can't see.
        rid = e.get("role_id", "")
        print(f"  UNKNOWN-TS IN-FLIGHT: {rid} "
              f"(status_updated={e.get('status_updated')!r}, "
              f"in_flight_at={e.get('in_flight_at')!r}) — not classified "
              f"stale; browser slot treated as possibly busy")
        if (not dry_run
                and not recent_gate_event_for_role(
                    MARKER_TIMESTAMP_MISSING, rid, DEDUPE_HOURS)):
            log_event.log("gate_encountered", role_id=rid,
                          company=e.get("company", ""), ats="",
                          source="feeder_watchdog",
                          details={"gate": MARKER_TIMESTAMP_MISSING,
                                   "status_updated": e.get("status_updated"),
                                   "in_flight_at": e.get("in_flight_at"),
                                   "note": "IN-FLIGHT marker has no "
                                           "parseable timestamp; cannot age "
                                           "it, so it is NOT called stale. "
                                           "Orchestrator should backfill "
                                           "status_updated/in_flight_at."})

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

    if len(workable_ready) >= READY_FLOOR:
        print("lane fed — nothing to do")
        return 0

    # UNKNOWN-timestamp markers count as possibly-busy: firing verify_retry
    # over a browser slot we cannot age would be the same class of error
    # as the false-stale (ARM 79).
    busy = fresh + unknown_ts
    if busy:
        print("browser appears busy (fresh or unageable IN-FLIGHT) — not "
              "triggering verify_retry; READY refill can wait")
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
    workable_after = [e for e in ready_after if workable(e)]
    print(f"READY after verify_retry: {len(ready_after)} "
          f"(workable {len(workable_after)})")
    if len(workable_after) < READY_FLOOR:
        if not recent_gate_event("feeder_empty", DEDUPE_HOURS):
            log_event.log(
                "gate_encountered", role_id="", company="", ats="",
                source="feeder_watchdog",
                details={"gate": "feeder_empty",
                         "ready_count": len(ready_after),
                         "workable_count": len(workable_after),
                         "note": "workable READY below floor after "
                                 "verify_retry --live; discovery may need "
                                 "to produce new leads, or materials-build "
                                 "may be lagging READY leads without resumes"})
            print("logged gate_encountered feeder_empty")
        else:
            print("feeder_empty already logged recently — not duplicating")
    return 0


if __name__ == "__main__":
    sys.exit(main())
