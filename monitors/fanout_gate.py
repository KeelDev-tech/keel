#!/usr/bin/env python3
"""Fan-out gate: QUIET vs FANOUT verdict from watched files + health signals.

The gate reads the pulse state snapshot and a watermark of watched-file
hashes. When no watched file changed since the last run, it stays QUIET
(the unchanged-file shortcut) — EXCEPT for starvation signals, which
bypass the shortcut:
  - Supply failures (missing, or stale past the liveness bound)
  - READY floor breach (the pool must never starve)

Supply fallback (2026-09-19): when the snapshot lacks `pool_supply` (the
pulse worker's hand-written abbreviated schema), the gate re-derives the
observation from the pool-guardian heartbeat stream (telemetry
pool_health events, source "pool-guardian").

Verify drought (ARM 90, 2026-09-15): the drought counter resets only on
verify-retry's OWN signals — lead_verified with source == "verify-retry",
promotions to READY (verify-retry gate_cleared on pending_verification /
materials_missing), or verify-retry scan_summary. Other sources never
reset it. Empty pending-verify pool never reports a drought. Sub-5-minute
windows carry the previous counter.

Verify contention (2026-09-17): keyed ONLY off true_concurrent_mutation,
never off the benign scan_apply_derivation_delta.

READY floor (Trent P0, 2026-09-17): below-floor READY fans out with
EMERGENCY_REFILL even when no file moved. Unreadable ready fails toward
breach, never toward silence.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from keel_local.supply import (
    SUPPLY_STALENESS_FANOUT_SECONDS,
    health_from_event,
    supply_alert_reason,
)

# Watched paths (all patchable by tests).
_SNAP_DIR = os.path.join(_ROOT, "hidden_files", "pulse")
SNAPSHOT = os.path.join(_SNAP_DIR, "state-snapshot.json")
WATERMARK = os.path.join(_SNAP_DIR, "fanout-watermark.json")
LEDGER = os.path.join(_ROOT, "data", "application-ledger.json")
QUEUE = os.path.join(_ROOT, "data", "queues", "standard-queue.json")
NEEDS_INPUT = os.path.join(_ROOT, "data", "queues", "needs_input-queue.json")
TELEMETRY = os.path.join(_ROOT, "data", "events.jsonl")
WATCHED_FILES = {
    "ledger": LEDGER,
    "queue": QUEUE,
    "needs_input": NEEDS_INPUT,
    "telemetry": TELEMETRY,
}

# Drought calibration (patchable; production disables uncalibrated droughts).
DROUGHT_CALIBRATED = False
VERIFY_CADENCE_MINUTES = 60
VERIFY_DROUGHT_RUNS = 3
FORCED_SCAN_EVERY = 6
READY_FLOOR = 5
DROUGHT_WINDOW_MINUTES = 5


def sha256_file(path):
    """Hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _latest_pool_health_event(now):
    """Latest contract-valid pool_health event from source pool-guardian."""
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
        return None
    return latest


def resolve_supply_reason(snapshot, now):
    """Supply verdict for the snapshot: None when healthy, else a reason."""
    health = snapshot.get("pool_supply") if isinstance(snapshot, dict) else None
    if isinstance(health, dict):
        return supply_alert_reason(snapshot, now=now)
    event = _latest_pool_health_event(now)
    if event is None:
        return "supply_unverified:missing_observation"
    checked = health_from_event(
        event, now=now, max_age_seconds=SUPPLY_STALENESS_FANOUT_SECONDS)
    if checked["state"] == "UNVERIFIED":
        return "supply_unverified:missing_observation"
    if checked["actionable"] == 0:
        return "supply_starved:actionable=0:AUDIT_REFILL_SUPPLY"
    return None


def ready_floor_reason(snapshot):
    """READY floor breach reason, or None. Unreadable ready breaches."""
    ready = (snapshot or {}).get("ready")
    if isinstance(ready, bool) or not isinstance(ready, int):
        return f"ready_floor_breach:ready={ready}_floor={READY_FLOOR}:EMERGENCY_REFILL"
    if ready < READY_FLOOR:
        return f"ready_floor_breach:ready={ready}_floor={READY_FLOOR}:EMERGENCY_REFILL"
    return None


def scan_telemetry_window(since):
    """Scan telemetry since `since` for verify-retry signals.

    Returns (has_reset_signal, has_promotion, has_scan, contention_sum):
      - has_reset_signal: verify-retry lead_verified, gate_cleared
        (pending_verification/materials_missing), or scan_summary in window
      - has_promotion: verify-retry gate_cleared promotion in window
      - has_scan: verify-retry scan_summary in window
      - contention_sum: sum of true_concurrent_mutation (never the
        derivation delta)
    """
    has_reset = False
    has_promotion = False
    has_scan = False
    contention = 0
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
                if not isinstance(event, dict):
                    continue
                # Parse ts; skip events outside the window.
                try:
                    ts = datetime.fromisoformat(
                        str(event.get("ts", "")).replace("Z", "+00:00"))
                except Exception:
                    continue
                if ts < since:
                    continue
                if event.get("source") != "verify-retry":
                    continue
                etype = event.get("event_type")
                details = event.get("details") or {}
                if etype == "lead_verified":
                    has_reset = True
                elif etype == "gate_cleared":
                    gate = details.get("gate")
                    if gate in ("pending_verification", "materials_missing"):
                        has_reset = True
                        has_promotion = True
                elif etype == "scan_summary":
                    has_reset = True
                    has_scan = True
                    try:
                        contention += int(details.get("true_concurrent_mutation") or 0)
                    except (TypeError, ValueError):
                        pass
    except FileNotFoundError:
        pass
    return (has_reset, has_promotion, has_scan, contention)


