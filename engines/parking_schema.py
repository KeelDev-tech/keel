#!/usr/bin/env python3
"""Contract-first schema for parked queue entries (ADOPTION 3 of 5).

Parked entries must carry the CONVENTIONAL fields the classifiers read
(verify_retry.is_verify_only scans `unresolved` / `status_reason` /
`gate_note` / `queue_notes`; the input tray digest renders from the same
fields):

  - `unresolved`: non-empty list of non-empty blocker strings
  - `status_reason`: non-empty string (never a stale "promoted READY" note
    that would let verify_retry resurrect a genuinely input-blocked lead)
  - `gate_note` or `queue_notes`: at least one of them a non-empty string
    or a list holding at least one non-empty string (sweep workers
    sometimes write queue_notes as a list; the schema tolerates both,
    mirroring the inflight_marker precedent)

Parking writers (verify_retry's form_park park-write region and
input_resolution/apply.py's park-mutation path) assert this schema BEFORE
the queue write. On violation the write is refused, a `contract_violation`
telemetry event names the violation, and the lead stays in its current
queue state -- fail-closed: a lead is never lost and never silently
mis-parked.
"""

PARKING_SCHEMA_VERSION = 1


class ParkingSchemaViolation(Exception):
    """Raised by assert_park_entry when the entry breaks the schema."""


def _nonempty_str(v):
    return isinstance(v, str) and bool(v.strip())


def _nonempty_note(v):
    """Note fields (gate_note/queue_notes) tolerate str or list-of-str.

    2026-09-19 (blackboard J-20260920-0355-gate-3406): sweep workers
    sometimes write queue_notes as a list, and input_resolution's
    qn_with_notes deliberately preserves that form -- so a schema checker
    that demands a plain string fires `note_required` on a lead that has
    perfectly good notes (observed: recurring contract_violation on
    SHIPBOB-SENIOR-MERCHANT-IMPLEMENTATION-MANAGER-REMOTE-CA-INC-20260913,
    every ~6h, parking_schema v1 note_required). The 2026-09-17
    inflight_marker precedent (AGENTS.md: "Any queue-notes writer/reader
    should tolerate list-or-str") says the reader must tolerate both, so
    the checker does: a list counts as non-empty when it holds at least
    one non-blank string.
    """
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, list):
        return any(isinstance(n, str) and n.strip() for n in v)
    return False


def validate_park_entry(entry):
    """Return the list of schema violations for a parked entry dict.

    Empty list = valid.
    """
    violations = []
    if not isinstance(entry, dict):
        return ["rule=entry_type: park entry must be a dict, got %s"
                % type(entry).__name__]
    un = entry.get("unresolved")
    if not isinstance(un, list) or not un:
        violations.append(
            "rule=unresolved_required: 'unresolved' must be a non-empty "
            "list of blocker strings")
    elif not all(_nonempty_str(u) for u in un):
        violations.append(
            "rule=unresolved_required: every 'unresolved' item must be a "
            "non-empty string")
    if not _nonempty_str(entry.get("status_reason")):
        violations.append(
            "rule=status_reason_required: 'status_reason' must be a "
            "non-empty string (stale verify text resurrects parked leads)")
    if not (_nonempty_note(entry.get("gate_note"))
            or _nonempty_note(entry.get("queue_notes"))):
        violations.append(
            "rule=note_required: at least one of 'gate_note'/'queue_notes' "
            "must be a non-empty string")
    return violations


def assert_park_entry(entry):
    """Raise ParkingSchemaViolation naming the schema version and the
    missing/invalid field(s). No-op when the entry is valid."""
    violations = validate_park_entry(entry)
    if violations:
        raise ParkingSchemaViolation(
            "parking_schema v%d: %s"
            % (PARKING_SCHEMA_VERSION, " | ".join(violations)))
