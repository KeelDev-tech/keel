#!/usr/bin/env python3
"""Atomic IN-FLIGHT marker transitions (stall-pm-3, 2026-09-17).

Two compounding defects, one shared transition:

1. IN-FLIGHT markers were written by ad-hoc paths (claim_packet's inline
   write, lane-driver direct queue edits) with no shared ownership. A side
   lane could double-fire a role the loop already owned (the Mercury
   double-fire pattern), because nothing tied the marker to the atomic
   launch lock.
2. A marker whose browser spawn failed stayed IN-FLIGHT forever — the
   queue claimed an application that did not exist (OpenTable sat
   IN-FLIGHT ~2.5h with no live task and no lock).

mark_inflight(role_id, task_id, phase) is the SINGLE transition for both
the loop and the lane driver:
  - it acquires/validates the atomic launch lock (launch_lock.py — no
    competing lock system is built here),
  - under queue_io.queue_lock it locates the lead in its home queue,
    refuses non-claimable states and foreign-owned markers (fail closed),
    and stamps status / status_updated / in_flight_at / browser_task_id,
  - it reads all four queue homes and refuses duplicate or unknown homes,
  - it requires fresh READY admission from standard/strategic under the
    shared static gate,
  - it always checks terminal, active, and unknown ledger holds by exact
    role/posting identity or company+title twin under the same queue lock.

Two phases:
  claim — run by apply_loop.claim_packet. No browser task exists yet, so
          the lock is acquired with the placeholder task_id
          "claim:<role_id>". The marker's browser_task_id stays empty.
  spawn — run by the lane driver immediately AFTER spawning the browser
          task, with the real browser task_id. Transfers the claim
          placeholder to the real task (refuses when the lock or marker
          is owned by someone else) and stamps browser_task_id, which is
          what stall-pm-2 attribution joins on.

rollback_inflight(role_id, task_id, reason) reverts a marker to READY
when the browser spawn failed — ONLY when the marker is owned by
task_id (marker browser_task_id or lock task_id). Never reverts a
foreign marker. Releases the launch lock on success.

CLI (for the lane driver, which is not Python):
    python3 inflight_marker.py --mark <role_id> [--company C --title T]
    python3 inflight_marker.py --spawn <role_id> <browser_task_id>
    python3 inflight_marker.py --rollback <role_id> <task_id> [--reason R]
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import launch_lock  # noqa: E402 — the one atomic lock; never rebuilt here
import queue_io  # noqa: E402
import submit_intent  # noqa: E402 — open-intent bar for the claim phase (P1)
import ready_gate  # noqa: E402 — shared static admission/ledger contract
from safe_io import rows as strict_rows  # noqa: E402
import task_liveness  # noqa: E402 — pure, fail-closed runtime row predicate
from status_matchers import is_inflight_status  # noqa: E402

from keel_paths import DATA, HOME  # noqa: E402 — repo path convention
QUEUES = {
    "standard": os.path.join(DATA, "queues", "standard-queue.json"),
    "needs_input": os.path.join(DATA, "queues", "needs_input-queue.json"),
    "strategic": os.path.join(DATA, "queues", "strategic-queue.json"),
    "rejected": os.path.join(DATA, "queues", "rejected-queue.json"),
}
# Test hook: when set, _queue_paths() uses this instead of the
# canonical keel_paths constants. Production code never sets it.
_PATHS_OVERRIDE = None


def set_queue_paths(paths):
    """Redirect queue files (test isolation). Pass None to restore the
    default keel_paths resolution."""
    global _PATHS_OVERRIDE
    _PATHS_OVERRIDE = dict(paths) if paths else None


def _queue_paths():
    """Queue home -> path. Resolves from keel_paths canonical constants.
    Tests use set_queue_paths() for isolation."""
    if _PATHS_OVERRIDE is not None:
        return _PATHS_OVERRIDE
    return dict(QUEUES)  # repo: canonical queue paths from keel_paths
READY_STATES = ("READY", "READY-FOR-BROWSER")

PDT = "America/Los_Angeles"


def _pdt_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(PDT)).strftime("%Y-%m-%d %H:%M PDT")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _claim_placeholder(role_id):
    return f"claim:{role_id}"


def _locate(role_id):
    """(home, entries, entry) for role_id across all configured queues.

    A missing role returns (None, ...). Missing/malformed queue homes or
    duplicate role rows raise ValueError; incomplete inventory is not
    evidence of one home.
    """
    hits = []
    for home, path in _queue_paths().items():
        raw = queue_io.read_snapshot(path)
        if raw is None:
            raise ValueError("queue home is missing or null: " + home)
        items = strict_rows(raw)
        for e in items:
            if isinstance(e, dict) and e.get("role_id") == role_id:
                hits.append((home, path, raw, items, e))
    if len(hits) > 1:
        raise ValueError(f"role_id {role_id} in multiple queues: "
                         + ", ".join(h[0] for h in hits))
    if not hits:
        return None, None, None, None, None
    home, path, raw, items, entry = hits[0]
    return home, path, raw, items, entry


def _transfer_claim_lock(role_id, task_id, owner):
    """Replace a claim placeholder lock with the real browser task_id.

    CALLER MUST HOLD queue_io.queue_lock(): the read-verify-replace below
    is only atomic against rival transfers when serialized by that lock
    (the lane's single-driver invariant plus the lock make double-transfer
    impossible; without the lock two racers could both replace).

    Returns (ok, info). Only the placeholder created for THIS role may be
    transferred; a lock owned by any other task refuses (foreign).
    """
    lock = launch_lock._read_lock(role_id)
    if lock and lock.get("task_id") == task_id:
        return True, {"status": "ACQUIRED", "lock": lock,
                      "note": "already owner"}
    if not lock or lock.get("task_id") != _claim_placeholder(role_id):
        if lock and launch_lock._is_fresh(lock):
            return False, {"status": "HELD", "lock": lock,
                           "note": "lock owned by another task; stand down"}
        # No lock or a stale foreign lock: fall through to a fresh
        # atomic acquire (fail-open like the rest of the lock system).
        # NOTE: launch_lock.acquire is itself atomic; calling it under
        # the queue lock is safe (no lock ordering inversion: the queue
        # lock is the outermost lock everywhere in this module).
        return launch_lock.acquire(role_id, task_id, owner)
    new_lock = {
        "role_id": role_id,
        "task_id": task_id,
        "owner": owner,
        "acquired_at": launch_lock._now().isoformat(),
        "ttl_hours": launch_lock.LOCK_TTL_H,
        "transferred_from": _claim_placeholder(role_id),
    }
    path = launch_lock._lock_path(role_id)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w",
                                         dir=os.path.dirname(path) or ".",
                                         prefix=".lock-", suffix=".tmp",
                                         delete=False) as f:
            tmp = f.name
            json.dump(new_lock, f, indent=1)
        # Serialized by the caller's queue_io.queue_lock: no rival
        # transfer can interleave between this re-verify and the replace.
        cur = launch_lock._read_lock(role_id)
        if not cur or cur.get("task_id") != _claim_placeholder(role_id):
            return False, {"status": "HELD", "lock": cur,
                           "note": "placeholder moved during transfer"}
        os.replace(tmp, path)
        tmp = None
        return True, {"status": "ACQUIRED", "lock": new_lock,
                      "note": "claim placeholder transferred to browser task"}
    except OSError as ex:
        return False, {"status": "ERROR", "note": f"transfer failed: {ex}"}
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _refuse(reason, **kw):
    out = {"ok": False, "reason": reason}
    out.update(kw)
    return out


def _atomic_queue_write(path, raw, role_id=""):
    """Crash-safe queue write with disk-full failing loud (P5 §3.4.2).

    Wraps queue_io.atomic_write_json: on DiskFullError a `disk_full`
    telemetry event (target path + bytes attempted) is emitted via
    log_event, then the error propagates — the marker write must not
    silently skip (a claim without its marker is a phantom launch).

    This tree's queue_io does NOT define DiskFullError/report_disk_full
    (silent-defect sweep 2026-09-19): getattr guards keep the ORIGINAL
    error propagating instead of the except clause itself raising
    AttributeError and masking it. DiskFullError subclasses OSError, so
    the OSError fallback is behavior-preserving where the names exist.
    """
    _DiskFullError = getattr(queue_io, "DiskFullError", OSError)
    try:
        queue_io.atomic_write_json(path, raw)
    except _DiskFullError as ex:
        _report = getattr(queue_io, "report_disk_full", None)
        if _report is not None:
            _report(path, getattr(ex, "bytes_attempted", 0), ex,
                    role_id=role_id)
        raise


def _append_qnote(entry, text):
    """Append a timestamped note to queue_notes, tolerating list or str form.

    2026-09-17: queue_notes is a list on entries touched by recent lane
    tooling; the old string-concat here crashed --spawn for Culture Amp.
    """
    stamped = f"[inflight-marker spawn {_pdt_now()}] {text}"
    qn = entry.get("queue_notes")
    if isinstance(qn, list):
        qn.append(stamped)
    elif isinstance(qn, str) and qn.strip():
        entry["queue_notes"] = (qn + "\n" + stamped).strip()
    else:
        entry["queue_notes"] = stamped


def _open_intent_bar(role_id, own_attempt_id):
    """Keel 0.4 P1 §3.2 step 3: the claim path consults the submit-intent
    store BEFORE writing the marker. Returns a refusal dict when the claim
    must not proceed, else None.

    - open intent for a DIFFERENT attempt_id -> refuse, naming the open
      intent id ("open intent <id> bars new attempt").
    - the si-UNREADABLE-STORE sentinel -> REFUSE (fail closed), never read
      as "no open intent".
    - no open intent, or the open intent is our own (the claim's freshly
      minted intent) -> proceed.
    """
    try:
        open_rec = submit_intent.open_unknown_for(role_id)
    except Exception as ex:
        return _refuse("submit-intent store unreadable; refusing new "
                       f"attempt (fail closed): {ex}")
    if open_rec is None:
        return None
    open_id = open_rec.get("attempt_id") or ""
    if open_id == "si-UNREADABLE-STORE":
        # Fail closed: an unreadable store must not un-bar the lead.
        return _refuse("submit-intent store unreadable (si-UNREADABLE-STORE "
                       "sentinel); refusing new attempt (fail closed)")
    if own_attempt_id and open_id == own_attempt_id:
        return None  # our own freshly minted intent: the claim's identity
    return _refuse(f"open intent {open_id} bars new attempt",
                   open_attempt_id=open_id,
                   open_intent_state=open_rec.get("state"))


def marker_attempt_id(role_id):
    """The attempt_id stamped on the role's IN-FLIGHT marker, or None.

    Read helper for loop-side launch telemetry (browser_launched joins on
    the marker's foreign key). Never raises on missing state.
    """
    try:
        _, _, _, _, e = _locate(role_id)
    except Exception:
        return None
    if e is None:
        return None
    return e.get("attempt_id") or None


def _admission_bar(home, entry, *, company=None, title=None):
    """Read-only claim boundary; caller holds the shared queue lock.

    Queue identity supplies employer/role facts, so omitted CLI arguments
    cannot bypass duplicate checks and supplied arguments cannot replace
    the record's actual identity. A ledger read failure is an unknown hold.
    """
    if home not in {"standard", "strategic"}:
        return _refuse("non_admission_queue: lead is held in " + str(home),
                       reason_codes=["non_admission_queue"])
    for key, supplied in (("company", company), ("title", title)):
        if supplied and launch_lock._norm(supplied) != launch_lock._norm(entry.get(key)):
            return _refuse("claim identity differs from current queue " + key,
                           reason_codes=["claim_identity_mismatch"])
    try:
        ledger = strict_rows(queue_io.read_snapshot(launch_lock.LEDGER_PATH))
        if ready_gate.ledger_holds(entry, ledger):
            return _refuse("ledger_hold: existing terminal, active or unknown application",
                           reason_codes=["ledger_hold"])
    except (OSError, ValueError, TypeError, KeyError):
        return _refuse("ledger_unconfirmed: refusing claim without readable application history",
                       reason_codes=["ledger_unconfirmed"])
    # Ownership bookkeeping for an existing IN-FLIGHT row does not create
    # fresh admission. Fresh READY transitions must satisfy the same static
    # policy boundary used by the READY inspector and packet preparation.
    if entry.get("status") in READY_STATES:
        assessment = ready_gate.entry_admission(entry, origin=home, workspace=HOME)
        if not assessment["allowed"]:
            return _refuse("static READY admission refused: " + "; ".join(assessment["reasons"]),
                           reason_codes=assessment["reason_codes"])
    return None


def mark_inflight(role_id, task_id=None, phase="claim", company=None,
                  title=None, owner="", attempt_id=None,
                  task_state_provider=None):
    """Atomically claim a lead IN-FLIGHT (lock + queue marker together).

    phase="claim": loop-side (apply_loop.claim_packet). task_id may be
        None -> the launch lock is acquired with the placeholder
        "claim:<role_id>". A prior browser_task_id on a READY record is
        cleared only when task_state_provider proves that exact task
        terminal; live or unknown ownership refuses the fresh claim.
    phase="spawn": driver-side, immediately after spawning. task_id is
        REQUIRED (the real browser task id); the claim placeholder is
        transferred to it and the marker's browser_task_id is stamped.

    attempt_id (P1): the content-bound attempt identity, stamped as
        e["attempt_id"] on the marker in both phases (additive foreign
        key; the submit-intent store stays the authority). In the claim
        phase the store is consulted first: an open intent for a different
        attempt_id — or an unreadable store — refuses the claim (fail
        closed); the just-acquired placeholder lock is released so a
        refused claim never leaves a phantom lock.

    task_state_provider: optional read-only callable taking the exact prior
        task ID and returning its authoritative runtime row. It is called
        under the queue lock and must use a bounded local lookup (no
        browser operations). None, missing rows, exceptions, or mismatched
        row identities fail closed. The record is re-read and compared
        after lookup so a changed owner/status cannot be overwritten.

    Returns {"ok": False, "reason": ...}. Never raises on missing state;
    fail-closed on foreign ownership. Raises ValueError on a CORRUPT lease
    file (K20 — never silently stolen; operator reconciliation required).
    Raises OSError (queue_io.DiskFullError where that name exists — this
    tree's queue_io does not define it, so the OSError fallback applies)
    when the marker write hits a full disk (P5 §3.4.2: a `disk_full`
    telemetry event is emitted first where report_disk_full exists, then
    the error propagates — a claim without its marker write must never
    silently skip). """
    if not role_id:
        return _refuse("role_id required")
    if phase not in ("claim", "spawn"):
        return _refuse(f"unknown phase {phase!r}")
    if phase == "spawn" and not task_id:
        return _refuse("spawn phase requires the real browser task_id")

    eff_task = task_id or _claim_placeholder(role_id)

    if phase == "spawn":
        # Spawn phase: the pre-check, the placeholder->task transfer, and
        # the marker write all happen inside ONE queue_io.queue_lock
        # critical section, so rival spawns serialize and exactly one
        # transfer can win.
        with queue_io.queue_lock():
            try:
                home, path, raw, items, e = _locate(role_id)
            except ValueError as ex:
                return _refuse(str(ex))
            if e is None:
                return _refuse(f"role_id {role_id} not found in any queue")
            barrier = _admission_bar(home, e, company=company, title=title)
            if barrier is not None:
                return barrier
            st = str(e.get("status") or "")
            mt = e.get("browser_task_id")
            if mt not in (None, "", task_id):
                return _refuse("marker already owned by "
                               f"{mt}; refusing foreign takeover",
                               marker_task_id=mt)
            if not is_inflight_status(st) and st not in READY_STATES:
                return _refuse(f"lead status is {st}; only "
                               f"{'/'.join(READY_STATES)} (or our own "
                               "marker) may transition to IN-FLIGHT")
            ok, info = _transfer_claim_lock(role_id, task_id,
                                            owner or "spawn")
            if not ok:
                return _refuse("launch lock not acquired: "
                               f"{info.get('status')}",
                               lock=info.get("lock"))
            if is_inflight_status(st):
                if task_id and mt != task_id:
                    e["browser_task_id"] = task_id
                    _append_qnote(e, f"browser_task_id stamped: {task_id}")
                if attempt_id:
                    e["attempt_id"] = attempt_id  # P1: join key, both phases
                note = "already marked by us (idempotent)"
            else:
                e["status"] = "IN-FLIGHT"
                e["status_updated"] = _pdt_now()
                e["in_flight_at"] = _utc_now()
                e["browser_task_id"] = task_id
                if attempt_id:
                    e["attempt_id"] = attempt_id  # P1: join key, both phases
                _append_qnote(e, f"IN-FLIGHT claimed (lock task {task_id})")
                note = "marked IN-FLIGHT"
            _atomic_queue_write(path, raw, role_id)
        return {"ok": True, "role_id": role_id, "queue_home": home,
                "phase": phase, "lock_task_id": task_id,
                "browser_task_id": task_id, "note": note}

    with queue_io.queue_lock():
        # Serialize placeholder acquisition with marker ownership. The
        # placeholder is shared by role ID, so acquiring it before the
        # queue lock lets two fresh callers both appear to own one claim.
        ok, info = launch_lock.acquire(role_id, eff_task, owner or "claim")
        if not ok:
            return _refuse(f"launch lock not acquired: {info.get('status')}",
                           lock=info.get("lock"))
        # Release only a lease created by this call. An idempotent replay
        # or refused rival must not drop an existing claim's lease.
        _lock_created = (info.get("status") == "ACQUIRED"
                         and info.get("note") != "already owner")
        bar = _open_intent_bar(role_id, attempt_id)
        if bar is not None:
            if _lock_created:
                launch_lock.release(role_id, eff_task)
            return bar
        # A failed/corrupt queue write must not leave a phantom lease.
        try:
            try:
                home, path, raw, items, e = _locate(role_id)
            except ValueError as ex:
                if _lock_created:
                    launch_lock.release(role_id, eff_task)
                return _refuse(str(ex))
            if e is None:
                if _lock_created:
                    launch_lock.release(role_id, eff_task)
                return _refuse(f"role_id {role_id} not found in any queue")
            barrier = _admission_bar(home, e, company=company, title=title)
            if barrier is not None:
                if _lock_created:
                    launch_lock.release(role_id, eff_task)
                return barrier
            status = str(e.get("status") or "")
            marker_task = e.get("browser_task_id")
            if is_inflight_status(status):
                # A role-based placeholder is not caller identity. Only
                # an exact task or exact attempt may replay a claim.
                same_attempt = bool(
                    attempt_id and e.get("attempt_id") == attempt_id)
                same_task = bool(task_id and marker_task == task_id)
                owned = (marker_task in (None, "", eff_task)
                         and (same_attempt or same_task))
                if not owned:
                    if _lock_created:
                        launch_lock.release(role_id, eff_task)
                    return _refuse("marker already IN-FLIGHT owned by "
                                   f"{marker_task or 'another task'}; refusing "
                                   "foreign takeover",
                                   marker_task_id=marker_task)
                if attempt_id and not e.get("attempt_id"):
                    e["attempt_id"] = attempt_id  # P1: backfill our join key
                note = "already marked by us (idempotent)"
            elif status in READY_STATES:
                if marker_task not in (None, ""):
                    fingerprint = json.dumps(e, sort_keys=True,
                                             separators=(",", ":"))
                    prior_task = None
                    if task_state_provider is not None:
                        try:
                            prior_task = task_state_provider(marker_task)
                        except Exception:
                            # No row is no evidence: never infer a phantom
                            # launch from provider failure or absence.
                            prior_task = None
                    liveness = task_liveness.classify_task(
                        prior_task, task_id=marker_task)
                    if liveness != task_liveness.TERMINAL:
                        if _lock_created:
                            launch_lock.release(role_id, eff_task)
                        return _refuse(
                            "prior browser task is live or unknown; "
                            "refusing fresh claim",
                            marker_task_id=marker_task,
                            task_liveness=liveness)
                    # Re-read after the injected lookup. queue_lock
                    # serializes cooperating writers; this CAS also
                    # refuses a changed row and retains unrelated updates.
                    try:
                        home2, path2, raw2, items2, entry2 = _locate(role_id)
                    except (OSError, ValueError) as exc:
                        if _lock_created:
                            launch_lock.release(role_id, eff_task)
                        return _refuse(
                            "claim source changed or unconfirmed during task lookup: " + str(exc),
                            marker_task_id=marker_task)
                    if (entry2 is None or home2 != home or path2 != path
                            or json.dumps(entry2, sort_keys=True,
                                          separators=(",", ":"))
                            != fingerprint):
                        if _lock_created:
                            launch_lock.release(role_id, eff_task)
                        return _refuse(
                            "claim source changed during task lookup; "
                            "refusing stale owner repair",
                            marker_task_id=marker_task)
                    # The local runtime lookup may reveal concurrent
                    # history/policy updates too. Re-read the ledger and
                    # static gate before publishing a fresh claim.
                    barrier = _admission_bar(home2, entry2, company=company, title=title)
                    if barrier is not None:
                        if _lock_created:
                            launch_lock.release(role_id, eff_task)
                        return barrier
                    raw, items, e = raw2, items2, entry2
                    e["browser_task_id"] = None
                    e["claim_stale_owner_repair"] = {
                        "schema_version": 1,
                        "prior_browser_task_id": marker_task,
                        "prior_attempt_id": e.get("attempt_id"),
                        "observed_at": _utc_now(),
                        "authority": "task_state_provider",
                        "status": (prior_task.get("status")
                                   if isinstance(prior_task.get("status"), str)
                                   else None),
                        "outcome_status": (
                            prior_task.get("outcome_status")
                            if isinstance(prior_task.get("outcome_status"), str)
                            else None),
                        "terminal_reason_present": bool(
                            prior_task.get("terminal_reason")),
                    }
                    _append_qnote(
                        e, f"terminal prior browser_task_id cleared: "
                        f"{marker_task} (authoritative task state)")
                e["status"] = "IN-FLIGHT"
                e["status_updated"] = _pdt_now()
                e["in_flight_at"] = _utc_now()
                # A fresh claim may not inherit the previous attempt's
                # identity. An explicit new attempt is the only join key.
                e["attempt_id"] = attempt_id
                prev = e.get("queue_notes") or ""
                if isinstance(prev, list):  # 2026-09-17: normalize list-form notes
                    prev = "\n".join(str(x) for x in prev)
                e["queue_notes"] = (
                    prev + f"\n[inflight-marker claim {_pdt_now()}] "
                    f"IN-FLIGHT claimed (lock task {eff_task})").strip()
                note = "marked IN-FLIGHT"
            else:
                # Acquire-then-fail must not leave a phantom lock: the
                # placeholder is ours by construction (atomic acquire).
                if _lock_created:
                    launch_lock.release(role_id, eff_task)
                return _refuse(f"lead status is {status}; only "
                               f"{'/'.join(READY_STATES)} (or our own marker) "
                               "may transition to IN-FLIGHT")
            _atomic_queue_write(path, raw, role_id)
        except Exception:
            if _lock_created:
                launch_lock.release(role_id, eff_task)
            raise
    return {"ok": True, "role_id": role_id, "queue_home": home,
            "phase": phase, "lock_task_id": eff_task,
            "browser_task_id": e.get("browser_task_id"), "note": note}


def rollback_inflight(role_id, task_id, reason="spawn-failed"):
    """Revert an IN-FLIGHT marker to READY after a failed browser spawn.

    ONLY reverts when the marker is owned by task_id (the marker's
    browser_task_id, or the launch lock's task_id when the marker has
    none yet). A foreign marker refuses — the rollback must never clear
    someone else's application. Releases the launch lock when owned by
    task_id.
    """
    if not role_id or not task_id:
        return _refuse("role_id and task_id required")
    with queue_io.queue_lock():
        try:
            home, path, raw, items, e = _locate(role_id)
        except ValueError as ex:
            return _refuse(str(ex))
        if e is None:
            return _refuse(f"role_id {role_id} not found in any queue")
        status = str(e.get("status") or "")
        if not is_inflight_status(status):
            return _refuse(f"lead status is {status}; nothing to roll back")
        marker_task = e.get("browser_task_id")
        lock = launch_lock._read_lock(role_id)
        lock_task = (lock or {}).get("task_id")
        owned = (marker_task == task_id
                 or (marker_task in (None, "")
                     and lock_task in (task_id, _claim_placeholder(role_id))))
        if not owned:
            return _refuse("marker not owned by "
                           f"{task_id} (marker task {marker_task!r}, lock "
                           f"task {lock_task!r}); refusing foreign rollback",
                           marker_task_id=marker_task, lock_task_id=lock_task)
        e["status"] = "READY"
        e["status_reason"] = (
            f"browser spawn failed ({reason}): IN-FLIGHT reverted to READY "
            f"by inflight_marker {_pdt_now()} — no application was made")
        e["in_flight_at"] = None
        e["browser_task_id"] = None
        e["attempt_id"] = None  # P1: the reverted marker is no attempt
        e["status_updated"] = _pdt_now()
        prev = e.get("queue_notes") or ""
        if isinstance(prev, list):  # 2026-09-17: normalize list-form notes
            prev = "\n".join(str(x) for x in prev)
        e["queue_notes"] = (
            prev + f"\n[inflight-marker rollback {_pdt_now()}] IN-FLIGHT -> "
            f"READY: {reason} (task {task_id}); no application made").strip()
        _atomic_queue_write(path, raw, role_id)
    released = False
    if lock and lock_task == task_id:
        ok, _ = launch_lock.release(role_id, task_id)
        released = ok
    return {"ok": True, "role_id": role_id, "queue_home": home,
            "reason": reason, "lock_released": released}


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd = argv[1]
    attempt_id = None
    for i, a in enumerate(argv):
        if a == "--attempt-id" and i + 1 < len(argv):
            attempt_id = argv[i + 1]
    if cmd == "--mark":
        role_id = argv[2]
        company = title = None
        for i, a in enumerate(argv):
            if a == "--company" and i + 1 < len(argv):
                company = argv[i + 1]
            if a == "--title" and i + 1 < len(argv):
                title = argv[i + 1]
        res = mark_inflight(role_id, phase="claim", company=company,
                            title=title, owner="cli", attempt_id=attempt_id)
    elif cmd == "--spawn":
        if len(argv) < 4:
            print("usage: inflight_marker.py --spawn <role_id> "
                  "<browser_task_id>", file=sys.stderr)
            return 2
        res = mark_inflight(argv[2], task_id=argv[3], phase="spawn",
                            owner="lane-driver", attempt_id=attempt_id)
    elif cmd == "--rollback":
        if len(argv) < 4:
            print("usage: inflight_marker.py --rollback <role_id> "
                  "<browser_task_id> [--reason <r>]", file=sys.stderr)
            return 2
        reason = "spawn-failed"
        for i, a in enumerate(argv):
            if a == "--reason" and i + 1 < len(argv):
                reason = argv[i + 1]
        res = rollback_inflight(argv[2], argv[3], reason)
    else:
        print(__doc__)
        return 2
    print(json.dumps(res, indent=1, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
