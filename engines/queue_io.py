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

A writer that bypasses this helper can still clobber; see AGENTS.md lesson.
"""

import contextlib
import fcntl
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
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
def queue_lock(timeout=None, owner=None):
    """Exclusive, reentrant-in-process lock for queue file writes.

    timeout: waiter bound in seconds (default 120). On expiry raises
        QueueLockTimeout with the holder's PID/owner — fail closed, never
        an unbounded hang. The reentrant fast path never blocks.
    owner: short diagnostic label for the holder (e.g. "verify_retry:apply",
        "apply_loop:claim"). Recorded in the lock meta + waits log.
    """
    if timeout is None:
        timeout = _LOCK_TIMEOUT_S
    depth = getattr(_state, "depth", 0)
    if depth:
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
                if _meta_is_stale(meta):
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
        _write_meta(owner)
        _log_wait(wait_ms, owner, timeout)
        _state.depth = 1
        try:
            yield
        finally:
            _state.depth = 0
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
    payload = json.dumps(items, indent=1).encode("utf-8")
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
        items = load_json(path)
        for e in items:
            if e.get("role_id") == role_id:
                e.update(fields)
                found = True
                break
        if found:
            atomic_write_json(path, items)
    return found


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
def notes_text(entry):
    """queue_notes as display text — normalizes str-vs-list shape."""
    n = (entry or {}).get("queue_notes")
    if isinstance(n, list):
        return " | ".join(str(x) for x in n if x)
    return str(n or "")


def commit_snapshot(changes, expected):
    """Atomic compare-and-refuse multi-file commit.

    changes: {path: new_content} — the writes to apply.
    expected: {path: expected_current_content} — a snapshot taken before
      the caller computed `changes`.

    Every path in `changes` is re-read under queue_lock() and compared
    to its expected snapshot BEFORE any write happens. Every changed
    path MUST have an entry in `expected` — a change without a prior
    snapshot is a programming error and fails closed. If any path's
    current content differs (stale snapshot — someone else wrote first),
    RuntimeError is raised and NOTHING is written. Only when every path
    matches does the commit proceed, writing all files via
    atomic_write_json. No partial commits, ever.
    """
    with queue_lock(owner="queue_io:commit_snapshot"):
        current = {}
        for path in changes:
            if path not in expected:
                raise RuntimeError(
                    f"queue snapshot missing for {path}: every changed "
                    "path must have an expected snapshot; refusing all changes")
            try:
                with open(path, "rb") as f:
                    current[path] = strict_loads(f.read())
            except FileNotFoundError:
                current[path] = None
            if current[path] != expected[path]:
                raise RuntimeError(
                    f"stale queue snapshot for {path}: expected snapshot "
                    "does not match current content; refusing all changes")
        for path, content in changes.items():
            atomic_write_json(path, content)
