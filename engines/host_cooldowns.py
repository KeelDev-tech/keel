"""Durable per-host HTTP 429 cooldowns, shared across cooperating processes.

Ported concept from the Keel 0.3.1 review (K43/K44 shared admission),
adapted to the live async verify transport (aiohttp, not the candidate's
urllib/threading stack):

- A 429 observed by ANY worker writes a durable cooldown file for that host.
- Admission checks the file BEFORE issuing a request: a later process (next
  hourly cron run, a concurrent sweep) never burns requests into a host that
  another worker just saw throttled. This makes the standing "429 = hard
  stop" rule durable across process boundaries instead of per-process.
- Retry-After is parsed strictly (integer seconds or HTTP-date, floor 60s);
  unrepresentable delays become an indefinite hold (until=None) requiring
  operator reconciliation via clear_host().
- Corrupt / wrong-host / wrong-schema state denies admission (fail-closed).
- deadline_remaining(): semaphore/queue waits consume the caller's monotonic
  budget instead of hanging outside it (K43).

State: <hidden_files>/http-cooldowns/<sha256(host)>.json
Locking: POSIX advisory flock on <file>.lock -- cooperating processes only,
same scope as the candidate. Not a distributed fleet limiter.

Stdlib only. No imports from the live engines (no import cycles).
"""

import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
MIN_DELAY_S = 60
STATE_SUBDIR = "http-cooldowns"


class HostCoolingDown(Exception):
    """Raised when a host has an active 429 cooldown. Carries `host` and
    `until` (epoch seconds, or None for an indefinite hold). Callers should
    treat this as 'skip/defer', never as an error or a verdict."""


class CooldownStateError(Exception):
    """Raised when a cooldown file exists but is corrupt, wrong-host, or
    wrong-schema. Admission is denied (fail-closed)."""


def _base_dir():
    override = os.environ.get("JOB_PIPELINE_HTTP_COOLDOWN_DIR")
    if override:
        return override
    from keel_paths import HOME  # noqa: E402 — repo path convention
    return os.path.join(HOME, "hidden_files", STATE_SUBDIR)


def cooldown_dir():
    return _base_dir()


def normalize_host(host):
    """Lowercase DNS host, no port, no trailing dot. Accepts a bare host or
    a netloc with port."""
    h = (host or "").strip().lower().rstrip(".")
    try:
        parsed = urlsplit("//" + h)
        if parsed.hostname:
            return parsed.hostname
    except ValueError:
        pass
    return h


def _cooldown_path(host):
    name = hashlib.sha256(normalize_host(host).encode("ascii")).hexdigest()
    return os.path.join(_base_dir(), name + ".json")


