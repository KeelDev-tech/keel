#!/usr/bin/env python3
"""Learning-loop watchdog: anti-rot guard for the charter-evaluator loop.

Lesson 2026-09-15: the charter-evaluator-weekly cron existed and was
enabled but had NEVER run — it was created after that week's Monday 08:00
fire, and nobody noticed because no watcher tracks evaluator run recency.
The ~60-proposal triage backlog accumulated silently until a manual cron run
drained it (107 proposals, 55 PROMOTE / 52 DEFER, 2026-09-15 ~20:05 PDT).

This script is the tripwire. Every run it:
  1. parses worker-charter/learning-proposals.md for the last
     "Charter-evaluator run <date>" footer and counts DRAFT / PENDING
     TRIAGE sections still lacking an EVALUATOR verdict;
  2. writes the metric to hidden_files/loop-health.json (the metrics-first
     bar: every upgrade ships a named metric);
  3. if the evaluator run is stale (> STALE_AFTER_DAYS) or the untriaged
     backlog is large (>= BACKLOG_ALERT_N), publishes ONE deduped blackboard
     finding (max one per DEDUPE_HOURS) so the pulse/main agent notices.

Read-only except loop-health.json and the blackboard publish on trip.
Never touches queue/ledger, never writes charter constraints.

Path convention: this module lives at the top-level worker-charter/ dir,
so it resolves paths locally with a $KEEL_HOME fallback (never the private
pipeline's workspace path).

Usage:
    python3 loop_watchdog.py            # check; publish on trip
    python3 loop_watchdog.py --dry-run  # report only, never publish
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

CHARTER_DIR = os.path.dirname(os.path.abspath(__file__))
KEEL_HOME = os.environ.get("KEEL_HOME") or os.path.dirname(CHARTER_DIR)
PROPOSALS = os.path.join(CHARTER_DIR, "learning-proposals.md")
HEALTH = os.path.join(KEEL_HOME, "hidden_files", "loop-health.json")
BLACKBOARD = os.path.join(KEEL_HOME, "monitors", "blackboard.py")

STALE_AFTER_DAYS = 8          # weekly cadence + 1 day slack
BACKLOG_ALERT_N = 30          # untriaged proposals that should have triaged
DEDUPE_HOURS = 24             # at most one finding per day per trip type

FOOTER_RE = re.compile(
    r"^# Charter-evaluator run (\d{4}-\d{2}-\d{2}) ~(\d{2}):(\d{2}) PDT", re.M)
SECTION_SPLIT = re.compile(r"(?m)^## (?=P-|ARM|Evidence)")


def parse_proposals():
    """Return (last_run_dt_utc_or_None, untriaged_count, total_sections)."""
    try:
        text = open(PROPOSALS).read()
    except FileNotFoundError:
        return None, -1, 0
    last_run = None
    for m in FOOTER_RE.finditer(text):
        d, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
        # PDT = UTC-7 (evaluator stamps PDT; DST in effect in September)
        dt = datetime.strptime(f"{d} {hh:02d}:{mm:02d}", "%Y-%m-%d %H:%M")
        dt = dt.replace(tzinfo=timezone.utc).timestamp() + 7 * 3600
        if last_run is None or dt > last_run:
            last_run = dt
    sections = SECTION_SPLIT.split(text)
    untriaged = 0
    total = 0
    for s in sections:
        if not s.strip():
            continue
        total += 1
        if "DRAFT / PENDING TRIAGE" in s and "EVALUATOR 20" not in s:
            untriaged += 1
    return last_run, untriaged, total


def publish_finding(title, evidence):
    if not os.path.exists(BLACKBOARD):
        return False, f"blackboard not found at {BLACKBOARD}"
    cmd = [sys.executable, BLACKBOARD, "publish",
           "--loop", "full-potential-coordinator",
           "--domain", "gate-clustering",
           "--title", title,
           "--evidence", evidence]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stdout + r.stderr)[-500:]


def main():
    dry = "--dry-run" in sys.argv
    now = datetime.now(timezone.utc).timestamp()
    last_run, untriaged, total = parse_proposals()

    days_stale = round((now - last_run) / 86400, 2) if last_run else None
    trips = []
    if last_run is None:
        trips.append(("no_evaluator_run", "charter evaluator has no recorded run footer"))
    elif days_stale > STALE_AFTER_DAYS:
        trips.append(("evaluator_stale",
                      f"last charter-evaluator run {days_stale}d ago (> {STALE_AFTER_DAYS}d)"))
    if untriaged >= BACKLOG_ALERT_N:
        trips.append(("backlog_growing",
                      f"{untriaged} untriaged proposals (>= {BACKLOG_ALERT_N})"))

    prior = {}
    if os.path.exists(HEALTH):
        try:
            prior = json.load(open(HEALTH))
        except Exception:
            prior = {}
    last_finding = prior.get("last_finding_ts", 0)
    published = []
    if trips and not dry and (now - last_finding) > DEDUPE_HOURS * 3600:
        title, evidence = trips[0]
        ok, out = publish_finding(
            f"learning-loop {title}",
            f"{evidence}; learning-proposals.md {total} sections, "
            f"{untriaged} untriaged; last evaluator run "
            f"{days_stale}d ago" if days_stale is not None else evidence)
        if ok:
            published.append(title)
            last_finding = now
        else:
            print(f"blackboard publish failed: {out}", file=sys.stderr)

    health = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_evaluator_run_utc": (
            datetime.fromtimestamp(last_run, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if last_run else None),
        "days_since_evaluator_run": days_stale,
        "untriaged_proposals": untriaged,
        "total_proposal_sections": total,
        "trips": [t[0] for t in trips],
        "findings_published": published,
        "last_finding_ts": last_finding,
        "verdict": "STALE" if trips else "HEALTHY",
    }
    if not dry:
        tmp = HEALTH + ".tmp"
        os.makedirs(os.path.dirname(HEALTH), exist_ok=True)
        json.dump(health, open(tmp, "w"), indent=1)
        os.replace(tmp, HEALTH)
    print(json.dumps(health, indent=1))
    return 0 if not trips else 1


if __name__ == "__main__":
    sys.exit(main())
