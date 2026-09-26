"""Bounded local inputs and private outputs for the self-hosted CLI."""
import json
import os
from pathlib import Path
import stat

from keel_flow.common import strict_json

MAX_JSON_BYTES = 8 * 1024 * 1024


def read_json(path):
    path = Path(path).absolute()
    if path.resolve(strict=True) != path:
        raise ValueError("symlink input paths are not accepted")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_JSON_BYTES:
            raise ValueError("bounded regular JSON file required")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            body = stream.read(MAX_JSON_BYTES + 1)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("input changed during read")
    finally:
        os.close(descriptor)
    return strict_json(body)


def write_private(path, value):
    body = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)


def private_home(path):
    path = Path(path).absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.resolve(strict=True) != path or not path.is_dir():
        raise ValueError("private regular home directory required")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("agent home must have private0700 permissions")
    return path
