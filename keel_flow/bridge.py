"""Explicitly compare the two supplied packages at the same 90-second boundary."""
from keel_local.supply import pool_health
from maintenance_workbench.keel_maint.monitoring import supply_health, conformance
from .common import clock, digest

POOL_STATES = {
    "BUFFER_PRESENT_SUPPLY_STARVED": "BUFFER_FULL_SUPPLY_STARVED",
    "BUFFER_LOW_SUPPLY_STARVED": "POOL_LOW_NO_ACTIONABLE",
    "BUFFER_AND_SUPPLY_PRESENT": "HEALTHY",
    "BUFFER_LOW_SUPPLY_PRESENT": "REFILL_REQUIRED",
    "UNVERIFIED": "UNVERIFIED",
}
ATTEMPT_STATES = {"INTENT": "PREPARED", "DISPATCHED": "IN_FLIGHT", "UNKNOWN": "UNKNOWN",
                  "SUBMISSION_CLAIMED": "UNKNOWN", "CANCELLED_BEFORE_DISPATCH": "HELD",
                  "RECONCILIATION_REPORTED": "HELD", "CONFIRMATION_REPORTED": "HELD"}


def compare_supply(observation, *, now):
    clock(now)
    maintenance = supply_health(observation, evaluated_at=now.isoformat(), maximum_age_seconds=90)
    base = pool_health(observation["ready"], observation["actionable"],
                       observed_at=observation["observed_at"], now=now)
    matches = base["state"] == POOL_STATES[maintenance["status"]]
    return {"state": base["state"] if matches else "CONTRACT_MISMATCH", "contracts_agree": matches,
            "supply_recovery": base, "maintenance": maintenance, "maximum_age_seconds": 90}


def inspect_attempts(events):
    # Hash IDs into the maintenance identifier grammar without changing grouping.
    normalized = [{"schema_version": 1, "event_id": digest(row["event_id"]),
                   "attempt_id": digest(row["attempt_id"]), "sequence": row["sequence"],
                   "state": ATTEMPT_STATES[row["state"]], "evidence_revision": row["content_hash"],
                   "observed_at": row["observed_at"]} for row in events]
    return {**conformance(normalized), "mapping": ATTEMPT_STATES,
            "confirmation_reports_mapped_to_completed": False}
