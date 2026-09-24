"""Bounded canonical JSON and exclusive local output helpers.

These checks are input hygiene, not authentication or an OS sandbox.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

MAX_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 64


class LokiError(ValueError):
    pass


def _check(value, depth=0):
    if depth > MAX_DEPTH:
        raise LokiError('JSON nesting limit exceeded')
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise LokiError('finite JSON numbers required')
        return
    if type(value) is list:
        for item in value:
            _check(item, depth + 1)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _check(item, depth + 1)
        return
    raise LokiError('plain JSON values with string keys required')


def canonical(value):
    _check(value)
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise LokiError('JSON size limit exceeded')
    return raw


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def clone(value):
    return json.loads(canonical(value))


def require_dict(value, keys, where='object'):
    if type(value) is not dict or set(value) != set(keys):
        raise LokiError(where + ': exact object keys required')
    return value


def require_id(value, where='identifier'):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise LokiError(where + ': canonical identifier required')
    return value


def require_hash(value, where='digest'):
    if type(value) is not str or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise LokiError(where + ': SHA-256 required')
    return value


def require_int(value, low=0, high=2**63 - 1, where='integer'):
    if type(value) is not int or not low <= value <= high:
        raise LokiError(where + ': bounded integer required')
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise LokiError('duplicate JSON key')
        result[key] = value
    return result


def decode_json(raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_BYTES:
        raise LokiError('bounded JSON input required')
    def bad(_):
        raise LokiError('nonfinite JSON number')
    try:
        result = json.loads(raw, object_pairs_hook=_pairs, parse_constant=bad)
        _check(result)
        canonical(result)
        return result
    except (RecursionError, UnicodeError, json.JSONDecodeError) as exc:
        raise LokiError('invalid bounded JSON') from exc


def load_json(path):
    from tools.bench_inventory import read_file
    path = Path(path).absolute()
    return decode_json(read_file(path.parent, path.name))


def atomic_json(path, value):
    """Publish a complete new JSON file; refuse every existing output.

    A descriptor anchors the parent; hard-link publication is exclusive. This
    routine never replaces an existing file or follows a caller output symlink.
    """
    from tools.bench_inventory import directory_fd, safe_name
    import secrets
    path = Path(path).absolute()
    safe_name(path.name)
    raw = canonical(value) + b'\n'
    if len(raw) > MAX_BYTES:
        raise LokiError('JSON output size limit exceeded')
    parent = directory_fd(path.parent)
    temporary = '.keel-loki-' + secrets.token_hex(16)
    temporary_path = Path('/proc/self/fd') / str(parent) / temporary
    identity = None
    try:
        fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        identity = os.fstat(fd)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            identity = os.fstat(stream.fileno())
            # Publish the held inode, not a temporary pathname another local
            # process could replace between writing and linking.
            # An absolute descriptor path lets the unchanged audit guard
            # resolve the destination. Supplying dir_fd selects linkat on
            # Linux so follow_symlinks applies to the source descriptor.
            os.link('/proc/self/fd/' + str(stream.fileno()),
                    Path('/proc/self/fd') / str(parent) / path.name,
                    dst_dir_fd=parent, follow_symlinks=True)
            os.fsync(parent)
    finally:
        try:
            current = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
            if identity is not None and (current.st_dev,current.st_ino) == (identity.st_dev,identity.st_ino):
                temporary_path.unlink()
        except FileNotFoundError:
            pass
        os.close(parent)
    return str(path)
