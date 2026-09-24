#!/usr/bin/env python3
"""Reproducible source-only ZIP with a reviewed file allowlist and SHA-256 manifest.

The allowlist prevents runtime-state inclusion; it is not a universal PII
scanner. Review source and images before sharing a release outside your team.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

try:
    from tools.transfer_reader import (MAX_BUNDLE_BYTES, MAX_FILE_BYTES,
                                       open_directory, strict_json, validate_paths)
except ModuleNotFoundError:
    from transfer_reader import (MAX_BUNDLE_BYTES, MAX_FILE_BYTES,
                                 open_directory, strict_json, validate_paths)

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / 'release-files.json'
MAX_RELEASE_FILES = 10000


def _read_source(root_fd, name):
    """Read a bounded regular file without following any symlink component."""
    descriptor = os.dup(root_fd)
    try:
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(file_fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise ValueError('release source must be a regular file of at most 8 MiB: ' + name)
            body = stream.read(MAX_FILE_BYTES + 1)
            if len(body) > MAX_FILE_BYTES:
                raise ValueError('release source file exceeds 8 MiB: ' + name)
            return body
    except OSError as exc:
        raise ValueError('release file missing, inaccessible or symlink: ' + name) from exc
    finally:
        os.close(descriptor)


def payload(root=ROOT):
    root_fd = open_directory(root)
    try:
        names = strict_json(_read_source(root_fd, 'release-files.json'))
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_RELEASE_FILES:
            raise ValueError('release allowlist must be a bounded list of unique file paths')
        validate_paths(names)
        result = {}
        total = 0
        for name in sorted(names):
            path = PurePosixPath(name)
            if (name.casefold() == 'manifest.json'
                    or any(part.casefold() in {'.git', '__pycache__', '.pytest_cache', 'data',
                                               'credentials', 'dist', 'briefs'} for part in path.parts)):
                raise ValueError('reserved or runtime path in release allowlist')
            body = _read_source(root_fd, name)
            total += len(body)
            if total > MAX_BUNDLE_BYTES:
                raise ValueError('release payload exceeds 100 MiB')
            result[name] = body
        return result
    finally:
        os.close(root_fd)


def build(output, root=ROOT):
    files = payload(root)
    if 'VERSION' not in files:
        raise ValueError('release allowlist must contain VERSION')
    manifest = {'schema_version': 1, 'version': files['VERSION'].decode().strip(),
                'files': {name: {'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}
                          for name, body in files.items()}}
    files['MANIFEST.json'] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    if sum(map(len, files.values())) > MAX_BUNDLE_BYTES:
        raise ValueError('release with manifest exceeds 100 MiB')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation refuses regular files, directories and dangling symlinks.
    with output.open('xb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, body in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, body, compresslevel=9)
    return {'path': str(output), 'files': len(files), 'bytes': output.stat().st_size,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


def verify(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        names = [info.filename for info in entries]
        if not 2 <= len(entries) <= MAX_RELEASE_FILES + 1:
            raise ValueError('invalid archive file count')
        validate_paths(names)
        for info in entries:
            mode = info.external_attr >> 16
            if (info.orig_filename != info.filename or info.is_dir() or info.flag_bits & 1 or info.external_attr & 0x10
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise ValueError('archive contains a non-regular, encrypted or ambiguous member')
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError('archive exceeds limits')
        if sum(info.file_size for info in entries) > MAX_BUNDLE_BYTES:
            raise ValueError('archive exceeds limits')
        if 'MANIFEST.json' not in names:
            raise ValueError('archive manifest missing')
        manifest = strict_json(archive.read('MANIFEST.json'))
        if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
                or manifest['schema_version'] != 1 or not isinstance(manifest.get('version'), str)
                or not isinstance(manifest.get('files'), dict)):
            raise ValueError('invalid archive manifest')
        if set(names) != set(manifest['files']) | {'MANIFEST.json'} or 'MANIFEST.json' in manifest['files']:
            raise ValueError('manifest file set mismatch')
        for name, record in manifest['files'].items():
            if (not isinstance(record, dict) or type(record.get('bytes')) is not int
                    or not 0 <= record['bytes'] <= MAX_FILE_BYTES
                    or type(record.get('sha256')) is not str
                    or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])):
                raise ValueError('invalid manifest file record: ' + name)
            with archive.open(name) as stream:
                body = stream.read(MAX_FILE_BYTES + 1)
            if len(body) != record['bytes'] or hashlib.sha256(body).hexdigest() != record['sha256']:
                raise ValueError('manifest mismatch: ' + name)
    return {'verified_integrity': True, 'files': len(names),
            'authenticity': 'not signed; compare a trusted archive digest'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out')
    parser.add_argument('--verify')
    args = parser.parse_args(argv)
    if not args.out and not args.verify:
        parser.error('--out or --verify required')
    print(json.dumps(verify(args.verify) if args.verify else build(args.out), indent=2))


if __name__ == '__main__':
    main()
