#!/usr/bin/env python3
"""Verify a 0.10 additive patch; write only with --install on an unchanged 0.9 target.

Included hashes prove internal integrity, not publisher identity. Existing files
are never replaced. Linux directory descriptors and renameat2(RENAME_NOREPLACE)
anchor publication; failures roll back only files created by this invocation.
Historical root MANIFEST.json is neither required nor installed. No source
records, approvals, dependencies, services, or execution authority are created.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.grounding_inventory import directory_fd, hash_map, read_file, safe_name, strict_json

MANIFEST = 'keel_grounding/PATCH_MANIFEST.json'
VERSION = '0.10.0'


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _allowed(name):
    parts = safe_name(name).parts
    return (name in {'grounding-base-manifest.json', 'grounding-release-files.json'}
            or len(parts) >= 2 and parts[0] in {'keel_grounding', 'keel_eval', 'tests', 'tools', 'docs'}
            or len(parts) >= 3 and parts[:2] in {('audit', 'grounding'), ('fixtures', 'grounding_eval')})


def _manifest(root):
    value = strict_json(read_file(root, MANIFEST))
    if (type(value) is not dict or set(value) != {'schema', 'version', 'base_files', 'files',
                                                  'execution_authorized', 'production_deployed'}
            or value['schema'] != 'keel.grounding.additive_patch.v1' or value['version'] != VERSION
            or value['execution_authorized'] is not False or value['production_deployed'] is not False):
        raise ValueError('invalid grounding patch manifest')
    hash_map(value['base_files'])
    hash_map(value['files'])
    if 'MANIFEST.json' in value['base_files'] or MANIFEST in value['files']:
        raise ValueError('historical or self-referential manifest is forbidden')
    if set(value['base_files']) & (set(value['files']) | {MANIFEST}):
        raise ValueError('patch attempts to replace base or unsupported path')
    if any(not _allowed(name) for name in value['files']):
        raise ValueError('patch attempts to replace base or unsupported path')
    return value


def inspect(patch_root, target):
    root, target = Path(patch_root).absolute(), Path(target).absolute()
    for directory in (root, target):
        os.close(directory_fd(directory))
    manifest = _manifest(root)
    for name, expected in manifest['base_files'].items():
        if _hash(read_file(target, name)) != expected:
            raise ValueError('base dependency differs: '+name)
    sources = {**manifest['files'], MANIFEST: _hash(read_file(root, MANIFEST))}
    pending, identical = [], []
    for name, expected in sources.items():
        if _hash(read_file(root, name)) != expected:
            raise ValueError('patch digest mismatch: '+name)
        try:
            actual = _hash(read_file(target, name))
        except FileNotFoundError:
            pending.append(name)
        else:
            if actual != expected:
                raise ValueError('existing file conflict: '+name)
            identical.append(name)
    return manifest, pending, identical


def _fd_path(parent, name):
    """Use our open Linux descriptor as the parent, independent of path races."""
    return Path('/proc/self/fd')/str(parent)/name


def _parent_fd(target, name):
    fd = directory_fd(target)
    try:
        for part in safe_name(name).parts[:-1]:
            try:
                os.mkdir(_fd_path(fd, part), 0o755)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _publish(parent, temporary, destination):
    library = ctypes.CDLL(None, use_errno=True)
    operation = getattr(library, 'renameat2', None)
    if operation is None:
        raise OSError('Linux renameat2 is required for safe additive installation')
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(parent, os.fsencode(temporary), parent, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def install(patch_root, target, *, write=False):
    root, target = Path(patch_root).absolute(), Path(target).absolute()
    manifest_digest = _hash(read_file(root, MANIFEST))
    manifest, pending, identical = inspect(root, target)
    def manifest_unchanged():
        if _hash(read_file(root, MANIFEST)) != manifest_digest:
            raise ValueError('manifest changed after verification')
    manifest_unchanged()
    created = []
    if write:
        try:
            for name in pending:
                manifest_unchanged()
                raw = read_file(root, name)
                if _hash(raw) != manifest['files'].get(name, manifest_digest):
                    raise ValueError('patch changed after verification')
                parent = _parent_fd(target, name)
                temporary = '.keel-grounding-'+secrets.token_hex(16)
                temporary_path = _fd_path(parent, temporary)
                try:
                    fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                    identity = os.fstat(fd)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    manifest_unchanged()
                    destination = safe_name(name).parts[-1]
                    _publish(parent, temporary, destination)
                    created.append((os.dup(parent), destination, identity.st_dev, identity.st_ino))
                    os.fsync(parent)
                finally:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
                    os.close(parent)
            inspect(root, target)
            manifest_unchanged()
        except BaseException:
            for parent, name, device, inode in reversed(created):
                try:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (device, inode):
                    _fd_path(parent, name).unlink()
                    os.fsync(parent)
            raise
        finally:
            for parent, _, _, _ in created:
                os.close(parent)
    return {'status': 'INSTALLED' if write else 'VERIFIED_FOR_ADDITION', 'version': manifest['version'],
            'base_files_verified': len(manifest['base_files']), 'new_files': len(pending),
            'identical_files': len(identical), 'existing_files_modified': 0,
            'execution_authorized': False, 'production_deployed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--patch-root', default=str(ROOT))
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install(args.patch_root, args.target, write=args.install), indent=2))
        return 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print('Keel grounding additive patch: '+str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
