#!/usr/bin/env python3
"""Live apply for the Input Resolution & Blocker Compression Engine.

Re-runs the engine on CURRENT queue state (never a stale report) and applies:
  - resolved_auto / false_blocker / dropped_stalled: removed from conventional
    fields; provenance appended to queue_notes (worded as cleared history,
    not a live blocker).
  - duplicate: collapsed — one FAMILY[family/variant] entry per lead per
    decision family (applicant-requiring families only).
  - fully-unblocked leads: conventional fields cleaned so is_verify_only()
    passes; the lead is NOT moved here — verify_retry (canonical owner)
    verifies and promotes on its next run.
  - input-tray watermark pre-seeded with rewritten keys (re-expression of
    already-logged blockers, not newly parked — no digest spam).
  - optimization-log entry appended (metrics-first: baseline vs measured post).
  - one learning-proposal appended (compounding loop; triage stays human).

SAFETY:
  - Backup of both queue files + tray watermark BEFORE any write.
  - A PID lockfile guards the read-modify-write.
  - Crash-safe writes (tmp + os.replace).
  - Fail closed: a record that raises mid-mutation is left byte-identical;
    fully-unblocked claims are verified with the REAL is_verify_only().
  - --dry-run prints the planned mutation summary and writes nothing.
  - The answer bank defaults to DATA/answer_bank.json (--bank to override).
    --live refuses to run without one; --dry-run proceeds with an empty
    bank (no auto-resolutions against missing data).

Usage:
  python3 apply.py --dry-run            # review only
  python3 apply.py --dry-run --bank PATH
  python3 apply.py --live               # authorized live apply
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

_ENG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ENG not in sys.path:
    sys.path.insert(0, _ENG)
from keel_paths import DATA  # noqa: E402

from input_resolution import blocker as B  # noqa: E402
from input_resolution import dedup as D  # noqa: E402
from input_resolution import preferences as P  # noqa: E402
from input_resolution.dry_run import (  # noqa: E402
    extract_blockers, verified_candidate_records, NEEDS_INPUT_STATUSES,
)
from verify_retry import is_verify_only  # noqa: E402

PDT = ZoneInfo("America/Los_Angeles")
STAMP = datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT")
STAMP_UTC = datetime.now().astimezone().strftime("%Y-%m-%dT%H-%M-%S")

STD_Q = os.path.join(DATA, "queue", "standard-queue.json")
NI_Q = os.path.join(DATA, "queue", "needs_input-queue.json")
WM = os.path.join(DATA, "hidden_files", "input-tray-logged.json")
OPT_LOG = os.path.join(DATA, "hidden_files", "optimization-log.jsonl")
PROPOSALS = os.path.join(DATA, "hidden_files", "learning-proposals.md")
LOCK_PATH = os.path.join(DATA, "hidden_files", "input_resolution.lock")

REMOVED_CLASSES = {"resolved_auto", "false_blocker", "dropped_stalled"}

# Classifications that genuinely need the applicant's own words/choice
# (tray-worthy).
GENUINE_APPLICANT_CLASSES = {"user_fact", "user_preference",
                             "user_self_assessment", "user_compensation",
                             "user_role_exception", "essay_applicant_only",
                             "legal_attestation", "free_text_applicant_only",
                             "generic_attestation"}

# Lines already decided/cleared are not re-hashed into tray keys.
POLICY_DECIDED_PAT = re.compile(
    r"(?i)(already decided|decided:|cleared\b|no longer open|not a live blocker)")


def _tray_key(rid: str, text: str) -> str:
    """Stable tray key: sha256 of role_id + normalized blocker text."""
    norm = re.sub(r"\s+", " ", str(text)).strip().lower()
    return hashlib.sha256(f"{rid}|{norm}".encode("utf-8")).hexdigest()[:16]


@contextmanager
def queue_lock(owner="input_resolution:apply"):
    """PID lockfile for the read-modify-write window."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {owner}".encode())
        os.close(fd)
        held = True
    except FileExistsError:
        held = False
    try:
        yield held
    finally:
        if held:
            try:
                os.remove(LOCK_PATH)
            except OSError:
                pass


