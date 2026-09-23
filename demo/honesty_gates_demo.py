#!/usr/bin/env python3
"""Keel honesty-gates terminal demo — runs against SYNTHETIC fixture input.

Demonstrates the three honest-automation contract clauses:
  1. Truthfulness gates — a question with no banked answer is REPORTED,
                         never filled (real repo engine: engines/prescreen).
  2. Fail closed      — a posting that violates the office/travel policy
                        is PARKED, never proceeded with (real repo engine:
                        engines/prescreen).
  3. Explicit confirmation — a submission counts ONLY on explicit
                        confirmation text (production's submit-intent ledger
                        is intentionally private, so this scene shows the
                        same rule on a demo-local scratch ledger; the rule
                        itself is the public recount's methodology —
                        "Counts increment only on explicit confirmation",
                        see docs/geo/stats.json).

All data is synthetic (fictional applicant "Alex Candidate"). The demo
runs entirely offline and touches only a temp scratch dir — no network,
no real queues, no private pipeline paths.

Usage:
    python3 demo/honesty_gates_demo.py
"""

import json
import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "engines"))  # engines import each other by module name

from engines import prescreen as ps               # noqa: E402

PAUSE = float(os.environ.get("KEEL_DEMO_PAUSE", "0.9"))


def say(*lines):
    for line in lines:
        print(line, flush=True)
    time.sleep(PAUSE)


def scene1_gap_reported():
    say("")
    say("=== SCENE 1: truthfulness gate — a gap is reported, never filled ===")
    say("")
    say("Synthetic applicant: Alex Candidate. Answer bank carries 2")
    say("keys (start date, work authorization). The form asks a 3rd")
    say("question — salary history — which the bank does NOT carry.")
    say("")
    bank = {
        "answers": {
            "start_timeframe": {"value": "Next day",
                                "provenance": "applicant stated 2026-09-20"},
            "us_work_auth": {"value": "Yes",
                             "provenance": "applicant stated 2026-09-20"},
        },
        "banded_questions": {},
    }
    questions = [
        "Are you authorized to work in the United States?",
        "When can you start?",
        "What was your salary at your last job?",  # the gap
    ]
    mapped, unmapped = 0, 0
    for label in questions:
        ok, key = ps.question_mappable(label, bank)
        if not ok:
            unmapped += 1
            say(f"  [UNMAPPED] {label!r}")
            say("           -> no banked answer for this question "
                "(gap reported, NOT invented)")
        else:
            mapped += 1
            say(f"  [MAPPED] {label!r} -> bank key {key!r}")
    say("")
    say(f"  result: {mapped} mapped from the bank, {unmapped} unmapped.")
    say("  Keel never bridges a missing answer with fiction.")


def scene2_unverifiable_parked(scratch):
    say("")
    say("=== SCENE 2: fail closed — an unverifiable posting is parked ===")
    say("")
    say("Synthetic posting: 'Staff Operations Manager' at Fictional Corp.")
    say("The posting demands 5 days/week on-site. Policy cap: 3 days/week.")
    say("No verified answer exists for that commitment.")
    say("")
    qdir = os.path.join(scratch, "queues")
    os.makedirs(qdir, exist_ok=True)
    role_id = "DEMO-synthetic-0001"
    queue = {"leads": [{"role_id": role_id, "company": "Fictional Corp",
                          "title": "Staff Operations Manager", "status": "READY"}]}
    with open(os.path.join(qdir, "standard-queue.json"), "w") as f:
        json.dump(queue, f)
    packet = {
        "role_id": role_id,
        "company": "Fictional Corp",
        "title": "Staff Operations Manager",
        "brief": "FORM INTEL section with the required office question",
        "posting_text": ("Staff Operations Manager. Must be on-site 5 days "
                         "per week at our San Francisco headquarters."),
    }
    verdict = ps.screen_packet(packet, {})
    say(f"  prescreen verdict: {verdict['verdict']}")
    for reason in verdict["reasons"]:
        say(f"           - {reason}")
    say("")
    if verdict["verdict"] == "PARK":
        out = ps.park_lead(role_id, verdict["reasons"], queue_dir=qdir)
        if out["ok"]:
            say("  park_lead -> ok: lead moved to needs_input-queue.json")
            say("  (needs the applicant's own answer before anything proceeds).")
        else:
            say(f"  park_lead -> REFUSED: {out.get('error')}")
    else:
        say("  (demo note: expected PARK verdict did not fire — inspect)")
    say("  Keel never proceeds past a commitment it cannot verify.")


