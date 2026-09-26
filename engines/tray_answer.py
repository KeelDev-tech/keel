#!/usr/bin/env python3
"""Apply one tray answer: bank it, retro-resolve every matching blocker.

The closing half of the draft-first HITL loop (LangGraph / OpenAI Agents SDK
approve-edit-reject, productized): the tray digest shows each card with an
optional bank draft; the applicant replies APPROVE / edits / answers fresh; this CLI
applies that decision:

  - one answer retro-clears EVERY lead on the card (LazyApply-style
    compounding: answer once, unblock N -- including leads parked since),
  - the answer is banked to answer_bank.json with provenance "the applicant's own
    words, <date> (tray reply)" so future applications never ask again,
  - resolved leads revive through the CANONICAL input_resolution +
    verify_retry path: fully-unblocked leads trigger an immediate targeted
    verify_retry --live --role-ids run (fail-safe; hourly cadence is the
    fallback) -- nothing is force-promoted here.

Usage:
  tray_answer.py --key <card-key> --answer "..."            # dry run (default)
  tray_answer.py --live --key <card-key> --answer "..."
  tray_answer.py --live --family travel_commitment/general --answer "..."
  tray_answer.py --live --key <k> --answer "..." --bank-new willing_overtime
  tray_answer.py --live --key <k> --answer "..." --bank-key existing_key
  tray_answer.py --live --key <k> --answer "..." --bank-new sms_x \
      --scope employer:Acme          # scoped (non-global) bank write

Banking is OPT-IN (--bank-new / --bank-key). Without it, blockers resolve on
the matched leads but no standing answer is created -- the human (relaying
the applicant) decides what becomes permanent. Never invents: the answer text comes
from the applicant's reply verbatim.

SAFETY (mirrors input_resolution/apply.py):
  - Backup of both queue files + answer bank BEFORE any write (--live only).
  - queue_io.queue_lock() held for the read-modify-write.
  - Crash-safe writes (tmp + os.replace).
  - Fail closed: fully-unblocked claims are verified with the REAL
    is_verify_only(); a no-match key changes nothing.
  - --dry-run (default) prints the planned mutation and writes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ENGINES = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ENGINES)

import input_tray_digest as tray
from genuine_pat import is_verify_only
import queue_io
import bank_scope as bs

PDT = ZoneInfo("America/Los_Angeles")
STAMP = datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT")

from keel_paths import HOME, DATA  # noqa: E402 — repo path convention
STD_Q = os.path.join(DATA, "queues", "standard-queue.json")
NI_Q = os.path.join(DATA, "queues", "needs_input-queue.json")
DEFAULT_BANK = os.path.join(DATA, "answer_bank.json")
ANSWERS_LOG = os.path.join(HOME, "hidden_files", "tray-answers.jsonl")


def load_list(path):
    with open(path, encoding="utf-8") as f:
        q = json.load(f)
    return q if isinstance(q, list) else []


def save_list(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def qn_with_notes(rec, notes):
    qn = rec.get("queue_notes")
    if isinstance(qn, list):
        return qn + notes
    return ((qn or "") + " | " + " | ".join(notes)).strip(" |")


def card_matches(text, key=None, family=None):
    fam = tray.family_of(text)
    norm = tray.normalize_question(text)
    if key:
        return tray.card_key_for(fam, norm) == key
    if family:
        return fam == family
    return False


def find_targets(key=None, family=None):
    """(card, [(entry, [matched raw blockers])]) for current queue state."""
    cards = tray.collect_cards()
    card = None
    if key:
        card = cards.get(key)
    elif family:
        cands = [c for c in cards.values() if c["family"] == family]
        card = cands[0] if len(cands) == 1 else None
        if len(cands) > 1:
            return None, [], f"family {family} matches {len(cands)} cards; use --key"
    if card is None:
        avail = sorted(
            ((c["family"] or "no-family", c["key"], c["unblock_leads"])
             for c in cards.values()),
        )
        hint = "\n".join(f"  {k}  [{f}] ({n} leads)" for f, k, n in avail[:15])
        return None, [], f"no tray card matches; available:\n{hint}"
    targets = []
    for fname, path in (("needs_input", NI_Q), ("standard", STD_Q)):
        for e in load_list(path):
            if "NEEDS-INPUT" not in str(e.get("status", "")):
                continue
            matched = [t for _, t in tray.genuine_blockers(e)[1]
                       if card_matches(t, key=card["key"])]
            if matched:
                targets.append((fname, e, matched))
    return card, targets, ""


def resolve_unresolved(unresolved, card_key, matched_texts):
    """Split an `unresolved` list into (removed, kept) for a card answer.

    Remove by CARD MATCH, not by exact raw-string membership:
    genuine_blockers() can return merged/fragmented text that never
    appears verbatim in `unresolved`. An unresolved entry belongs to
    this card iff it card-matches, or it is a substring of the card's
    raw matched text (the merge case) and doesn't belong to another
    live card.
    """
    other_norms = [c["norm"] for k, c in tray.collect_cards().items()
                   if k != card_key]
    mtexts = [str(m) for m in matched_texts]

    def _belongs(u):
        us = str(u)
        if card_matches(us, key=card_key):
            return True
        if any(us in m or m in us for m in mtexts):
            if any(us in n or n in us for n in other_norms):
                return False  # another card owns it
            return True
        return False

    removed, kept = [], []
    for u in (unresolved or []):
        (removed if _belongs(u) else kept).append(u)
    return removed, kept


def apply_bank_write(bank_path, banked_key, answer, scope_arg, card,
                     stamp, employers=None, question_variants=None):
    """Write one banked answer with governed scope (ADOPTION 1 of 5).

    Scope decision (bank_scope.decide_bank_scope): explicit --scope always
    wins; a consent/attestation-family card or an employer-named question
    without explicit scope infers AMBIGUOUS and is written to
    ``_quarantined`` (same shape as existing _quarantined entries) instead
    of ``answers`` -- a per-employer consent can NEVER land in global
    answers without the applicant's deliberate --scope.

    Returns (banked: bool, quarantined: bool, scope: str).

    Fail-safe: any scope-check failure quarantines the entry and never
    raises -- banking must never break unblock routing.
    """
    banked = False
    quarantined = True
    scope = bs.SCOPE_AMBIGUOUS
    try:
        with open(bank_path, encoding="utf-8") as f:
            bank = json.load(f)
        scope, qreason = bs.decide_bank_scope(scope_arg, card, employers)
        quarantined = (scope == bs.SCOPE_AMBIGUOUS)
        prov = bank.setdefault("_provenance", {})
        prov[banked_key] = {
            "source": (f"the applicant's own words, {stamp} "
                       f"(tray reply; card {card['key']})"),
            "added": datetime.now(PDT).strftime("%Y-%m-%d"),
            "via": "tray_answer",
            "scope": scope,
        }
        if quarantined:
            bank.setdefault("_quarantined", {})[banked_key] = {
                "was": answer,
                "quarantined_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                "reason": (qreason or "ambiguous scope; held out of answers "
                           "pending the applicant's explicit scoped authorization"),
            }
        else:
            bank.setdefault("answers", {})[banked_key] = {
                "value": answer, "scope": scope,
                # 2026-09-17 tray-answer compounding: the exact question he
                # answered, so the prescreen can consume this answer via
                # _scoped_question_match instead of re-parking the lead on
                # the same question.
                "question": card.get("question") or card.get("norm") or "",
                "card_key": card.get("key") or "",
                # Alternate wrappings of the same question seen across the
                # matched leads (blind-pattern prefixes etc.) so the prescreen
                # keeps consuming this answer as harvest wording drifts.
                "question_variants": sorted({
                    str(m)[:300] for m in (question_variants or [])
                    if str(m).strip()
                })[:5],
            }
            banked = True
        meta = bank.setdefault("_meta", {})
        meta["last_updated"] = datetime.now(PDT).strftime("%Y-%m-%d")
        tmp = bank_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(bank, f, indent=1, ensure_ascii=False)
        os.replace(tmp, bank_path)
    except Exception as e:  # scope-check failure: fail to quarantine + continue
        try:
            with open(bank_path, encoding="utf-8") as f:
                bank = json.load(f)
        except Exception:
            bank = {"answers": {}, "_provenance": {}, "_quarantined": {}}
        bank.setdefault("_quarantined", {})[banked_key] = {
            "was": answer,
            "quarantined_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "reason": (f"scope-check failed ({type(e).__name__}); "
                       "fail-closed to quarantine"),
        }
        try:
            tmp = bank_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(bank, f, indent=1, ensure_ascii=False)
            os.replace(tmp, bank_path)
        except Exception:
            pass
        quarantined, scope = True, bs.SCOPE_AMBIGUOUS
    return banked, quarantined, scope


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply one tray answer.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--key", help="tray card key from the digest / input-tray.json")
    g.add_argument("--family", help="decision family, e.g. travel_commitment/general")
    ap.add_argument("--answer", required=True, help="the applicant's verbatim answer")
    ap.add_argument("--live", action="store_true", help="apply (default: dry run)")
    ap.add_argument("--bank-new", metavar="KEY",
                    help="also bank as new answer_bank key KEY")
    ap.add_argument("--bank-key", metavar="KEY",
                    help="also overwrite existing answer_bank key KEY")
    ap.add_argument("--scope", metavar="SCOPE",
                    help="explicit bank scope: 'global', 'employer:<Name>', "
                         "or 'ambiguous'. Overrides scope inference; a "
                         "consent/attestation or employer-named answer is "
                         "quarantined (never banked globally) without this.")
    ap.add_argument("--bank", metavar="PATH", default=DEFAULT_BANK,
                    help="answer-bank JSON path (default: <keel-home>/data/answer_bank.json)")
    args = ap.parse_args(argv)

    card, targets, err = find_targets(key=args.key, family=args.family)
    if card is None:
        print(f"NO-MATCH: {err}")
        return 2

    # Keel 0.4 P3 (2026-09-17): banking guard. A the applicant answer is applied to
    # an employer-prior blocker ONLY when the question is confirmed
    # (authoritative form intel). An intel_gap card is an UNCONFIRMED prior
    # — the form may not even ask the question — so resolving or banking
    # against it is refused, fail closed. The promotion path
    # (tray_sources.promote_intel_gap) produces the confirmed genuine_missing
    # card the applicant may then answer. (Blind-safe keys per BLIND_SAFE_BANKED_KEYS
    # are a prescreen-layer concern and never reach the tray as parks.)
    try:
        from tray_sources import classify_source
        if classify_source(card.get('question', ''), card.get('norm', '')) == "intel_gap":
            print("REFUSED: card is SYSTEM-BLOCKED (intel-gap) — the question "
                  "is unconfirmed (no authoritative form intel). the applicant cannot "
                  "answer a question the form may not ask; route: "
                  "authoritative form probe first, then promote. Fail closed.")
            return 3
    except SystemExit:
        raise
    except Exception:
        print("REFUSED: blocker-source classification failed — fail closed.")
        return 3

    total_blockers = sum(len(m) for _, _, m in targets)
    print(f"card: [{card['family']}] {card['question'][:100]}")
    print(f"key: {card['key']}")
    print(f"answer: \"{args.answer[:120]}\"")
    print(f"would resolve {total_blockers} blockers across {len(targets)} leads")
    if args.bank_new or args.bank_key:
        print(f"would bank as: {args.bank_new or args.bank_key}")
    if not args.live:
        print("dry run -- nothing written (pass --live to apply)")
        return 0

    # ---- live path ----
    bkdir = os.path.join(DATA, "queues",
                         f"_backup-{datetime.now(PDT).strftime('%Y%m%dT%H%M%S')}-tray-answer")
    os.makedirs(bkdir, exist_ok=True)
    for src in (STD_Q, NI_Q, args.bank):
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(bkdir, os.path.basename(src)))
    print(f"backup: {bkdir}")

    with queue_io.queue_lock(owner="tray_answer:apply"):
        queues = {NI_Q: load_list(NI_Q), STD_Q: load_list(STD_Q)}
        changed = {"needs_input": 0, "standard": 0}
        fully = 0
        fully_rids = []
        for fname, rows in (("needs_input", queues[NI_Q]), ("standard", queues[STD_Q])):
            by_rid = {r.get("role_id"): r for r in rows}
            for qfname, entry, matched in targets:
                if qfname != fname:
                    continue
                rec = by_rid.get(entry.get("role_id"))
                if rec is None:
                    continue
                removed, kept = resolve_unresolved(
                    rec.get("unresolved"), card["key"], matched)
                new_unresolved = kept
                notes = [
                    f"tray-answer {STAMP}: blocker cleared — "
                    f"\"{m[:70]}\" -> answered \"{args.answer[:60]}\" "
                    f"(the applicant's words, tray reply; key {card['key']})"
                    for m in removed
                ]
                rec["unresolved"] = new_unresolved
                rec["queue_notes"] = qn_with_notes(rec, notes)
                # 2026-09-17 (pulse 250 / ARM 8): stamp hygiene -- a
                # material mutation (blockers cleared) moves status_updated
                # at second resolution. The P2 guard keys on
                # status_updated_park_ref, not this stamp, so the bump is
                # truthfulness for age-based sweeps, not guard logic.
                rec["status_updated"] = datetime.now(timezone.utc).isoformat()
                if not new_unresolved:
                    # Fully unblocked: mirror input_resolution/apply.py --
                    # clean conventional fields, verify with the REAL
                    # classifier, fail closed.
                    trial = dict(rec)
                    trial["unresolved"] = []
                    trial["status_reason"] = (
                        f"tray-answer {STAMP}: all input blockers cleared "
                        f"via tray answer (key {card['key']}); liveness "
                        f"unverified — queued for verification")
                    gn = rec.get("gate_note") or ""
                    kept = [ln for ln in str(gn).splitlines()
                            if not any(m in ln for m in removed)]
                    trial["gate_note"] = "\n".join(kept).strip() or (
                        f"tray-answer {STAMP}: no open input blockers")
                    trial["queue_notes"] = rec["queue_notes"]
                    if is_verify_only(trial):
                        rec["status_reason"] = trial["status_reason"]
                        rec["gate_note"] = trial["gate_note"]
                        # Immediate-verify fast path (2026-09-17): clear the
                        # retry-cooldown stamp so the targeted verify run
                        # below (and the hourly cadence as fallback) picks
                        # this lead up without waiting out a stale cooldown.
                        rec["last_verify_attempt"] = None
                        fully += 1
                        if rec.get("role_id"):
                            fully_rids.append(rec["role_id"])
                    else:
                        rec["queue_notes"] = qn_with_notes(rec, [(
                            f"tray-answer {STAMP}: unresolved cleared but "
                            f"classifier still sees input blockers — left for review")])
                changed[fname] += 1
            save_list(NI_Q if fname == "needs_input" else STD_Q, rows)

        banked_key = None
        banked_scope = None
        banked_quarantined = False
        if args.bank_new or args.bank_key:
            banked_key = args.bank_new or args.bank_key
            # Matched raw blocker texts across the answered leads become
            # question variants, so the prescreen keeps recognizing this
            # question as harvest wording drifts.
            _variants = [m for _, _, ms in targets for m in ms]
            banked, banked_quarantined, banked_scope = apply_bank_write(
                args.bank, banked_key, args.answer, args.scope, card, STAMP,
                question_variants=_variants)
            if banked_quarantined:
                print(f"QUARANTINED: answer_bank._quarantined[{banked_key}] "
                      f"-- scope ambiguous, held out of answers")
            # ECV-T3 gate (2026-09-16, the applicant-approved): a global bank
            # promotion is a T3 decision — log an auto-filled packet.
            # Fail-open (never-halt): emit() catches everything internally
            # and the hook is guarded, so a packet failure can never break
            # banking or unblock routing. Fires only on real answers
            # writes (never on quarantine).
            if banked:
                try:
                    import ecv_packet_gate
                    ecv_packet_gate.emit(
                        "global-bank-promotion",
                        subject=banked_key,
                        decision_summary=(
                            f"answer_bank[{banked_key}] banked with scope "
                            f"'{banked_scope}' from "
                            f"the applicant's own words (tray card {card['key']}, family "
                            f"{card['family']})"),
                        component="tray_answer")
                except Exception:
                    pass

    # Immediate-verify fast path (2026-09-17): fully-unblocked leads
    # re-enter verify_retry NOW via a targeted --role-ids run instead of
    # waiting for the next hourly cadence. Fail-safe by design (never-halt):
    # any failure here is logged and swallowed — banking and unblocking
    # above already succeeded. A skipped_lock contender simply means the
    # hourly cadence is running; the cleared cooldown stamps guarantee it
    # picks these leads up.
    if fully_rids:
        try:
            import subprocess
            vr = os.path.join(ENGINES, "verify_retry.py")
            proc = subprocess.run(
                [sys.executable, vr, "--live", "--role-ids",
                 ",".join(sorted(set(fully_rids)))],
                capture_output=True, text=True, timeout=600, cwd=ENGINES)
            tail = (proc.stdout or "").strip().splitlines()[-3:]
            print(f"immediate verify: exit={proc.returncode} "
                  f"for {len(fully_rids)} lead(s)")
            for line in tail:
                print(f"  verify: {line[:160]}")
            if proc.returncode != 0:
                print(f"  verify stderr: {(proc.stderr or '').strip()[:300]}")
        except Exception as exc:
            print(f"immediate verify skipped ({type(exc).__name__}: {exc}); "
                  f"hourly cadence will pick up {len(fully_rids)} lead(s)")

    # append-only answer log (telemetry, never secrets)
    os.makedirs(os.path.dirname(ANSWERS_LOG), exist_ok=True)
    with open(ANSWERS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": STAMP, "card_key": card["key"], "family": card["family"],
            "leads": len(targets), "blockers": total_blockers,
            "fully_unblocked": fully,
            "banked_key": banked_key,
            "banked_scope": banked_scope,
            "banked_quarantined": banked_quarantined,
            "answer_chars": len(args.answer),
        }) + "\n")

    print(f"applied: {len(targets)} leads updated "
          f"({changed['needs_input']} needs_input, {changed['standard']} standard), "
          f"{fully} fully unblocked -> immediate targeted verify above "
          f"(hourly cadence as fallback)")
    if banked_key:
        if banked_quarantined:
            print(f"QUARANTINED: answer_bank._quarantined[{banked_key}] "
                  f"(scope ambiguous -- held out of answers)")
        else:
            print(f"banked: answer_bank[{banked_key}] (scope {banked_scope})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
