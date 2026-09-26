"""Run history derived ONLY from the append-only telemetry log (prototype, G-1/B-22).

Nothing here is inferred or hand-written. Every number in a run row traces to
logged events; runs with zero activity render honestly instead of vanishing.

Event schema (JSONL, one event per line):
  {"t": <iso ts>, "type": <event type>, "run_id": <str>, ...type-specific fields}

Event types:
  run_started        — {name}
  run_ended          — {outcome}  (outcome: completed | paused_end | aborted)
  scan_completed     — {leads_promoted: int}
  attempt_recorded   — {attempt_id, verdict}   verdict: confirmed | ambiguous | failed
  lead_parked        — {reason}
  run_paused / run_resumed — {}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

KNOWN_TYPES = {
    "run_started",
    "run_ended",
    "scan_completed",
    "attempt_recorded",
    "lead_parked",
    "run_paused",
    "run_resumed",
}

CONFIRMED = "confirmed"
AMBIGUOUS = "ambiguous"
FAILED = "failed"


@dataclass
class RunRow:
    run_id: str
    name: str = ""
    started_at: str = ""
    ended_at: str = ""
    status: str = "OPEN"  # OPEN | COMPLETED | PAUSED_END | ABORTED
    scans_completed: int = 0
    leads_promoted: int = 0
    attempts_confirmed: int = 0
    attempts_ambiguous: int = 0
    attempts_failed: int = 0
    leads_parked: int = 0
    unmapped_events: int = 0
    _seen_attempts: set = field(default_factory=set, repr=False)

    @property
    def honesty_note(self) -> str:
        if (
            self.scans_completed == 0
            and self.attempts_confirmed == 0
            and self.attempts_ambiguous == 0
            and self.attempts_failed == 0
        ):
            return "0 promotions this run — no activity recorded"
        return ""


def _get_run(runs: Dict[str, RunRow], run_id: str) -> RunRow:
    if run_id not in runs:
        # Events for an unknown run still count — the row is honest about its
        # orphaned state instead of dropping the evidence.
        runs[run_id] = RunRow(run_id=run_id, name="(events before run_started)")
    return runs[run_id]


def build_run_history(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold an append-only event stream into per-run history rows.

    Rules:
    - attempt_recorded is deduplicated on attempt_id (one durable attempt, F22).
    - Unknown event types are counted in unmapped_events, never silently dropped.
    - Ambiguous verdicts never leak into confirmed counts.
    """
    runs: Dict[str, RunRow] = {}
    for ev in events:
        etype = ev.get("type")
        run_id = ev.get("run_id", "")
        if etype not in KNOWN_TYPES:
            _get_run(runs, run_id).unmapped_events += 1
            continue
        row = _get_run(runs, run_id)
        if etype == "run_started":
            row.started_at = ev.get("t", "")
            row.name = ev.get("name", "")
            row.status = "OPEN"
        elif etype == "run_ended":
            row.ended_at = ev.get("t", "")
            outcome = ev.get("outcome", "completed")
            row.status = {
                "completed": "COMPLETED",
                "paused_end": "PAUSED_END",
                "aborted": "ABORTED",
            }.get(outcome, "COMPLETED")
        elif etype == "scan_completed":
            row.scans_completed += 1
            row.leads_promoted += int(ev.get("leads_promoted", 0))
        elif etype == "attempt_recorded":
            attempt_id = ev.get("attempt_id")
            verdict = ev.get("verdict")
            if attempt_id is None or verdict not in (CONFIRMED, AMBIGUOUS, FAILED):
                row.unmapped_events += 1
                continue
            if attempt_id in row._seen_attempts:
                continue  # same attempt seen twice (pause/resume replay) — count once
            row._seen_attempts.add(attempt_id)
            if verdict == CONFIRMED:
                row.attempts_confirmed += 1
            elif verdict == AMBIGUOUS:
                row.attempts_ambiguous += 1
            else:
                row.attempts_failed += 1
        elif etype == "lead_parked":
            row.leads_parked += 1
        # run_paused / run_resumed intentionally change nothing in the row.

    return [
        {
            "run_id": r.run_id,
            "name": r.name,
            "started_at": r.started_at,
            "ended_at": r.ended_at,
            "status": r.status,
            "scans_completed": r.scans_completed,
            "leads_promoted": r.leads_promoted,
            "attempts_confirmed": r.attempts_confirmed,
            "attempts_ambiguous": r.attempts_ambiguous,
            "attempts_failed": r.attempts_failed,
            "leads_parked": r.leads_parked,
            "unmapped_events": r.unmapped_events,
            "honesty_note": r.honesty_note,
        }
        for r in runs.values()
    ]


def load_events(path: str) -> List[Dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]
