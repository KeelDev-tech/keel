#!/usr/bin/env python3
"""Browser-lifecycle poller.

The start/ready/form-start sequence is emitted by browser task agents in
~0% of launches in some environments — latency can't be decomposed, we
fly blind. The loop-side launch watcher cannot observe Chromium internals
from a cron shell: task state lives in the runtime browser task store,
reachable only by an agent turn.

This poller is the emission half. The lane owner (or any agent turn with
DB access) dumps the current browser-task rows to a snapshot file and
runs:

    python3 browser_lifecycle_poll.py --snapshot <rows.json> [--dry-run]

For each task row it:
  1. matches the task to a queue role_id (title/company parse via
     parked_task_sweep.match_role + a time anchor: task created_at within
     60 min of a browser_launched event or IN-FLIGHT marker for that
     role — BOTH must hold, fail closed otherwise);
  2. derives the observable stages from the row:
       browser_task_started — row created_at (exact);
       browser_ready       — row run_started_at (exact, "session running");
       form_started        — first evidence of navigation/work
         (browser_navigation_attempted or step_count > 0). On later polls
         the seen-state file turns this into a real transition timestamp
         (observed between polls); on the first poll for a historical row
         the exact first-navigation instant is NOT recoverable, so the
         event is emitted with observed_at=null and an explicit attested
         note — never an invented timestamp;
  3. dedupes against lifecycle events already in telemetry (a stage is
     never emitted twice for a role);
  4. emits via the sanctioned log_event path with source=
     "browser-lifecycle-poll" and details.observed_via=
     "runtime.browser_tasks".

State: hidden_files/browser-lifecycle-seen.json under the repo home
(per-task last-seen row shape for transition detection).
Default --dry-run: zero writes.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import log_event  # noqa: E402 — sanctioned telemetry path
import parked_task_sweep as sweep  # noqa: E402 — title/role matching
from keel_paths import HOME, DATA  # noqa: E402 — repo path convention

QUEUE = os.path.join(DATA, "queues", "standard-queue.json")
SEEN = os.path.join(HOME, "hidden_files", "browser-lifecycle-seen.json")
SOURCE = "browser-lifecycle-poll"
ANCHOR_MIN = 60   # task created_at must be within this of a launch anchor
STAGES = ("browser_task_started", "browser_ready", "form_started")


def parse_ts(s):
    if not s:
        return None
    s = str(s).strip()
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_snapshot(path):
    with open(path) as f:
        d = json.load(f)
    return d if isinstance(d, list) else d.get("tasks", [])


def load_queue():
    d = json.load(open(QUEUE))
    return d if isinstance(d, list) else d.get("entries", d.get("items", []))


def launch_anchors(events_path=None):
    """role_id -> [browser_launched ts, ...] (repaired: same-role 60-min
    dedupe, first kept). Also folds in queue IN-FLIGHT markers below."""
    path = events_path or log_event.EVENTS
    rows = []
    try:
        with open(path) as f:
            for line in f:
                if "browser_launched" not in line:
                    continue
                try:
                    ev = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if ev.get("event_type") != "browser_launched":
                    continue
                ts = parse_ts(ev.get("ts"))
                if ts and ev.get("role_id"):
                    rows.append((ts, ev["role_id"]))
    except FileNotFoundError:
        pass
    rows.sort(key=lambda r: r[0])
    anchors = {}
    last = {}
    for ts, rid in rows:
        if rid in last and (ts - last[rid]) < timedelta(minutes=ANCHOR_MIN):
            continue
        anchors.setdefault(rid, []).append(ts)
        last[rid] = ts
    return anchors


def queue_anchors(entries):
    """role_id -> [IN-FLIGHT marker ts, ...] from queue state."""
    out = {}
    for e in entries:
        st = str(e.get("status") or "")
        if "IN-FLIGHT" not in st.upper() and not e.get("in_flight"):
            continue
        ts = (parse_ts(e.get("in_flight_at"))
              or parse_ts(e.get("status_updated")))
        if ts and e.get("role_id"):
            out.setdefault(e["role_id"], []).append(ts)
    return out


def anchored(role_id, created, anchors):
    return any(abs((created - a).total_seconds()) / 60 <= ANCHOR_MIN
               for a in anchors.get(role_id, []))


def existing_stages(events_path=None):
    """role_id -> set of lifecycle stages already in telemetry."""
    path = events_path or log_event.EVENTS
    out = {}
    try:
        with open(path) as f:
            for line in f:
                if not any(s in line for s in STAGES):
                    continue
                try:
                    ev = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if ev.get("event_type") in STAGES and ev.get("role_id"):
                    out.setdefault(ev["role_id"], set()).add(
                        ev["event_type"])
    except FileNotFoundError:
        pass
    return out


def load_seen():
    try:
        with open(SEEN) as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def save_seen(seen):
    os.makedirs(os.path.dirname(SEEN), exist_ok=True)
    tmp = SEEN + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seen, f, indent=1)
    os.replace(tmp, SEEN)


def derive_stages(task, prev_seen, now):
    """Return [(stage, observed_at_or_None, note), ...] for the task row.

    observed_at is a real timestamp from the DB row when one exists;
    form_started on a first-seen historical row gets observed_at=None with
    an attested note (the exact instant is not recoverable — never
    invented). On later polls a nav transition between polls is stamped
    at the poll time with a transition note.
    """
    out = []
    created = parse_ts(task.get("created_at"))
    if created:
        out.append(("browser_task_started", created.isoformat(),
                    "row created_at"))
    run_started = parse_ts(task.get("run_started_at"))
    if run_started:
        out.append(("browser_ready", run_started.isoformat(),
                    "row run_started_at (session running)"))
    nav = bool(task.get("browser_navigation_attempted")) or \
        (task.get("step_count") or 0) > 0
    if nav:
        prev = prev_seen.get(task.get("task_id"))
        if prev is None:
            out.append(("form_started", None,
                        "attested on first poll: navigation/work evidence "
                        "present on row; exact first-navigation instant not "
                        "recoverable from DB — not invented"))
        elif not prev.get("nav"):
            out.append(("form_started", now.isoformat(),
                        "transition observed between polls "
                        f"(prev seen {prev.get('seen_at')})"))
    return out


def poll(snapshot_path, dry_run=True, now=None, events_path=None,
         seen_path=None):
    now = now or datetime.now(timezone.utc)
    tasks = load_snapshot(snapshot_path)
    entries = load_queue()
    by_anchor = launch_anchors(events_path)
    for rid, tsl in queue_anchors(entries).items():
        by_anchor.setdefault(rid, []).extend(tsl)
    have = existing_stages(events_path)
    seen = load_seen() if seen_path is None else _load_seen_at(seen_path)
    emitted = []
    skipped = []
    for task in tasks:
        tid = task.get("task_id")
        created = parse_ts(task.get("created_at"))
        if not tid or not created:
            skipped.append((tid, "no task_id/created_at"))
            continue
        role_id, conf = sweep.match_role(task, entries)
        if conf == "none" or not role_id:
            skipped.append((tid, f"no safe role match (conf={conf})"))
            continue
        if not anchored(role_id, created, by_anchor):
            skipped.append((tid, f"no launch anchor within {ANCHOR_MIN}m "
                                 f"for {role_id}"))
            continue
        entry = next((e for e in entries if e.get("role_id") == role_id),
                     {})
        for stage, observed_at, note in derive_stages(task, seen, now):
            if stage in have.get(role_id, set()):
                skipped.append((tid, f"{stage} already in telemetry"))
                continue
            details = {
                "observed_via": "runtime.browser_tasks",
                "task_id": tid,
                "match_confidence": conf,
                "observed_at": observed_at,
                "note": note,
                # Canonical attribution keys. browser_task_id is the
                # exact runtime task id; start_ts is the exact task birth
                # (row created_at) for browser_task_started, else the
                # stage's observed_at. Legacy keys (task_id,
                # observed_at) are kept for backward compatibility with
                # existing consumers.
                "browser_task_id": tid,
                "start_ts": observed_at,
            }
            if not dry_run:
                log_event.log(stage, role_id=role_id,
                              company=entry.get("company") or "",
                              ats=entry.get("ats") or "",
                              source=SOURCE, details=details)
                have.setdefault(role_id, set()).add(stage)
            emitted.append({"task_id": tid, "role_id": role_id,
                            "stage": stage, "observed_at": observed_at,
                            "note": note})
        seen[tid] = {"seen_at": now.isoformat(),
                     "status": task.get("status"),
                     "step_count": task.get("step_count"),
                     "nav": bool(task.get("browser_navigation_attempted")) or
                     (task.get("step_count") or 0) > 0}
    if not dry_run:
        if seen_path is None:
            save_seen(seen)
        else:
            tmp = seen_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(seen, f, indent=1)
            os.replace(tmp, seen_path)
    return {"emitted": emitted, "skipped": skipped,
            "n_tasks": len(tasks), "dry_run": dry_run}


def _load_seen_at(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def main(argv):
    snapshot = None
    dry_run = "--live" not in argv
    i = 0
    while i < len(argv):
        if argv[i] == "--snapshot" and i + 1 < len(argv):
            snapshot = argv[i + 1]; i += 2
        elif argv[i] in ("--dry-run", "--live"):
            i += 1
        else:
            i += 1
    if not snapshot:
        sys.exit("usage: browser_lifecycle_poll.py --snapshot <rows.json> "
                 "[--dry-run|--live]")
    res = poll(snapshot, dry_run=dry_run)
    print(json.dumps(res, indent=1))
    if dry_run:
        print("\nDRY RUN: zero writes (no telemetry, no seen-state).")


if __name__ == "__main__":
    main(sys.argv[1:])
