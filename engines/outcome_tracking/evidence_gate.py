"""Grade local submission claims without pretending to authenticate a provider.

Quotes, hashes and caller-supplied flags are evidence to review, not proof of
provider acceptance. Local receipts support review and manual attestation;
provider acceptance requires an explicitly injected, authenticating host adapter.
"""
import argparse
from collections import defaultdict
from datetime import timedelta
import json
import os
import sys

_ENGINES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ENGINES not in sys.path:
    sys.path.insert(0, _ENGINES)
from keel_paths import DATA, TELEMETRY
from safe_io import read_json, loads, rows, atomic_json, file_lock, aware_time, utc_now, digest

LEDGER_PATH = os.path.join(DATA, "application-ledger.json")
DECISIONS_PATH = os.path.join(DATA, "state", "evidence-gate-decisions.json")
EVENTS_PATH = os.path.join(TELEMETRY, "events.jsonl")
STATUS_VERIFIED, STATUS_PENDING, STATUS_UNEVIDENCED = "verified", "pending", "unevidenced"
PENDING_WINDOW_H = 24


def load_ledger(path=LEDGER_PATH):
    """Read supported row envelopes strictly; corrupt data never means empty."""
    return rows(read_json(path, missing=[]))


def find_submitted_rows(records):
    return [record for record in rows(records) if str(record.get("status", "")).upper()
            in {"SUBMITTED", "SUBMISSION_CLAIMED"}]


def events(events_path=EVENTS_PATH):
    """Stream strict JSON objects with a per-event size bound.

    A malformed stream raises, so aggregation cannot silently call incomplete
    evidence complete. A missing file yields no events.
    """
    try:
        stream = open(events_path, "rb")
    except FileNotFoundError:
        return
    with stream:
        while True:
            line = stream.readline(256 * 1024 + 1)
            if not line:
                break
            if len(line) > 256 * 1024:
                raise ValueError("event exceeds 256 KiB")
            if line.strip():
                event = loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event must be an object")
                yield event


def load_gate_events(events_path=EVENTS_PATH):
    return [event for event in events(events_path) if event.get("event_type") in
            {"gate_encountered", "gate_blocked", "gate_cleared"}]


def record_gate_decision(role_id, company, claim, evidence, ts=None, path=DECISIONS_PATH):
    """Append a review decision; it cannot certify provider acceptance."""
    record = {"role_id": role_id, "company": company, "claim": claim, "evidence": evidence,
              "ts": ts or utc_now().isoformat(), "verification_status": "UNVERIFIED"}
    aware_time(record["ts"])
    with file_lock(os.fspath(path) + ".lock"):
        data = read_json(path, missing=[])
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("decision log must be an array of objects")
        data.append(record)
        atomic_json(path, data)
    return record


def load_decisions(path=DECISIONS_PATH):
    return rows(read_json(path, missing=[]))


def _identity(value):
    return value if isinstance(value, str) and value and value == value.strip() else None


