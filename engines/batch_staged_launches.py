#!/usr/bin/env python3
"""Batch fire staged launches when the sleep window ends.

During the sleep window the browser lane does not spawn; READY launches
stage to data/.state/staged-launches.json. When the window ends, this
script plans the wake-up batch:

  - reads the staged list (never touches the queue, never fires anything
    itself — firing is the lane owner's job);
  - re-runs the launch-lock prelaunch_guard on every STAGED entry
    (conditions drift while leads wait: a duplicate/twin submission may
    have landed overnight);
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

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import DATA  # noqa: E402 — repo path convention
import queue_io  # noqa: E402 — mutual exclusion for the --apply section

STATE = os.path.join(DATA, ".state")
STAGED_FILE = os.path.join(STATE, "staged-launches.json")
MAXMODE_FILE = os.path.join(STATE, "max-mode.json")
PACKET_DIR = os.path.join(DATA, "launch-packets")

DEFAULT_LIMIT = 4


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
    """Re-check the prelaunch guard right before emitting a spawn
    instruction. Only explicit verdict GO passes; guard errors and any
    other verdict fail closed (the entry is skipped, never force-fired)."""
    try:
        import launch_lock
    except Exception as ex:
        return False, f"launch_lock unavailable ({ex})"
    try:
        ok, info = launch_lock.prelaunch_guard(role_id, "batch-staged",
                                               company, title,
                                               owner="batch-staged-launches")
    except ValueError:
        # Corrupt lease (K20): never silently absorbed — the ValueError
        # must propagate loudly for operator reconciliation
        # (silent-defect sweep 2026-09-19).
        raise
    except Exception as ex:
        return False, f"prelaunch_guard raised ({ex})"
    verdict = (info or {}).get("verdict", "")
    if ok and verdict == "GO":
        # The guard ACQUIRED the lock under the "batch-staged" placeholder
        # task_id; the emitted instruction tells the spawner to acquire
        # with its OWN task_id, so release ours first — otherwise every
        # emitted instruction is unlaunchable (HELD) for the full TTL
        # (silent-defect sweep 2026-09-19). The duplicate-check value of
        # the guard is kept: it still ran before this release.
        rok, rinfo = launch_lock.release(role_id, "batch-staged")
        if not rok:
            return False, ("guard GO but lock release failed "
                           f"({(rinfo or {}).get('status')}); refusing emit")
        return True, "GO"
    return False, "guard %s: %s" % (verdict or "REFUSED",
                                    (info or {}).get("reason", ""))


def plan_batch(entries, limit):
    """Return (spawn_instructions, skipped) — pure, no writes."""
    spawn, skipped = [], []
    for e in entries:
        if len(spawn) >= limit:
            break
        rid = e.get("role_id", "")
        status = (e.get("status") or "STAGED").upper()
        if status != "STAGED":
            skipped.append({"role_id": rid, "reason": f"status {status}, not STAGED"})
            continue
        packet_path = os.path.join(PACKET_DIR, f"{rid}.json")
        if not os.path.exists(packet_path):
            skipped.append({"role_id": rid,
                            "reason": "no packet file — rebuild before firing"})
            continue
        ok, note = _guard_go(rid, e.get("company", "") or "",
                             e.get("title", "") or "")
        if not ok:
            skipped.append({"role_id": rid, "reason": note})
            continue
        spawn.append({
            "role_id": rid,
            "company": e.get("company", ""),
            "title": e.get("title", ""),
            "packet": packet_path,
            "claim": ("python3 launch_lock.py --acquire %s <task_id> "
                      "--owner batch-staged" % rid),
            "note": ("acquire the launch lock BEFORE spawning; the guard "
                     "was GO at batch time"),
        })
    return spawn, skipped


def main(argv):
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
    with queue_io.queue_lock(owner="batch_staged_launches:apply"):
        now = datetime.now(timezone.utc)
        maxmode = _load_json(MAXMODE_FILE, {})
        staged = _load_json(STAGED_FILE, {})
        entries = (staged.get("entries", []) if isinstance(staged, dict)
                   else staged if isinstance(staged, list) else [])

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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
