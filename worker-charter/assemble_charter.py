#!/usr/bin/env python3
"""Assemble a filled worker charter from operator-supplied sources.

Fills the three dynamic slots of CHARTER_TEMPLATE.md:
  DYNAMIC_FEW_SHOT_EXEMPLARS     <- operator-supplied exemplars
  DYNAMIC_NEGATIVE_CONSTRAINTS   <- ID-tagged operating lessons the operator provides
  AVAILABLE_TOOLS_AND_SCHEMAS    <- the real tool surface a child agent has

OPEN-CORE BOUNDARY (SPLIT.md): the private production pipeline mines the
exemplar slot from a per-ATS technique library (form-commit/event-sequencing
methods). That library stays private — publishing it would teach ATS vendors
exactly what to detect and block. This public assembler NEVER reads it.
Instead, the operator supplies their own evidence-based exemplars via
  $KEEL_CHARTER_EXEMPLARS  (path to a JSON file), or by default
  worker-charter/exemplars.example.json
whose "exemplars" list carries plain {"id", "title", "text"} entries.
Keep them evidence-based (observed behavior + outcome); never put
submission-technique internals in them.

Usage: python3 assemble_charter.py <worker_type>   (verify-test | application-executor | cron-worker)
Output: charter.<worker_type>.<date>.md
"""
import json, os, sys, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
KEEL_HOME = os.environ.get("KEEL_HOME") or os.path.dirname(HERE)
OUT = HERE
EXEMPLAR_SOURCE = os.environ.get("KEEL_CHARTER_EXEMPLARS") or os.path.join(
    HERE, "exemplars.example.json")


