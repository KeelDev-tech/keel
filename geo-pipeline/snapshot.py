#!/usr/bin/env python3
"""Keel GEO pipeline — weekly snapshot.

Runs recount.py then probe.py (subprocesses, JSON captured), appends one
timestamped record to geo-pipeline/history/snapshots.jsonl, and logs an
outcome_observed event to the marketing telemetry via the sanctioned
logging path (marketing-engine/telemetry/log_event.py — never invent a new
schema, never write the JSONL by hand).

Exit codes: 0 on success; 2 if the recount failed (its own fail-closed
rules apply — a bad recount poisons nothing); probe failures never fail
the run (probe.py already marks unknowns).
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HISTORY_DIR = os.path.join(HERE, "history")
SNAPSHOTS = os.path.join(HISTORY_DIR, "snapshots.jsonl")
LOG_EVENT = os.path.join(REPO, "marketing-engine", "telemetry", "log_event.py")


def run_script(path, name):
    try:
        proc = subprocess.run(
            [sys.executable, path], capture_output=True, text=True, timeout=600
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"snapshot: FATAL: {name} failed to run: {e}", file=sys.stderr)
        sys.exit(2)
    if proc.returncode != 0:
        print(f"snapshot: FATAL: {name} exited {proc.returncode}: "
              f"{proc.stderr.strip()[:500]}", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"snapshot: FATAL: {name} returned non-JSON: {e}", file=sys.stderr)
        sys.exit(2)


def main():
    recount = run_script(os.path.join(HERE, "recount.py"), "recount.py")
    probe = run_script(os.path.join(HERE, "probe.py"), "probe.py")

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "recount": recount,
        "probe": probe,
    }
    os.makedirs(HISTORY_DIR, exist_ok=True)
    with open(SNAPSHOTS, "a") as f:
        f.write(json.dumps(record) + "\n")

    details = {
        "source": "geo-pipeline-snapshot",
        "counted_at": recount.get("counted_at"),
        "verified_submissions_now": recount.get("verified_submissions_now"),
        "fabrication_gate_leads": recount.get("fabrication_gate_leads"),
        "leads_stopped_by_any_gate": recount.get("leads_stopped_by_any_gate"),
        "telemetry_events": recount.get("telemetry_events"),
        "interview_invites": recount.get("interview_invites"),
        "rejections_recorded": recount.get("rejections_recorded"),
        "repo_stars": (probe.get("repo_api") or {}).get("stargazers_count", "unknown"),
        "repo_forks": (probe.get("repo_api") or {}).get("forks_count", "unknown"),
        "pages_llms_txt": ((probe.get("pages") or {}).get("/llms.txt") or {}).get("status", "unknown"),
        "awesome_prs_merged": sum(
            1 for p in (probe.get("awesome_prs") or []) if p.get("merged_at")
        ),
        "awesome_prs_open": sum(
            1 for p in (probe.get("awesome_prs") or []) if p.get("state") == "open"
        ),
    }
    try:
        proc = subprocess.run(
            [sys.executable, LOG_EVENT, "outcome_observed",
             "--avenue-id", "geo-pipeline-snapshot",
             "--channel", "geo-pipeline",
             "--details", json.dumps(details)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            print(f"snapshot: warning: log_event exited {proc.returncode}: "
                  f"{proc.stderr.strip()[:300]}", file=sys.stderr)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"snapshot: warning: log_event failed: {e}", file=sys.stderr)

    print(json.dumps({"snapshot_ts": record["ts"], "history": SNAPSHOTS,
                      "telemetry": "outcome_observed logged"}, indent=2))


if __name__ == "__main__":
    main()
