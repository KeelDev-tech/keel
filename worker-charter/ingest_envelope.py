#!/usr/bin/env python3
"""Ingest a charter worker's JSON envelope into the learning loop.

Usage:
    python3 ingest_envelope.py <envelope.json> [--dry-run]

What it does:
  1. Validates the envelope schema strictly (status/result/telemetry).
  2. Routes each attempt in result.attempts into record_outcome.record()
     (outcome evidence + append-only telemetry), exactly once.
  3. Fail-closes: an attempt claiming outcome "submitted" whose note lacks
     quoted confirmation evidence is REJECTED, never recorded.
  4. Appends novel edge cases to edge-case-review.md for the evaluator loop.
  5. If status is "failed" with no attempts, logs one "error" telemetry event.

The worker must NOT call log_event.py or write the outcome evidence store
itself (charter constraint C-11) — this ingester is the single logging path.

Path convention: this module lives at the top-level worker-charter/ dir,
so it resolves paths locally with a $KEEL_HOME fallback (never the private
pipeline's workspace path).
"""
import json
import hashlib
import os
import re
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
KEEL_HOME = os.environ.get("KEEL_HOME") or os.path.dirname(BASE)
EXEC = os.path.join(KEEL_HOME, "engines")
PACKETS = os.path.join(KEEL_HOME, "hidden_files", "apply-launch-packets")
sys.path.insert(0, EXEC)
OT = os.path.join(EXEC, "outcome-tracking")
if os.path.isdir(OT):
    sys.path.insert(0, OT)
sys.path.insert(0, BASE)
import log_event  # noqa: E402
from record_outcome import record  # noqa: E402
try:
    import evidence_gate  # noqa: E402 — canonical SUBMITTED evidence gate
except ImportError:  # outcome-tracking module syncs in a sibling lane
    evidence_gate = None  # fail-closed: submitted claims cannot be gated

REVIEW = os.path.join(BASE, "edge-case-review.md")
VALID_STATUS = {"success", "blocked", "failed"}
VALID_OUTCOME = {"submitted", "blocked"}

# Configurable resume-filename pattern for resume-lane resolution. The
# private production pipeline embedded the applicant's name and lane in
# uploaded resume filenames; no personal name may appear here. The operator
# configures the pattern that matches their own naming convention:
#   $KEEL_RESUME_FILENAME_RE — regex with exactly one group = the lane tag.
# Default matches "<name>_<LANE>_..." (any two name-ish tokens, then a
# lane tag like A-AI-TECH-OPS).
APPLICANT_FILENAME_RE = re.compile(os.environ.get(
    "KEEL_RESUME_FILENAME_RE", r"[A-Za-z]+_[A-Za-z]+_([A-Z]-[A-Za-z0-9-]+)_"))


def fail(msg):
    sys.exit(f"ingest_envelope: REJECTED — {msg}")


def validate_envelope(env: dict) -> dict:
    if not isinstance(env, dict):
        fail("envelope is not a JSON object")
    # Manifest adoption (2026-09-17): stamp the envelope schema version.
    # Additive field only — no logic change. Workers should emit
    # "schema_version": 1 on new envelopes going forward.
    env.setdefault("schema_version", 1)
    if env.get("status") not in VALID_STATUS:
        fail(f"status must be one of {sorted(VALID_STATUS)}")
    if not isinstance(env.get("result"), dict):
        fail("result must be an object")
    tel = env.get("telemetry")
    if not isinstance(tel, dict):
        fail("telemetry must be an object")
    for k, t in (("applied_constraints", list), ("tool_calls_count", int),
                 ("encountered_novel_edge_case", bool), ("notes_for_evaluator", str)):
        if not isinstance(tel.get(k), t):
            fail(f"telemetry.{k} must be {t.__name__}")
    return env


def validate_attempt(a: dict, i: int) -> dict:
    if not isinstance(a, dict):
        fail(f"attempts[{i}] is not an object")
    for k in ("role_id", "company", "ats", "technique", "outcome", "note"):
        if not isinstance(a.get(k), str) or not a[k].strip():
            fail(f"attempts[{i}].{k} must be a non-empty string")
    if a["outcome"] not in VALID_OUTCOME:
        fail(f"attempts[{i}].outcome must be one of {sorted(VALID_OUTCOME)}")
    # C-12 / C-05 enforcement: fail closed on unverified submission claims.
    # The attempt's note carries the confirmation quote; the evidence gate
    # refuses anything without quoted confirmation text in a canonical
    # field.
    if a["outcome"] == "submitted":
        if evidence_gate is None:
            fail(f"attempts[{i}] evidence_gate unavailable — refusing to "
                 f"record (role_id={a['role_id']})")
        ok, reason = evidence_gate.gate(
            {"status": "SUBMITTED", "confirmation_text": a["note"]})
        if not ok:
            fail(f"attempts[{i}] {reason} "
                 f"(role_id={a['role_id']}) — refusing to record")
    # R1: optional predictive score captured at application time (R6).
    # Carried onto record_outcome + telemetry so score→conversion is
    # measurable. Must be numeric when present; junk is dropped, not guessed.
    fs = a.get("fit_score")
    if fs is not None:
        if isinstance(fs, bool) or not isinstance(fs, (int, float)):
            fail(f"attempts[{i}].fit_score must be numeric when present")
    # P-2026-09-14 22:35-arm4-2: resume_lane is optional on the envelope; a
    # present value must be a non-empty string. Absent lanes are resolved
    # from the launch packet (or 'unknown'), never guessed.
    if "resume_lane" in a and (not isinstance(a["resume_lane"], str)
                               or not a["resume_lane"].strip()):
        fail(f"attempts[{i}].resume_lane must be a non-empty string when present")
    return a


