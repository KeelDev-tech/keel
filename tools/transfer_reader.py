"""Standalone extractor template. The release builder embeds configuration and payload.

Integrity hashes detect accidental corruption; they do not authenticate the sender.
Only use a transfer received from a trusted source, after inspecting this code.
"""
import argparse
import base64
import hashlib
import io
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import sys
import stat
import unicodedata

SOURCE_BYTES = 0
SOURCE_SHA256 = ""
SOURCE_FILES = 0
RELEASE_VERSION = "UNBUILT"
PAYLOAD_B64 = ""


MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 100 * 1024 * 1024
MAX_FILES = 1000
MAGIC = b"KEEL_TEXT_BUNDLE_V1\n"


def strict_json(raw):
    """Reject duplicate keys and non-standard constants at every object depth."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def constant(value):
        raise ValueError("invalid JSON constant: " + value)

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def validate_paths(names):
    """Require canonical, portable file paths and unambiguous directory topology."""
    folded = set()
    directory_spelling = {}
    reserved = {"CON", "PRN", "AUX", "NUL", *("COM" + str(n) for n in range(1, 10)),
                *("LPT" + str(n) for n in range(1, 10))}
    for name in names:
        if type(name) is not str:
            raise ValueError("invalid source path")
        path = PurePosixPath(name)
        if (not path.parts or path.is_absolute() or ".." in path.parts
                or any(c in name for c in '\\:<>"|?*') or str(path) != name
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or unicodedata.normalize("NFC", name) != name
                or any(part.endswith((".", " ")) or part.split(".")[0].upper() in reserved
                       for part in path.parts)):
            raise ValueError("unsafe source path")
        key = name.casefold()
        if key in folded:
            raise ValueError("duplicate or case-conflicting source path")
        folded.add(key)
        for parent in path.parents:
            spelling = str(parent)
            parent_key = spelling.casefold()
            if parent_key in directory_spelling and directory_spelling[parent_key] != spelling:
                raise ValueError("case-conflicting directory path")
            directory_spelling[parent_key] = spelling
    for name in names:
        if any(str(parent).casefold() in folded for parent in PurePosixPath(name).parents):
            raise ValueError("file/directory path conflict")


def parse_bundle(raw, expected_files=None):
    if type(raw) is not bytes or len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("bundle exceeds 100 MiB")
    stream = io.BytesIO(raw)
    if stream.readline(8192) != MAGIC:
        raise ValueError("unsupported transfer format")
    metadata = strict_json(stream.readline(8192))
    if not isinstance(metadata, dict):
        raise ValueError("invalid bundle metadata")
    count = metadata.get("files")
    if type(count) is not int or not 1 <= count <= MAX_FILES:
        raise ValueError("invalid source file count")
    if expected_files is not None and count != expected_files:
        raise ValueError("unexpected source file count")
    files = {}
    for _ in range(count):
        header = stream.readline(8192)
        if not header.startswith(b"FILE "):
            raise ValueError("missing source file header")
        info = strict_json(header[5:])
        if not isinstance(info, dict):
            raise ValueError("invalid source file header")
        name = info.get("path"); size = info.get("bytes")
        if type(name) is not str or name in files:
            raise ValueError("invalid or duplicate source path")
        if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError("invalid source file size")
        if type(info.get("executable", False)) is not bool:
            raise ValueError("invalid executable flag")
        digest = info.get("sha256")
        if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid source file digest")
        body = stream.read(size)
        if len(body) != size or stream.read(1) != b"\n" or hashlib.sha256(body).hexdigest() != digest:
            raise ValueError("source file length or hash mismatch")
        body.decode("utf-8")
        files[name] = (body, info.get("executable", False))
    if stream.read() != b"END\n":
        raise ValueError("missing end marker or trailing data")
    validate_paths(files)
    return files


def decode_transfer(encoded, source_bytes, source_sha256, source_files):
    if type(source_bytes) is not int or not 1 <= source_bytes <= MAX_BUNDLE_BYTES:
        raise ValueError("invalid declared transfer size")
    if type(source_files) is not int or not 1 <= source_files <= MAX_FILES:
        raise ValueError("invalid declared file count")
    if type(source_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("invalid declared digest")
    if type(encoded) is not str or len(encoded) > 150 * 1024 * 1024:
        raise ValueError("encoded transfer exceeds limit")
    compressed = base64.b64decode("".join(encoded.split()).encode("ascii"), validate=True)
    decoder = lzma.LZMADecompressor(memlimit=64 * 1024 * 1024)
    raw = decoder.decompress(compressed, max_length=source_bytes + 1)
    if len(raw) != source_bytes or not decoder.eof or decoder.unused_data or hashlib.sha256(raw).hexdigest() != source_sha256:
        raise ValueError("transfer incomplete or modified; no files extracted")
    return parse_bundle(raw, source_files)


def open_directory(path, create=False):
    """Open an absolute directory, refusing symlinks in every component (POSIX)."""
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise OSError("safe extraction requires POSIX no-follow directory support")
    path = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def extract(files, destination, create_parents=False):
    """Write validated files under an exclusively created private directory.

    Directory descriptors prevent symlink swaps from redirecting writes. An I/O
    failure may leave an incomplete private destination; it is never reused or
    silently replaced. No archive content is executed.
    """
    files = dict(files)
    validate_paths(files)
    if not 1 <= len(files) <= MAX_FILES:
        raise ValueError("invalid source file count")
    total = 0
    for entry in files.values():
        if (not isinstance(entry, tuple) or len(entry) != 2
                or type(entry[0]) is not bytes or type(entry[1]) is not bool
                or len(entry[0]) > MAX_FILE_BYTES):
            raise ValueError("invalid extraction payload")
        total += len(entry[0])
    if total > MAX_BUNDLE_BYTES:
        raise ValueError("extraction exceeds 100 MiB")
    destination = Path(os.path.abspath(destination))
    parent_fd = open_directory(destination.parent, create=create_parents)
    root_fd = None
    try:
        # mkdir is the exclusive claim; unlike rename, it cannot replace even an
        # empty directory created by another process between check and commit.
        os.mkdir(destination.name, mode=0o700, dir_fd=parent_fd)
        root_fd = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        identity = os.fstat(root_fd)
        for name, (body, executable) in sorted(files.items()):
            parts = PurePosixPath(name).parts
            directory_fd = os.dup(root_fd)
            try:
                for part in parts[:-1]:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    os.close(directory_fd)
                    directory_fd = next_fd
                file_fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o600, dir_fd=directory_fd)
                with os.fdopen(file_fd, "wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o755 if executable else 0o644)
            finally:
                os.close(directory_fd)
        current = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("destination changed during extraction")
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify or extract this one-file Keel transfer; never executes bundled code")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--verify-only", action="store_true")
    action.add_argument("--out", help="NEW isolated output directory; existing destinations are refused")
    args = parser.parse_args(argv)
    try:
        files = decode_transfer(PAYLOAD_B64, SOURCE_BYTES, SOURCE_SHA256, SOURCE_FILES)
        result = {"version": RELEASE_VERSION, "files": len(files), "source_sha256": SOURCE_SHA256,
                  "verified_integrity": True, "publisher_authenticated": False, "production_deployed": False}
        if args.out:
            destination = extract(files, args.out)
            result.update(extracted_to=str(destination), next_step="Read MUSE_HANDOFF.md and docs/WORKFLOW.md")
        else: result["extracted"] = False
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, TypeError, KeyError, OSError, lzma.LZMAError) as exc:
        print("Keel transfer: " + str(exc), file=sys.stderr)
        return 2
