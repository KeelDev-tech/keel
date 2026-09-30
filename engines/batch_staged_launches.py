#!/usr/bin/env python3
"""Batch fire staged launches when the sleep window ends.

During the sleep window the browser lane does not spawn; READY launches
stage to data/.state/staged-launches.json. When the window ends, this
script plans the wake-up batch:

  - reads the staged list (never touches the queue, never fires anything
    itself — firing is the lane owner's job);
  - rechecks the unique current queue row, scoped packet manifest, and
    duplicate/lease state for every STAGED entry;
  - emits spawn instructions for at most MAX_CONCURRENT (4) entries that
    still return verdict GO;
  - marks entries FIRED (with --apply) as it emits their instructions so a
    second run never double-emits the same lead;
  - SKIPPED entries stay for the operator to inspect (guard refusal,
    already FIRED, missing packet).

Dry-run is the default. This script NEVER spawns a browser task, NEVER
submits, NEVER writes the ledger, and NEVER invents role_ids — it only
plans and marks.

Usage:
    python3 batch_staged_launches.py             # dry-run: print the batch
    python3 batch_staged_launches.py --apply     # mark FIRED as instructions
                                                 # are emitted
    python3 batch_staged_launches.py --limit 4   # concurrency (default 4)
    python3 batch_staged_launches.py --max N      # alias for --limit

Each emitted instruction carries: role_id, company, title, packet path,
and the launch-lock task_id the spawner must claim with
`launch_lock.py --acquire` BEFORE spawning (the one-browser-task-per-role
rule still applies after staging).
"""
import json
import os
import sys
from datetime import datetime, timezone
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import HOME, DATA  # noqa: E402 — repo path convention
import queue_io  # noqa: E402 — mutual exclusion for the --apply section
import ready_gate  # noqa: E402 — current queue and scoped packet admission
from safe_io import atomic_json, contained_path, file_lock, read_json, rows  # noqa: E402

STATE = os.path.join(DATA, ".state")
STAGED_FILE = os.path.join(STATE, "staged-launches.json")
MAXMODE_FILE = os.path.join(STATE, "max-mode.json")
PACKET_DIR = os.path.join(DATA, "launch-packets")

DEFAULT_LIMIT = 4


def _posting_url(entry):
    for key in ("ats_url", "application_url", "posting_url", "job_url", "apply_url", "url"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _ledger_hold(path, role_id, *, posting_url="", company="", title=""):
    """Use the shared exact identity/twin policy without acquiring a lease."""
    document = read_json(path)
    if document is None:
        raise ValueError("current canonical ledger required")
    entry = {"role_id": role_id, "ats_url": posting_url, "company": company, "title": title}
    for row in rows(document):
        if ready_gate.ledger_holds(entry, [row]):
            return f"ledger hold {row.get('status')}: exact role/posting/twin already active/terminal"
    return None


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _atomic_write(path, obj):
    atomic_json(path, obj)


def _staged_entries(document):
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict):
        keys = [key for key in ("entries", "staged") if key in document]
        if len(keys) != 1:
            raise ValueError("staged document must have exactly one entry container")
        entries = document[keys[0]]
    else:
        raise ValueError("invalid staged document")
    if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
        raise ValueError("staged entries must be a list of objects")
    return entries


