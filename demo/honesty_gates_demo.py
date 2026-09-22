#!/usr/bin/env python3
"""Keel honesty-gates terminal demo — runs REAL repo engines against
SYNTHETIC fixture input.

Demonstrates the three honest-automation contract clauses:
  1. Truthfulness gates — an answer-bank gap is REPORTED, never filled.
  2. Fail closed      — a posting that violates the office/travel policy
                        is PARKED, never proceeded with.
  3. Explicit confirmation — a submission counts ONLY on explicit
                        confirmation text; an ambiguous attempt goes
                        UNKNOWN and bars any retry.

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

from engines import answer_resolver as ar      # noqa: E402
from engines import prescreen as ps           # noqa: E402
from engines import submit_intent as si        # noqa: E402

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
    say("global-scope keys (start date, work authorization). The form asks")
    say("a 3rd question — salary history — which the bank does NOT carry.")
    say("")
    bank = {
        "start_timeframe": {"value": "Next day", "scope": "global",
                            "provenance": "applicant stated 2026-09-20"},
        "us_work_auth": {"value": "Yes", "scope": "global",
                         "provenance": "applicant stated 2026-09-20"},
    }
    questions = [
        ("us_work_auth", "Are you authorized to work in the United States?"),
        ("start_timeframe", "When can you start?"),
        ("salary_history", "What was your salary at your last job?"),  # the gap
    ]
    filled, abstained = 0, 0
    for key, label in questions:
        res = ar.resolve(key, bank.get(key, {}))
        if res.status == ar.STATUS_ABSTAIN:
            abstained += 1
            say(f"  [ABSTAIN] {label!r}")
            say(f"           -> {res.reason}  (gap reported, NOT invented)")
        else:
            filled += 1
            say(f"  [RESOLVED] {label!r} = {res.value!r}")
    say("")
    say(f"  result: {filled} answered from the bank, {abstained} abstained.")
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
            say(f"  park_lead -> ok: lead moved to needs_input-queue.json")
            say("  (needs the applicant's own answer before anything proceeds).")
        else:
            say(f"  park_lead -> REFUSED: {out.get('error')}")
    else:
        say("  (demo note: expected PARK verdict did not fire — inspect)")
    say("  Keel never proceeds past a commitment it cannot verify.")


def scene3_explicit_confirmation(scratch):
    say("")
    say("=== SCENE 3: explicit confirmation — nothing counts without it ===")
    say("")
    store_dir = os.path.join(scratch, "intents")
    os.makedirs(store_dir, exist_ok=True)
    si.set_store_dir(store_dir)
    try:
        role_id = "DEMO-synthetic-0002"
        say("Step 1: intent is recorded BEFORE any attempt.")
        attempt_id = si.record_intent(
            role_id, "Fictional Corp", transport="browser",
            bundle_digest="sha256:demo-synthetic-digest",
            page_ref={"board": "demo-board", "job_id": "0002"})
        say(f"  recorded intent {attempt_id}  state=INTENT")
        say("")
        say("Step 2: the attempt times out ambiguously.")
        say("  (A timeout after POST may already have been accepted.)")
        si.mark_unknown(attempt_id, reason="network timeout after POST",
                        evidence={"http_status": None})
        say(f"  state after ambiguous outcome: {si.get(attempt_id)['state']}")
        say("  An ambiguous attempt is NEVER reported as a plain failure")
        say("  and NEVER retried — that would risk a duplicate.")
        say("")
        say("Step 3: a second attempt for the same role is refused.")
        try:
            si.record_intent(role_id, "Fictional Corp", transport="browser",
                             bundle_digest="sha256:demo-synthetic-digest")
            say("  !! unexpected: second attempt minted")
        except si.OpenIntentExists:
            say("  REFUSED: OpenIntentExists — the role stays barred")
            say("  until the open attempt is reconciled.")
        say("")
        say("Step 4: marking SUBMITTED without confirmation is refused.")
        try:
            si.mark_submitted(attempt_id, "")
            say("  !! unexpected: empty confirmation accepted")
        except ValueError as e:
            say(f"  REFUSED: ValueError ({e})")
        say("")
        say("Step 5: only explicit confirmation text closes it.")
        si.mark_submitted(
            attempt_id,
            confirmation="Ashby confirmation page: 'Application submitted' "
                         "with reference #DEMO-0002, captured 2026-09-22")
        say(f"  state after confirmation: {si.get(attempt_id)['state']}")
        say("")
        say("  A submission counts ONLY on explicit confirmation evidence.")
        say("  Nothing else.")
    finally:
        si.reset_store_dir()


def main():
    scratch = tempfile.mkdtemp(prefix="keel-honesty-demo-")
    try:
        say("Keel honesty-gates demo — synthetic data, real engines.")
        say("=======================================================")
        scene1_gap_reported()
        scene2_unverifiable_parked(scratch)
        scene3_explicit_confirmation(scratch)
        say("")
        say("Demo complete. Scratch dir cleaned up:", scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
