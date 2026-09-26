"""Explicit 0.10 source snapshot, preserving every 0.9 reference member."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 8 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")
BASELINE = "grounding-base-manifest.json"
ALLOWLIST = "grounding-release-files.json"


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    def constant(value):
        raise ValueError("nonfinite JSON number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def safe_name(name):
    if not isinstance(name, str):
        raise ValueError("canonical relative file name required")
    part = PurePosixPath(name)
    if (str(part) != name or part.is_absolute() or not part.parts or '..' in part.parts
            or '\\' in name or any(ord(c) < 32 for c in name)
            or any(p in {'.git', '__pycache__', '.pytest_cache', 'credentials', 'data', 'dist'} for p in part.parts)):
        raise ValueError("canonical relative file name required")
    return part


def directory_fd(root):
    """Open each absolute directory component without following symlinks."""
    root = Path(root).absolute()
    if root.resolve(strict=True) != root:
        raise ValueError("canonical existing directories required")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root.anchor, flags)
    try:
        for name in root.parts[1:]:
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_file(root, name):
    """Read one bounded regular file, anchored to directory descriptors."""
    parts = safe_name(name).parts
    parent = directory_fd(root)
    fd = None
    try:
        for name in parts[:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE_BYTES:
            raise ValueError("bounded singly linked regular file required")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise ValueError("release file too large")
        after = os.fstat(fd)
        stable = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
        if stable(before) != stable(after) or total != after.st_size:
            raise ValueError("file changed during read")
        return b''.join(chunks)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def hash_map(value):
    if type(value) is not dict or not value:
        raise ValueError("nonempty file hash map required")
    for name, digest in value.items():
        safe_name(name)
        if not isinstance(digest, str) or not HEX.fullmatch(digest):
            raise ValueError("SHA-256 digest required")
    return value


def baseline_manifest(raw):
    value = strict_json(raw)
    if (type(value) is not dict or set(value) != {'schema', 'files', 'count'}
            or value['schema'] != 'keel.grounding.baseline.v1'):
        raise ValueError("invalid grounding baseline")
    hash_map(value['files'])
    if type(value['count']) is not int or value['count'] != len(value['files']):
        raise ValueError("baseline file count mismatch")
    return value


def payload(root=ROOT, *, for_inventory=False):
    root = Path(root).absolute()
    baseline_raw = read_file(root, BASELINE)
    allowlist_raw = read_file(root, ALLOWLIST)
    baseline = dict(baseline_manifest(baseline_raw)['files'])
    extensions = strict_json(allowlist_raw)
    if (type(extensions) is not list or not all(isinstance(n, str) for n in extensions)
            or len(extensions) != len(set(extensions))):
        raise ValueError("unique explicit release file list required")
    for name in extensions:
        safe_name(name)
    if set(extensions) & set(baseline):
        raise ValueError("grounding must not replace baseline files")
    if not {BASELINE, ALLOWLIST}.issubset(extensions):
        raise ValueError("release must bind both manifest files")
    if any(n.startswith('audit/grounding/') or n == 'keel_grounding/PATCH_MANIFEST.json' for n in extensions):
        raise ValueError("generated evidence cannot be a source input")
    # Historical 0.7 ZIP metadata is preserved in the full reference payload,
    # but neither required nor installed on a host restored from TXT.
    if for_inventory:
        baseline.pop('MANIFEST.json', None)
    captured = {BASELINE: baseline_raw, ALLOWLIST: allowlist_raw}
    result = {}
    for name in sorted(set(baseline) | set(extensions)):
        body = captured[name] if name in captured else read_file(root, name)
        if name in baseline and hashlib.sha256(body).hexdigest() != baseline[name]:
            raise ValueError("preserved baseline changed: " + name)
        result[name] = body
    return result


def inventory_from_payload(files):
    """Bind already-read bytes; never race a separate source read."""
    records = {name: hashlib.sha256(body).hexdigest() for name, body in files.items() if name != 'MANIFEST.json'}
    raw = json.dumps(records, sort_keys=True, separators=(',', ':')).encode()
    return {'schema': 'keel.grounding.inventory.v1', 'sha256': hashlib.sha256(raw).hexdigest(),
            'files': records, 'file_count': len(records)}


def inventory(root=ROOT):
    return inventory_from_payload(payload(root, for_inventory=True))
