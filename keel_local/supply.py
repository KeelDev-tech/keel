"""Supply health contracts. No browser, queue, scheduler or network operations.

READY is a queue count, not proof of posting liveness or execution permission.
The live owner must supply actionable counts using its current verifier rules.
"""
from datetime import datetime, timezone
from .contracts import ContractError, integer, number, timestamp

READY_FLOOR = 5
CHECK_SECONDS = 30
SPAWN_THROTTLE_SECONDS = 600
HEARTBEAT_SECONDS = 60
MAX_OBSERVATION_AGE_SECONDS = 90
# Gate-level liveness bound for supply_alert_reason only (ARM 1, 2026-09-19).
# Measured from 24h of pool_health telemetry (1120 heartbeats): median
# interval 64.8s, p99 203.5s, 42/1119 intervals (3.8%) exceeded 90s --
# including a sustained 90-96s run and isolated 115-229s jitter spikes.
# The 90s event-contract bound fired supply_unverified:refresh_observation on
# every such spike while the guardian was alive (pulse-488: observation aged
# 115s by gate time; 10 of 22 digests on 2026-09-19 carried the reason), i.e.
# phantom FANOUTs the bypass rationale ("flatlined supply with unchanged
# files") was never meant to cover. The 12 silences >180s were genuine
# multi-minute outages (415-1523s, no other guardian events); all but the
# 203-229s self-recovering edge cases exceed 240s. So: jitter up to 240s
# stays VERIFIED; a truly stopped guardian FANOUTs once its silence crosses
# 4 min -- inside the ~4-5 min dead-guardian requirement. Residual
# UNVERIFIED exposure at this bound is ~69 min/day, all inside real outages.
SUPPLY_STALENESS_FANOUT_SECONDS = 240
SOURCE = "pool-guardian"
REASONS = {
    "HEALTHY": "pool healthy",
    "BUFFER_FULL_SUPPLY_STARVED": "buffer full, supply starved",
    "POOL_LOW_NO_ACTIONABLE": "pool low but nothing actionable (all in cooldown)",
    "REFILL_REQUIRED": "pool low with actionable supply",
}


