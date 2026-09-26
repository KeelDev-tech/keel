#!/usr/bin/env python3
"""Keel GEO pipeline — canonical recount.

Counts the private production pipeline's ledger and append-only telemetry
log and rewrites docs/geo/stats.json. Public-safe: reads private local
files, outputs aggregates only.

METHODOLOGY v2 (2026-09-15) — read before trusting a number:
- Sources: PIPELINE_LEDGER (application-ledger.json, a list of rows) and
  PIPELINE_TELEMETRY (telemetry/events.jsonl, append-only JSONL).
- "Verified submissions" = rows with status SUBMITTED. Submission is only
  recorded on explicit page-confirmation evidence (pipeline reporting
  rule); this script COUNTS the ledger, it never re-verifies the
  confirmations and never estimates, projects, or extrapolates.
- "Verified submissions at launch" = 55 is a HISTORICAL canon counted at
  2026-09-15 00:17 PT during the launch push. It is a constant and is
  never recomputed.
- Evidence split (evidenced / url_only / unevidenced) comes from the
  canonical audit reader in engines/outcome-tracking/evidence_gate.py
  (`audit` CLI). All evidence counting goes through that reader — never
  hand-rolled field checks.
- Gate figures = DISTINCT (role_id, gate) pairs in gate_blocked events
  (event_type == 'gate_blocked', gate at details.gate). Backfilled events
  (details.backfilled == true) dedupe ON THE PAIR: a backfilled pair that
  also appears live counts once; backfilled-only pairs are real historical
  stops and count. (Correction 2026-09-15: the 02:15 methodology dropped
  ALL backfilled events as "duplicates", but the log was verified to have
  ZERO overlap between live and backfilled (role_id, gate) pairs, so that
  rule was an undercount. This recount corrects it.)
- Excluded from gate counts: events with no role_id (queue-level events
  like feeder_empty / new_ats_detected) and role_ids starting with
  TEST- (test fixtures, e.g. TEST-ROLE — the 02:15 fabrication figure of
  5 included one such fixture; the corrected figure counts real leads
  only).
- Ledger outcome statuses counted directly: INTERVIEW_INVITED, REJECTED.
- telemetry_events = non-blank line count of events.jsonl.
- gate_encountered (hit but continued) and gate_cleared (resolved) are NOT
  in the gate counts.

Fail-closed: any source read error -> non-zero exit and NO partial write
(stats.json is written atomically via temp file + rename). The JSON is
also printed to stdout.

Env:
    PIPELINE_LEDGER    default ~/workspace/job-pipeline/ledger/application-ledger.json
    PIPELINE_TELEMETRY default ~/workspace/job-pipeline/telemetry/events.jsonl
    EVIDENCE_GATE      default ~/workspace/job-pipeline/engines/outcome-tracking/evidence_gate.py
    STATS_JSON         default <repo>/docs/geo/stats.json (derived from this file's location)
"""

import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

DEFAULT_LEDGER = os.path.expanduser("~/workspace/job-pipeline/ledger/application-ledger.json")
DEFAULT_TELEMETRY = os.path.expanduser("~/workspace/job-pipeline/telemetry/events.jsonl")
DEFAULT_EVIDENCE_GATE = os.path.expanduser(
    "~/workspace/job-pipeline/engines/outcome-tracking/evidence_gate.py"
)

LEDGER_PATH = os.environ.get("PIPELINE_LEDGER", DEFAULT_LEDGER)
TELEMETRY_PATH = os.environ.get("PIPELINE_TELEMETRY", DEFAULT_TELEMETRY)
EVIDENCE_GATE = os.environ.get("EVIDENCE_GATE", DEFAULT_EVIDENCE_GATE)
STATS_JSON = os.environ.get("STATS_JSON", os.path.join(REPO, "docs", "geo", "stats.json"))

LAUNCH_CANON = 55  # historical; counted 2026-09-15 00:17 PT; never recomputed
PT = ZoneInfo("America/Los_Angeles")

# Public-safe gate names for the checked-in stats.json. The snapshot is a
# public artifact, so private operator names are aliased here at generation
# time: re-running recount.py reproduces the sanitized keys instead of
# restoring the private ones (review thread on docs/geo/stats.json, PR
# #21). Telemetry itself keeps the private names.
PUBLIC_GATE_NAMES = {
    "needs_trent_input": "needs_operator_input",
    "trent_input_needs_user": "operator_input_needs_user",
}

# Canonical ledger-status mapping lives next to the evidence gate. The
# recount's cross-check must use the same canonicalization as the audit
# (LEDGER_STATUS_ALIAS: the two historical lowercase "submitted" rows count
# as SUBMITTED). Importing it here keeps the two definitions from drifting.
sys.path.insert(0, os.path.dirname(os.path.abspath(EVIDENCE_GATE)))
from ledger_append import canon_ledger_status  # noqa: E402



