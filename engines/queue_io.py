"""Atomic queue-file IO (2026-09-15, main-agent fix; lock surgery 2026-09-16).

ROOT CAUSE (2026-09-15): every queue writer did plain read-modify-write on
standard-queue.json (1.3 MB). verify_retry.py loads the queues at main()
start, runs HTTP verification for up to ~15 minutes, then dumps the whole
file. Any write made in that window — browser-lane SUBMITTED marks, parks,
lead_dead — was silently reverted. 2026-09-15 17:44 PDT: one verify_retry
save clobbered 4 lane writes (Harvey/Finni/Persona reverted READY,
Decagon's park lost). The reverted READY states were a live
double-submission hazard: apply_loop could have re-launched already
submitted leads.

FIX (2026-09-15): flock-based mutual exclusion around every read-modify-write.
The lock is reentrant within one process (park_lead() is called from inside
apply_loop's pass, which may itself hold the lock) via a process-global
depth counter. Cross-process, flock serializes. Writes also go through
tmp+os.replace so a crash never leaves a torn file.

LOCK SURGERY (2026-09-16, P0-1 complementarity audit): the 2026-09-15 fix
traded silent corruption for queue-write starvation — the flock blocked
FOREVER on contention (observed: a holder sleeping 80 min while a waiter
blocked 47 min, SIGTERM-immune; ~66 lock-blocked process-minutes in ~1h).
This revision bounds every wait:
  - waiters poll with LOCK_NB and give up after `timeout` (default 120s),
    raising QueueLockTimeout carrying the holder PID + owner for diagnosis.
  - the holder writes PID + acquire timestamp + heartbeat + owner into a
    sidecar meta file (<lock>.meta) at acquisition; waiters read it to
    report WHO holds the lock.
  - stale-holder cleanup: if the meta's PID is dead AND its heartbeat is
    older than _LOCK_STALE_S, the meta is cleared (a dead holder's flock
    was already released by the kernel, so this is cosmetic hygiene that
    keeps waiters from reporting a ghost PID).
  - every acquisition's wait latency is appended to
    hidden_files/queue_lock_waits.jsonl (ts, pid, owner, wait_ms) so the
    p99 queue-write wait metric is measurable per engine call.
  - tests NEVER take the production lock: set_lock_path() redirects the
    lock (+ meta + waits log) at a tmp path; test files set it in setUp
    and restore in tearDown.

All queue writers must use queue_lock() around their load->mutate->save
critical section:
  - prescreen.park_lead
  - apply_loop.refresh_buffer reconcile section + claim_packet
  - verify_retry apply phase (scan runs lock-free since 2026-09-16)
  - any ad-hoc lane script (use patch_entry() below or the __main__ CLI)

Cross-file transactions (2026-09-29): commit_snapshot() pre-serializes every
image, compares complete source snapshots, and durably journals PREPARED
before replacing queue files. A new lock holder completes pending journals
before exposing queue state. This gives recoverable transactions to cooperating
lock users; separate file renames remain observable to lock-free readers.
Read-only clients use queue_lock(recover=False) and refuse pending recovery.

A writer that bypasses this helper can still clobber; see AGENTS.md lesson.
"""

import contextlib
import copy
import hashlib
import fcntl
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

_PDT = ZoneInfo("America/Los_Angeles")


def pdt_now():
    """Canonical pipeline clock: current time in America/Los_Angeles.

    J-20260916-0103-gate-451 (2026-09-15): the main-chat orchestrator's
    ad-hoc 'lane:' writer snippets stamped queue entries with UTC-6
    labeled 'PDT' (~1h future) — the same bug class as the fixed PDT
    timestamp bug. Future-dated stamps delay the feeder watchdog's >2h
    stale-IN-FLIGHT flag by the offset. Every lane writer (engine code,
    ad-hoc snippets, one-off scripts) uses this — never UTC arithmetic,
    never a hardcoded offset.

    Returns an aware datetime in America/Los_Angeles. Format for queue
    stamps: pdt_now().strftime("%Y-%m-%d %H:%M PDT").
    """
    return datetime.now(_PDT)

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import HOME as _PIPE  # noqa: E402 — repo root; never the private pipeline path

