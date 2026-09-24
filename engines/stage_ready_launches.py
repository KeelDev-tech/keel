#!/usr/bin/env python3
"""stage_ready_launches.py — writer for the quiet-window staging contract.

Contract: during the operator-configured quiet window no browser tasks spawn;
READY leads with fresh packets stage into hidden_files/staged-launches.json
so the 10:05 digest lists them and batch_staged_launches.py can fire them
(up to 4 per browser task) after the window ends. staged-launch-preflight.py
re-validates every entry at fire time — this stager does only the cheap
static checks; the preflight is the deep guard.

After the 2026-09-16 purge the file sat EMPTY with no writer, so the 10:00
fire flow had nothing to fire. This script restores the writer:

  stage:   standard-queue READY + fresh parseable packet (<=12h) + no
           SUBMITTED ledger row + not in needs_input + employer not on the
           blocklist + not already staged
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
STAGED = os.path.join(HOME, "hidden_files", "staged-launches.json")
STD_Q = os.path.join(DATA, "queues", "standard-queue.json")
NI_Q = os.path.join(DATA, "queues", "needs_input-queue.json")
LEDGER = os.path.join(DATA, "application-ledger.json")
BLOCKLIST = os.environ.get("KEEL_BLOCKLIST",
                           os.path.join(HOME, "employer-blocklist.md"))
BUFFER_DIR = os.path.join(HOME, "hidden_files", "apply-launch-packets", "buffer")
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
    names = []
    try:
        text = open(BLOCKLIST).read()
        sec = text.split("## Blocked employers", 1)[-1]
        names = re.findall(r"\*\*(.+?)\*\*", sec)
    except OSError:
        pass
    return [n.strip().lower() for n in names]


def _on_blocklist(company, blocked):
    hay = (company or "").lower()
    return any(b and b in hay for b in blocked)


def _submitted_ids(ledger_rows):
    return {r.get("role_id") for r in ledger_rows
            if r.get("role_id")
            and str(r.get("status", "")).upper() == "SUBMITTED"}


def _packet_ok(rid):
    """(ok, path, reason): fresh parseable packet with required keys."""
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
        pkt = json.load(open(path))
    except (json.JSONDecodeError, OSError):
        return False, path, "packet unparseable"
    missing = [k for k in REQUIRED_PACKET_KEYS if not pkt.get(k)]
    if missing:
        return False, path, f"packet missing keys: {missing}"
    return True, path, ""


def _entry_fireable(e, staged_ids, submitted, ni_ids, blocked):
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
    ok, path, reason = _packet_ok(rid)
    if not ok:
        return False, reason
    return True, ""


def _prune_ok(se, ready_ids, blocked_info, submitted, ni_ids):
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
    if rid in ready_ids:
        ok, _path, reason = _packet_ok(rid)
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
    std = _load_json(STD_Q, [])
    ni = _load_json(NI_Q, [])
    ledger = _load_json(LEDGER, [])
    staged_doc = _load_json(STAGED, {})
    staged = staged_doc.get("staged", []) if isinstance(staged_doc, dict) else []

    ready = [e for e in std if isinstance(e, dict)
             and e.get("status") == "READY"]
    ready_ids = {e.get("role_id") for e in ready if e.get("role_id")}
    ni_ids = {e.get("role_id") for e in ni if isinstance(e, dict)
              and e.get("role_id")}
    submitted = _submitted_ids(ledger if isinstance(ledger, list) else [])
    blocked = _blocked_employers()
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
        keep, reason, entry = _prune_ok(se, ready_ids, blocked_info,
                                       submitted, ni_ids)
        (kept if keep else pruned).append(
            entry if keep else {"role_id": se.get("role_id"), "reason": reason})

    new, skipped = [], []
    for e in ready:
        ok, reason = _entry_fireable(e, staged_ids, submitted, ni_ids, blocked)
        if not ok:
            if reason != "already staged":
                skipped.append({"role_id": e.get("role_id"), "reason": reason})
            continue
        rid = e.get("role_id")
        okp, path, _r = _packet_ok(rid)
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
        })
        staged_ids.add(rid)

    return {
        "kept": kept, "pruned": pruned, "new": new, "skipped": skipped,
        "ready_total": len(ready),
        "note": staged_doc.get("note", "") if isinstance(staged_doc, dict) else "",
    }


def apply_plan(plan):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(HOME, "hidden_files",
                          f"_backup-staged-launches-{ts}.json")
    if os.path.exists(STAGED):
        shutil.copy2(STAGED, backup)
    doc = {"note": plan["note"], "staged": plan["kept"] + plan["new"]}
    tmp = STAGED + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, STAGED)
    return backup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="write staged-launches.json (default: dry run)")
    args = ap.parse_args()

    plan = build_plan()
    print(f"READY in queue: {plan['ready_total']}")
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
