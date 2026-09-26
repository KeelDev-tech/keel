#!/usr/bin/env python3
"""Run keel_flow.board on a canonical export snapshot; write markdown to a NEW file.

Read-only: never touches queues, telemetry, tray, or schedules.
Usage: flow_board.py <snapshot.json> <report-out.md>
"""

import json
import math
import os
import sys
from datetime import datetime, timezone

KEEL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KEEL_DIR)


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: flow_board.py <snapshot.json> <report-out.md>")
    snapshot_path, out_path = sys.argv[1], sys.argv[2]
    if os.path.exists(out_path):
        print(f"flow_board: refusing to overwrite existing {out_path}", file=sys.stderr)
        sys.exit(2)
    with open(snapshot_path) as f:
        snapshot = json.load(f)
    from keel_flow.board import build, markdown
    now = datetime.now(timezone.utc)
    report = build(snapshot, now=now)
    text = markdown(report)
    # observer-only appendix: evidenced release groups + discovery measurement,
    # read straight from the snapshot (the board validates them; the markdown
    # renderer does not print them).
    appendix = ["", "## Upcoming supply (evidenced releases)", "",
                "| Release | Roles | Available | Expected @ measured conversion |",
                "|---|---|---|---|"]
    import math
    for rel in snapshot.get("releases", []):
        expected = math.floor(rel["count"] * rel["estimated_conversion"])
        appendix.append(
            f"| {rel['release_id']} | {rel['count']} | {rel['available_at']} | "
            f"~{expected} (conv={rel['estimated_conversion']:.3f}) |")
    if not snapshot.get("releases"):
        appendix.append("_No evidenced release groups in this export._")
    appendix += ["", "_Evidence refs:_"]
    for rel in snapshot.get("releases", []):
        appendix.append(f"- {rel['release_id']}: {rel['conversion_evidence_ref']}")
    disc = snapshot.get("discovery", {}).get("sources", [])
    if disc:
        s = disc[0]
        appendix += ["", "## Discovery measurement",
                     f"- Source {s['source_id']}: measurement_complete="
                     f"{s['measurement_complete']}, checks={s['checks']}, "
                     f"qualified_unique_live={s['qualified_unique_live']}, "
                     f"verification_minutes={s['verification_minutes']}",
                     f"- Evidence: {s['evidence_ref']}"]
    appendix += ["",
                 "This is an observation and proposal report. No browser, queue, "
                 "scheduler, or HTTP actions occurred; execution_authorized=false."]
    text = text + "\n".join(appendix) + "\n"
    header = (f"<!-- flow board report | snapshot={snapshot_path} "
              f"| snapshot_sha256={report['snapshot_sha256']} "
              f"| evaluated_at={report['evaluated_at']} -->\n\n")
    with open(out_path, "x", encoding="utf-8") as f:
        f.write(header + text)
    print(f"flow_board: wrote {out_path}", file=sys.stderr)
    # stdout: compact signal summary for cron/pulse consumption
    source = disc[0] if disc else {}
    summary = {
        "status": report["status"],
        "execution_authorized": report["execution_authorized"],
        "nominal_ready": report["readiness"]["nominal_ready"],
        "executable_ready": report["readiness"]["executable_ready"],
        "supply_state": report["integration"]["supply"]["state"],
        "contracts_agree": report["integration"]["supply"]["contracts_agree"],
        "forecast_state": report["forecast"]["state"],
        "releases": [
            {"release_id": r["release_id"], "count": r["count"],
             "available_at": r["available_at"],
             "expected": math.floor(r["count"] * r["estimated_conversion"]),
             "estimated_conversion": round(r["estimated_conversion"], 4)}
            for r in snapshot.get("releases", [])
        ],
        "discovery": {
            "source_id": source.get("source_id"),
            "measurement_complete": source.get("measurement_complete", False),
            "verification_minutes": source.get("verification_minutes"),
            "allocated_minutes": report["discovery"]["allocated_minutes"],
            "discovery_state": report["discovery"]["state"],
        },
        "actions": [a["action"] for a in report["actions"]],
        "snapshot_sha256": report["snapshot_sha256"],
    }
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
