#!/usr/bin/env python3
"""stage_ready_launches.py — writer for the quiet-window staging contract.

Contract: during the operator-configured quiet window no browser tasks spawn;
READY leads with fresh packets stage into data/.state/staged-launches.json
so the 10:05 digest lists them and batch_staged_launches.py can fire them
(up to 4 per browser task) after the window ends. staged-launch-preflight.py
re-validates every entry at fire time — this stager does only the cheap
static checks; the preflight is the deep guard.

After the 2026-09-16 purge the file sat EMPTY with no writer, so the 10:00
fire flow had nothing to fire. This script restores the writer:

  stage:   standard-queue READY + fresh parseable packet (<=12h) + no
           SUBMITTED ledger row + not in needs_input + employer not on the
           blocklist + not already staged + shared fit/hold/question gates +
           current scoped packet manifest and material bytes
  prune:   drop staged entries whose queue entry left READY, whose packet
           went missing/stale, that got SUBMITTED, or that moved to
           needs_input (the staleness that caused the 09-16 purge)
           EXCEPT: entries whose queue entry moved to a BLOCKED-* status are
           kept but annotated with the blocker reason + reverify_required
           (revive-gate, ARM-9 P4) — never silently deleted

Dry-run by default; --live writes (backup + atomic write). Never touches
queues, ledger, or telemetry. Never spawns browser tasks. No HTTP — posting
liveness is the preflight's job (check 5), not the stager's.

NOTE for the fire path (batch_staged_launches / staged-launch-preflight):
staged entries carrying fireable=False MUST be skipped until the
reverify_required annotation is cleared by a successful re-verify. That
consumption lives in the fire path (owned by ARM-2/lifecycle); this stager
only stamps the annotation.

Dry-run by default; --live writes (backup + atomic write). Never touches
queues, ledger, or telemetry. Never spawns browser tasks. No HTTP — posting
liveness is the preflight's job (check 5), not the stager's.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import HOME, DATA  # noqa: E402 — repo path convention
from safe_io import atomic_json, digest, file_lock, read_json, rows  # noqa: E402
import apply_loop  # noqa: E402 — current workspace answer-bank loader
import ready_gate  # noqa: E402 — one static READY and packet-admission contract
STAGED = os.path.join(DATA, ".state", "staged-launches.json")
STD_Q = os.path.join(DATA, "queues", "standard-queue.json")
NI_Q = os.path.join(DATA, "queues", "needs_input-queue.json")
LEDGER = os.path.join(DATA, "application-ledger.json")
BLOCKLIST = os.environ.get("KEEL_BLOCKLIST",
                           os.path.join(DATA, "employer-blocklist.md"))
BUFFER_DIR = os.path.join(DATA, "launch-packets", "buffer")
BUFFER_MAX_AGE_HOURS = 12

REQUIRED_PACKET_KEYS = ("role_id", "company", "title", "brief", "ats_url")


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _blocked_employers():
    return ready_gate.blocked_employers(BLOCKLIST)


def _on_blocklist(company, blocked):
    return ready_gate.answer_resolver.normalize_employer(company) in blocked


def _submitted_ids(ledger_rows):
    return {r.get("role_id") for r in ledger_rows
            if r.get("role_id")
            and str(r.get("status", "")).upper() in ready_gate.LEDGER_HOLD_STATES}


def _packet_ok(rid, entry=None, bank=None):
    """(ok, path, reason): fresh parseable packet with required keys."""
    if (not isinstance(rid, str) or len(rid) > 256
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", rid)):
        return False, "", "role_id is unsafe for a packet filename"
    path = os.path.join(BUFFER_DIR, rid + ".json")
    if not os.path.exists(path):
        return False, path, "packet missing"
    try:
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
    except OSError:
        return False, path, "packet unreadable"
    if age_h > BUFFER_MAX_AGE_HOURS:
        return False, path, f"packet stale ({age_h:.1f}h)"
    try:
        pkt = read_json(path)
    except (ValueError, OSError):
        return False, path, "packet unparseable"
    if not isinstance(pkt, dict):
        return False, path, "packet must be an object"
    missing = [k for k in REQUIRED_PACKET_KEYS if not pkt.get(k)]
    if missing:
        return False, path, f"packet missing keys: {missing}"
    if entry is None:
        return False, path, "current queue context required"
    result = ready_gate.packet_admission(pkt, entry, apply_loop.load_answer_bank() if bank is None else bank, workspace=HOME)
    if not result["allowed"]:
        return False, path, "; ".join(result["reasons"])
    return True, path, ""


def _entry_fireable(e, staged_ids, submitted, ni_ids, blocked, bank=None):
    """Cheap static checks. Returns (ok, reason)."""
    rid = e.get("role_id") or ""
    if not rid:
        return False, "no role_id"
    if rid in staged_ids:
        return False, "already staged"
    if rid in submitted:
        return False, "already submitted"
    if rid in ni_ids:
        return False, "in needs_input"
    if _on_blocklist(e.get("company", ""), blocked):
        return False, "blocklisted employer"
    result = ready_gate.entry_admission(e)
    if not result["allowed"]:
        return False, "; ".join(result["reasons"])
    ok, path, reason = _packet_ok(rid, e, bank)
    if not ok:
        return False, reason
    return True, ""


def _prune_ok(se, ready_by_id, blocked_info, submitted, ni_ids, blocked, bank):
    """Staged entry still valid? Returns (keep, reason, entry).

    ARM-9 P4 — revive-gate for staged-but-blocked packets (ARM-4 proposal P4).
    A staged entry whose queue entry moved to a BLOCKED-* status is KEPT but
    annotated (never deleted): the blocker reason is recorded and
    reverify_required=True + fireable=False are stamped so a revive path
    cannot fire the packet without re-verification. When the queue entry
    returns to READY (unblocked + re-verified), the annotation is stripped
    and the entry is fireable again.
    """
    rid = se.get("role_id") or ""
    if rid in submitted:
        return False, "submitted since staging", se
    if rid in ni_ids:
        return False, "moved to needs_input", se
    if rid in ready_by_id:
        ok, reason = _entry_fireable(ready_by_id[rid], set(), submitted,
                                     ni_ids, blocked, bank)
        if not ok:
            return False, reason, se
        entry = dict(se)
        cleared = False
        for k in ("blocked_status", "blocked_reason", "reverify_required",
                  "fireable"):
            if k in entry:
                del entry[k]
                cleared = True
        return True, "revive annotation cleared" if cleared else "", entry
    if rid in blocked_info:
        info = blocked_info[rid]
        entry = dict(se)
        entry["blocked_status"] = info["status"]
        entry["blocked_reason"] = info["reason"]
        entry["reverify_required"] = True
        entry["fireable"] = False
        return True, f"blocked ({info['status']}); annotated, not fireable", entry
    return False, "queue entry left READY", se


def build_plan():
    std = rows(read_json(STD_Q))
    ni = rows(read_json(NI_Q))
    ledger = rows(read_json(LEDGER))
    strategic = rows(read_json(os.path.join(DATA, "queues", "strategic-queue.json")))
    rejected = rows(read_json(os.path.join(DATA, "queues", "rejected-queue.json")))
    all_rows = std + ni + strategic + rejected
    staged_doc = read_json(STAGED, missing={"entries": []})
    if isinstance(staged_doc, dict):
        keys = [key for key in ("entries", "staged") if key in staged_doc]
        if len(keys) != 1:
            raise ValueError("staged file requires exactly one entries or legacy staged container")
        staged = staged_doc[keys[0]]
    elif isinstance(staged_doc, list):
        staged = staged_doc
    else:
        raise ValueError("invalid staged file")
    if not isinstance(staged, list):
        raise ValueError("invalid staged entries")

    ready = [e for e in std if isinstance(e, dict)
             and e.get("status") == "READY"]
    ready_by_id = {e.get("role_id"): e for e in ready if e.get("role_id")}
    duplicates = {e.get("role_id") for e in ready
                  if sum(other.get("role_id") == e.get("role_id") for other in all_rows) != 1}
    for rid in duplicates:
        ready_by_id.pop(rid, None)
    ni_ids = {e.get("role_id") for e in ni if isinstance(e, dict)
              and e.get("role_id")}
    submitted = _submitted_ids(ledger if isinstance(ledger, list) else [])
    submitted.update(e.get("role_id") for e in ready if ready_gate.ledger_holds(e, ledger))
    blocked = _blocked_employers()
    bank = apply_loop.load_answer_bank()
    staged_ids = {s.get("role_id") for s in staged if isinstance(s, dict)}
    # ARM-9 P4: map of role_id -> blocker info for queue entries in a
    # BLOCKED-* status (e.g. BLOCKED-DAILYREMOTE-GATE). Staged entries for
    # these leads are annotated (revive-gated), never silently deleted.
    blocked_info = {}
    for e in std:
        if not isinstance(e, dict):
            continue
        rid = e.get("role_id")
        status = str(e.get("status") or "")
        if rid and status.startswith("BLOCKED"):
            detail = (e.get("status_reason") or e.get("gate_note") or "").strip()
            reason = f"{status}: {detail}" if detail else status
            blocked_info[rid] = {"status": status, "reason": reason}

    kept, pruned = [], []
    for se in staged:
        if not isinstance(se, dict):
            pruned.append({"role_id": "?", "reason": "malformed entry"})
            continue
        keep, reason, entry = _prune_ok(se, ready_by_id, blocked_info,
                                       submitted, ni_ids, blocked, bank)
        (kept if keep else pruned).append(
            entry if keep else {"role_id": se.get("role_id"), "reason": reason})

    new, skipped = [], []
    for e in ready:
        if e.get("role_id") in duplicates:
            skipped.append({"role_id": e.get("role_id"), "reason": "duplicate queue role_id"})
            continue
        ok, reason = _entry_fireable(e, staged_ids, submitted, ni_ids, blocked, bank)
        if not ok:
            if reason != "already staged":
                skipped.append({"role_id": e.get("role_id"), "reason": reason})
            continue
        rid = e.get("role_id")
        okp, path, _r = _packet_ok(rid, e, bank)
        pkt = json.load(open(path)) if okp else {}
        new.append({
            "role_id": rid,
            "company": e.get("company", ""),
            "title": e.get("title", ""),
            "fit_score": e.get("fit_score"),
            "packet_path": path,
            "apply_url": pkt.get("ats_url", ""),
            "resume_lane": pkt.get("resume_lane", ""),
            "staged_at": _utcnow_iso(),
            "status": "STAGED",
        })
        staged_ids.add(rid)

    return {
        "kept": kept, "pruned": pruned, "new": new, "skipped": skipped,
        "ready_total": len(ready),
        "admissible_packet_total": len(kept) + len(new) -
                                   sum(row.get("fireable") is False for row in kept),
        "note": staged_doc.get("note", "") if isinstance(staged_doc, dict) else "",
        "staged_input_sha256": digest(staged_doc),
    }


def apply_plan(plan):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(DATA, ".state", "staged-backups", f"staged-launches-{ts}.json")
    with file_lock(STAGED + ".lock"):
        current = read_json(STAGED, missing={"entries": []})
        if digest(current) != plan.get("staged_input_sha256"):
            raise ValueError("staged entries changed after planning; rerun dry run")
        if os.path.exists(STAGED):
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            shutil.copy2(STAGED, backup)
        doc = {"note": plan["note"], "entries": plan["kept"] + plan["new"]}
        atomic_json(STAGED, doc)
    return backup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="write staged-launches.json (default: dry run)")
    args = ap.parse_args()

    plan = build_plan()
    print(f"READY labels in queue: {plan['ready_total']}; admissible staged packets: {plan['admissible_packet_total']}")
    print(f"staged kept: {len(plan['kept'])}, pruned: {len(plan['pruned'])}, "
          f"new: {len(plan['new'])}")
    for p in plan["pruned"]:
        print(f"  PRUNE {p['role_id']}: {p['reason']}")
    for n in plan["new"]:
        print(f"  STAGE {n['role_id']} ({n['company']} — {n['title']}) "
              f"fit={n['fit_score']}")

    if not args.live:
        print("dry run: no writes")
        return 0
    backup = apply_plan(plan)
    print(f"wrote {STAGED} (backup: {backup})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