# P0-1 (2026-09-16): bounded waits, holder instrumentation, test isolation.
_LOCK_TIMEOUT_S = 120   # waiter bound: give up (raising) after 120s
_LOCK_STALE_S = 600     # meta heartbeat older than this + PID dead => stale
_LOCK_POLL_S = 0.5      # LOCK_NB retry cadence


def _default_lock_path():
    return os.path.join(_PIPE, "hidden_files", "queue.lock")


_LOCK_PATH = _default_lock_path()


def _meta_path():
    return _LOCK_PATH + ".meta"


def _waits_log_path():
    return os.path.join(os.path.dirname(_LOCK_PATH), "queue_lock_waits.jsonl")


def set_lock_path(path):
    """Redirect the lock file (test isolation). Tests MUST call this with a
    tmpdir path in setUp and restore in tearDown — no test may take the
    production lock. Production code never calls this."""
    global _LOCK_PATH
    _LOCK_PATH = path


def get_lock_path():
    return _LOCK_PATH


class QueueLockTimeout(RuntimeError):
    """Raised when a queue_lock() waiter exceeds its timeout.

    Carries the holder's PID/owner (from the lock meta) so the contention
    is diagnosable instead of a silent infinite hang. Raised BEFORE any
    mutation — the caller's read-modify-write never started, so failing
    closed here loses nothing but the attempt (the caller may retry).
    """

    def __init__(self, holder_pid, holder_owner, waited_s, timeout_s):
        self.holder_pid = holder_pid
        self.holder_owner = holder_owner
        self.waited_s = waited_s
        self.timeout_s = timeout_s
        super().__init__(
            f"queue_lock: waiter timed out after {waited_s:.1f}s "
            f"(bound {timeout_s}s); holder pid={holder_pid} "
            f"owner={holder_owner!r}")


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_meta():
    try:
        with open(_meta_path(), "rb") as f:
            d = strict_loads(f.read())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_meta(owner):
    try:
        os.makedirs(os.path.dirname(_meta_path()), exist_ok=True)
        with open(_meta_path(), "w") as f:
            json.dump({"pid": os.getpid(), "owner": owner,
                       "acquired_at": time.time(),
                       "heartbeat": time.time()}, f)
    except OSError:
        pass  # meta is diagnostic-only; never break the lock flow


def _clear_meta():
    try:
        os.remove(_meta_path())
    except OSError:
        pass


def _meta_is_stale(meta):
    """A dead holder's flock was already released by the kernel — the meta
    is then just a ghost record confusing waiters. Stale only when the PID
    is dead AND the heartbeat is old (guards against PID reuse racing a
    live holder's fresh heartbeat)."""
    if not meta:
        return False
    if _pid_alive(meta.get("pid")):
        return False
    hb = meta.get("heartbeat") or meta.get("acquired_at") or 0
    try:
        return (time.time() - float(hb)) > _LOCK_STALE_S
    except (TypeError, ValueError):
        return True


def _log_wait(wait_ms, owner, timeout_s):
    try:
        os.makedirs(os.path.dirname(_waits_log_path()), exist_ok=True)
        with open(_waits_log_path(), "a") as f:
            f.write(json.dumps({
                "ts": datetime.now(_PDT).isoformat(),
                "pid": os.getpid(), "owner": owner,
                "wait_ms": round(wait_ms, 1),
                "timeout_s": timeout_s}) + "\n")
    except OSError:
        pass


_state = threading.local()