# ---- Scene 3: demo-local confirmation ledger ---------------------------
# Production's submit-intent module is intentionally private (see SPLIT.md:
# submission techniques stay private). This scene implements the SAME
# counting rule — "a submission counts ONLY on explicit confirmation
# evidence" — on a scratch ledger inside the demo, so the rule is shown
# without probing the private side.

class ConfirmationLedger:
    """Scratch ledger: an attempt is an open row until reconciled."""

    def __init__(self):
        self.attempts = {}  # attempt_id -> {"role_id", "state", "confirmation"}

    def record_intent(self, role_id):
        for aid, row in self.attempts.items():
            if row["role_id"] == role_id and row["state"] != "CLOSED":
                raise RuntimeError(
                    f"OpenIntentExists: {aid} for {role_id} is unresolved; "
                    "no retry may be minted until it is reconciled")
        attempt_id = f"attempt-{len(self.attempts) + 1:04d}"
        self.attempts[attempt_id] = {"role_id": role_id, "state": "INTENT",
                                     "confirmation": ""}
        return attempt_id

    def mark_submitted(self, attempt_id, confirmation):
        if not (confirmation or "").strip():
            raise ValueError("empty confirmation refused: nothing counts "
                             "without confirmation evidence")
        row = self.attempts[attempt_id]
        row["confirmation"] = confirmation.strip()
        row["state"] = "SUBMITTED"

    def mark_unresolved(self, attempt_id):
        self.attempts[attempt_id]["state"] = "UNRESOLVED"

    def close_unsubmitted(self, attempt_id):
        self.attempts[attempt_id]["state"] = "CLOSED"

    def honest_count(self):
        return sum(1 for row in self.attempts.values()
                   if row["state"] == "SUBMITTED")


def scene3_explicit_confirmation():
    say("")
    say("=== SCENE 3: explicit confirmation — nothing counts without it ===")
    say("")
    ledger = ConfirmationLedger()
    role_id = "DEMO-synthetic-0002"
    say("Step 1: intent is recorded BEFORE any attempt.")
    attempt_id = ledger.record_intent(role_id)
    say(f"  recorded intent {attempt_id}  state=INTENT")
    say("")
    say("Step 2: the attempt ends ambiguously (timeout after POST).")
    say("  (The server may already have accepted it — or not.)")
    ledger.mark_unresolved(attempt_id)
    say(f"  state: {ledger.attempts[attempt_id]['state']}")
    say("  An ambiguous attempt is NEVER reported as a plain failure")
    say("  and NEVER retried — that would risk a duplicate.")
    say("")
    say("Step 3: a second attempt for the same role is refused.")
    try:
        ledger.record_intent(role_id)
        say("  !! unexpected: second attempt minted")
    except RuntimeError as e:
        say(f"  REFUSED: {e}")
    say("")
    say("Step 4: marking SUBMITTED without confirmation is refused.")
    try:
        ledger.mark_submitted(attempt_id, "")
        say("  !! unexpected: empty confirmation accepted")
    except ValueError as e:
        say(f"  REFUSED: ValueError ({e})")
    say("")
    say("Step 5: only explicit confirmation text closes it.")
    ledger.mark_submitted(
        attempt_id,
        confirmation="Ashby confirmation page: 'Application submitted' "
                     "with reference #DEMO-0002, captured 2026-09-22")
    say(f"  state after confirmation: "
        f"{ledger.attempts[attempt_id]['state']}")
    say("")
    say("Step 6: an attempt closed with no evidence is NOT counted.")
    attempt2 = ledger.record_intent("DEMO-synthetic-0003")
    ledger.close_unsubmitted(attempt2)  # reconciled as no-application
    say(f"  honest submission count: {ledger.honest_count()} "
        "(attempts: 2, counted: 1)")
    say("")
    say("  A submission counts ONLY on explicit confirmation evidence.")
    say("  Nothing else.")


def main():
    scratch = tempfile.mkdtemp(prefix="keel-honesty-demo-")
    try:
        say("Keel honesty-gates demo — synthetic data, real engines.")
        say("=======================================================")
        scene1_gap_reported()
        scene2_unverifiable_parked(scratch)
        scene3_explicit_confirmation()
        say("")
        say("Demo complete. Scratch dir cleaned up:", scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
