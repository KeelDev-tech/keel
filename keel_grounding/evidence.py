"""Resolve exact local evidence bytes; file integrity does not establish truth.

Only UTF-8 text, UTF-8 JSON and opaque binary identities are supported. Document
contents are data, including any apparent instructions. Every source result is
a bounded snapshot, not a promise that a mutable host file stays unchanged.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat

from keel_flow.common import ContractError, canonical, strict_json


MAX_FILE_BYTES = 2 * 1024 * 1024
HARD_MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SELECTORS = 100
MAX_JSON_NODES = 100_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
_MEDIA_TYPES = {"application/json", "text/plain", "application/octet-stream"}


class GroundingError(ValueError):
    """A safe reason code, never a file path, document excerpt or OS error."""

    def __init__(self, code):
        self.code = code if type(code) is str and _CODE.fullmatch(code) else "invalid_grounding_input"
        super().__init__(self.code)


def _require(condition, code):
    if not condition:
        raise GroundingError(code)


def _bounded_string(value, *, maximum=4096):
    if type(value) is not str or not 0 < len(value) <= maximum or not value.strip():
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _sha(value):
    _require(type(value) is str and _SHA256.fullmatch(value) is not None, "invalid_content_sha256")
    return value


def _relative_parts(value):
    _require(_bounded_string(value) and "\\" not in value and not value.startswith("/"),
             "unsafe_relative_path")
    parts = value.split("/")
    _require(all(part not in {"", ".", ".."} for part in parts), "unsafe_relative_path")
    _require(len(parts) <= 128, "unsafe_relative_path")
    return parts


def _root_parts(root):
    try:
        raw = os.fspath(root)
    except TypeError:
        raise GroundingError("unsafe_root_path") from None
    _require(_bounded_string(raw) and "\\" not in raw and ".." not in raw.split("/"),
             "unsafe_root_path")
    absolute = os.path.abspath(raw)
    parts = [part for part in absolute.split("/") if part]
    _require(len(parts) <= 128, "unsafe_root_path")
    return parts


def _file_signature(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class _ReadBudget:
    """One verification's cumulative I/O budget, including failed sources."""
    def __init__(self, remaining):
        _require(type(remaining) is int and remaining >= 0, "invalid_file_limit")
        self.remaining = remaining

    def consume(self, size):
        _require(size <= self.remaining, "total_evidence_byte_limit")
        self.remaining -= size