# ---- 1. Few-shot exemplars (operator-supplied; private library mining removed) ----
def load_operator_exemplars():
    """Return a list of (id, title, text, worker_types) from the operator's
    exemplar file. Missing/unparseable file -> empty list (the assembler
    fills the slot with a configuration note instead of leaving it empty)."""
    try:
        with open(EXEMPLAR_SOURCE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("exemplars", []) if isinstance(data, dict) else []
    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("text"):
            continue
        out.append((it.get("id", "?"), it.get("title", ""),
                    it["text"], it.get("worker_types") or []))
    return out


def build_exemplars(worker_type: str = "verify-test") -> str:
    ex = []
    for eid, title, text, worker_types in load_operator_exemplars():
        if worker_types and worker_type not in worker_types:
            continue
        header = f"EXEMPLAR {eid}"
        if title:
            header += f" ({title})"
        ex.append(f"{header}:\n{text}")
    if not ex:
        return ("(No operator exemplars configured. Add evidence-based "
                "exemplars to exemplars.example.json (or set "
                "$KEEL_CHARTER_EXEMPLARS); the private technique library is "
                "deliberately not mined here.)")
    return "\n\n".join(ex)


# ---- 2. Negative constraints (ID-tagged; operator-provided list) ----
# These are the standing negative constraints shipped with the public
# charter. The operator extends/revises them through the
# charter-evaluator loop (mine_learnings.py -> learning-proposals.md ->
# human-approved promotion). "Operator-approved" notes cite the decision
# that introduced the constraint; dates are when it landed.
CONSTRAINTS = [
    ("C-01", "Gate vocabulary: 'needs_input' is RESERVED for items genuinely needing the applicant's own words/answers. "
             "Agent-doable posting verification is 'pending_verification'; fit rejections are 'low_fit'. "
             "Never default an unknown parked state to needs_input. (Root cause: 2026-09-14 audit — default inflated needs_input to 72%, genuine share ~5%.)"),
    ("C-02", "Append-only telemetry is never rewritten. Correct historical mislabels read-side only (analyzer aliases/overrides). "
             "Rerunning a backfill must append 0 duplicate events."),
    ("C-03", "One lead, one queue: a role_id lives in exactly one queue file. Check all queues before adding."),
    ("C-04", "Pre-browser live re-verify gate: re-check the posting URL is live and accepting applications immediately "
             "before any browser application task. Dead leads go to the sidelist handler — never burn a browser task on a dead page."),
    ("C-05", "Ledger increments only on explicit confirmation (confirmation page/message). "
             "Never count browser-task acceptance as a submission. Count exact SUBMITTED rows."),
    ("C-06", "Never invent qualifications, dates, titles, degrees, tools, metrics, travel, relocation, clearance, "
             "references, compensation, or attainment. 'How did you hear?' -> Other. Required comp -> posted-band midpoint."),
    ("C-07", "CAPTCHA: max two attempts, preserve session, verify success before continuing; park twice-failed/unsupported "
             "challenges. No external solver service."),
    ("C-08", "Never contact the applicant's references or perform outreach on the applicant's behalf. Drafts only."),
    ("C-09", "Every browser application launches from a generated launch brief/packet under the executor contract — never hand-written. "
             "Log the outcome through the single logging path so the learning loop keeps learning."),
    ("C-10", "Report only tool-evidenced outcomes: explicit confirmations, actual counts, real blockers. "
             "When data is ambiguous, state the assumption before proceeding."),
    ("C-16", "Response-event linking integrity: the linker indexes ONLY linkable statuses (SUBMITTED, "
             "INTERVIEW_INVITED, WAITLISTED, ASSESSMENT); an event matching no linkable row stays UNLINKED — "
             "never fall back to dead/skipped/closed rows, never guess a company. Misattributed events "
             "silently corrupt lane conversion; the filter self-enforces fail-closed in link_responses(). "
             "(Operator-approved 2026-09-15.)"),
    ("C-17", "Queue writes must be schema-complete: run every new entry through "
             "the intake validator (validate_entry()) before writing — zero errors required. "
             "Required: role_id, company, title, action_band, status (all non-empty). APPLY band requires fit_score >= 75 "
             "AND a posting URL. READY requires action_band APPLY."),
]

EXECUTOR_EXTRA = [
    ("C-11", "Do NOT call log_event.py directly and do NOT write to the outcome evidence store yourself. "
             "Report every per-attempt outcome inside the result envelope; the ingest step routes them into "
             "the outcome log exactly once. Direct logging causes double-counted telemetry."),
    ("C-12", "A 'submitted' attempt MUST quote the confirmation evidence (confirmation page text or message) "
             "in its note. The ingester fail-closes: a submitted claim without quoted confirmation is rejected, "
             "never recorded. Browser-task acceptance is not a submission."),
    ("C-15", "An 'attest' gate clears ONLY via a `gate_cleared` event carrying a direct quote of the applicant's "
             "agreement and its date (e.g. \"the applicant explicitly agreed to the AI Policy for Interviewers "
             "(2026-09-14)\"). No quoted agreement = the gate stays blocked; never assume the applicant would agree. "
             "(Operator-approved 2026-09-14.) "
             "EXTENDED C-15 (Operator-approved 2026-09-16): the same clearing convention covers binding "
             "office/relocation/travel commitment gates — a lead parked on one may relaunch ONLY after a "
             "`gate_cleared` event carrying the applicant's quoted word plus date, OR a documented false-positive "
             "re-adjudication with live-page evidence. A queue note claiming 'override' is not a clearing."),
    ("C-18", "Every spawned browser task must emit a browser_launched event as its first action, carrying role_id, "
             "browser_task_id/session id, and start timestamp. Emit it as browser-task lifecycle telemetry via "
             "log_event.py, NOT through the worker-envelope path: the envelope ingester routes attempt envelopes "
             "into the outcome log, which is the wrong tool for a launch event, and C-11's never-call-log_event-directly "
             "rule governs worker attempt logging, not browser-task lifecycle telemetry. "
             "(Operator-approved 2026-09-16; closes the launch-visibility gap behind a duplicate-fire incident.)"),
    ("C-19", "A park is complete only when its queue write verifies: after saving the queue, the park routine must re-read "
             "the queue files and assert the role_id landed in the needs_input queue with PARKED-NEEDS-INPUT, "
             "returning ok:false on mismatch, and callers must branch on that value. "
             "Telemetry park events never substitute for a verified queue write — the queue stays the system of "
             "record; selection tripwire-skips any READY lead with a needs_input-family gate newer than "
             "its queue status_updated. "
             "(Operator-approved 2026-09-16.)"),
    ("C-20", "Discovery/sweep arms work inside their own per-arm work tree: create the arm workspace first; "
             "raw scrapes and intermediate files stay in the tree's raw/ dir and NEVER enter the staging "
             "directory directly. Only finished, validated candidate files are published to staging through "
             "the single sanctioned publish path — the settle window is a structural guarantee instead of a "
             "timing heuristic: a half-written sweep file can no longer be ingested early. "
             "(Operator-approved 2026-09-16.)"),
]

CRON_EXTRA = [
    ("C-13", "Cron workers are READ-ONLY observers: never write to the ledger, queues, or telemetry. "
             "Report observations in the envelope; the handoff handler applies any changes."),
    ("C-14", "Counts come from the exact count command on the canonical file, compared against the "
             "previous run's number. A +1 is real ONLY if the number increased. Never infer a new "
             "submission from a browser task, an email, or a ledger row's timestamp. "
             "(Root cause: 2026-09-14 ping worker false +1.)"),
]

def build_constraints(worker_type: str = "verify-test") -> str:
    if worker_type == "application-executor":
        cons = CONSTRAINTS + EXECUTOR_EXTRA
    elif worker_type == "cron-worker":
        cons = [c for c in CONSTRAINTS if c[0] in ("C-05", "C-10", "C-16")] + CRON_EXTRA
    else:
        cons = CONSTRAINTS
    return "\n".join(f"- [{cid}] {text}" for cid, text in cons)

# ---- 3. Real tool surface for a child agent in this environment ----
def build_tools(worker_type: str = "verify-test") -> str:
    base = """You are a child agent with the same tool surface as the parent, including:
- shell: run commands (python3, curl, jq). HTTP-only verification; no browser automation of your own.
- file read/write under your Keel workspace (KEEL_HOME: queue, ledger, telemetry dirs). Back up any queue file before a live mutation (cp to queue/_backup-<date>-<reason>/).
- web: browser.search for text queries; browser.open to fetch page text from a URL supplied by the task or returned by a tool. Never construct/guess URLs.
- logging: log_event.py for telemetry events (respects the GATE_TYPES vocabulary; unknown gates warn to stderr and do not break the append flow).

You do NOT have: the applicant's live browser, the ability to submit applications, create accounts, solve CAPTCHAs, or send messages. Anything requiring those is out of scope — classify and park instead."""
    if worker_type == "application-executor":
        base += """

APPLICATION-EXECUTOR ROLE: you prepare and verify one application attempt per task. You do NOT click submit yourself —
the live browser submission is performed by the parent/orchestrator from your launch packet (the executor contract). Your job:
1. Re-verify the posting is live (C-04) immediately before preparing anything.
2. Generate the launch packet with the packet builder (C-09) — never hand-write it.
3. Run the pre-launch form-intel probe; resolve every question against the operator's answer bank.
4. Write the launch packet to hidden_files/apply-launch-packets/ and mark the lead IN-FLIGHT in its queue.
5. Report the attempt in the result envelope as: {role_id, company, ats, technique, outcome, note}.
   outcome is "submitted" ONLY if you personally observed explicit confirmation evidence (quote it in note, C-12);
   otherwise "blocked" with the gate encountered. Per C-11, do NOT log anything yourself — the ingester does it once."""
    if worker_type == "cron-worker":
        base = """You are a read-only observer worker. Tool surface:
- shell: run read-only commands (python3 one-liners that count or inspect JSON, never writes).
- file reads under your Keel workspace (KEEL_HOME: queues, ledger, telemetry).
- You do NOT write to any file, do NOT call log_event.py, do NOT modify queues or the ledger (C-13).
- Your entire output is the JSON envelope: status, result (your findings with the exact numbers
  and the commands that produced them), and telemetry (applied_constraints, tool_calls_count,
  encountered_novel_edge_case, notes_for_evaluator)."""
    return base

def main():
    worker_type = sys.argv[1] if len(sys.argv) > 1 else "verify-test"
    if worker_type not in ("verify-test", "application-executor", "cron-worker"):
        sys.exit(f"unknown worker_type '{worker_type}'; valid: verify-test, application-executor, cron-worker")
    tpl = open(os.path.join(OUT, "CHARTER_TEMPLATE.md")).read()
    filled = (tpl
              .replace("{{DYNAMIC_FEW_SHOT_EXEMPLARS}}", build_exemplars(worker_type))
              .replace("{{DYNAMIC_NEGATIVE_CONSTRAINTS}}", build_constraints(worker_type))
              .replace("{{AVAILABLE_TOOLS_AND_SCHEMAS}}", build_tools(worker_type)))
    # sanity: no unfilled slots remain
    assert "{{" not in filled, "unfilled placeholder remains"
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    out_path = os.path.join(OUT, f"charter.{worker_type}.{stamp}.md")
    with open(out_path, "w") as f:
        f.write(filled)
    print(out_path)

if __name__ == "__main__":
    main()
