#!/usr/bin/env python3
"""Dry-run driver for the Input Resolution & Blocker Compression Engine.

Reads the current backlog (needs_input queue + parked pools in the
standard queue), normalizes every blocker, runs the 10-point proof,
collapses duplicates, and reports BEFORE/AFTER compression numbers.

DRY-RUN ONLY: zero writes to queue / tray / answer bank / optimization log.
Report goes to <DATA>/hidden_files/input-resolution-dryrun-<date>.md for
review.

All candidate facts in this module are generic EXAMPLE fixtures — replace
with the applicant's own verified records before any live use.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

_ENG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ENG not in sys.path:
    sys.path.insert(0, _ENG)
from keel_paths import DATA  # noqa: E402

from input_resolution import blocker as B  # noqa: E402
from input_resolution import dedup as D  # noqa: E402
from input_resolution import metrics as M  # noqa: E402
from input_resolution import preferences as P  # noqa: E402

NEEDS_INPUT_STATUSES = {"PARKED-NEEDS-INPUT", "PACKET-READY-NEEDS-APPLICANT"}


def load_queue(path):
    with open(path, encoding="utf-8") as f:
        q = json.load(f)
    return q if isinstance(q, list) else q.get("records", q.get("leads", []))


def extract_blockers(rec):
    """Blocker strings live in the conventional fields the classifier reads."""
    out = []
    un = rec.get("unresolved")
    if isinstance(un, list):
        out.extend([s for s in un if isinstance(s, str) and s.strip()])
    elif isinstance(un, str) and un.strip():
        out.append(un)
    for k in ("status_reason", "gate_note"):
        v = rec.get("gate_note") if k == "gate_note" else rec.get("status_reason")
        if isinstance(v, str) and v.strip():
            # status_reason often carries history; only treat short,
            # blocker-shaped lines as blockers.
            for line in v.splitlines():
                s = line.strip(" |")
                # prescreen packs multiple "; "-separated verdicts per line —
                # split them so each verdict classifies on its own merits.
                parts = ([p.strip() for p in s.split("; ")]
                         if ("(attest):" in s or "(travel):" in s or "Required " in s)
                         else [s])
                for part in parts:
                    if 20 < len(part) < 400 and any(
                            w in part.lower() for w in
                            ("need", "require", "block", "missing", "must", "await",
                             "pending", "unresolved", "applicant", "consent",
                             "attest", "essay", "dropdown", "text:")):
                        out.append(part)
    # de-dupe identical strings within a record, keep order
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def verified_candidate_records():
    """EXAMPLE candidate facts — generic fixtures only.

    Replace every value with the applicant's own verified records before
    any live use. The proof treats these as verified (Class-1) facts, so
    real data must come from the applicant's own standing directives.
    """
    return {
        "phone": ("+1-555-0100", "example candidate record"),
        "email": ("applicant@example.com", "example candidate record"),
        "first_name": ("Example", "example candidate record"),
        "last_name": ("Candidate", "example candidate record"),
        "linkedin": ("https://www.linkedin.com/in/example-candidate",
                     "example candidate record"),
        "location_city": ("Example City, ST", "example candidate record"),
        "us_citizen": ("Yes", "example candidate record"),
        "security_clearance": ("No", "example candidate record"),
    }


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bank_path = os.path.join(DATA, "answer_bank.json")
    try:
        with open(bank_path, encoding="utf-8") as f:
            bank = json.load(f)
    except (FileNotFoundError, ValueError):
        print(f"no answer bank at {bank_path}: running with an empty bank "
              f"(no auto-resolutions)")
        bank = {"answers": {}}
    prefs = P.seed_preferences()
    ctx = B.ProofContext(answer_bank=bank, preferences=prefs,
                         candidate_records=verified_candidate_records())

    recs = load_queue(os.path.join(DATA, "queue", "needs_input-queue.json"))
    std = load_queue(os.path.join(DATA, "queue", "standard-queue.json"))
    parked = [r for r in std if r.get("status") in NEEDS_INPUT_STATUSES
              or r.get("status") == "PARKED-NEEDS-INPUT"]

    blockers: list[B.Blocker] = []
    for rec in recs + parked:
        for raw in extract_blockers(rec):
            b = B.Blocker(lead_id=rec.get("role_id", "?"),
                          company=rec.get("company", "") or "",
                          role=rec.get("title", "") or "",
                          raw_blocker=raw,
                          verified_current_form=False)
            ctx.employer = b.company
            ctx.role_id = b.lead_id
            blockers.append(B.run_proof(b, ctx))

    families = D.collapse(blockers)
    report = M.compute(blockers, families)

    # Novel patterns for the compounding loop (drafts only — NOT appended to
    # learning-proposals.md until reviewed; zero live writes).
    # Honest curation: a "novel pattern" is either a RECURRING unknown theme
    # (same shape across >=2 leads — worth a new family) or a process defect
    # the upstream writers should fix. One-off unknowns are NOT proposals.
    unknowns = [b for b in blockers
                if b.normalized_family == "unknown" and b.classification not in
                {"false_blocker", "duplicate", "resolved_auto", "manual_takeover",
                 "policy_conflict", "agent_action", "agent_verification_required",
                 "form_verification_required", "automation_retry"}]
    theme = Counter(B._sig(b.raw_blocker) or b.raw_blocker[:40] for b in unknowns)
    novel = [b for b in unknowns if theme[B._sig(b.raw_blocker) or b.raw_blocker[:40]] >= 2]
    narrative = [b for b in unknowns
                 if re.search(r"\bparked\b.{0,20}\b(genuine|verified|prepared)\b", b.raw_blocker, re.I)]
    manual = [b for b in blockers if b.classification == "manual_takeover"]

    tap = [f for f in families if f.requires_applicant]
    tap.sort(key=lambda f: len(f.blocker_ids), reverse=True)

    lines = []
    A = lines.append
    A(f"# Input Resolution dry-run — {ts} (UTC)")
    A("")
    A("## BEFORE")
    A(f"- Leads in tray: {report.n_leads}")
    A(f"- Individual blockers: {report.n_blockers}")
    A("")
    A("## AFTER")
    A(f"- Auto-resolved: {report.auto_resolved}")
    A(f"- False blockers removed: {report.false_blockers_removed}")
    A(f"- Answer-bank resolutions: {report.answer_bank_resolutions}")
    A(f"- Duplicates collapsed: {report.duplicates_collapsed}")
    A(f"- Policy conflicts (parked, not re-asked): {report.policy_conflicts}")
    A(f"- Manual actions: {report.manual_actions}")
    A(f"- Legal/attestation: {report.legal_attestation}")
    A(f"- Human-only writing: {report.human_only_writing}")
    A(f"- True applicant decisions: {report.true_applicant_decisions}")
    A("")
    A(f"- COMPRESSION RATE: {report.compression_rate:.1%}")
    A(f"- LEADS UNLOCKED PER DECISION: {report.leads_unlocked_per_decision:.2f} "
      f"(baseline 1.00, no dedup)")
    A(f"- false_blocker_rate={report.false_blocker_rate:.2%} "
      f"duplicate_question_rate={report.duplicate_question_rate:.2%} "
      f"answer_bank_reuse_rate={report.answer_bank_reuse_rate:.2%} "
      f"agent_resolvable_blocker_rate={report.agent_resolvable_blocker_rate:.2%}")
    A("")
    A("## Compressed tap list (highest leverage first)")
    for f in tap[:25]:
        A(f"- **{f.family}/{f.variant}** — {len(f.blocker_ids)} applications "
          f"[{f.classification}]")
        A(f"  {f.decision_prompt}")
        for co, ro in f.affected_roles[:6]:
            A(f"  - {co} — {ro}")
        if len(f.affected_roles) > 6:
            A(f"  - … +{len(f.affected_roles) - 6} more")
    if len(tap) > 25:
        A(f"- … +{len(tap) - 25} more decision families")
    A("")
    A("## Manual actions (separate from decisions)")
    for b in manual[:20]:
        A(f"- [{b.lead_id[:50]}] {b.raw_blocker[:110]}")
    if len(manual) > 20:
        A(f"- … +{len(manual) - 20} more")
    A("")
    A("## Classification histogram")
    for cls, n in Counter(b.classification for b in blockers).most_common():
        A(f"- {cls}: {n}")
    A("")
    A("## Novel blocker patterns (draft learning-proposals, NOT yet appended)")
    A("")
    A("### Recurring unknown themes (candidate new families)")
    seen_pat = set()
    shown = 0
    for b in novel:
        key = (B._sig(b.raw_blocker) or b.raw_blocker[:40])
        if key in seen_pat:
            continue
        seen_pat.add(key)
        shown += 1
        if shown > 10:
            break
        A(f"- [{b.lead_id[:50]}] {b.raw_blocker[:110]}")
    if shown == 0:
        A("- none: no unknown theme recurred across >=2 leads this run")
    A("")
    A("### Process defects (upstream writer fixes)")
    if narrative:
        A("- Narrative park summaries are not actionable blockers "
          f"({len(narrative)} this run). PROPOSAL: park paths (prescreen/verify) "
          "should enumerate concrete required fields in `unresolved` instead of "
          "narrative summaries, so the tray stays machine-actionable.")
        for b in narrative[:5]:
            A(f"  - [{b.lead_id[:50]}] {b.raw_blocker[:100]}")
    else:
        A("- none observed")
    A("")
    A("_Zero live writes performed. Optimization-log entry prepared in "
      "metrics.optimization_log_entry(); appending is parent-authorized._")

    out_dir = os.path.join(DATA, "hidden_files")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"input-resolution-dryrun-{ts}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("blockers:", report.n_blockers, "leads:", report.n_leads)
    print("true_applicant_decisions:", report.true_applicant_decisions,
          "LUPD:", round(report.leads_unlocked_per_decision, 2))
    print("report:", out_path)


if __name__ == "__main__":
    main()
