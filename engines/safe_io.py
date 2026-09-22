"""Local POSIX storage primitives. These are not a sandbox for untrusted code."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

MAX_JSON_BYTES = 16 * 1024 * 1024
ROW_KEYS = ("entries", "items", "leads", "rows", "applications")
_state = threading.local()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def loads(raw):
    def invalid(value):
        raise ValueError("non-finite JSON number: " + value)
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid)
    canonical(value)  # also rejects floating-point overflow such as 1e999
    return value


def read_json(path, *, missing=None, limit=MAX_JSON_BYTES):
    try:
        with open(path, "rb") as stream:
            raw = stream.read(limit + 1)
    except FileNotFoundError:
        return missing
    if len(raw) > limit:
        raise ValueError("JSON file exceeds size limit")
    return loads(raw)


def rows(value):
    if isinstance(value, list):
        result = value
    elif isinstance(value, dict):
        keys = [key for key in ROW_KEYS if key in value]
        if len(keys) != 1:
            raise ValueError("expected exactly one supported row container")
        result = value[keys[0]]
    else:
        raise ValueError("expected a row list or supported object container")
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise ValueError("rows must be a list of objects")
    return result


def with_rows(container, entries):
    rows(entries)
    if isinstance(container, list):
        return entries
    rows(container)
    return {**container, next(key for key in ROW_KEYS if key in container): entries}


def utc_now():
    return datetime.now(timezone.utc)


def aware_time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO 8601 string with offset")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamp requires an explicit timezone")
    return stamp.astimezone(timezone.utc)


def fresh(value, seconds, *, now=None):
    try:
        age = ((now or utc_now()) - aware_time(value)).total_seconds()
        return math.isfinite(seconds) and 0 <= age <= seconds
    except (TypeError, ValueError, OverflowError):
        return False


def contained_path(root, value, *, must_exist=True):
    root = Path(root).resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=must_exist)
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("path must be inside the workspace")
    if must_exist and not resolved.is_file():
        raise ValueError("path must identify a regular file")
    return resolved


def file_digest(path, *, limit=20 * 1024 * 1024):
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            size += len(chunk)
            if size > limit:
                raise ValueError("file exceeds size limit")
            h.update(chunk)
    if size == 0:
        raise ValueError("empty file")
    return {"sha256": h.hexdigest(), "bytes": size}


def atomic_bytes(path, data, *, mode=0o600):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".keel-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_json(path, value):
    atomic_bytes(path, canonical(value) + b"\n")


@contextmanager
def file_lock(path, timeout=10):
    """Advisory lock; all cooperating writers must use the same path.

    Locks never unlink their inode. Reentrancy is scoped to thread + path.
    Timeout uses monotonic time; kernel releases locks on process death.
    """
    path = str(Path(path).absolute())
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("invalid lock timeout")
    held = getattr(_state, "held", None)
    if held is None:
        _state.held = held = {}
    if path in held:
        yield
        return
    Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    start = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - start >= timeout:
                    raise TimeoutError("storage lock timeout")
                time.sleep(min(.01, timeout))
        held[path] = fd
        try:
            yield
        finally:
            del held[path]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def append_jsonl(path, record):
    raw = canonical(record) + b"\n"
    if len(raw) > 256 * 1024:
        raise ValueError("event exceeds 256 KiB")
    with file_lock(str(path) + ".lock"):
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
