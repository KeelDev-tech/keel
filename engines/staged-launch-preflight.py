#!/usr/bin/env python3
"""Pre-flight validator for staged launches (sleep-window pattern).

While the operator is away/unavailable, the browser lane does not spawn:
READY launches are staged to data/.state/staged-launches.json and fire when
the window ends. Conditions drift while leads wait (postings die, other
lanes submit the same role, rate-limit budgets exhaust, operator policy
changes), so every staged entry is re-validated here immediately before it
fires. A stale entry never spawns.

Reads: data/.state/max-mode.json, data/.state/staged-launches.json,
       the unique current canonical queue home, scoped packet manifest,
       current answer bank/materials, and duplicate/lease evidence.
Runs:  rate_limits.is_allowed and prescreen.screen_packet — exactly the
       same checks the live lane runs before spawn.
Never: spawns browser tasks, submits anything, writes ledgers, or touches
       the queue. With --apply it updates only staged bookkeeping;
       an operational deferral returns to STAGED only after full checks.

Usage:
    python3 staged-launch-preflight.py            # dry-run: report verdicts
    python3 staged-launch-preflight.py --apply    # mark STALE/DEAD entries
    python3 staged-launch-preflight.py --entry <role_id>
                                                  # single-entry report

A staged entry fires only when ALL of these hold:
  1. status is STAGED (not already FIRED/SKIPPED/STALE/DEAD).
  2. The current unique queue row passes READY fit/hold/question gates.
  3. The scoped manifest is current and binds packet context, answer
     authority and actual material bytes; filesystem mtime is no authority.
  4. rate_limits.is_allowed(employer) is True (budget may have exhausted
     while the entry waited).
  5. prescreen.screen_packet(packet, answer_bank) still returns CLEAN
     (form intel may have changed since staging; run a fresh probe first
     with --probe when available), and duplicate/lease checks pass.

The --probe path: probe the live posting before screening, mirroring the
live lane's order (verify -> packet -> prescreen). When no probe source is
available the entry is marked STALE-UNPROBED, never force-fired.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import HOME, ENGINES, DATA  # noqa: E402 — repo path convention
import batch_staged_launches as staging  # noqa: E402 — shared current admission
import queue_io  # noqa: E402
from safe_io import atomic_json, file_lock, read_json  # noqa: E402

STATE = os.path.join(DATA, ".state")
STAGED_FILE = os.path.join(STATE, "staged-launches.json")
MAXMODE_FILE = os.path.join(STATE, "max-mode.json")
PACKET_DIR = os.path.join(DATA, "launch-packets")
BACKUP_DIR = os.path.join(DATA, "queues", "_backup-preflight")

STALE_AFTER_H = 12


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _atomic_write(path, obj):
    atomic_json(path, obj)


def _utc(s):
    try:
        d = datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _backup_staged():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, "staged-launches.json")
    with open(dest, "w") as f:
        json.dump(_load_json(STAGED_FILE, {}), f, indent=1)
    return dest


def _guard_go(role_id, company, title):
    """Inspect duplicate/lease state without leaving a planning lease."""
    ok, note = staging._guard_go(role_id, company, title)
    return ok, "prelaunch_guard GO (plan only)" if ok else note


def _packet_fresh(role_id, now):
    path = os.path.join(PACKET_DIR, f"{role_id}.json")
    if not os.path.exists(path):
        return False, "no packet file (needs rebuild)"
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path),
                                       tz=timezone.utc)
    except OSError as ex:
        return False, f"packet unreadable ({ex})"
    if now - mtime > timedelta(hours=STALE_AFTER_H):
        return False, "packet older than %dh (rebuild before firing)" % (
            STALE_AFTER_H,)
    return True, "packet fresh"


def _budget_ok(company):
    try:
        from rate_limits import is_allowed
    except Exception as ex:
        return False, f"rate_limits unavailable ({ex})"
    try:
        ok, reason = is_allowed(company)
    except Exception as ex:
        return False, f"rate_limits check raised ({ex})"
    return ok, reason


def _screen_packet(role_id, packet):
    try:
        from prescreen import screen_packet
    except Exception as ex:
        return {"verdict": "PARK", "reasons": [f"prescreen unavailable ({ex})"]}
    bank = staging._answer_bank(data=DATA)
    return screen_packet(packet, bank)


def _probe_entry(entry):
    """Fresh form-intel probe before screening, mirroring the live lane.

    Returns (intel_dict|None, note). A probe source is whoever owns the live
    form surface (the browser lane or its tooling); this module only defines
    the contract. No probe available -> STALE-UNPROBED, never force-fire.
    """
    try:
        import form_intel as _fi
    except Exception:
        return None, "no probe source available"
    url = entry.get("ats_url") or entry.get("application_url") or ""
    if not url:
        return None, "no application URL to probe"
    try:
        intel = _fi.probe_url(url)
    except Exception as ex:
        return None, f"probe failed ({ex})"
    return (intel if isinstance(intel, dict) else None,
            "probe ok" if intel else "probe returned no intel")


def check_entry(entry, now, do_probe=False):
    """Return (verdict, [reasons]) for one staged entry.

    Verdicts: FIRE (all checks pass), SKIP (status already terminal),
    STALE-*, DEAD (never fire: guard refusal, budget exhausted, or a
    screening park)."""
    role_id = entry.get("role_id", "")
    company = entry.get("company", "") or ""
    title = entry.get("title", "") or ""
    status = (entry.get("status") or "STAGED").upper()
    reasons = []
    if status in ("FIRED", "SKIPPED", "STALE", "DEAD") or \
            status.startswith("STALE-") or status == "STALE-UNPROBED":
        return "SKIP", [f"already {status}"]
    if status not in ("STAGED", "DEFERRED-GUARD", "DEFERRED-BUDGET"):
        return "SKIP", [f"status {status}, not plannable"]
    admitted = staging._current_admission(
        entry, now=now, workspace=HOME, data=DATA, packet_dir=PACKET_DIR)
    if not admitted["allowed"]:
        return "STALE-ADMISSION", admitted["reasons"]
    current = admitted["entry"]
    company, title = current.get("company", "") or "", current.get("title", "") or ""
    ok, note = _guard_go(role_id, company, title)
    if not ok:
        reasons.append(note)
        confirmed_duplicate = any(code in note for code in (
            "ALREADY_SUBMITTED", "TWIN_SUBMITTED", "TELEMETRY_SUBMITTED"))
        return "DEAD" if confirmed_duplicate else "DEFERRED-GUARD", reasons
    reasons.append(note)
    # Scoped manifest time and actual material bytes were checked above;
    # filesystem mtime cannot establish packet freshness or authority.
    reasons.append("current queue and scoped packet admission PASS")
    ok, note = _budget_ok(company)
    if not ok:
        reasons.append(note)
        return "DEFERRED-BUDGET", reasons
    reasons.append(note)
    packet = admitted["packet"]
    if do_probe:
        intel, note = _probe_entry(entry)
        if intel is None:
            reasons.append(note)
            return "STALE-UNPROBED", reasons
        reasons.append(note)
        try:
            from prescreen import render_probe_brief
            packet = dict(packet)
            packet["brief"] = render_probe_brief(intel)
            packet["ats"] = packet.get("ats") or intel.get("ats") or ""
        except Exception as ex:
            reasons.append(f"probe render failed ({ex})")
            return "STALE", reasons
        # New probe text must be rebuilt and sealed before it can replace
        # an authorized packet. Never accept an in-memory manifest bypass.
        integrity = staging.ready_gate.packet_admission(
            packet, current, staging._answer_bank(data=DATA), now=now, workspace=HOME)
        if not integrity["allowed"]:
            return "STALE-ADMISSION", reasons + integrity["reasons"]
    try:
        res = _screen_packet(role_id, packet)
    except Exception:
        return "STALE-PRESCREEN", reasons + ["prescreen unconfirmed"]
    if not isinstance(res, dict) or res.get("verdict") != "CLEAN":
        detail = res.get("reasons", []) if isinstance(res, dict) else []
        return "STALE-PRESCREEN", reasons + list(detail or ["prescreen unconfirmed"])
    reasons.append("prescreen CLEAN")
    return "FIRE", reasons


def _main_locked(argv):
    apply = "--apply" in argv
    do_probe = "--probe" in argv
    only = None
    if "--entry" in argv:
        i = argv.index("--entry")
        if i + 1 < len(argv):
            only = argv[i + 1]
    now = datetime.now(timezone.utc)

    maxmode = _load_json(MAXMODE_FILE, {})
    staged = read_json(STAGED_FILE, missing={"entries": []})
    entries = staging._staged_entries(staged)

    if not entries:
        print("staged-launch-preflight: no staged entries "
              f"(checked {STAGED_FILE})")
        return 0

    results = []
    for entry in entries:
        if only and entry.get("role_id") != only:
            continue
        verdict, reasons = check_entry(entry, now, do_probe=do_probe)
        results.append({"role_id": entry.get("role_id"), "company": entry.get("company"),
                        "title": entry.get("title"), "was": entry.get("status"),
                        "verdict": verdict, "reasons": reasons})

    fire = [r for r in results if r["verdict"] == "FIRE"]
    print(json.dumps({"ts": now.isoformat(),
                      "sleep_window": maxmode.get("sleep_window"),
                      "results": results}, indent=1))
    print(f"\nverdicts: {len(fire)} FIRE, "
          f"{sum(1 for r in results if r['verdict'] == 'SKIP')} SKIP, "
          f"{sum(1 for r in results if r['verdict'] not in ('FIRE', 'SKIP'))} stale/dead")

    if apply and results:
        backup = _backup_staged()
        changed = 0
        by_id = {e.get("role_id"): e for e in entries}
        for r in results:
            if r["verdict"] == "SKIP":
                continue
            e = by_id.get(r["role_id"])
            if e is None:
                continue
            if r["verdict"] == "FIRE":
                if e.get("status") not in ("DEFERRED-GUARD", "DEFERRED-BUDGET"):
                    continue
                # The fresh full preflight re-arms only an operational
                # deferral; it never revives a permanent skip or queue hold.
                e["status"] = "STAGED"
            else:
                e["status"] = r["verdict"]
            e["preflight_reasons"] = r["reasons"]
            e["preflight_at"] = now.isoformat()
            changed += 1
        _atomic_write(STAGED_FILE, staged)
        print(f"applied: updated {changed} staged entr(ies) "
              f"(backup: {backup})")
    elif results:
        print("dry-run: no writes (pass --apply to mark STALE/DEAD)")
    return 0


def main(argv):
    with queue_io.queue_lock(owner="staged-preflight:plan", recover=False):
        if "--apply" in argv:
            with file_lock(STAGED_FILE + ".lock"):
                return _main_locked(argv)
        return _main_locked(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
