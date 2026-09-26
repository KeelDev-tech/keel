"""Bounded POSIX reads and new-directory outputs. Same-principal attacks excluded.

No existing target is overwritten. Root and relative path components are opened
with O_NOFOLLOW. Generated artifacts are not an immutable or authenticated store.
"""
from __future__ import annotations
from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import stat
import re
from .contracts import ContractError, require, text

MAX_FILE = 512 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILES = 1000


def relative(value: str) -> str:
    text(value, "path", 240)
    require("\\" not in value and ":" not in value and not value.startswith("/"), "unsafe path")
    parts = value.split("/")
    require(all(p not in ("", ".", "..") and re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts),
            "unsafe path component")
    require(all(not p.endswith((".", " ")) for p in parts), "ambiguous path")
    return value

@contextmanager
def directory(path: Path | str):
    require(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX no-follow support required")
    absolute = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            newfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = newfd
        yield fd
    except OSError as exc:
        raise ContractError(f"directory unavailable or unsafe (errno={exc.errno})") from exc
    finally:
        os.close(fd)


def read_under(root: Path | str, path: str, limit: int = MAX_FILE) -> bytes:
    parts = relative(path).split("/")
    try:
        with directory(root) as base:
            current = os.dup(base)
            try:
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                    os.close(current); current = child
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
                try:
                    before = os.fstat(fd)
                    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                            "only regular, non-hardlinked files are accepted")
                    require(before.st_size <= limit, "file exceeds byte limit")
                    chunks=[]; remaining=limit + 1
                    while remaining:
                        block=os.read(fd, min(remaining, 65536))
                        if not block: break
                        chunks.append(block); remaining-=len(block)
                    raw=b"".join(chunks)
                    after=os.fstat(fd)
                    require(len(raw) <= limit, "file exceeds byte limit")
                    require((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                            (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                            "file changed while reading")
                    return raw
                finally: os.close(fd)
            finally: os.close(current)
    except OSError as exc:
        raise ContractError(f"file unavailable or unsafe: {path} (errno={exc.errno})") from exc


def read_file(path: Path | str, limit: int = MAX_FILE) -> bytes:
    p=Path(os.path.abspath(path))
    return read_under(p.parent, p.name, limit)


def write_new_file(path: Path | str, raw: bytes) -> None:
    p=Path(os.path.abspath(path)); relative(p.name)
    try:
        with directory(p.parent) as parent:
            fd=os.open(p.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(raw); stream.flush(); os.fsync(fd)
            finally: os.close(fd)
            os.fsync(parent)
    except OSError as exc:
        raise ContractError(f"output must be a new safe file (errno={exc.errno})") from exc


def write_tree(destination: Path | str, files: dict[str, bytes]) -> None:
    """Reserve a fresh directory. An interrupted result is not a complete bundle.

    Parent must exist. Do not place output inside an active source repository.
    This is not a multi-file filesystem transaction or a power-loss guarantee.
    """
    require(type(files) is dict and 0 < len(files) <= MAX_FILES + 8, "invalid file count")
    require(sum(len(x) for x in files.values()) <= MAX_TOTAL, "output exceeds total limit")
    folded=set()
    for name in files:
        relative(name)
        require(name.casefold() not in folded, "case-colliding path")
        folded.add(name.casefold())
        require(not any(str(p) in files for p in PurePosixPath(name).parents if str(p) != "."),
                "file/directory collision")
    p=Path(os.path.abspath(destination)); relative(p.name)
    try:
        with directory(p.parent) as parent:
            os.mkdir(p.name, 0o700, dir_fd=parent)
        for name, raw in sorted(files.items()):
            child=p/name
            child.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_new_file(child, raw)
        with directory(p) as final: os.fsync(final)
    except OSError as exc:
        raise ContractError(f"new output directory unavailable (errno={exc.errno}); partial output may remain") from exc