def fail(msg):
    print(f"recount: FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def load_ledger(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(f"cannot read ledger {path}: {e}")
    rows = data if isinstance(data, list) else data.get("rows", data.get("applications", []))
    if not isinstance(rows, list):
        fail(f"ledger {path} has no row list")
    return rows


def run_evidence_audit(ledger_path):
    try:
        proc = subprocess.run(
            [sys.executable, EVIDENCE_GATE, "audit", ledger_path],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        fail(f"evidence_gate audit failed to run: {e}")
    if proc.returncode != 0:
        fail(f"evidence_gate audit exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        fail(f"evidence_gate audit returned non-JSON: {e}")


def count_gates(telemetry_path):
    live_pairs = set()
    backfilled_pairs = set()
    per_gate = defaultdict(set)
    n_lines = 0
    try:
        f = open(telemetry_path)
    except OSError as e:
        fail(f"cannot read telemetry {telemetry_path}: {e}")
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_lines += 1
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue  # append-only log may carry a torn tail line; skip, don't die
            if not isinstance(e, dict) or e.get("event_type") != "gate_blocked":
                continue
            details = e.get("details") or {}
            rid = e.get("role_id")
            if not rid or str(rid).startswith("TEST-"):
                continue
            gate = details.get("gate")
            if not gate:
                continue
            gate = PUBLIC_GATE_NAMES.get(str(gate), str(gate))
            pair = (str(rid), str(gate))
            if details.get("backfilled"):
                backfilled_pairs.add(pair)
            else:
                live_pairs.add(pair)
    union = live_pairs | backfilled_pairs
    for rid, gate in union:
        per_gate[gate].add(rid)
    return {
        "telemetry_events": n_lines,
        "distinct_pairs": len(union),
        "live_pairs": len(live_pairs),
        "backfilled_pairs": len(backfilled_pairs),
        "overlap": len(live_pairs & backfilled_pairs),
        "per_gate": {g: len(s) for g, s in per_gate.items()},
        "fabrication_leads": sorted(per_gate.get("fabrication", ())),
    }


def main():
    rows = load_ledger(LEDGER_PATH)
    audit = run_evidence_audit(LEDGER_PATH)
    gates = count_gates(TELEMETRY_PATH)

    total_submitted = audit.get("total_submitted")
    if not isinstance(total_submitted, int):
        fail("evidence_gate audit returned no integer total_submitted")

    # Cross-check: direct canonical SUBMITTED row count must agree with the
    # audit. Both sides use canon_ledger_status (LEDGER_STATUS_ALIAS); a naive
    # exact-match count falsely failed closed on 2026-09-22 (209 vs 207)
    # because the two historical lowercase "submitted" rows were excluded.
    direct = sum(
        1 for r in rows
        if isinstance(r, dict) and canon_ledger_status(r.get("status")) == "SUBMITTED"
    )

    if direct != total_submitted:
        fail(f"audit total_submitted ({total_submitted}) != direct SUBMITTED count ({direct})")

    invites = sum(1 for r in rows if isinstance(r, dict) and r.get("status") == "INTERVIEW_INVITED")
    rejections = sum(1 for r in rows if isinstance(r, dict) and r.get("status") == "REJECTED")

    counted_at = datetime.now(PT).isoformat(timespec="seconds")

    stats = {
        "counted_at": counted_at,
        "verified_submissions_at_launch": LAUNCH_CANON,
        "verified_submissions_now": total_submitted,
        "evidence": {
            "evidenced": audit.get("evidenced"),
            "url_only": audit.get("url_only"),
            "unevidenced": audit.get("unevidenced"),
        },
        "fabrication_gate_leads": gates["per_gate"].get("fabrication", 0),
        "leads_stopped_by_any_gate": gates["distinct_pairs"],
        "per_gate": dict(sorted(gates["per_gate"].items(), key=lambda kv: -kv[1])),
        "telemetry_events": gates["telemetry_events"],
        "interview_invites": invites,
        "rejections_recorded": rejections,
        "sources": ["application-ledger.json", "telemetry/events.jsonl"],
        "methodology": (
            "Counts taken directly from the private production pipeline's ledger and "
            "append-only telemetry log. Verified submissions = SUBMITTED rows (explicit "
            "page-confirmation rule; the ledger is counted, confirmations are not "
            "re-verified). Gate figures are distinct (role_id, gate) pairs in "
            "gate_blocked events; backfilled events dedupe on the pair "
            "(backfilled-only pairs are real historical stops and count); "
            "queue-level events without role_id and TEST- fixtures are excluded. "
            "Launch figure 55 is the historical canon counted 2026-09-15 00:17 PT. "
            f"Submission figures recounted {counted_at[:10]}: {total_submitted} canonical "
            "SUBMITTED rows (exact-status plus the 2 historical lowercase-alias rows "
            "counted per LEDGER_STATUS_ALIAS); evidence split "
            f"{audit.get('evidenced')} quoted / {audit.get('pointer')} pointer / "
            f"{audit.get('url_only')} url_only / {audit.get('unevidenced')} unevidenced. "
            "The ledger is the count of record. "

            "Full methodology in docs/data-story.md."
        ),
        "refresh_policy": (
            "Figures update only by direct ledger recount at the time of publication. "
            "Never estimated, projected, or extrapolated."
        ),
        "repo": "https://github.com/KeelDev-tech/keel",
        "license": "Apache-2.0",
    }

    # Atomic write: temp file in the same directory, then rename. No partial write.
    out_dir = os.path.dirname(STATS_JSON)
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".stats.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(stats, f, indent=2)
            f.write("\n")
        os.replace(tmp, STATS_JSON)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        fail(f"cannot write {STATS_JSON}: {e}")

    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