@contextlib.contextmanager
def queue_lock(timeout=None, owner=None, recover=True):
    """Exclusive, reentrant-in-process lock for queue file writes.

    timeout: waiter bound in seconds (default 120). On expiry raises
        QueueLockTimeout with the holder's PID/owner — fail closed, never
        an unbounded hang. The reentrant fast path never blocks.
    owner: short diagnostic label for the holder (e.g. "verify_retry:apply",
        "apply_loop:claim"). Recorded in the lock meta + waits log.
    recover: default True completes durable prepared transactions. False
        refuses pending journals and skips metadata/log writes for read-only
        clients (the lock file may still be created if missing).
    """
    if timeout is None:
        timeout = _LOCK_TIMEOUT_S
    depth = getattr(_state, "depth", 0)
    if depth:
        if not recover:
            _refuse_pending_transactions()
        _state.depth = depth + 1
        try:
            yield
        finally:
            _state.depth -= 1
        return
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    f = open(_LOCK_PATH, "a+")
    start = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                meta = _read_meta()
                if recover and _meta_is_stale(meta):
                    # Dead holder: the kernel already released its flock;
                    # clear the ghost meta and retry immediately.
                    _clear_meta()
                    continue
                if time.monotonic() - start >= timeout:
                    hp = (meta or {}).get("pid")
                    ho = (meta or {}).get("owner")
                    raise QueueLockTimeout(hp, ho,
                                           time.monotonic() - start, timeout)
                time.sleep(_LOCK_POLL_S)
        wait_ms = (time.monotonic() - start) * 1000.0
        if recover:
            _write_meta(owner)
            _log_wait(wait_ms, owner, timeout)
        _state.depth = 1
        try:
            # A prepared cross-file transaction must complete before a
            # cooperating reader or writer observes the queues.
            if recover:
                _state.recovered = _recover_transactions_locked()
            else:
                _refuse_pending_transactions()
                _state.recovered = []
            yield
        finally:
            _state.depth = 0
            if recover:
                _clear_meta()
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        f.close()