def aware(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ContractError("now must be an aware datetime")
    return value.astimezone(timezone.utc)


def classify_pool(ready, actionable):
    integer(ready, "ready"); integer(actionable, "actionable")
    if ready >= READY_FLOOR:
        state = "HEALTHY" if actionable > 0 else "BUFFER_FULL_SUPPLY_STARVED"
    else:
        state = "REFILL_REQUIRED" if actionable > 0 else "POOL_LOW_NO_ACTIONABLE"
    return {"state": state, "reason": REASONS[state], "healthy": state == "HEALTHY",
            "ready": ready, "actionable": actionable, "ready_floor": READY_FLOOR}


def should_fire(ready, actionable, *, elapsed_since_spawn, verifier_running=False):
    """Reference decision contract, not a replacement for an unseen live guardian.

    elapsed_since_spawn=None means a confirmed absence of previous spawns.
    A running verifier, <600s since spawning, or a backward clock prevents fire.
    No task is spawned by this function.
    """
    health = classify_pool(ready, actionable)
    if type(verifier_running) is not bool:
        raise ContractError("verifier_running must be an explicit bool")
    if elapsed_since_spawn is not None:
        number(elapsed_since_spawn, "elapsed_since_spawn", minimum=-float("inf"))
    if health["state"] != "REFILL_REQUIRED":
        return False, health["reason"]
    if verifier_running:
        return False, "verifier already running"
    if elapsed_since_spawn is not None and elapsed_since_spawn < SPAWN_THROTTLE_SECONDS:
        return False, "spawn throttled"
    return True, health["reason"]


def unverified(reason):
    return {"schema_version": 1, "state": "UNVERIFIED", "reason": reason,
            "healthy": False, "ready": None, "actionable": None,
            "ready_floor": READY_FLOOR, "observed_at": None}


def pool_health(ready, actionable, *, observed_at, now,
                max_age_seconds=MAX_OBSERVATION_AGE_SECONDS):
    """Freshness contract for one observation. The gate's liveness bound is
    wider: pass max_age_seconds=SUPPLY_STALENESS_FANOUT_SECONDS there. The
    90s default stays the event contract used by the guardian observer."""
    aware(now)
    try:
        observed = timestamp(observed_at)
        age = (now - observed).total_seconds()
        if not 0 <= age <= max_age_seconds:
            return unverified("pool observation is stale or future-dated")
        return {"schema_version": 1, **classify_pool(ready, actionable),
                "observed_at": observed.astimezone(timezone.utc).isoformat()}
    except (ValueError, TypeError):
        return unverified("pool observation is missing or invalid")


def health_from_event(event, *, now, max_age_seconds=MAX_OBSERVATION_AGE_SECONDS):
    """Recompute health; a logged 'healthy' string alone never establishes it.

    max_age_seconds defaults to the 90s event contract; the fan-out gate
    passes SUPPLY_STALENESS_FANOUT_SECONDS so heartbeat jitter on a live
    guardian does not read as a dead supply."""
    aware(now)
    if type(event) is not dict or event.get("event_type") != "pool_health" or event.get("source") != SOURCE:
        return unverified("no pool-health observation")
    details = event.get("details")
    if type(details) is not dict:
        return unverified("invalid pool-health event")
    if type(details.get("schema_version")) is not int or details["schema_version"] != 1 or details.get("ready_floor") != READY_FLOOR:
        return unverified("unsupported pool-health contract")
    result = pool_health(details.get("ready"), details.get("actionable"),
                         observed_at=details.get("observed_at"), now=now,
                         max_age_seconds=max_age_seconds)
    if result["state"] != "UNVERIFIED" and (details.get("state") != result["state"] or details.get("healthy") is not result["healthy"]):
        return unverified("pool-health event contradicts its counts")
    return result


def supply_alert_reason(snapshot, *, now):
    """Used BEFORE the pulse's unchanged-file shortcut, including stale data.

    Liveness uses SUPPLY_STALENESS_FANOUT_SECONDS (240s), not the 90s event
    contract: the gate asks "is the guardian alive", not "is this observation
    fresh enough to classify". A jittered-but-alive guardian (observation
    90-240s old, e.g. pulse-488's 115s case) returns None here; a guardian
    silent 240s+ still FANOUTs supply_unverified within ~4-5 min of stopping.
    The unchanged-file bypass rationale is preserved: a flatlined supply --
    missing, or stale past 240s -- fans out even when no watched file moved."""
    health = snapshot.get("pool_supply") if type(snapshot) is dict else None
    if type(health) is not dict:
        return "supply_unverified:missing_observation"
    checked = health_from_event({"event_type": "pool_health", "source": SOURCE,
                                 "details": health}, now=now,
                                max_age_seconds=SUPPLY_STALENESS_FANOUT_SECONDS)
    if checked["state"] == "UNVERIFIED":
        return "supply_unverified:refresh_observation"
    if checked["actionable"] == 0:
        return "supply_starved:actionable=0:AUDIT_REFILL_SUPPLY"
    return None


class PoolHealthObserver:
    """Transition + 60s heartbeat through the existing append-only logger.

    One instance per guardian process. An emit failure propagates and does not
    advance its watermark. A restart can repeat a heartbeat; not exactly once.
    Call on EVERY 30s check, before should_fire or its early returns.

    Transition debounce (J-20260918-1140-feed-2061): a state-signature change
    must be observed on DEBOUNCE_SAMPLES consecutive checks before a
    "transition" is emitted. A single-sample spike -- a torn queue read, or a
    clobbered watermark that makes a same-state sample look new -- can never
    become a transition; it stays pending and is cleared if the next sample
    reverts. While pending, heartbeats continue on the last CONFIRMED state.
    Real flips confirm one check (~30s) later; guardian decisions are
    unaffected (they never consume the observer).
    """
    DEBOUNCE_SAMPLES = 2

    def __init__(self, logger):
        self.logger = logger
        self.last_signature = None
        self.last_health = None
        self.last_emitted = None
        self.pending_signature = None
        self.pending_count = 0

    def observe(self, ready, actionable, *, observed_at, now):
        health = pool_health(ready, actionable, observed_at=observed_at, now=now)
        signature = (health["state"], health["reason"])
        elapsed = None if self.last_emitted is None else (now - self.last_emitted).total_seconds()

        if signature == self.last_signature:
            # Matches the confirmed state: clear any pending candidate and
            # heartbeat on the normal cadence.
            self.pending_signature, self.pending_count = None, 0
            self.last_health = health
            due = elapsed is None or elapsed < 0 or elapsed >= HEARTBEAT_SECONDS
            if due:
                receipt = self.logger("pool_health", source=SOURCE,
                                      details={**health, "observation_kind": "heartbeat"})
                if type(receipt) is not dict or receipt.get("event_type") != "pool_health":
                    raise RuntimeError("pool-health logger did not acknowledge the append")
                self.last_emitted = now
            return {**health, "event_emitted": due}

        # Signature differs from the confirmed state: debounce. Only a
        # consecutive repeat of the new signature confirms a transition.
        if signature == self.pending_signature:
            self.pending_count += 1
        else:
            self.pending_signature, self.pending_count = signature, 1
        if self.pending_count >= self.DEBOUNCE_SAMPLES:
            receipt = self.logger("pool_health", source=SOURCE,
                                  details={**health, "observation_kind": "transition"})
            if type(receipt) is not dict or receipt.get("event_type") != "pool_health":
                raise RuntimeError("pool-health logger did not acknowledge the append")
            self.last_signature, self.last_emitted = signature, now
            self.last_health = health
            self.pending_signature, self.pending_count = None, 0
            return {**health, "event_emitted": True}

        # Pending: one sample of the new signature, not yet confirmed.
        # Heartbeat on the last confirmed state if due; never publish the
        # unconfirmed sample.
        due_hb = (self.last_health is not None
                  and (elapsed is None or elapsed < 0 or elapsed >= HEARTBEAT_SECONDS))
        if due_hb:
            receipt = self.logger("pool_health", source=SOURCE,
                                  details={**self.last_health,
                                           "observation_kind": "pending_heartbeat"})
            if type(receipt) is not dict or receipt.get("event_type") != "pool_health":
                raise RuntimeError("pool-health logger did not acknowledge the append")
            self.last_emitted = now
            return {**health, "event_emitted": True, "pending": True}
        return {**health, "event_emitted": False, "pending": True}
