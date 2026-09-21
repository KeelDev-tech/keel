#!/usr/bin/env python3
"""Pre-flight validator for staged launches (sleep-window pattern).

While the operator is away/unavailable, the browser lane does not spawn:
READY launches are staged to data/.state/staged-launches.json and fire when
the window ends. Conditions drift while leads wait (postings die, other
lanes submit the same role, rate-limit budgets exhaust, operator policy
changes), so every staged entry is re-validated here immediately before it
fires. A stale entry never spawns.

Reads: data/.state/max-mode.json, data/.state/staged-launches.json,
       the apply loop's refresh buffer (source of truth on what is
       already submitted/IN-FLIGHT), and the launch-lock prelaunch guard.
Runs:  rate_limits.is_allowed and prescreen.screen_packet — exactly the
       same checks the live lane runs before spawn.
Never: spawns browser tasks, submits anything, writes ledgers, or touches
       the queue. Marking a staged entry STALE/DEAD is the only write, and
       only with --apply (dry-run is the default).

Usage:
    python3 staged-launch-preflight.py            # dry-run: report verdicts
    python3 staged-launch-preflight.py --apply    # mark STALE/DEAD entries
    python3 staged-launch-preflight.py --entry <role_id>
                                                  # single-entry report

A staged entry fires only when ALL of these hold:
  1. status is STAGED (not already FIRED/SKIPPED/STALE/DEAD).
  2. launch-lock prelaunch_guard returns verdict GO (duplicate/submitted
     twin check — the guard is the single source of truth on "already
     handled").
  3. The packet file still exists and is fresh (STALE_AFTER_H; a stale
     packet's form intel may be wrong — rebuild it instead of firing).
  4. rate_limits.is_allowed(employer) is True (budget may have exhausted
     while the entry waited).
  5. prescreen.screen_packet(packet, answer_bank) still returns CLEAN
     (form intel may have changed since staging; run a fresh probe first
     with --probe when available).

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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


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
    """Ask the launch-lock prelaunch guard. Any verdict other than explicit
    GO blocks firing; guard errors fail closed too."""
    try:
        import launch_lock
    except Exception as ex:
        return False, f"launch_lock unavailable ({ex})"
    try:
        ok, info = launch_lock.prelaunch_guard(role_id, "preflight", company,
                                               title, owner="preflight")
    except ValueError:
        # Corrupt lease (K20): never silently absorbed — the ValueError
        # must propagate loudly for operator reconciliation
        # (silent-defect sweep 2026-09-19).
        raise
    except Exception as ex:
        return False, f"prelaunch_guard raised ({ex})"
    verdict = (info or {}).get("verdict", "")
    if ok and verdict == "GO":
        return True, "prelaunch_guard GO"
    return False, "prelaunch_guard %s: %s" % (
        verdict or ("ok" if ok else "REFUSED"), (info or {}).get("reason", ""))


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
    bank = _load_json(os.path.join(ENGINES, "answer_bank.json"), {})
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
    ok, note = _guard_go(role_id, company, title)
    if not ok:
        reasons.append(note)
        return "DEAD", reasons
    reasons.append(note)
    fresh, note = _packet_fresh(role_id, now)
    if not fresh:
        reasons.append(note)
        return "STALE", reasons
    reasons.append(note)
    ok, note = _budget_ok(company)
    if not ok:
        reasons.append(note)
        return "DEAD", reasons
    reasons.append(note)
    packet = _load_json(os.path.join(PACKET_DIR, f"{role_id}.json"), {})
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
    res = _screen_packet(role_id, packet)
    if res.get("verdict") == "PARK":
        return "DEAD", reasons + list(res.get("reasons", []))
    reasons.append("prescreen CLEAN")
    return "FIRE", reasons


def main(argv):
    apply = "--apply" in argv
    do_probe = "--probe" in argv
    only = None
    if "--entry" in argv:
        i = argv.index("--entry")
        if i + 1 < len(argv):
            only = argv[i + 1]
    now = datetime.now(timezone.utc)

    maxmode = _load_json(MAXMODE_FILE, {})
    staged = _load_json(STAGED_FILE, {})
    if isinstance(staged, dict):
        entries = staged.get("entries", [])
    elif isinstance(staged, list):
        entries = staged
    else:
        entries = []

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
            if r["verdict"] in ("FIRE", "SKIP"):
                continue
            e = by_id.get(r["role_id"])
            if e is None:
                continue
            e["status"] = r["verdict"]
            e["preflight_reasons"] = r["reasons"]
            e["preflight_at"] = now.isoformat()
            changed += 1
        _atomic_write(STAGED_FILE, staged)
        print(f"applied: marked {changed} entr(ies) STALE/DEAD "
              f"(backup: {backup})")
    elif results:
        print("dry-run: no writes (pass --apply to mark STALE/DEAD)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
