#!/usr/bin/env python3
"""Pulse state-snapshot builder.

Builds the state-snapshot.json the fan-out gate reads: queue counts
(pinned from the queue files, never overwritten by telemetry) plus the
pool_supply observation replayed from the latest valid pool-guardian
heartbeat.

Replay contract:
  - The latest contract-valid pool_health event from source
    "pool-guardian" is replayed when it is within
    SUPPLY_STALENESS_FANOUT_SECONDS (240s); the writer and the gate
    share this liveness bound.
  - A numeric event_age_seconds is added when a valid event exists.
  - The live pool state (_live_pool_state) is a cross-check ONLY: when
    replayed READY differs from live READY, a skew_note is added, but
    the replayed values are NEVER overwritten with the live cross-check.
    A cross-check failure cannot break snapshot production.
  - With no valid event, pool_supply.state is "UNVERIFIED" and neither
    event_age_seconds nor skew_note is present (no explicit nulls).
"""
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from keel_local.supply import (
    READY_FLOOR,
    SUPPLY_STALENESS_FANOUT_SECONDS,
    health_from_event,
)

TELEMETRY = os.path.join(_ROOT, "data", "events.jsonl")
_QUEUE_DIR = os.path.join(_ROOT, "data", "queues")
# Order matters for tests: standard is second.
_QUEUE_FILES = {
    "needs_input": os.path.join(_QUEUE_DIR, "needs_input-queue.json"),
    "standard": os.path.join(_QUEUE_DIR, "standard-queue.json"),
    "strategic": os.path.join(_QUEUE_DIR, "strategic-queue.json"),
    "rejected": os.path.join(_QUEUE_DIR, "rejected-queue.json"),
}


def _entries(queue_name):
    """Load entries for one queue. Patchable by tests."""
    path = _QUEUE_FILES.get(queue_name)
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (FileNotFoundError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("leads", data.get("entries", []))
    return data if isinstance(data, list) else []


def _api_direct_stats():
    """API-direct lane stats for the snapshot. Patchable by tests."""
    return {}


def _live_pool_state():
    """Live READY cross-check (never overwrites replayed values).
    Patchable by tests."""
    ready = 0
    for entry in _entries("standard") or []:
        if isinstance(entry, dict) and entry.get("status") == "READY":
            ready += 1
    return {"ready": ready}


def _latest_valid_pool_health(now):
    """(event, health, age_seconds) for the latest valid heartbeat.

    Returns (None, None, None) when no contract-valid pool_health event
    from source pool-guardian falls within the liveness bound.
    """
    latest = None
    try:
        with open(TELEMETRY, "r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if (isinstance(event, dict)
                        and event.get("event_type") == "pool_health"
                        and event.get("source") == "pool-guardian"):
                    latest = event
    except FileNotFoundError:
        return None, None, None
    if latest is None:
        return None, None, None
    health = health_from_event(
        latest, now=now, max_age_seconds=SUPPLY_STALENESS_FANOUT_SECONDS)
    if health.get("state") == "UNVERIFIED":
        return None, None, None
    try:
        observed = datetime.fromisoformat(
            str(health.get("observed_at", "")).replace("Z", "+00:00"))
        age = (now - observed).total_seconds()
    except Exception:
        return None, None, None
    return latest, health, age


def build_snapshot(now=None):
    """Build the pulse state snapshot dict."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    counts = {}
    queues = {}
    for queue_name in _QUEUE_FILES:
        entries = _entries(queue_name) or []
        queues[queue_name] = entries
        counts[queue_name] = len(entries)
    # The READY pool lives in the standard queue; its count is pinned
    # here and never overwritten by the telemetry replay below.
    ready = sum(
        1 for entry in queues.get("standard", [])
        if isinstance(entry, dict) and entry.get("status") == "READY")
    snapshot = {
        "schema_version": 1,
        "observed_at": now.isoformat(),
        "ready": ready,
        "queue_counts": counts,
        "api_direct": _api_direct_stats(),
    }
    _event, health, age = _latest_valid_pool_health(now)
    if health is None:
        snapshot["pool_supply"] = {"state": "UNVERIFIED"}
        return snapshot
    supply = {
        "schema_version": 1,
        "state": health.get("state"),
        "ready": health.get("ready"),
        "actionable": health.get("actionable"),
        "healthy": health.get("healthy"),
        "ready_floor": health.get("ready_floor", READY_FLOOR),
        "observed_at": health.get("observed_at"),
        "event_age_seconds": age,
    }
    # Live cross-check: note the skew, never overwrite the replay.
    # _live_pool_state may return {"ready": n} or a (ready, actionable)
    # tuple; either shape is accepted.
    try:
        live = _live_pool_state()
        if isinstance(live, dict):
            live_ready = live.get("ready")
        elif isinstance(live, (tuple, list)) and live:
            live_ready = live[0]
        else:
            live_ready = None
        if live_ready is not None and live_ready != supply["ready"]:
            supply["skew_note"] = (
                f"replayed READY={supply['ready']} differs from live "
                f"READY={live_ready}; replayed values kept")
    except Exception:
        pass
    snapshot["pool_supply"] = supply
    return snapshot


def main():
    snapshot = build_snapshot()
    print(json.dumps(snapshot, indent=1))


if __name__ == "__main__":
    main()