def load_list(path):
    with open(path, encoding="utf-8") as f:
        q = json.load(f)
    return q if isinstance(q, list) else q.get("records", q.get("leads", []))


def proof_source(b: B.Blocker) -> str:
    if b.answer_bank_match:
        return f"answer-bank:{b.answer_bank_match}"
    det = " ".join(str(v.get("detail", "")) for v in (b.proof or {}).values()
                   if isinstance(v, dict))
    if "standing preference" in det.lower() or "preference" in det.lower():
        return "standing-preference"
    if "candidate record" in det.lower():
        return "candidate-record"
    return "10-point-proof"


def cleared_note(raw: str, b: B.Blocker) -> str:
    # History, never a live blocker: worded as cleared so downstream
    # classifiers strip it from live-blocker input.
    return (f"input-resolution {STAMP}: blocker cleared — "
            f"\"{raw[:70]}\" -> {b.resolution or b.classification} "
            f"[proof: {proof_source(b)}]")


def family_entry(fam: str, var: str, prompt: str, n: int) -> str:
    return f"FAMILY[{fam}/{var}]: {prompt} [{n} applications]"


def qn_with_notes(rec: dict, notes: list[str]):
    """Append provenance notes to queue_notes, preserving list-typed fields
    (sweep workers sometimes write queue_notes as lists)."""
    qn = rec.get("queue_notes")
    if isinstance(qn, list):
        return qn + notes
    return ((qn or "") + " | " + " | ".join(notes)).strip(" |")


def blocker_raws(rec: dict) -> list[str]:
    """Raw blocker instances for proof/dedup: extract_blockers minus our own
    bookkeeping entries (RESOLVED:/FAMILY[), so re-runs are idempotent."""
    out = []
    for raw in extract_blockers(rec):
        t = str(raw).strip()
        if not t or t.startswith("RESOLVED:") or t.startswith("FAMILY["):
            continue
        out.append(t)
    return out


# Applicant families where ONE judgment is genuinely portable across
# applications (accept-as-described / park / standing rule). All other
# applicant families are employer-, wording-, or lead-specific:
# attestations and legal agreements differ per employer, essays are per-lead
# creative work, facts are per-lead. Cross-lead duplicates in non-portable
# families are NOT collapsed — each lead keeps its own raw blocker
# (within-lead duplicates still collapse).
PORTABLE_APPLICANT_FAMILIES = {"travel_commitment", "office_frequency",
                               "interview_recording_consent"}


def build_global_state(records, bank, prefs, candidate_records):
    """Run the 10-point proof on every blocker across ALL target records,
    then collapse duplicates GLOBALLY (cross-application).

    Materiality guard: for applicant families that are NOT portable, a
    duplicate whose canonical head sits on a DIFFERENT lead is reverted to
    its proven classification — collapsing materially different commitments
    (attestation wordings, per-employer legal agreements, per-lead essays)
    into one decision would manufacture a false standing decision.

    Returns (all_blockers, index_to_family). Each Blocker keeps
    .lead_id so per-record mutation can select its slice.
    """
    allb: list[B.Blocker] = []
    for rec in records:
        rid = rec.get("role_id", "?")
        for raw in blocker_raws(rec):
            b = B.Blocker(lead_id=rid, company=rec.get("company", "") or "",
                          role=rec.get("title", "") or "", raw_blocker=raw,
                          verified_current_form=False)
            ctx = B.ProofContext(answer_bank=bank, preferences=prefs,
                                 candidate_records=candidate_records)
            ctx.employer = b.company
            ctx.role_id = b.lead_id
            allb.append(B.run_proof(b, ctx))
    pre_class = {i: b.classification for i, b in enumerate(allb)}
    families = D.collapse(allb)
    for f in families:
        if not (f.requires_applicant
                and f.family not in PORTABLE_APPLICANT_FAMILIES):
            continue
        # Non-portable applicant family: dedup is per-lead only. The first
        # member on EACH lead is that lead's head (proven classification
        # restored); later members on the same lead stay collapsed.
        by_lead: dict[str, list[int]] = {}
        for mi in f.member_indices:
            by_lead.setdefault(allb[mi].lead_id, []).append(mi)
        for lead, mis in by_lead.items():
            head_mi = mis[0]
            if allb[head_mi].classification == "duplicate":
                allb[head_mi].classification = pre_class[head_mi]
                allb[head_mi].duplicate_of = None
            for mi in mis[1:]:
                allb[mi].classification = "duplicate"
                allb[mi].duplicate_of = lead
    idx_to_fam = {}
    for f in families:
        for mi in f.member_indices:
            idx_to_fam[mi] = f
    return allb, idx_to_fam