def coverage_report(records, events_path=EVENTS_PATH, *, now=None, receipts_path=None,
                    provider_validators=None):
    """Count distinct claims and grade their local, unverified correlations.

    Exact role/attempt IDs are required when an attempt is present. Legacy
    role-and-time correlation is explicitly unverified. Future/unknown times,
    conflicting claim or event identities and corrupt streams cannot correlate.
    Pending expires after 24 hours even when quoted text is present.
    """
    now = now or utc_now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now requires an explicit timezone")
    groups, attempt_roles, warnings = defaultdict(list), defaultdict(set), []
    for index, record in enumerate(find_submitted_rows(records)):
        role, attempt = _identity(record.get("role_id")), _identity(record.get("attempt_id"))
        key = (role, attempt) if role else (None, index)
        groups[key].append(record)
        if attempt and role:
            attempt_roles[attempt].add(role)
    event_records, by_role, seen_events = [], defaultdict(list), {}
    complete = os.path.isfile(events_path)
    if not complete:
        warnings.append("Telemetry unavailable")
    try:
        for event in events(events_path):
            eid = event.get("event_id")
            if eid is not None:
                if not _identity(eid):
                    raise ValueError("invalid event identifier")
                hashed = digest(event)
                if eid in seen_events:
                    if seen_events[eid] != hashed:
                        raise ValueError("conflicting event identifier")
                    continue
                seen_events[eid] = hashed
            if event.get("event_type") in {"submitted", "submission_claimed"}:
                event_records.append(event)
        for event in event_records:
            rid = _identity(event.get("role_id"))
            if rid:
                by_role[rid].append(event)
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        complete = False
        by_role.clear()
        warnings.append("Telemetry incomplete or malformed: " + type(exc).__name__)
    from outcome_tracking.receipt_intake import ReceiptStore, grade_claim
    receipt_snapshot, receipts_complete = [], receipts_path is not None
    if receipts_path is not None:
        try:
            if not os.path.isfile(receipts_path):
                raise ValueError("receipt store unavailable")
            receipt_snapshot = ReceiptStore(receipts_path).snapshot()
        except (OSError, ValueError, TypeError) as exc:
            receipts_complete = False
            warnings.append("Receipt intake unavailable or malformed: " + type(exc).__name__)
    pending = unevidenced = correlated = verified = observed = manual = held_receipts = 0
    for key, duplicates in groups.items():
        record = duplicates[0]
        role, attempt = _identity(record.get("role_id")), _identity(record.get("attempt_id"))
        if not role or any(other != record for other in duplicates) or (attempt and len(attempt_roles[attempt]) != 1):
            unevidenced += 1
            warnings.append("Claim identity missing or conflicting; reconciliation required")
            continue
        if record.get("attempt_id") not in (None, "") and attempt is None:
            unevidenced += 1
            warnings.append("Claim attempt identifier is invalid")
            continue
        try:
            claim = aware_time(record.get("date_submitted", record.get("ts")))
        except (ValueError, TypeError, OverflowError):
            unevidenced += 1
            warnings.append("Claim timestamp unavailable or invalid")
            continue
        grade = grade_claim(record, receipt_snapshot, now=now, validators=provider_validators)
        observed += int(grade["observed"])
        manual += int(grade["manual_attested"])
        held_receipts += int(grade["held"])
        warnings.extend(grade["warnings"])
        if grade["provider_verified"]:
            verified += 1
            continue
        matched = False
        for event in by_role.get(role, []):
            details = event.get("details") or {}
            if not isinstance(details, dict):
                continue
            outer, inner = event.get("attempt_id"), details.get("attempt_id")
            if outer and inner and outer != inner:
                continue
            event_attempt = outer or inner
            if attempt and event_attempt != attempt:
                continue
            try:
                stamp = aware_time(event.get("ts"))
                matched |= claim <= stamp <= min(now, claim + timedelta(hours=PENDING_WINDOW_H))
            except (ValueError, TypeError, OverflowError):
                continue
        correlated += int(matched)
        if matched and 0 <= (now - claim).total_seconds() <= PENDING_WINDOW_H * 3600:
            pending += 1
        else:
            unevidenced += 1
    return {"submission_claims": len(groups), "verified": verified, "pending": pending,
            "unevidenced": unevidenced, "correlated_unverified": correlated,
            "observed_receipts": observed, "manual_attested": manual,
            "receipt_claims_held": held_receipts, "receipts_complete": receipts_complete,
            "verification_available": bool(provider_validators) and receipts_complete,
            "verification_supported": True,
            "verification_reason": ("Explicit host provider validators supplied; acceptance requires a successful bound validation"
                                    if provider_validators else "No authenticated provider receipt adapter is configured"),
            "correlation_scope": "local role/attempt/time metadata only; not proof of acceptance",
            "telemetry_complete": complete, "warnings": sorted(set(warnings))}


def coverage(records, events_path=EVENTS_PATH, *, now=None):
    report = coverage_report(records, events_path, now=now)
    return report["verified"], report["pending"], report["unevidenced"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", default=LEDGER_PATH)
    parser.add_argument("--events", default=EVENTS_PATH)
    parser.add_argument("--decisions", default=DECISIONS_PATH)
    parser.add_argument("--receipts", help="Local observed/manual receipts; never configures provider trust")
    args = parser.parse_args(argv)
    try:
        if not os.path.isfile(args.ledger):
            raise ValueError("ledger unavailable")
        report = coverage_report(load_ledger(args.ledger), args.events, receipts_path=args.receipts)
    except (OSError, ValueError, TypeError) as exc:
        report = {"submission_claims": None, "verified": None, "pending": None,
                  "unevidenced": None, "verification_available": False,
                  "warnings": ["Ledger unavailable or malformed: " + type(exc).__name__]}
    print(json.dumps({**report, "pass": False}, indent=2))
    return 1


if __name__ == "__main__":
    sys.exit(main())
