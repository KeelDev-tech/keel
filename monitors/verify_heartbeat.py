#!/usr/bin/env python3
"""Verify heartbeat: streak-based gating for verify_cron runs.

classify_run maps one run record to a class: OK, GATED
("gated_no_eligible" — a deliberate pre-gate abort, not a transport
failure), or ERROR. decide() folds a sequence of (ts, classification,
record) triples into a verdict:

  - GATED is streak-neutral: it neither triggers error_status nor
    advances/resets the zero-verdict streak.
  - Any ERROR run gates with trigger "error_status"; error_runs lists
    the offending records.
  - An OK run with zero verdicts (scanned but nothing verified or dead)
    advances the streak; reaching the threshold gates with trigger
    "zero_verdict_streak".
  - An OK run with verdicts resets the streak.
  - Otherwise the verdict is QUIET.
"""
import json
import os
import sys

OK = "ok"
GATED = "gated_no_eligible"
ERROR = "error"
QUIET = "QUIET"
GATE = "GATE"

ZERO_VERDICT_STREAK_THRESHOLD = 1


def classify_run(record):
    """Map one run record to OK, GATED, or ERROR.

    "gated_no_eligible" is a deliberate pre-gate abort (healthy no-op),
    not a transport failure. "ok" is OK. Anything else is ERROR.
    """
    record = record or {}
    status = record.get("status")
    if status == "gated_no_eligible":
        return GATED
    if status == "ok":
        return OK
    return ERROR


def _verdicts(record):
    """Number of verdicts (verified_live + dead) in a run record."""
    counts = (record or {}).get("counts") or {}
    try:
        live = int(counts.get("verified_live", 0))
    except (TypeError, ValueError):
        live = 0
    try:
        dead = int(counts.get("dead", 0))
    except (TypeError, ValueError):
        dead = 0
    return live + dead


def decide(classified_runs, threshold=ZERO_VERDICT_STREAK_THRESHOLD):
    """Fold (ts, classification, record) triples into a verdict.

    Returns {"verdict", "trigger", "streak", "error_runs"}. GATED runs
    are streak-neutral. ERROR runs are collected and gate at the end.
    OK zero-verdict runs advance the streak; OK runs with verdicts
    reset it. Verdict is GATE when errors exist or the streak reaches
    `threshold`, else QUIET.
    """
    streak = 0
    error_runs = []
    for item in classified_runs or []:
        _ts, classification, record = item
        if classification == ERROR:
            error_runs.append(record)
        elif classification == GATED:
            continue
        elif classification == OK:
            if _verdicts(record) == 0:
                streak += 1
            else:
                streak = 0
        else:
            streak = 0
    if error_runs:
        return {"verdict": GATE, "trigger": "error_status",
                "streak": streak, "error_runs": error_runs}
    if streak >= threshold:
        return {"verdict": GATE, "trigger": "zero_verdict_streak",
                "streak": streak, "error_runs": error_runs}
    return {"verdict": QUIET, "trigger": None,
            "streak": streak, "error_runs": error_runs}


def main():
    records = []
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "data", "verify_runs.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        continue
    except FileNotFoundError:
        pass
    classified = [(r.get("ts"), classify_run(r), r) for r in records]
    print(json.dumps(decide(classified)))


if __name__ == "__main__":
    main()