def mutate_record(rec: dict, allb=None, idx_to_fam=None) -> dict | None:
    """Return mutation dict or None if the record needs no change.

    allb/idx_to_fam come from build_global_state (cross-application
    dedup). When omitted (unit checks), falls back to a per-record pass.
    """
    rid = rec.get("role_id", "?")
    bank = mutate_record.bank
    prefs = mutate_record.prefs
    candidate_records = mutate_record.records
    if allb is None:
        recs = [rec]
        allb, idx_to_fam = build_global_state(recs, bank, prefs,
                                              candidate_records)
    mine = [(i, b) for i, b in enumerate(allb) if b.lead_id == rid]
    if not mine:
        return None
    blockers = [b for _, b in mine]

    new_unresolved: list[str] = []
    notes: list[str] = []
    need_fam: dict[tuple[str, str], D.DecisionFamily] = {}
    replaced_raws: dict[str, list[str]] = {}
    removed = 0
    dup_n = 0

    def _track_family(fam: D.DecisionFamily, raw: str):
        need_fam[(fam.family, fam.variant)] = fam
        replaced_raws.setdefault(rid, []).append(raw)

    for gi, b in mine:
        f = idx_to_fam.get(gi)
        if b.classification in REMOVED_CLASSES:
            removed += 1
            notes.append(cleared_note(b.raw_blocker, b))
            continue
        if b.classification == "duplicate" and f is not None:
            dup_n += 1
            # FAMILY entries are cross-application only: the entry is worth
            # the tray space only when >1 distinct leads share the decision
            # AND the family's decision is genuinely portable (one judgment
            # covers all applications — never for per-employer legal
            # commitments, attestations, or per-lead essays).
            if (f.requires_applicant and len(f.blocker_ids) > 1
                    and f.family in PORTABLE_APPLICANT_FAMILIES):
                _track_family(f, b.raw_blocker)
            continue
        if (f is not None and len(f.blocker_ids) > 1 and f.requires_applicant
                and f.family in PORTABLE_APPLICANT_FAMILIES
                and b.classification != "duplicate"):
            # canonical head of a multi-lead portable applicant family ->
            # entry
            _track_family(f, b.raw_blocker)
            continue
        new_unresolved.append(b.raw_blocker)

    for (fam, var), f in sorted(need_fam.items()):
        new_unresolved.append(
            family_entry(fam, var, f.decision_prompt, len(f.blocker_ids)))
    if dup_n:
        fams = ", ".join(f"FAMILY[{a}/{b}]" for a, b in sorted(need_fam))
        notes.append(f"input-resolution {STAMP}: {dup_n} duplicate blockers "
                     f"cleared — collapsed into {fams or 'heads'}")

    if not new_unresolved:
        # Fully unblocked: every extracted blocker resolved or collapsed away.
        # Clean conventional fields so the canonical verify path can take it.
        # Verify the claim with the REAL classifier, on the exact post-mutation
        # field values (including the new provenance notes).
        trial = dict(rec)
        trial["unresolved"] = []
        trial["queue_notes"] = qn_with_notes(rec, notes)
        trial["status_reason"] = (
            f"input-resolution {STAMP}: all input blockers cleared "
            f"({removed + dup_n}); liveness unverified — queued for verification")
        # scrub resolved raws out of gate_note lines
        gn = rec.get("gate_note") or ""
        my_raws = [b.raw_blocker for _, b in mine]
        kept = [ln for ln in str(gn).splitlines()
                if not any(r in ln for r in my_raws)]
        trial["gate_note"] = "\n".join(kept).strip() or (
            f"input-resolution {STAMP}: no open input blockers")
        if is_verify_only(trial):
            return {"unresolved": [], "status_reason": trial["status_reason"],
                    "gate_note": trial["gate_note"], "notes": notes,
                    "fully_unblocked": True, "removed": removed + dup_n,
                    "replaced_raws": replaced_raws}
        # else: fail closed — keep the unresolved cleanup but do NOT claim
        # unblocked; leave status_reason/gate_note untouched.
        notes.append(f"input-resolution {STAMP}: unresolved cleared but "
                     f"classifier still sees input blockers — left for review")
        return {"unresolved": new_unresolved, "notes": notes,
                "fully_unblocked": False, "removed": removed + dup_n,
                "review": True, "replaced_raws": replaced_raws}

    if removed == 0 and dup_n == 0 and not need_fam:
        return None
    return {"unresolved": new_unresolved, "notes": notes,
            "fully_unblocked": False, "removed": removed + dup_n,
            "replaced_raws": replaced_raws}


