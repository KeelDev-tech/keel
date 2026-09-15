"""Record application outcomes (Keel public edition).

Public edition: the technique library is part of the private execution
layer (see SPLIT.md), so the library update is skipped when
technique_library.json is absent. The append-only telemetry event is
always emitted — that is the public learning loop's evidence base.

Original docstring follows.
""""""Record application outcomes back into the technique library.

Usage:
    python3 record_outcome.py <ats> "<technique>" <submitted|blocked> "<note>"
        [--role-id ID] [--company NAME] [--source SRC]

Promotes techniques only on verified submissions. Blocked attempts are recorded
as evidence too, so the library learns what fails where.

Also emits a telemetry event (submitted / gate_blocked) so the append-only
event log stays in sync with the reinforcement loop's evidence base.
"""

import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(BASE, "technique_library.json")

sys.path.insert(0, BASE)
import log_event  # noqa: E402


def record(ats: str, technique: str, outcome: str, note: str,
           role_id: str = "", company: str = "", source: str = ""):
    if os.path.exists(LIB):
        lib = json.load(open(LIB))
        entry = lib["ats"].setdefault(ats, {}).setdefault(
            "primary_technique", {"name": technique, "steps": [], "evidence": []}
        )
        entry.setdefault("evidence", []).append(
            {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "technique": technique,
                "result": outcome,
                "note": note,
            }
        )
        lib["_meta"]["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        json.dump(lib, open(LIB, "w"), indent=2)
        print(f"recorded {outcome} for {ats}/{technique}")
    else:
        # Public edition: no technique library on disk (private layer).
        # The telemetry event below is the durable record.
        print(f"recorded {outcome} for {ats}/{technique} (telemetry only; no technique library present)")
    # Telemetry: mirror the outcome into the append-only event log.
    ev_type = "submitted" if outcome == "submitted" else "gate_blocked"
    details = {"technique": technique, "note": note}
    if outcome == "blocked":
        details["gate"] = "technique_blocked"
    log_event.log(ev_type, role_id=role_id, company=company, ats=ats,
                  source=source or "record_outcome", details=details)


if __name__ == "__main__":
    if len(sys.argv) < 5:
        sys.exit("usage: record_outcome.py <ats> <technique> <submitted|blocked> <note> "
                 "[--role-id ID] [--company NAME] [--source SRC]")
    role_id = company = source = ""
    i = 5
    while i < len(sys.argv):
        if sys.argv[i] == "--role-id" and i + 1 < len(sys.argv):
            role_id = sys.argv[i + 1]; i += 2
        elif sys.argv[i] == "--company" and i + 1 < len(sys.argv):
            company = sys.argv[i + 1]; i += 2
        elif sys.argv[i] == "--source" and i + 1 < len(sys.argv):
            source = sys.argv[i + 1]; i += 2
        else:
            i += 1
    record(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4],
           role_id=role_id, company=company, source=source)
