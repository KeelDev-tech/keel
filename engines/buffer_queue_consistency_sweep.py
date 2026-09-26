#!/usr/bin/env python3
"""Buffer-queue consistency sweep (AGENTS.md 2026-09-17 standing rule).

After ANY queue park/purge, every buffered launch packet whose queue entry
is not READY/IN-FLIGHT must move to _parked/. The lane must never spawn
from a stale packet; canonical state is the queue, not the buffer.

Read-only by default (dry-run). Pass --live to move files. No queue
writes are made by this script (file moves only); it emits telemetry
packet_shelved events on live moves via log_event.

ARM3 2026-09-17: buffer sweeper ownership (file ownership this cycle).
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import HOME, DATA  # noqa: E402 — repo path convention
from queue_entries import load_queue_entries  # noqa: E402 — shared queue-entry loader

BUFFER = os.path.join(HOME, "hidden_files", "apply-launch-packets", "buffer")
PARKED = os.path.join(HOME, "hidden_files", "apply-launch-packets", "_parked")
QUEUES = {
    "standard": os.path.join(DATA, "queues", "standard-queue.json"),
    "needs_input": os.path.join(DATA, "queues", "needs_input-queue.json"),
    "strategic": os.path.join(DATA, "queues", "strategic-queue.json"),
    "rejected": os.path.join(DATA, "queues", "rejected-queue.json"),
}

READY = {"READY", "READY-FOR-BROWSER"}
# Canonical definition: apply_loop.READY_STATES ("READY", "READY-FOR-BROWSER").
# Kept in sync here deliberately (no import: apply_loop is heavy); a sweep
# that does not recognize READY-FOR-BROWSER shelves live buffered packets.
INFLIGHT_TOKENS = {"IN-FLIGHT", "IN_FLIGHT", "INFLIGHT", "LAUNCHING", "STAGED"}


def is_launchable(status):
    s = str(status or "").upper().strip()
    if s in READY:
        return True
    return any(t in s for t in INFLIGHT_TOKENS)


def load_queue_statuses():
    statuses = {}  # role_id -> (queue_name, status)
    for qname, path in QUEUES.items():
        if not os.path.exists(path):
            continue
        try:
            data = json.load(open(path))
        except Exception as e:
            print(f"WARN: could not read {qname}: {e}", file=sys.stderr)
            continue
        entries = load_queue_entries(data)
        for e in entries:
            rid = e.get("role_id")
            if rid and rid not in statuses:
                statuses[rid] = (qname, e.get("status"))
    return statuses


def role_id_from_packet(fname):
    # launch packets are <role_id>.json; strip one trailing extension
    return os.path.splitext(fname)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="actually move stale packets (default: dry-run)")
    args = ap.parse_args()

    statuses = load_queue_statuses()
    files = sorted(f for f in os.listdir(BUFFER)
                   if os.path.isfile(os.path.join(BUFFER, f))
                   and not f.startswith("."))
    valid, moved, orphan = [], [], []
    for fname in files:
        rid = role_id_from_packet(fname)
        q = statuses.get(rid)
        if q is None:
            orphan.append((fname, "no queue entry"))
            continue
        qname, st = q
        if is_launchable(st):
            valid.append((fname, qname, st))
        else:
            moved.append((fname, qname, st))

    print(f"buffered files: {len(files)}")
    print(f"valid (READY/IN-FLIGHT): {len(valid)}")
    for fname, qname, st in valid:
        print(f"  KEEP {fname} [{qname}:{st}]")
    print(f"stale (move to _parked/): {len(moved)}")
    for fname, qname, st in moved:
        print(f"  SHELVE {fname} [{qname}:{st}]")
    print(f"orphan (no queue entry): {len(orphan)}")
    for fname, why in orphan:
        print(f"  ORPHAN {fname} [{why}]")

    if args.live and moved:
        os.makedirs(PARKED, exist_ok=True)
        import log_event  # append-only telemetry
        for fname, qname, st in moved:
            rid = role_id_from_packet(fname)
            src = os.path.join(BUFFER, fname)
            dst = os.path.join(PARKED, fname)
            shutil.move(src, dst)
            log_event.log(
                "gate_blocked", role_id=rid, company="",
                source="buffer_queue_consistency_sweep",
                details={"gate": "packet_shelved",
                         "reason": "buffer-queue consistency sweep: entry not READY/IN-FLIGHT",
                         "queue": qname, "queue_status": st,
                         "moved_to": "_parked/"})
            print(f"  MOVED {fname} -> _parked/")
    elif not args.live:
        print("dry-run: no files moved (pass --live to move)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
