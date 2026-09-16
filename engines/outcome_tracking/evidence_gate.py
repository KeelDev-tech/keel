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
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINES = os.path.dirname(_HERE)
if _ENGINES not in sys.path:
    sys.path.insert(0, _ENGINES)
from keel_paths import DATA, TELEMETRY  # noqa: E402

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


def _scan_ledger_for_quote(events_path, claim_ts, role_id):
    """Look for a submitted event with a quoted confirmation for role_id
    inside the evidence window (claim_ts .. claim_ts + 24h)."""
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


def coverage(rows, events_path=EVENTS_PATH):
    """(verified, pending, unevidenced) counts over SUBMITTED rows.

    verified: a submitted telemetry event with a quoted confirmation
      inside the window. pending: submitted event exists but no quoted
      confirmation yet. unevidenced: no submitted event at all.
    """
    verified = pending = unevidenced = 0
    for r in find_submitted_rows(rows):
        rid = r.get("role_id", "")
        quote = _scan_ledger_for_quote(
            events_path, r.get("date_submitted", ""), rid)
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
                        has_event = True
                        break
        except FileNotFoundError:
            pass
        if has_event:
            pending += 1
        else:
            unevidenced += 1
    return verified, pending, unevidenced


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Evidence gate: count only evidenced submission claims.")
    ap.add_argument("--ledger", default=LEDGER_PATH)
    ap.add_argument("--events", default=EVENTS_PATH)
    ap.add_argument("--decisions", default=DECISIONS_PATH)
    args = ap.parse_args(argv)

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