def digest_keys_for(rid: str, unresolved: list) -> list[str]:
    out = []
    for u in unresolved or []:
        t = str(u)
        if t.startswith("RESOLVED"):
            continue
        if POLICY_DECIDED_PAT.search(t):
            continue
        out.append(_tray_key(rid, t))
    return out


def _load_bank(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def main():
    live = "--live" in sys.argv
    bank_path = os.path.join(DATA, "answer_bank.json")
    if "--bank" in sys.argv:
        bank_path = sys.argv[sys.argv.index("--bank") + 1]
    bank = _load_bank(bank_path)
    if bank is None:
        if live:
            print(f"refusing --live: no answer bank at {bank_path} "
                  f"(pass --bank PATH)")
            return 1
        print(f"no answer bank at {bank_path}: dry-run with an empty bank "
              f"(no auto-resolutions)")
        bank = {"answers": {}}
    mutate_record.bank = bank
    mutate_record.prefs = P.seed_preferences()
    mutate_record.records = verified_candidate_records()

    with queue_lock() as held:
        if not held:
            print("input_resolution:apply — another apply holds the lock; "
                  "aborting")
            return 2
        std = load_list(STD_Q)
        ni = load_list(NI_Q)

        targets = []  # (queue_name, record)
        for r in ni:
            targets.append(("needs_input", r))
        for r in std:
            st = (r.get("status") or "")
            if st in NEEDS_INPUT_STATUSES or st == "PARKED-NEEDS-INPUT":
                targets.append(("standard", r))

        # Global proof + cross-application dedup over the whole target pool.
        records = [rec for _, rec in targets]
        allb, idx_to_fam = build_global_state(
            records, bank, mutate_record.prefs, mutate_record.records)

        plans = []
        for qname, rec in targets:
            try:
                m = mutate_record(rec, allb, idx_to_fam)
            except Exception as exc:  # fail closed: leave byte-identical
                plans.append((qname, rec, None, f"mutation error: {exc}"))
                continue
            plans.append((qname, rec, m, ""))

        changed = [p for p in plans if p[2]]
        need_review = [p for p in changed if p[2].get("review")]
        fully = [p for p in changed if p[2].get("fully_unblocked")]
        total_removed = sum(p[2]["removed"] for p in changed)

        # Honest metrics: classification breakdown over the global proof,
        # plus unresolved-field instance counts before/after.
        from collections import Counter
        class_counts = Counter(b.classification for b in allb)
        n_dup = class_counts.get("duplicate", 0)

        def field_raw_count(recs):
            n = 0
            for r in recs:
                for u in r.get("unresolved") or []:
                    t = str(u).strip()
                    if t and not t.startswith("RESOLVED:") \
                            and not t.startswith("FAMILY["):
                        n += 1
            return n

        n_before_field = field_raw_count(records)
        n_leads = len(targets)
        n_families = len({(f.family, f.variant) for f in idx_to_fam.values()})
        multi_lead_fams = len({(f.family, f.variant) for f in idx_to_fam.values()
                               if len(f.blocker_ids) > 1
                               and f.family in PORTABLE_APPLICANT_FAMILIES})

        print(f"leads scanned: {n_leads} | blocker instances (global proof): {len(allb)}")
        print(f"  classifications: " + ", ".join(
            f"{k}={v}" for k, v in sorted(class_counts.items())))
        print(f"  duplicate instances (cross-app): {n_dup} | "
              f"decision families: {n_families} ({multi_lead_fams} multi-lead)")
        print(f"  unresolved-field raw blockers before: {n_before_field}")
        print(f"records to mutate: {len(changed)} "
              f"(fully unblocked: {len(fully)}, need review: {len(need_review)})")
        for qname, rec, m, err in need_review[:10]:
            print(f"  REVIEW {rec.get('role_id', '?')[:60]}: {m['notes'][-1][:100]}")

        if not live:
            print("--dry-run: no writes performed")
            return 0

        # ---- backup ----
        bdir = os.path.join(DATA, "queue",
                            f"_backup-{STAMP_UTC}-input-resolution-apply")
        os.makedirs(bdir, exist_ok=True)
        for src in (STD_Q, NI_Q, WM):
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(bdir, os.path.basename(src)))
        print(f"backup: {bdir}")

        # ---- apply ----
        for qname, rec, m, err in changed:
            if m is None:
                continue
            rec["unresolved"] = m["unresolved"]
            if m.get("status_reason"):
                rec["status_reason"] = m["status_reason"]
            if m.get("gate_note") is not None:
                rec["gate_note"] = m["gate_note"]
            rec["queue_notes"] = qn_with_notes(rec, m["notes"])

        def save(p, rows):
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rows, f, indent=1)
            os.replace(tmp, p)

        save(STD_Q, std)
        save(NI_Q, ni)

        # ---- watermark pre-seed ----
        # Only FAMILY[...] re-expressions whose underlying raw blockers were
        # ALL already presented are pre-seeded (true rewordings, not new
        # information). Kept raw blockers keep their original keys, so the
        # existing watermark handles them: already-presented ones stay
        # silent, never-presented ones correctly post as new.
        try:
            seen = set(json.load(open(WM, encoding="utf-8")))
        except (FileNotFoundError, ValueError):
            seen = set()
        pre_wm = set(seen)
        seeded = 0
        for qname, rec, m, err in changed:
            if m is None:
                continue
            rid = rec.get("role_id", "")
            for u in rec.get("unresolved") or []:
                t = str(u)
                if not t.startswith("FAMILY["):
                    continue
                # underlying raws this family entry replaced on this lead
                replaced = m.get("replaced_raws", {}).get(rid, [])
                if replaced and all(_tray_key(rid, r) in pre_wm
                                    for r in replaced):
                    k = _tray_key(rid, t)
                    if k not in seen:
                        seen.add(k)
                        seeded += 1
        with open(WM, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f)
        print(f"watermark pre-seeded with {seeded} family re-expression keys")

        # ---- post metrics (field-level, auditable) ----
        n_after_field = field_raw_count(records)
        print(f"unresolved-field raw blockers after: {n_after_field} "
              f"(net removed {n_before_field - n_after_field})")

        # ---- optimization log ----
        genuine_applicant = sum(v for k, v in class_counts.items()
                                if k in GENUINE_APPLICANT_CLASSES)
        entry = {
            "ts": datetime.now().astimezone().isoformat(),
            "loop": "input-resolution",
            "upgrade": ("Input Resolution & Blocker Compression Engine — live apply "
                        "(10-point proof + cross-app dedup + leads-unlocked-per-decision)"),
            "metric": "leads_unlocked_per_decision",
            "baseline_metric": (f"leads_unlocked_per_decision=1.00 (per-lead tray, no dedup; "
                                f"{len(allb)} blocker instances across {n_leads} leads; "
                                f"{genuine_applicant} genuine-applicant instances)"),
            "post_metric": (f"unresolved-field raw blockers {n_before_field} -> {n_after_field}; "
                            f"{n_dup} cross-app duplicate instances collapsed into "
                            f"{multi_lead_fams} multi-lead families; {len(changed)} records "
                            f"rewritten; {len(fully)} leads fully unblocked -> canonical "
                            f"verify_retry; {len(need_review)} flagged for review"),
            "verdict": "SHIPPED — live apply complete, backup " + os.path.basename(bdir),
            "evidence": [bdir],
        }
        with open(OPT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        print("optimization log appended")

        # ---- learning proposal (compounding loop; triage stays human) ----
        # Idempotent: never append a duplicate proposal on rerun.
        prop_id = f"P-{STAMP[:10]}-input-resolution-1"
        prop = (f"\n## {prop_id} (DRAFT / PENDING TRIAGE): "
                f"narrative park summaries are not machine-actionable blockers\n"
                f"- Evidence: input-resolution dry-run found narrative park "
                f"summaries in `unresolved`-adjacent fields; the engine had to "
                f"regex-split prescreen verdicts to recover structure.\n"
                f"- Candidate: park paths should enumerate concrete required "
                f"fields in `unresolved` instead of narrative summaries, so the "
                f"tray stays machine-actionable.\n"
                f"- Reviewer: confirm the pattern recurs, then promote.\n")
        existing = open(PROPOSALS, encoding="utf-8").read() if os.path.exists(PROPOSALS) else ""
        if prop_id not in existing:
            with open(PROPOSALS, "a", encoding="utf-8") as f:
                f.write(prop)
            print("learning proposal appended")
        else:
            print("learning proposal already present; skipped duplicate")

        # ---- telemetry ----
        try:
            import log_event
            if "input_resolution_applied" in log_event.EVENT_TYPES:
                log_event.log("input_resolution_applied", source="input-resolution",
                              details={"records_rewritten": len(changed),
                                       "fully_unblocked": len(fully),
                                       "need_review": len(need_review),
                                       "instances_proven": len(allb),
                                       "duplicates_cross_app": n_dup,
                                       "multi_lead_families": multi_lead_fams,
                                       "field_raw_before": n_before_field,
                                       "field_raw_after": n_after_field,
                                       "classifications": dict(class_counts),
                                       "backup": os.path.basename(bdir)})
                print("telemetry event logged")
            else:
                print("telemetry skipped: event_type not registered in this install")
        except Exception as exc:
            print(f"telemetry skipped: {exc}")

        # ---- apply report ----
        rep = os.path.join(DATA, "hidden_files",
                           f"input-resolution-apply-{STAMP_UTC}.md")
        with open(rep, "w", encoding="utf-8") as f:
            f.write(f"# Input Resolution live apply — {STAMP}\n\n"
                    f"- Live apply: parent-authorized (--live); default is --dry-run.\n"
                    f"- Backup: {bdir}\n"
                    f"- Leads scanned: {n_leads} | blocker instances (global proof): {len(allb)}\n"
                    f"- Classifications: " + ", ".join(f"{k}={v}" for k, v in sorted(class_counts.items())) + "\n"
                    f"- Records rewritten: {len(changed)}\n"
                    f"- Cross-app duplicate instances collapsed: {n_dup} into {multi_lead_fams} multi-lead families\n"
                    f"- Unresolved-field raw blockers: {n_before_field} -> {n_after_field}\n"
                    f"- Fully unblocked -> canonical verify_retry: {len(fully)}\n"
                    f"- Flagged for review (classifier still sees blockers): {len(need_review)}\n"
                    f"- Watermark family re-expression keys pre-seeded: {seeded}\n"
                    f"- Essays: drafting is not built; essays stay applicant-only.\n")
        print(f"report: {rep}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
