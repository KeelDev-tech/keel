#!/usr/bin/env python3
"""Evidence gate: only claims with sources count.

Public, portable version. Keeps a JSON decision log of every submission
claim (verified / pending / unevidenced) so dashboard numbers are always
backed by quoted evidence. Fail-closed: anything unparseable is treated
as unverified, never as proven.

Paths resolve through keel_paths; nothing is hardcoded to a workspace.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINES = os.path.dirname(_HERE)
if _ENGINES not in sys.path:
    sys.path.insert(0, _ENGINES)
from keel_paths import DATA, TELEMETRY  # noqa: E402
from safe_io import aware_time, digest, loads, utc_now  # noqa: E402

LEDGER_PATH = os.path.join(DATA, "ledger", "application-ledger.json")
DECISIONS_PATH = os.path.join(DATA, "state", "evidence-gate-decisions.json")
EVENTS_PATH = os.path.join(TELEMETRY, "events.jsonl")

STATUS_VERIFIED = "verified"     # claim backed by evidence
STATUS_PENDING = "pending"       # claim awaiting evidence (24h window)
STATUS_UNEVIDENCED = "unevidenced"  # claim with no evidence after window

QUOTE_PAT = re.compile(r'"([^"]{8,})"')
PENDING_WINDOW_H = 24


def load_ledger(path=LEDGER_PATH):
    """Ledger rows; tolerant of list, {"rows": [...]}, {"applications": [...]}."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return []
    if isinstance(data, dict):
        return data.get("rows", data.get("applications", []))
    return data if isinstance(data, list) else []


def _scan_ledger_for_quote(events_path, claim_ts, role_id, now=None):
    """Look for a submitted event with a quoted confirmation for role_id
    inside the evidence window (claim_ts .. claim_ts + 24h), additionally
    bounded by the evaluation time: when `now` is given, the event must
    also fall inside (now - 24h .. now), so stale evidence cannot verify
    a claim evaluated later."""
    try:
        with open(events_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None
    claim_dt = None
    try:
        claim_dt = datetime.fromisoformat(str(claim_ts).replace("Z", "+00:00"))
    except Exception:
        pass
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("event_type") != "submitted" or e.get("role_id") != role_id:
            continue
        det = e.get("details") or {}
        conf = det.get("confirmation") or det.get("evidence") or ""
        m = QUOTE_PAT.search(str(conf))
        if m:
            if claim_dt is not None:
                try:
                    ev_dt = datetime.fromisoformat(
                        str(e.get("ts", "")).replace("Z", "+00:00"))
                    hours = (ev_dt - claim_dt).total_seconds() / 3600
                    if not (0 <= hours <= PENDING_WINDOW_H):
                        continue
                    if now is not None:
                        age_h = (now - ev_dt).total_seconds() / 3600
                        if not (0 <= age_h <= PENDING_WINDOW_H):
                            continue
                except Exception:
                    continue
            return m.group(1)
    return None


def find_submitted_rows(rows):
    return [r for r in rows if str(r.get("status", "")).upper() == "SUBMITTED"]


def record_gate_decision(role_id, company, claim, evidence, ts=None,
                         path=DECISIONS_PATH):
    """Append one decision record. Atomic-ish via tmp+rename."""
    rec = {
        "role_id": role_id,
        "company": company,
        "claim": claim,
        "evidence": evidence,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = []
    if not isinstance(data, list):
        data = []
    data.append(rec)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
    return rec


def load_decisions(path=DECISIONS_PATH):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


def load_gate_events(events_path=EVENTS_PATH):
    out = []
    try:
        with open(events_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if isinstance(e, dict) and e.get("event_type") in (
                        "gate_encountered", "gate_blocked", "gate_cleared"):
                    out.append(e)
    except FileNotFoundError:
        pass
    return out


def coverage(rows, events_path=EVENTS_PATH, *, now=None):
    """(verified, pending, unevidenced) counts over SUBMITTED rows.

    verified: a submitted telemetry event with a quoted confirmation
      inside the 24h claim window. pending: submitted event exists but
      no quoted confirmation yet. unevidenced: no submitted event at all.

    When `now` is explicitly given, it bounds the evaluation: a quoted
    confirmation or submitted event older than 24h before `now` no
    longer counts (stale evidence cannot verify a claim evaluated
    later). When `now` is omitted, only the 24h claim window applies.
    """
    explicit_now = now
    now = now or utc_now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now requires an explicit timezone")
    window_start = now - timedelta(hours=PENDING_WINDOW_H)
    verified = pending = unevidenced = 0
    for r in find_submitted_rows(rows):
        rid = r.get("role_id", "")
        quote = _scan_ledger_for_quote(
            events_path, r.get("date_submitted", ""), rid,
            now=explicit_now)
        if quote:
            verified += 1
            continue
        has_event = False
        try:
            with open(events_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if (e.get("event_type") == "submitted"
                            and e.get("role_id") == rid):
                        if explicit_now is not None:
                            # Stale-event bound only when evaluating at
                            # an explicit time.
                            try:
                                ev_dt = datetime.fromisoformat(
                                    str(e.get("ts", "")).replace("Z", "+00:00"))
                            except Exception:
                                continue
                            if ev_dt.tzinfo is None:
                                ev_dt = ev_dt.replace(tzinfo=timezone.utc)
                            if not (window_start <= ev_dt <= now):
                                continue
                        has_event = True
                        break
        except FileNotFoundError:
            pass
        if has_event:
            pending += 1
        else:
            unevidenced += 1
    return verified, pending, unevidenced


# --- Receipt-intake grading (ported from the evolution line) ---
#
# coverage_report() grades local submission claims against locally observed
# or manually attested receipts. It never authenticates a provider: provider
# acceptance requires an explicitly injected, authenticating host adapter
# (provider_validators). Required by tests/test_receipt_intake_evolution.py.

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


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Evidence gate: count only evidenced submission claims.")
    ap.add_argument("--ledger", default=LEDGER_PATH)
    ap.add_argument("--events", default=EVENTS_PATH)
    ap.add_argument("--decisions", default=DECISIONS_PATH)
    ap.add_argument("--receipts",
                    help="Local observed/manual receipts; never configures provider trust")
    args = ap.parse_args(argv)

    if args.receipts:
        report = coverage_report(load_ledger(args.ledger), args.events,
                                 receipts_path=args.receipts)
        print(json.dumps({**report, "pass": False}, indent=1))
        return 1

    rows = load_ledger(args.ledger)
    verified, pending, unevidenced = coverage(rows, args.events)
    total = verified + pending + unevidenced
    print(json.dumps({
        "submitted_rows": total,
        "verified": verified,
        "pending": pending,
        "unevidenced": unevidenced,
        "gate": "verified > 0 and unevidenced == 0",
        "pass": verified > 0 and unevidenced == 0,
    }, indent=1))
    return 0 if (verified > 0 and unevidenced == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