def _backup_staged():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(DATA, "queues",
                        f"_backup-{ts}-batch-staged-launches",
                        "staged-launches.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
        json.dump(_load_json(STAGED_FILE, {}), f, indent=1)
    return dest


def _guard_go(role_id, company, title):
    """Read-only duplicate/lease check; actual acquisition belongs to caller.

    A planned instruction is not ownership. Do not mint a placeholder
    lease during a dry run or retain one that blocks the real lane owner.
    Corrupt ledger/lease reads continue to raise for reconciliation.
    """
    try:
        import launch_lock
    except Exception as ex:
        return False, f"launch_lock unavailable ({ex})"
    try:
        hold = _ledger_hold(launch_lock.LEDGER_PATH, role_id, company=company, title=title)
        if hold:
            return False, hold
        submitted = launch_lock._load_submitted(launch_lock.LEDGER_PATH)
        if role_id in submitted:
            return False, "guard ALREADY_SUBMITTED: confirmed role in ledger"
        nco, nti = launch_lock._norm(company), launch_lock._norm(title)
        if nco and nti:
            if any(launch_lock._norm(co) == nco and launch_lock._norm(ti) == nti
                   for co, ti in submitted.values()):
                return False, "guard TWIN_SUBMITTED: confirmed company/title in ledger"
            if launch_lock._telemetry_submitted_match(company, title):
                return False, "guard TELEMETRY_SUBMITTED: confirmed company/title in telemetry"
        lease = launch_lock._read_lock_strict(role_id)
        if lease and launch_lock._is_fresh(lease):
            return False, "guard HELD: launch lease already owned"
    except ValueError:
        # Corrupt lease (K20): never silently absorbed — the ValueError
        # must propagate loudly for operator reconciliation
        # (silent-defect sweep 2026-09-19).
        raise
    except Exception as ex:
        return False, f"prelaunch read failed ({ex})"
    return True, "GO"


def _answer_bank(*, data=None):
    """Match the application loop's current workspace-first bank lookup."""
    path = os.path.join(data or DATA, "answer_bank.json")
    bank = read_json(path)
    if bank is None:
        bank = read_json(os.path.join(BASE, "answer_bank.json"))
    if bank is None:
        bank = {"answers": {}, "banded_questions": {}, "gates": {}}
    return bank


def _current_admission(staged, *, now=None, workspace=None, data=None,
                       packet_dir=None):
    """Re-read one canonical queue home and the current packet authority.

    The staged copy cannot authorize launch. Refuse interrupted queue
    transactions instead of silently repairing state during a plan.
    """
    workspace, data = workspace or HOME, data or DATA
    packet_dir = packet_dir or PACKET_DIR
    result = {"allowed": False, "reasons": [], "entry": None,
              "packet": None, "packet_path": None}
    try:
        if not isinstance(staged, dict):
            raise ValueError("staged entry must be an object")
        rid = staged.get("role_id")
        if (not isinstance(rid, str) or not rid.strip()
                or rid in (".", "..") or os.path.basename(rid) != rid):
            raise ValueError("valid role_id required")
        if staged.get("fireable") is False or staged.get("reverify_required"):
            raise ValueError("staged entry requires re-verification")
        with queue_io.queue_lock(owner="staged:admission", recover=False):
            matches = []
            for origin in ("standard", "needs_input", "strategic", "rejected"):
                path = os.path.join(data, "queues", f"{origin}-queue.json")
                snapshot = queue_io.read_snapshot_checked(path)
                if snapshot is None:
                    raise ValueError(f"missing canonical queue: {origin}")
                matches.extend((origin, row) for row in queue_io.queue_entries(snapshot)
                               if row.get("role_id") == rid)
            if len(matches) != 1:
                raise ValueError("role must have exactly one current canonical queue home")
            origin, current = matches[0]
            if origin not in ("standard", "strategic"):
                raise ValueError("current home is not an admission queue")
            hold = _ledger_hold(os.path.join(data, "application-ledger.json"), rid,
                                posting_url=_posting_url(current),
                                company=current.get("company"), title=current.get("title"))
            if hold:
                raise ValueError(hold)
            admission = ready_gate.entry_admission(current, origin, now=now, workspace=workspace)
            if not admission["allowed"]:
                result["reasons"] = admission["reasons"]
                return result
            if any(staged.get(key) != current.get(key) for key in ("company", "title")):
                raise ValueError("staged target differs from current queue context")
            requested = staged.get("packet_path") or staged.get("packet")
            if not requested:
                buffered = os.path.join(packet_dir, "buffer", f"{rid}.json")
                requested = buffered if os.path.exists(buffered) else os.path.join(packet_dir, f"{rid}.json")
            path = contained_path(packet_dir, requested)
            packet = read_json(path)
            admission = ready_gate.packet_admission(
                packet, current, _answer_bank(data=data), now=now, workspace=workspace)
            if not admission["allowed"]:
                result["reasons"] = admission["reasons"]
                return result
            result.update(allowed=True, entry=current, packet=packet, packet_path=str(path))
    except (ValueError, TypeError, OSError, queue_io.QueueRecoveryRequired,
            queue_io.QueueLockTimeout) as ex:
        result["reasons"] = [str(ex)]
    return result


def plan_batch(entries, limit):
    """Return (spawn_instructions, skipped) — pure, no writes."""
    spawn, skipped = [], []
    staged_counts = Counter(e.get("role_id") for e in entries if isinstance(e, dict)
                            and isinstance(e.get("role_id"), str))
    for e in entries:
        if len(spawn) >= limit:
            break
        if not isinstance(e, dict):
            skipped.append({"role_id": None, "reason": "malformed staged entry"})
            continue
        rid = e.get("role_id", "")
        if staged_counts.get(rid, 0) != 1:
            skipped.append({"role_id": rid, "reason": "duplicate staged role_id"})
            continue
        status = (e.get("status") or "STAGED").upper()
        if status != "STAGED":
            skipped.append({"role_id": rid, "reason": f"status {status}, not plannable"})
            continue
        admitted = _current_admission(e)
        if not admitted["allowed"]:
            skipped.append({"role_id": rid, "reason": "; ".join(admitted["reasons"])})
            continue
        current = admitted["entry"]
        ok, note = _guard_go(rid, current.get("company", "") or "",
                             current.get("title", "") or "")
        if not ok:
            skipped.append({"role_id": rid, "reason": note})
            continue
        spawn.append({
            "role_id": rid,
            "company": current.get("company", ""),
            "title": current.get("title", ""),
            "packet": admitted["packet_path"],
            "claim": ("python3 launch_lock.py --acquire %s <task_id> "
                      "--owner batch-staged" % rid),
            "note": ("plan only: revalidate current queue/packet and run "
                     "the actual prelaunch guard before any browser task; "
                     "this instruction holds no launch ownership"),
        })
    return spawn, skipped


def _main_locked(argv):
    apply = "--apply" in argv
    limit = DEFAULT_LIMIT
    for flag in ("--limit", "--max"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                try:
                    limit = max(1, int(argv[i + 1]))
                except ValueError:
                    pass
    # --apply critical section (silent-defect sweep 2026-09-19):
    # load -> plan -> mark-FIRED -> write must be mutually
    # exclusive. Two concurrent runs otherwise both pass the
    # guard (same task_id re-acquire is idempotent) and
    # double-emit the same lead.
    with queue_io.queue_lock(owner="batch_staged_launches:plan", recover=False):
        now = datetime.now(timezone.utc)
        maxmode = _load_json(MAXMODE_FILE, {})
        staged = read_json(STAGED_FILE, missing={"entries": []})
        entries = _staged_entries(staged)

        out = {"ts": now.isoformat(),
               "sleep_window": maxmode.get("sleep_window"),
               "limit": limit, "apply": apply,
               "spawn": [], "skipped": []}
        if entries:
            out["spawn"], out["skipped"] = plan_batch(entries, limit)
        print(json.dumps(out, indent=1))
        print(f"\nbatch: {len(out['spawn'])} spawn instruction(s) "
              f"(limit {limit}), {len(out['skipped'])} skipped")

        if not out["spawn"]:
            print("nothing to fire")
            return 0
        if not apply:
            print("dry-run: no writes (pass --apply to mark FIRED on emit)")
            return 0

        backup = _backup_staged()
        by_id = {e.get("role_id"): e for e in entries}
        for s in out["spawn"]:
            e = by_id.get(s["role_id"])
            if e is None:
                continue
            e["status"] = "FIRED"
            e["fired_at"] = now.isoformat()
            e["fired_by"] = "batch_staged_launches"
        _atomic_write(STAGED_FILE, staged)
        print(f"applied: marked {len(out['spawn'])} entr(ies) FIRED "
              f"(backup: {backup}). The lane owner still fires each browser "
              f"task from these instructions — this script spawned nothing.")
        return 0


def main(argv):
    with queue_io.queue_lock(owner="batch_staged_launches:plan", recover=False):
        if "--apply" in argv:
            # Same ordering as preflight: queue lock then staging lock.
            # Share the canonical writer's lock so replenishment survives.
            with file_lock(STAGED_FILE + ".lock"):
                return _main_locked(argv)
        return _main_locked(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