@contextmanager
def _locked(lock_path, timeout=5.0):
    """POSIX advisory exclusive lock with a bounded wait. Raises TimeoutError
    when the wait exceeds `timeout` (so lock waits consume the caller's
    deadline instead of hanging)."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("cooldown lock wait exceeded deadline")
                time.sleep(0.01)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# In-process backoff: a 429 recorded in THIS process blocks immediately,
# without waiting for a disk round-trip on the next admission check.
_backoff = {}
_backoff_lock = threading.Lock()


def parse_retry_after(raw):
    """Strict Retry-After parse. Integer seconds or HTTP-date -> float seconds
    from now (floored at MIN_DELAY_S). Unparseable -> MIN_DELAY_S.
    Unrepresentably large -> None (indefinite hold)."""
    text = str(raw).strip() if raw is not None else ""
    if re.fullmatch(r"[0-9]+", text):
        try:
            value = float(text)
        except ValueError:
            return float(MIN_DELAY_S)
        if not math.isfinite(value):
            return None
        return max(float(MIN_DELAY_S), value)
    try:
        leap = bool(re.search(r"\d{2}:\d{2}:60(?: |$)", text))
        date_text = (re.sub(r"(\d{2}:\d{2}):60(?= |$)", r"\1:59", text)
                     if leap else text)
        date = parsedate_to_datetime(date_text)
        if date is None:
            return float(MIN_DELAY_S)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)  # obsolete asctime form
        now = datetime.now(timezone.utc)
        short_year = re.fullmatch(
            r"[A-Za-z]+, \d{2}-[A-Za-z]{3}-(\d{2}) \d{2}:\d{2}:\d{2} GMT", text)
        if short_year:
            year = now.year // 100 * 100 + int(short_year.group(1))
            date = date.replace(year=year)
            if (date.year, date.month, date.day,
                    date.hour, date.minute, date.second) > (
                    now.year + 50, now.month, now.day,
                    now.hour, now.minute, now.second):
                date = date.replace(year=year - 100)
        if leap:
            from datetime import timedelta
            date += timedelta(seconds=1)
        delay = (date - now).total_seconds()
        if not math.isfinite(delay):
            return None
        return max(float(MIN_DELAY_S), delay)
    except (ValueError, TypeError, OverflowError):
        return float(MIN_DELAY_S)


def _read_state_locked(path, host):
    """Read + validate under the caller's lock. Returns the state dict, or
    None when no file exists. Raises CooldownStateError on corrupt state."""
    try:
        with open(path, "r") as f:
            state = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise CooldownStateError(f"unreadable cooldown state for {host}: {exc}")
    if (not isinstance(state, dict)
            or type(state.get("schema_version")) is not int
            or state.get("schema_version") != SCHEMA_VERSION
            or state.get("host") != normalize_host(host)
            or "until" not in state):
        raise CooldownStateError(f"invalid cooldown state for {host}")
    until = state["until"]
    if until is not None and (
            type(until) not in (int, float)
            or not math.isfinite(until) or until < 0):
        raise CooldownStateError(f"invalid cooldown expiry for {host}")
    return state


# Attach host/until to the exception without changing its constructor shape.
def _cooling_down(host, until):
    exc = HostCoolingDown(
        f"host cooling down after HTTP 429"
        + ("" if until is None else f" until {until:.0f}")
        + f": {host}")
    exc.host = host
    exc.until = until
    return exc


def check_host(host):
    """Admission check. Returns None when the host is clear.

    Raises HostCoolingDown when an active cooldown exists (in-process or
    durable). Raises CooldownStateError when durable state is corrupt
    (fail-closed: deny admission rather than risk burning a throttled host).
    """
    host = normalize_host(host)
    with _backoff_lock:
        held_until = _backoff.get(host)
        if held_until is not None:
            now_m = time.monotonic()
            if held_until is None or held_until > now_m:
                raise _cooling_down(host, None if held_until is None
                                    else time.time() + (held_until - now_m))
            _backoff.pop(host, None)
    path = _cooldown_path(host)
    with _locked(path + ".lock", timeout=5.0):
        state = _read_state_locked(path, host)
    if state is None:
        return None
    until = state["until"]
    if until is None or until > time.time():
        raise _cooling_down(host, until)
    return None


def record_429(host, retry_after=None):
    """Record an observed HTTP 429. Sets the in-process hold first (so this
    process stops immediately even if the disk write fails), then persists
    the durable cooldown. Returns the absolute `until` (epoch, or None for
    indefinite). Raises OSError if the durable write fails -- the in-process
    hold is retained, matching the candidate's failed-write semantics."""
    host = normalize_host(host)
    delay = parse_retry_after("60" if retry_after is None else retry_after)
    now_m = time.monotonic()
    hold_until_m = None if delay is None else now_m + delay
    with _backoff_lock:
        _backoff[host] = hold_until_m
    until = None if delay is None else time.time() + delay
    path = _cooldown_path(host)
    state = {"schema_version": SCHEMA_VERSION, "host": host, "until": until,
             "recorded_at": time.time()}
    with _locked(path + ".lock", timeout=5.0):
        _atomic_write_json(path, state)
    return until


def clear_host(host):
    """Operator reconciliation escape hatch (e.g. indefinite holds after the
    provider recovered). Removes the durable file and the in-process hold."""
    host = normalize_host(host)
    with _backoff_lock:
        _backoff.pop(host, None)
    path = _cooldown_path(host)
    with _locked(path + ".lock", timeout=5.0):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    return True


def status(host):
    """Introspection for telemetry. Returns a dict or None when no durable
    state exists. Never raises on missing files."""
    host = normalize_host(host)
    path = _cooldown_path(host)
    try:
        with _locked(path + ".lock", timeout=2.0):
            state = _read_state_locked(path, host)
    except (CooldownStateError, TimeoutError, OSError):
        return {"host": host, "state": "unreadable"}
    if state is None:
        return None
    until = state["until"]
    now = time.time()
    return {"host": host,
            "state": ("indefinite" if until is None
                      else ("active" if until > now else "expired")),
            "until": until}


def deadline_remaining(deadline):
    """Return seconds left on a monotonic deadline; raise TimeoutError when
    exhausted. Use to bound semaphore/lock/DNS waits inside the caller's
    total budget (K43)."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("deadline exceeded")
    return remaining
