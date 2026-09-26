#!/usr/bin/env python3
"""Verify an additive patch against Keel0.7; install only with --install.

This checks file integrity against the included manifest, not publisher identity.
New files are created exclusively. Existing different files are never replaced.
A crash can leave a partial addition; rerunning verifies and completes identical
files. No source records, approvals, services or runtime dependencies are created.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "keel_sources/PATCH_MANIFEST.json"


def _safe(root, name, *, present=False):
    path = PurePosixPath(name)
    if (not isinstance(name, str) or str(path) != name or path.is_absolute() or ".." in path.parts
            or "\\" in name or not path.parts or any(ord(c) < 32 for c in name)):
        raise ValueError("invalid manifest path")
    result = root / name
    for current in (result, *result.parents):
        if current == root.parent:
            break
        if current.is_symlink():
            raise ValueError("symlink in patch or target path")
    if present:
        info = result.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8*1024*1024:
            raise ValueError("bounded regular source file required")
    return result


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _publish(temporary, destination):
    """Linux atomic no-replace rename; never falls back to overwriting rename."""
    library = ctypes.CDLL(None, use_errno=True)
    operation = getattr(library, "renameat2", None)
    if operation is None:
        raise OSError("Linux renameat2 is required for safe additive installation")
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(-100, os.fsencode(temporary), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def inspect(patch_root, target):
    root, target = Path(patch_root).absolute(), Path(target).absolute()
    if root.resolve(strict=True) != root or target.resolve(strict=True) != target or not target.is_dir():
        raise ValueError("canonical existing directories required")
    manifest_path = _safe(root, MANIFEST, present=True)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != "keel.additive_patch.v1" or not manifest.get("base_files") or not manifest.get("files"):
        raise ValueError("invalid patch manifest")
    for name, expected in manifest["base_files"].items():
        if _hash(_safe(target, name, present=True)) != expected:
            raise ValueError("base dependency differs: " + name)
    pending, identical = [], []
    sources = {**manifest["files"], MANIFEST: _hash(manifest_path)}
    for name, expected in sources.items():
        if name in manifest["base_files"] or PurePosixPath(name).parts[0] not in {"keel_sources", "tests", "tools", "docs", "audit"}:
            raise ValueError("patch attempts to replace base or unsupported path")
        source = _safe(root, name, present=True)
        if _hash(source) != expected:
            raise ValueError("patch digest mismatch: " + name)
        destination = _safe(target, name)
        if destination.exists():
            if _hash(_safe(target, name, present=True)) != expected:
                raise ValueError("existing file conflict: " + name)
            identical.append(name)
        else:
            pending.append(name)
    return manifest, pending, identical


def install(patch_root, target, *, write=False):
    root, target = Path(patch_root).absolute(), Path(target).absolute()
    manifest, pending, identical = inspect(root, target)
    created = []
    if write:
        try:
            for name in pending:
                source = _safe(root, name, present=True)
                destination = _safe(target, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _safe(target, name)
                raw = source.read_bytes()
                expected = manifest["files"].get(name, _hash(root/MANIFEST))
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("patch changed after verification")
                fd, temporary = tempfile.mkstemp(prefix=".keel-addition-", dir=destination.parent)
                try:
                    identity = os.fstat(fd)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, 0o644)
                    # Atomic publication cannot overwrite an existing path and
                    # never exposes a partly written authoritative source file.
                    _publish(temporary, destination)
                    created.append((name, identity.st_dev, identity.st_ino))
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                directory_fd = os.open(destination.parent, os.O_RDONLY|getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            inspect(root, target)
        except BaseException:
            # Roll back only the exact files this invocation created. No existing
            # file is changed; don't remove a concurrently replaced path.
            for name, device, inode in reversed(created):
                path = target/name
                info = path.lstat() if path.exists() else None
                if info is not None and stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (device, inode):
                    path.unlink()
            raise
    return {"status": "INSTALLED" if write else "VERIFIED_FOR_ADDITION", "version": manifest["version"],
            "base_files_verified": len(manifest["base_files"]), "new_files": len(pending),
            "identical_files": len(identical), "existing_files_modified": 0,
            "execution_authorized": False, "production_deployed": False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target",required=True)
    parser.add_argument("--patch-root",default=str(ROOT))
    parser.add_argument("--install",action="store_true")
    args=parser.parse_args(argv)
    try:
        print(json.dumps(install(args.patch_root,args.target,write=args.install),indent=2))
        return 0
    except (ValueError,TypeError,KeyError,OSError) as exc:
        print("Keel additive patch: "+str(exc))
        return 2


if __name__=="__main__":
    raise SystemExit(main())
