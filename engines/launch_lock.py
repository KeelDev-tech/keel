#!/usr/bin/env python3
"""Launch lock: one browser application task per role_id at a time.

Closes the 2026-09-15 Mercury duplicate-fire: the apply-loop launch
watcher fired an event into main chat at the same moment a side-chat arm
was already launching the same role_id, and main chat spawned a second
browser task on it (stood down at 2 steps, no external actions — but a
double-submit would have been a blacklist-grade incident).

Convention (see AGENTS.md): BEFORE spawning a browser application task
for a role_id, the spawning agent acquires the lock:

    python3 launch_lock.py --acquire <role_id> <task_id> [--owner <name>]

If the lock is HELD by a different task_id (exit 1), stand down — another
task owns the lead. On task completion/cancellation, release it:

    python3 launch_lock.py --release <role_id> <task_id>

Locks expire after LOCK_TTL_H hours (fail-open: a dead task's stale lock
never blocks the lane forever, same philosophy as packet_watchdog).
Re-acquiring with the same task_id refreshes the lock (idempotent).

x20 EXTENSION (2026-09-16): parallel lanes race on the old read-then-write
acquire (TOCTOU: two lanes both see FREE, both write, both believe they
own the lead). v2 makes acquisition ATOMIC via O_CREAT|O_EXCL — exactly
one winner per role_id, the loser sees HELD. It also adds the pre-launch
duplicate guard:

    python3 launch_lock.py --guard <role_id> <task_id> --company "<c>" --title "<t>"

which refuses launch when the ledger already holds a SUBMITTED row for the
exact role_id (ALREADY_SUBMITTED) or for the same normalized
company+title under a different role_id (TWIN_SUBMITTED — the 2026-09-16
Napa Valley Reserve twin case). Verdicts are machine-readable; only GO
proceeds to spawn. Defense in depth with apply_loop's eligible() posting-
URL dedupe and ARM-R1 claim-time dedupe — this guard is the pre-spawn
gate, those remain the pre-build gates.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import HOME as PIPE, DATA  # noqa: E402 — repo root; never the private pipeline path
LOCK_DIR = os.path.join(PIPE, "hidden_files", "launch-locks")
LOCK_TTL_H = 2
# Silent-defect sweep 2026-09-19: the old path (<HOME>/ledger/...) was a
# dead letter — this tree's writer and all other readers use
# <HOME>/data/application-ledger.json and nothing writes to ledger/.
LEDGER_PATH = os.path.join(DATA, "application-ledger.json")


def _lock_path(role_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", role_id)
    return os.path.join(LOCK_DIR, safe + ".json")


def _now():
    return datetime.now(timezone.utc)


def _read_lock(role_id):
    path = _lock_path(role_id)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _read_lock_strict(role_id):
    """_read_lock that raises ValueError on a present-but-unreadable lease.

    K20 (silent-defect sweep 2026-09-19): a corrupt lease file must never
    be silently stolen — the old code read None from the JSONDecodeError
    swallow and treated the corrupt-but-present lease as stale, deleting
    and re-creating it. Operator reconciliation is required instead.
    """
    path = _lock_path(role_id)
    lock = _read_lock(role_id)
    if lock is None and os.path.exists(path):
        raise ValueError(
            f"launch lock for role_id {role_id!r} is present but "
            "unreadable (corrupt lease); refusing takeover — operator "
            "reconciliation required")
    return lock


def _is_fresh(lock):
    if not lock or "acquired_at" not in lock:
        return False
    try:
        acquired = datetime.fromisoformat(lock["acquired_at"])
    except (ValueError, TypeError):
        return False
    return _now() - acquired < timedelta(hours=LOCK_TTL_H)


def check(role_id):
    """Return the lock dict if fresh, else None."""
    lock = _read_lock(role_id)
    return lock if _is_fresh(lock) else None


def _write_lock_atomically(path, lock):
    """Publish the lock file atomically. Returns True on success,
    False if the file already existed (someone else won the race).

    Write-temp + os.link: link creation is atomic on POSIX, so a
    competing reader never observes a partially-written lock file.
    (The previous O_CREAT|O_EXCL-then-write left a create/write window
    where a rival could read an empty file, delete it as 'stale', and
    become a second winner — caught by the flaky 20-thread race test
    2026-09-16.)
    """
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", dir=os.path.dirname(path) or ".",
                prefix=".lock-", suffix=".tmp", delete=False) as f:
            tmp = f.name
            json.dump(lock, f, indent=1)
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def acquire(role_id, task_id, owner=""):
    """Acquire the lock ATOMICALLY. Returns (ok: bool, info: dict).

    Exactly one concurrent caller wins per role_id. A stale lock (older
    than LOCK_TTL_H) is taken over: the old file is removed and the
    atomic create is retried once. Same-task re-acquire refreshes the
    lease (acquired_at rewritten atomically).

    Raises ValueError when the existing lease file is present but
    unreadable (corrupt lease, K20) — never silently stolen; the caller
    must let it propagate for operator reconciliation.
    """
    os.makedirs(LOCK_DIR, exist_ok=True)
    path = _lock_path(role_id)
    lock = {
        "role_id": role_id,
        "task_id": task_id,
        "owner": owner,
        "acquired_at": _now().isoformat(),
        "ttl_hours": LOCK_TTL_H,
    }
    if _write_lock_atomically(path, lock):
        return True, {"status": "ACQUIRED", "lock": lock}
    # Someone holds a file here — inspect it. Strict read: a present but
    # unreadable (corrupt) lease raises ValueError here and at every
    # re-read below — never silently stolen (K20).
    existing = _read_lock_strict(role_id)
    if existing and existing.get("task_id") == task_id:
        if _is_fresh(existing):
            # Same-task re-acquire refreshes the lease — the docstring's
            # promise (the old code returned the existing lock unchanged,
            # a false promise for lanes re-acquiring to keep their lease).
            # remove + atomic re-create via the existing helper; the
            # "already owner" note is kept verbatim so callers keying on
            # it (inflight_marker's _lock_created gate) keep their
            # semantics.
            refreshed = dict(existing)
            refreshed["acquired_at"] = _now().isoformat()
            try:
                os.remove(path)
            except OSError:
                pass
            if _write_lock_atomically(path, refreshed):
                return True, {"status": "ACQUIRED", "lock": refreshed,
                              "note": "already owner"}
            existing = _read_lock_strict(role_id)
            if existing and existing.get("task_id") == task_id:
                return True, {"status": "ACQUIRED", "lock": existing,
                              "note": "already owner"}
            return False, {"status": "HELD", "lock": existing}
        # Own lock went stale: take it over.
        try:
            os.remove(path)
        except OSError:
            pass
        if _write_lock_atomically(path, lock):
            return True, {"status": "ACQUIRED", "lock": lock,
                          "note": "stale own lock refreshed"}
        existing = _read_lock_strict(role_id)
        return False, {"status": "HELD", "lock": existing}
    if existing and _is_fresh(existing):
        return False, {"status": "HELD", "lock": existing}
    # Stale lock held by someone else: take over, then retry. (The
    # unreadable case can no longer reach here — _read_lock_strict raised
    # above.)
    try:
        os.remove(path)
    except OSError:
        pass
    if _write_lock_atomically(path, lock):
        return True, {"status": "ACQUIRED", "lock": lock,
                      "note": "stale lock taken over"}
    existing = _read_lock_strict(role_id)
    return False, {"status": "HELD", "lock": existing}


def release(role_id, task_id):
    """Release the lock if owned by task_id. Returns (ok: bool, info)."""
    existing = _read_lock(role_id)
    if existing and existing.get("task_id") == task_id:
        os.remove(_lock_path(role_id))
        return True, {"status": "RELEASED"}
    if existing and _is_fresh(existing):
        return False, {"status": "NOT_OWNER", "lock": existing}
    return True, {"status": "NO_LOCK"}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _load_submitted(ledger_path=LEDGER_PATH):
    """Return {role_id: (company, title)} for SUBMITTED ledger rows."""
    try:
        with open(ledger_path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    rows = data if isinstance(data, list) else data.get("rows", data.get("applications", []))
    out = {}
    for r in rows:
        if str(r.get("status", "")).upper() != "SUBMITTED":
            continue
        rid = r.get("role_id")
        if rid:
            out[rid] = (r.get("company", ""), r.get("title", ""))
    return out


def _telemetry_submitted_match(company, title, telemetry_path=None):
    """Raw-telemetry submitted evidence for (company, title), or None.

    2026-09-22: the canonical ledger lags raw telemetry — a browser
    submission can be fully confirmed in telemetry/events.jsonl while
    the ledger has no SUBMITTED row yet. This consults the raw submitted /
    submission_claimed events via parked_task_sweep's authoritative matcher
    (both aliases, company+normalized-title, never role_id alone).
    Fail-closed: any read/parse problem yields no match (guard then behaves
    as before — ledger-only — rather than refusing everything).

    Lazy-imports parked_task_sweep (module-level coupling is avoided so the
    lock file stays importable from minimal contexts; the matcher caches
    by file mtime so repeated guard calls stay cheap).
    """
    nco, nti = _norm(company), _norm(title)
    if not nco or not nti:
        return None
    try:
        import parked_task_sweep as pts
    except ImportError:
        return None
    path = telemetry_path or getattr(pts, "EVENTS_JSONL", None)
    try:
        by_company = pts._telemetry_submitted_by_company(path)
        matched = pts._entry_telemetry_submitted(
            {"company": company, "title": title}, by_company)
    except Exception:
        return None
    return matched


def prelaunch_guard(role_id, task_id, company="", title="", owner="",
                    ledger_path=LEDGER_PATH, telemetry_path=None):
    """Pre-spawn duplicate guard. Returns (ok: bool, info: dict).

    Verdicts:
      GO                — lock acquired, no duplicate; safe to spawn.
      HELD              — another live task owns this role_id; stand down.
      ALREADY_SUBMITTED — ledger already has SUBMITTED for this role_id.
      TWIN_SUBMITTED    — ledger has SUBMITTED for the same normalized
                          company+title under a different role_id.
      TELEMETRY_SUBMITTED — raw telemetry shows a submitted event for
                          company+title with no ledger row yet (ledger lag).
    Only GO proceeds to browser-task spawn. The ledger read + atomic
    acquire happen in one call so lanes cannot interleave a duplicate.
    """
    submitted = _load_submitted(ledger_path)
    if role_id in submitted:
        return False, {"status": "ALREADY_SUBMITTED", "verdict": "REFUSE",
                       "role_id": role_id,
                       "note": "ledger already holds SUBMITTED for this role_id"}
    nco, nti = _norm(company), _norm(title)
    if nco and nti:
        for rid, (co, ti) in submitted.items():
            if _norm(co) == nco and _norm(ti) == nti:
                return False, {"status": "TWIN_SUBMITTED", "verdict": "REFUSE",
                               "role_id": role_id, "twin_role_id": rid,
                               "twin_company": co, "twin_title": ti,
                               "note": "same company+title already SUBMITTED "
                                       "under a different role_id"}
        tel_match = _telemetry_submitted_match(company, title, telemetry_path)
        if tel_match:
            return False, {"status": "TELEMETRY_SUBMITTED", "verdict": "REFUSE",
                           "role_id": role_id,
                           "telemetry_match_title": tel_match,
                           "note": "raw telemetry shows a submitted event "
                                   "for this company+title with no ledger "
                                   "SUBMITTED row yet (ledger lag) — "
                                   "relaunch would risk a duplicate "
                                   "submission; no lock acquired"}
    ok, info = acquire(role_id, task_id, owner)
    if not ok:
        return False, {"status": "HELD", "verdict": "STAND_DOWN",
                       "role_id": role_id, "lock": info.get("lock")}
    return True, {"status": "ACQUIRED", "verdict": "GO",
                  "role_id": role_id, "lock": info["lock"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--acquire", nargs=2, metavar=("ROLE_ID", "TASK_ID"))
    ap.add_argument("--check", metavar="ROLE_ID")
    ap.add_argument("--release", nargs=2, metavar=("ROLE_ID", "TASK_ID"))
    ap.add_argument("--guard", nargs=2, metavar=("ROLE_ID", "TASK_ID"),
                    help="pre-spawn duplicate guard (acquires on GO)")
    ap.add_argument("--company", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--owner", default="")
    ap.add_argument("--ledger", default=LEDGER_PATH)
    ap.add_argument("--events", default=None,
                    help="telemetry events.jsonl path for the --guard "
                         "TELEMETRY_SUBMITTED check (default: repo data/telemetry)")
    args = ap.parse_args()

    if args.acquire:
        ok, info = acquire(args.acquire[0], args.acquire[1], args.owner)
    elif args.check:
        lock = check(args.check)
        ok, info = (True, {"status": "FREE" if lock is None else "HELD",
                           "lock": lock})
    elif args.release:
        ok, info = release(args.release[0], args.release[1])
    elif args.guard:
        ok, info = prelaunch_guard(args.guard[0], args.guard[1],
                                   args.company, args.title, args.owner,
                                   args.ledger, args.events)
    else:
        ap.print_help()
        return 2
    print(json.dumps(info, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