def find_launch_packet(role_id):
    """Return the launch-packet dict for role_id, or None.

    Read-only lookup; never mutates packets. Matches <role_id>.json first,
    then the first packet file whose name starts with the role_id.
    """
    if not role_id or not os.path.isdir(PACKETS):
        return None
    exact = os.path.join(PACKETS, role_id + ".json")
    cand = [exact] if os.path.exists(exact) else [
        os.path.join(PACKETS, f) for f in sorted(os.listdir(PACKETS))
        if f.startswith(role_id) and f.endswith(".json")]
    if not cand:
        return None
    try:
        return json.load(open(cand[0]))
    except Exception:
        return None


def resolve_resume_lane(a):
    """Resolve the resume lane for an attempt, fail-closed to 'unknown'.

    Precedence: (1) the attempt's own resume_lane field, (2) the lane tag
    embedded in the launch packet's resume filename via APPLICANT_FILENAME_RE
    (the operator configures the pattern for their own naming convention),
    (3) 'unknown'. Never guessed, never invented.
    (P-2026-09-14 22:35-arm4-2)
    """
    lane = (a.get("resume_lane") or "").strip()
    if lane:
        return lane
    pkt = find_launch_packet(a.get("role_id", ""))
    if pkt:
        for f in pkt.get("upload_files", []) or []:
            m = APPLICANT_FILENAME_RE.search(os.path.basename(str(f)))
            if m:
                return m.group(1)
    return "unknown"


def append_edge_case(env: dict):
    tel = env["telemetry"]
    line = (f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')} — {env['status']}\n"
            f"- constraints applied: {', '.join(tel['applied_constraints']) or '(none)'}\n"
            f"- tool calls: {tel['tool_calls_count']}\n"
            f"- notes: {tel['notes_for_evaluator'].strip()}\n")
    with open(REVIEW, "a") as f:
        f.write(line)
    print(f"edge case appended to {REVIEW}")


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: ingest_envelope.py <envelope.json> [--dry-run]")
    dry = "--dry-run" in sys.argv
    env = validate_envelope(json.load(open(sys.argv[1])))
    attempts = env["result"].get("attempts", [])
    if not isinstance(attempts, list):
        fail("result.attempts must be a list")

    recorded, rejected = 0, 0
    for i, a in enumerate(attempts):
        try:
            a = validate_attempt(a, i)
        except SystemExit as e:
            print(str(e))
            rejected += 1
            continue
        if dry:
            print(f"[dry-run] would record {a['outcome']} {a['ats']}/{a['technique']} "
                  f"for {a['role_id']}")
        else:
            try:
                # Replay-safe event ID: the digest covers the whole
                # (validated) envelope, so re-ingesting the same envelope
                # yields the same IDs — no duplicate telemetry events, no
                # duplicate evidence rows.
                env_digest = hashlib.sha256(
                    json.dumps(env, sort_keys=True,
                               allow_nan=False).encode()).hexdigest()
                record(a["ats"], a["technique"], a["outcome"], a["note"],
                       role_id=a["role_id"], company=a["company"],
                       source="charter-worker-ingest",
                       fit_score=a.get("fit_score"),
                       resume_lane=resolve_resume_lane(a),
                       event_id=f"envelope:{env_digest}:{i}")
            except ValueError as e:
                # ADD-2 2026-09-16: worker-reported garbage (e.g. unknown ats
                # keys) must fail one attempt closed and keep the envelope
                # loop alive — never abort the whole envelope mid-loop.
                print(f"attempts[{i}] record() refused: {e} "
                      f"— failing this attempt closed")
                rejected += 1
                continue
        recorded += 1

    if env["telemetry"]["encountered_novel_edge_case"] and not dry:
        append_edge_case(env)
    elif env["telemetry"]["encountered_novel_edge_case"]:
        print("[dry-run] would append edge case to edge-case-review.md")

    if env["status"] == "failed" and not attempts and not dry:
        log_event.log("error", source="charter-worker-ingest",
                      details={"note": env["telemetry"]["notes_for_evaluator"][:500]})
        print("logged error telemetry for failed worker with no attempts")

    print(f"ingest complete: recorded={recorded} rejected={rejected} "
          f"dry_run={dry}")


if __name__ == "__main__":
    main()