def read_verified_file(root, relative_path, expected_sha256, *, max_bytes=MAX_FILE_BYTES,
                       _budget=None):
    """Return a regular single-link file whose exact bytes match the given hash.

    Open every directory from the filesystem root using descriptor-relative,
    no-follow operations. Check every held path edge after reading, rejecting
    replacement or rename observed during the read. Linux/POSIX secure file
    primitives are required; unsupported hosts fail closed.
    """
    _sha(expected_sha256)
    _require(type(max_bytes) is int and 1 <= max_bytes <= HARD_MAX_FILE_BYTES,
             "invalid_file_limit")
    parts = _relative_parts(relative_path)
    root_parts = _root_parts(root)
    _require(all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
             and os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
             and os.stat in os.supports_follow_symlinks, "secure_file_io_unavailable")
    descriptors = []
    edges = []
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        parent = os.open("/", directory_flags)
        descriptors.append(parent)
        for component in root_parts + parts[:-1]:
            child = os.open(component, directory_flags, dir_fd=parent)
            descriptors.append(child)
            edges.append((parent, component, child))
            parent = child
        descriptor = os.open(parts[-1], file_flags, dir_fd=parent)
        descriptors.append(descriptor)
        edges.append((parent, parts[-1], descriptor))
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "unsafe_evidence_file")
        _require(0 <= before.st_size <= max_bytes, "evidence_file_too_large")
        if _budget is not None:
            _require(before.st_size <= _budget.remaining, "total_evidence_byte_limit")
        chunks = []
        remaining = min(max_bytes + 1, _budget.remaining) if _budget is not None else max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            if _budget is not None:
                _budget.consume(len(chunk))
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        _require(_file_signature(before) == _file_signature(after) and len(raw) == before.st_size,
                 "evidence_changed_during_read")
        _require(len(raw) <= max_bytes, "evidence_file_too_large")
        for parent, component, child in edges:
            linked = os.stat(component, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(child)
            _require((linked.st_dev, linked.st_ino, linked.st_mode) ==
                     (held.st_dev, held.st_ino, held.st_mode), "evidence_path_changed")
        _require(_file_signature(after) == _file_signature(os.fstat(descriptor)),
                 "evidence_changed_during_read")
        _require(hashlib.sha256(raw).hexdigest() == expected_sha256, "content_hash_mismatch")
        return raw
    except OSError:
        raise GroundingError("evidence_path_unavailable") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _json(raw):
    try:
        value = strict_json(raw.decode("utf-8", errors="strict"))
    except (ContractError, UnicodeError, RecursionError):
        raise GroundingError("invalid_source_json") from None
    stack = [value]
    nodes = 0
    while stack:
        current = stack.pop()
        nodes += 1
        _require(nodes <= MAX_JSON_NODES, "source_json_limit_exceeded")
        if type(current) is dict:
            stack.extend(current.values())
        elif type(current) is list:
            stack.extend(current)
    return value


def _pointer(value, pointer):
    _require(type(pointer) is str and len(pointer) <= 4096, "invalid_json_pointer")
    if pointer == "":
        return value
    _require(pointer.startswith("/"), "invalid_json_pointer")
    parts = pointer[1:].split("/")
    _require(len(parts) <= 40, "invalid_json_pointer")
    for encoded in parts:
        _require(re.search(r"~(?:[^01]|$)", encoded) is None, "invalid_json_pointer")
        token = encoded.replace("~1", "/").replace("~0", "~")
        if type(value) is dict:
            _require(token in value, "selector_not_found")
            value = value[token]
        elif type(value) is list:
            _require(re.fullmatch(r"0|[1-9][0-9]{0,8}", token) is not None,
                     "invalid_array_pointer")
            index = int(token)
            _require(index < len(value), "selector_not_found")
            value = value[index]
        else:
            raise GroundingError("selector_not_found")
    return value


def resolve_source(root, source, binding, *, _budget=None):
    """Resolve caller-declared source revision and selectors against actual bytes.

    The caller must independently check the source's status, scope, freshness,
    provenance and review requirements. This function establishes neither
    source authenticity nor independent factual truth.
    """
    _require(type(source) is dict and type(binding) is dict, "invalid_source_binding")
    _require(set(binding) == {"source_id", "revision", "path", "media_type", "selectors"},
             "invalid_source_binding")
    for key in ("source_id", "revision"):
        _require(_bounded_string(source.get(key)) and _bounded_string(binding.get(key))
                 and source[key] == binding[key], "source_revision_mismatch")
    expected_sha256 = _sha(source.get("content_hash"))
    _relative_parts(binding["path"])
    media_type = binding["media_type"]
    _require(type(media_type) is str and media_type in _MEDIA_TYPES, "unsupported_source_media_type")
    selectors = binding["selectors"]
    _require(type(selectors) is list and len(selectors) <= MAX_SELECTORS, "invalid_selectors")
    if media_type == "application/octet-stream":
        _require(not selectors, "binary_selectors_forbidden")
    else:
        _require(bool(selectors), "selectors_required")
    seen = set()
    for selector in selectors:
        _require(type(selector) is dict and _bounded_string(selector.get("selector_id"), maximum=256),
                 "invalid_selector")
        selector_id = selector["selector_id"]
        _require(selector_id not in seen, "duplicate_selector_id")
        seen.add(selector_id)
        if media_type == "application/json":
            _require(set(selector) == {"selector_id", "kind", "pointer"}
                     and selector["kind"] == "json_pointer", "invalid_selector")
        else:
            _require(set(selector) == {"selector_id", "kind", "start", "end"}
                     and selector["kind"] == "utf8_bytes", "invalid_selector")
    raw = read_verified_file(root, binding["path"], expected_sha256, _budget=_budget)
    selected = {}
    total_selected_bytes = 0

    def add_selection(selector_id, value):
        nonlocal total_selected_bytes
        total_selected_bytes += len(canonical(value))
        _require(total_selected_bytes <= MAX_FILE_BYTES, "selected_content_limit_exceeded")
        selected[selector_id] = value

    if media_type == "application/json":
        document = _json(raw)
        for selector in selectors:
            add_selection(selector["selector_id"], _pointer(document, selector["pointer"]))
    elif media_type == "text/plain":
        try:
            raw.decode("utf-8", errors="strict")
        except UnicodeError:
            raise GroundingError("invalid_source_utf8") from None
        for selector in selectors:
            start, end = selector["start"], selector["end"]
            _require(type(start) is int and type(end) is int and 0 <= start < end <= len(raw),
                     "invalid_text_range")
            try:
                value = raw[start:end].decode("utf-8", errors="strict")
            except UnicodeError:
                raise GroundingError("invalid_utf8_boundary") from None
            add_selection(selector["selector_id"], value)
    return {"source_id": source["source_id"], "revision": source["revision"],
            "content_sha256": expected_sha256, "media_type": media_type,
            "selectors": selected, "bytes": len(raw)}