def _pending_verify_pool_size():
    """Count PARKED-PENDING-VERIFICATION leads in the queue file."""
    try:
        with open(QUEUE, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (FileNotFoundError, ValueError):
        return 0
    if isinstance(data, dict):
        data = data.get("leads", data.get("entries", []))
    if not isinstance(data, list):
        return 0
    return sum(1 for e in data
               if isinstance(e, dict)
               and e.get("status") == "PARKED-PENDING-VERIFICATION")


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def evaluate(snap, wm, now):
    """Evaluate the snapshot against the watermark.

    Returns (reasons, sig, extra) where extra carries the drought
    counter. Reasons may include supply, floor, drought, and contention
    signals.
    """
    reasons = []
    # Supply health (with heartbeat fallback).
    supply_reason = resolve_supply_reason(snap, now)
    if supply_reason:
        reasons.append(supply_reason)
    # READY floor.
    floor_reason = ready_floor_reason(snap)
    if floor_reason:
        reasons.append(floor_reason)
    # Verify drought.
    drought = int((wm or {}).get("verify_drought_runs", 0) or 0)
    pool_size = _pending_verify_pool_size()
    if DROUGHT_CALIBRATED and pool_size > 0:
        last_run_ts = _parse_ts((wm or {}).get("last_run_ts"))
        if last_run_ts is not None:
            window_min = (now - last_run_ts).total_seconds() / 60
            if window_min >= DROUGHT_WINDOW_MINUTES:
                has_reset, _promo, _scan, _cont = scan_telemetry_window(last_run_ts)
                if has_reset:
                    drought = 0
                else:
                    drought += 1
                if drought >= VERIFY_DROUGHT_RUNS:
                    reasons.append(
                        f"verify_drought:{VERIFY_DROUGHT_RUNS}_runs_no_verification")
            # Sub-window: carry the previous counter, no reason.
        else:
            # No last-run timestamp; count up.
            drought += 1
            if drought >= VERIFY_DROUGHT_RUNS:
                reasons.append(
                    f"verify_drought:{VERIFY_DROUGHT_RUNS}_runs_no_verification")
    elif pool_size == 0:
        drought = 0
    # Verify contention (keyed only off true_concurrent_mutation).
    if DROUGHT_CALIBRATED:
        last_run_ts = _parse_ts((wm or {}).get("last_run_ts"))
        since = last_run_ts if last_run_ts else now - timedelta(minutes=10)
        _r, _p, _s, contention = scan_telemetry_window(since)
        if contention > 0:
            reasons.append(
                f"verify_contention:true_concurrent_mutation_x{contention}")
    sig = {
        "ledger_submitted": (snap or {}).get("ledger_submitted"),
        "ready": (snap or {}).get("ready"),
        "inflight": (snap or {}).get("inflight"),
        "needs_input": (snap or {}).get("needs_input"),
    }
    extra = {"drought": drought}
    return (reasons, sig, extra)


def build_watermark(snapshot, hashes, now, drought=0):
    """Build the watermark dict persisted after a run."""
    return {
        "pulse_count": 1,
        "last_run_ts": now.isoformat(),
        "hashes": dict(hashes),
        "signature": {
            "ledger_submitted": (snapshot or {}).get("ledger_submitted"),
            "ready": (snapshot or {}).get("ready"),
            "inflight": (snapshot or {}).get("inflight"),
            "needs_input": (snapshot or {}).get("needs_input"),
        },
        "known_gates": [],
        "verify_drought_runs": drought,
        "last_verify_retry_signal_ts": now.isoformat(),
    }


def main():
    now = datetime.now(timezone.utc)
    with open(SNAPSHOT, "r", encoding="utf-8") as stream:
        snapshot = json.load(stream)
    try:
        with open(WATERMARK, "r", encoding="utf-8") as stream:
            watermark = json.load(stream)
    except (FileNotFoundError, ValueError):
        watermark = {}
    hashes = {}
    for name, path in WATCHED_FILES.items():
        try:
            hashes[name] = sha256_file(path)
        except (FileNotFoundError, OSError):
            hashes[name] = None
    reasons, _sig, extra = evaluate(snapshot, watermark, now)
    # Floor breach dominates: when the pool is starving, the starvation
    # reason is the signal (supply observation is moot at ready=0).
    floor_reasons = [r for r in reasons if r.startswith("ready_floor_breach:")]
    if floor_reasons:
        reasons = floor_reasons
    # Unchanged-file shortcut: quiet when nothing moved AND no reasons.
    # Supply failures and floor breaches bypass the shortcut.
    old_hashes = (watermark or {}).get("hashes") or {}
    files_changed = any(
        hashes.get(name) != old_hashes.get(name) for name in hashes)
    bypass = any(r.startswith("supply_") or r.startswith("ready_floor_breach:")
                 for r in reasons)
    if not files_changed and not reasons:
        result = {"verdict": "QUIET", "reasons": [], "ts": now.isoformat()}
        print(json.dumps(result))
        raise SystemExit(0)
    if not bypass and not files_changed:
        # No starvation signal and nothing moved: quiet.
        result = {"verdict": "QUIET", "reasons": [], "ts": now.isoformat()}
        print(json.dumps(result))
        raise SystemExit(0)
    watermark = build_watermark(snapshot, hashes, now,
                                drought=extra.get("drought", 0))
    try:
        os.makedirs(os.path.dirname(WATERMARK), exist_ok=True)
        with open(WATERMARK, "w", encoding="utf-8") as stream:
            json.dump(watermark, stream, indent=1)
    except OSError:
        pass
    verdict = "FANOUT" if reasons else "QUIET"
    result = {"verdict": verdict, "reasons": reasons, "ts": now.isoformat()}
    print(json.dumps(result))
    raise SystemExit(0)


if __name__ == "__main__":
    main()