def strict_loads(raw):
    """Strict JSON parse (K50 port, Keel 0.3.1 review; adapted from the
    candidate's safe_io.loads — strict-JSON half ONLY).

    Rejects, raising ValueError:
      - duplicate object keys (silent last-wins is a data-loss hazard in
        queue/bank reads),
      - non-finite numbers (NaN / Infinity tokens),
      - float overflow (e.g. 1e999 parses to inf — caught by a
        non-finitely-tolerant re-serialization).
    The candidate's integer-version component does NOT port (no live
    counterpart, excluded by triage).
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")

    def _reject_dupes(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError("duplicate JSON key: %r" % key)
            seen[key] = value
        return seen

    def _reject_nonfinite(value):
        raise ValueError("non-finite JSON number: %s" % value)

    value = json.loads(raw, object_pairs_hook=_reject_dupes,
                       parse_constant=_reject_nonfinite)
    # Overflow check: 1e999 parses to inf without parse_constant firing
    # (the parser produces the float directly). Re-serializing with
    # allow_nan=False refuses any non-finite float anywhere in the tree.
    json.dumps(value, allow_nan=False)
    return value


def _dir_fsync(dirpath):
    """fsync a directory so a just-completed rename is durable.

    A directory-fsync failure means the rename may not survive a crash;
    swallowing it would lie about durability, so it raises — the caller
    treats it as a failed write (the file itself is already fsynced and
    visible, so the failure mode is retry-able, never torn).
    """
    fd = os.open(dirpath, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_json(path):
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        data = strict_loads(f.read())
    if isinstance(data, dict) and "leads" in data:
        return data["leads"]
    return data


def atomic_write_json(path, items):
    """Crash-safe write (tmp + os.replace). Caller must hold queue_lock().

    Durability half (K38 port, Keel 0.3.1 review; adapted from the
    candidate's safe_io.atomic_bytes):
      - unique temp via tempfile.mkstemp — no fixed `<path>.tmp` name, so
        two writers in different directories (or a leftover .tmp) can never
        collide;
      - 0600 mode on the temp before any bytes are written (the queue file
        inherits it at rename);
      - json bytes flushed and os.fsync'd BEFORE os.replace, so a crash
        cannot commit torn page-cache data;
      - directory fsync AFTER the rename, so the rename itself is durable;
      - the temp is always unlinked on failure, never left behind.

    The flock mutual-exclusion half is untouched (2026-09-15/16 design);
    the measured K38 precondition is +35ms per 5.7 MiB write on this VM
    (~1.3% of the async-verify 60-leads/2.7min budget), so it is enabled
    globally rather than gated.
    """
    payload = _strict_json_bytes(items)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".queue-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _dir_fsync(parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def patch_entry(path, role_id, fields):
    """Atomically merge `fields` into the entry with `role_id`.

    Returns True when the entry was found and patched.
    """
    found = False
    with queue_lock(owner=f"patch_entry:{role_id}"):
        payload = read_snapshot(path)
        items = queue_entries([] if payload is None else payload)
        for e in items:
            if e.get("role_id") == role_id:
                e.update(fields)
                found = True
                break
        if found:
            atomic_write_json(path, _replace_entries(payload, items))
    return found


def notes_text(entry):
    """queue_notes as display text — normalizes str-vs-list shape."""
    n = (entry or {}).get("queue_notes")
    if isinstance(n, list):
        return " | ".join(str(x) for x in n if x)
    return str(n or "")


class QueueRecoveryRequired(RuntimeError):
    """A read-only snapshot was refused because a transaction needs recovery."""


class QueueTransactionConflict(RuntimeError):
    """A snapshot or recovery image conflicts with current durable content."""


def _strict_json_bytes(value):
    """Encode before any filesystem change; never stringify unknown types."""
    def validate(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise TypeError("JSON object keys must be strings")
            for child in item.values():
                validate(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child)
    validate(value)
    return json.dumps(value, indent=1, allow_nan=False).encode("utf-8")


def json_safe_copy(value):
    """Detached strict-JSON value; date/datetime become ISO 8601 strings.

    Sets and arbitrary objects are refused: their string representation is
    not evidence, and choosing an ordering would invent queue semantics.
    """
    def convert(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if isinstance(item, dict):
            return {key: convert(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(child) for child in item]
        return item
    return strict_loads(_strict_json_bytes(convert(value)))


def read_snapshot(path):
    """Read the complete JSON envelope. Missing files return None.

    Hold queue_lock() across all reads when a consistent multi-file view
    is required. Individual atomic renames do not make lock-free readers
    atomic across files; a process can die between the two renames.
    """
    try:
        with open(path, "rb") as stream:
            return strict_loads(stream.read())
    except FileNotFoundError:
        return None


def queue_entries(payload):
    """Get queue records without dropping metadata or guessing bad shapes."""
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        keys = [key for key in ("entries", "items", "leads") if key in payload]
        if len(keys) != 1 or not isinstance(payload[keys[0]], list):
            raise ValueError("queue envelope must have exactly one record list")
        entries = payload[keys[0]]
    else:
        raise ValueError("queue must be a list or an envelope with records")
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("queue records must be JSON objects")
    return entries


def _replace_entries(payload, entries):
    if isinstance(payload, list):
        return entries
    result = copy.deepcopy(payload)
    for key in ("entries", "items", "leads"):
        if key in result:
            result[key] = entries
            return result
    raise ValueError("queue envelope has no record list")


def _snapshot_digest(value):
    # Type-sensitive canonical comparison: 1, 1.0 and True are not equal.
    encoded = json.dumps(value, sort_keys=True, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _transaction_dir():
    return os.path.abspath(_LOCK_PATH) + ".transactions"


def _refuse_pending_transactions():
    directory = _transaction_dir()
    if os.path.isdir(directory) and any(name.endswith(".json")
                                       for name in os.listdir(directory)):
        raise QueueRecoveryRequired("queue transaction recovery required before a consistent read")


def read_snapshot_checked(path):
    """Read without recovery or writes; refuse unresolved transactions.

    For coherent reads across multiple files, hold queue_lock(recover=False)
    while reading every snapshot. This helper alone is a single-file read.
    """
    _refuse_pending_transactions()
    value = read_snapshot(path)
    _refuse_pending_transactions()
    return value


def _transaction_step(step, journal):
    """Fault-injection seam: production intentionally performs no action."""


def _unlink_durable(path):
    os.unlink(path)
    _dir_fsync(os.path.dirname(path))


def _validate_journal(journal):
    if (not isinstance(journal, dict) or type(journal.get("version")) is not int
            or journal["version"] != 1
            or journal.get("state") not in ("PREPARED", "COMMITTED")
            or not isinstance(journal.get("op_id"), str) or not journal["op_id"]
            or journal.get("lock_path") != os.path.abspath(_LOCK_PATH)
            or not isinstance(journal.get("changes"), list)
            or not journal["changes"]):
        raise QueueTransactionConflict("invalid queue transaction journal")
    seen = set()
    for row in journal["changes"]:
        if not isinstance(row, dict):
            raise QueueTransactionConflict("invalid queue transaction row")
        path = row.get("path")
        if (not isinstance(path, str) or not os.path.isabs(path)
                or path != os.path.realpath(path) or path in seen
                or not isinstance(row.get("before_exists"), bool)):
            raise QueueTransactionConflict("invalid queue transaction path")
        seen.add(path)
        if not row["before_exists"] and row.get("before") is not None:
            raise QueueTransactionConflict("absent queue has a nonempty before image")
        for image in ("before", "after"):
            if image not in row or row.get(image + "_digest") != _snapshot_digest(row[image]):
                raise QueueTransactionConflict("corrupt queue transaction image")


def _recover_transactions_locked():
    """Roll prepared transactions forward before releasing the queue lock.

    Never overwrite an unrecognized image. A bypass writer or a damaged
    journal requires explicit adjudication rather than a guessed repair.
    """
    directory = _transaction_dir()
    if not os.path.isdir(directory):
        return []
    recovered = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        if os.path.islink(path):
            raise QueueTransactionConflict("queue journal must not be a symlink")
        journal = read_snapshot(path)
        _validate_journal(journal)
        # Check every image before changing any file during recovery.
        pending = []
        for row in journal["changes"]:
            exists = os.path.exists(row["path"])
            current = read_snapshot(row["path"])
            digest = _snapshot_digest(current)
            is_after = exists and digest == row["after_digest"]
            is_before = (exists == row["before_exists"]
                         and digest == row["before_digest"])
            if journal["state"] == "COMMITTED" and not is_after:
                raise QueueTransactionConflict(
                    "committed queue transaction changed before cleanup: " + row["path"])
            if not is_after and not is_before:
                raise QueueTransactionConflict(
                    "queue recovery conflicts with current content: " + row["path"])
            if not is_after:
                pending.append(row)
        for row in pending:
            atomic_write_json(row["path"], row["after"])
        for row in journal["changes"]:
            if (not os.path.exists(row["path"])
                    or _snapshot_digest(read_snapshot(row["path"])) != row["after_digest"]):
                raise QueueTransactionConflict("queue recovery read-back failed")
        if journal["state"] == "PREPARED":
            journal["state"] = "COMMITTED"
            atomic_write_json(path, journal)
        _unlink_durable(path)
        recovered.append(journal["op_id"])
    return recovered


def recover_transactions():
    """Recover all prepared moves under the same lock used by queue writers."""
    with queue_lock(owner="queue_io:recover"):
        return list(getattr(_state, "recovered", [])) + _recover_transactions_locked()


def commit_snapshot(changes, expected, op_id=None):
    """Durable, recoverable multi-file compare-and-set commit.

    All changed payloads are serialized before even acquiring the lock.
    Every expected snapshot (including read-only dependencies) is checked
    as a complete envelope under the lock, before PREPARED is durable.
    After PREPARED, any crash/failure is rolled forward on the next lock
    acquisition. Success requires read-back of every after-image.

    This is transactional for cooperating lock users. Lock-free readers
    may see intermediate images and must not drive ownership decisions.
    """
    def normalize(mapping):
        result = {}
        for original, content in mapping.items():
            original_path = os.path.abspath(os.fspath(original))
            path = os.path.realpath(original_path)
            if path in result:
                raise ValueError("duplicate normalized queue path")
            if os.path.islink(original_path):
                raise ValueError("transaction queue paths must not be symlinks")
            result[path] = strict_loads(_strict_json_bytes(content))
        return result
    prepared = normalize(changes)
    snapshots = normalize(expected)
    for path in prepared:
        if path not in snapshots:
            raise QueueTransactionConflict(
                f"queue snapshot missing for {path}: every changed path must have an expected snapshot")
    if not prepared:
        return None
    if op_id is None:
        op_id = "queue-" + uuid.uuid4().hex
    if not isinstance(op_id, str) or not op_id:
        raise ValueError("queue transaction op_id must be a nonempty string")
    with queue_lock(owner="queue_io:" + str(op_id)):
        # A previous failed transaction may have been prepared in this
        # same reentrant outer lock. Recover it before accepting new work.
        _recover_transactions_locked()
        current = {}
        for path, snapshot in snapshots.items():
            value = read_snapshot(path)
            if _snapshot_digest(value) != _snapshot_digest(snapshot):
                raise QueueTransactionConflict(
                    f"stale queue snapshot for {path}: refusing all changes")
            current[path] = value
        rows = [{"path": path,
                 "before_exists": os.path.exists(path),
                 "before": current[path], "after": content,
                 "before_digest": _snapshot_digest(current[path]),
                 "after_digest": _snapshot_digest(content)}
                for path, content in prepared.items()]
        journal = {"version": 1, "state": "PREPARED", "op_id": op_id,
                   "lock_path": os.path.abspath(_LOCK_PATH), "changes": rows}
        # Pre-serialize the whole journal too, before any directory/write.
        _strict_json_bytes(journal)
        directory = _transaction_dir()
        if not os.path.exists(directory):
            os.makedirs(directory, mode=0o700)
            _dir_fsync(os.path.dirname(directory))
        journal_path = os.path.join(directory, uuid.uuid4().hex + ".json")
        atomic_write_json(journal_path, journal)
        _transaction_step("prepared", journal)
        for index, row in enumerate(rows):
            atomic_write_json(row["path"], row["after"])
            _transaction_step("write:" + str(index), journal)
        for row in rows:
            if (not os.path.exists(row["path"])
                    or _snapshot_digest(read_snapshot(row["path"])) != row["after_digest"]):
                raise QueueTransactionConflict("queue transaction read-back failed")
        _transaction_step("verified", journal)
        journal["state"] = "COMMITTED"
        atomic_write_json(journal_path, journal)
        _transaction_step("committed", journal)
        _unlink_durable(journal_path)
        _transaction_step("cleaned", journal)
    return {"op_id": op_id, "status": "COMMITTED", "paths": list(prepared)}


def move_entry_atomic(role_id, source_path, destination_path, mutate=None,
                      op_id=None, expected=None):
    """Move exactly one row, preserving both queues' complete envelopes.

    Mutation and strict serialization happen before acquiring the lock;
    full source/destination snapshots then act as the CAS. `expected` may
    include extra queues consulted when determining the sole owner.
    """
    if os.path.islink(source_path) or os.path.islink(destination_path):
        raise ValueError("transaction queue paths must not be symlinks")
    source_path = os.path.realpath(os.fspath(source_path))
    destination_path = os.path.realpath(os.fspath(destination_path))
    if source_path == destination_path:
        raise ValueError("source and destination must differ")
    source = read_snapshot(source_path)
    destination = read_snapshot(destination_path)
    source_rows = queue_entries(source)
    destination_rows = queue_entries([] if destination is None else destination)
    matches = [row for row in source_rows if row.get("role_id") == role_id]
    if len(matches) != 1:
        raise QueueTransactionConflict("source must contain exactly one role: " + str(role_id))
    if any(row.get("role_id") == role_id for row in destination_rows):
        raise QueueTransactionConflict("role already exists in destination: " + str(role_id))
    candidate = copy.deepcopy(matches[0])
    if mutate is not None:
        changed = mutate(candidate)
        if changed is not None:
            candidate = changed
    candidate = json_safe_copy(candidate)
    if not isinstance(candidate, dict) or candidate.get("role_id") != role_id:
        raise ValueError("move mutation must preserve role identity")
    snapshots = {os.path.realpath(os.fspath(path)): value
                 for path, value in (expected or {}).items()}
    # Caller-supplied snapshots must match the snapshots we used, not be
    # silently replaced (that would defeat an owner-discovery CAS).
    for path, value in ((source_path, source), (destination_path, destination)):
        supplied = snapshots.get(path, value)
        if _snapshot_digest(supplied) != _snapshot_digest(value):
            raise QueueTransactionConflict("move snapshot changed before preparation: " + path)
        snapshots[path] = value
    changes = {source_path: _replace_entries(source, [row for row in source_rows
                                                     if row.get("role_id") != role_id]),
               destination_path: _replace_entries([] if destination is None else destination,
                                                   destination_rows + [candidate])}
    result = commit_snapshot(changes, snapshots, op_id=op_id)
    result["record"] = candidate
    return result


if __name__ == "__main__":
    # CLI for ad-hoc lane scripts:
    #   python3 queue_io.py patch <queue_path> <role_id> '<json fields>'
    if len(sys.argv) == 5 and sys.argv[1] == "patch":
        _path, _rid = sys.argv[2], sys.argv[3]
        _fields = json.loads(sys.argv[4])
        ok = patch_entry(_path, _rid, _fields)
        print("patched" if ok else "role_id not found")
        sys.exit(0 if ok else 1)
    print(__doc__)
    sys.exit(2)
