"""Verify a source delta and build a NEW code directory; never edit its base.

Run from an extracted update bundle containing UPDATE_MANIFEST.json and
changes/. Linux/WSL is required. Hashes establish byte correspondence, not
publisher authenticity. This is a source update, not a deployment or migration
of runtime state. On interrupted extraction discard the incomplete NEW output.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
if (HERE / 'transfer_reader.py').is_file():
    sys.path.insert(0, str(HERE))
    from transfer_reader import open_directory, strict_json, validate_paths, extract
else:
    from tools.transfer_reader import open_directory, strict_json, validate_paths, extract

MAX_SOURCE_BYTES = 100 * 1024 * 1024
MAX_SOURCE_FILES = 10000
MAX_FILE_BYTES = 8 * 1024 * 1024


def _read(root, name):
    import stat
    validate_paths([name])
    parent = open_directory(root)
    try:
        parts = name.split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise ValueError('bounded regular source file required: ' + name)
            data = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
            version = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if version(before) != version(after) or len(data) != after.st_size or len(data) > MAX_FILE_BYTES:
                raise ValueError('source changed while reading: ' + name)
            return data, bool(before.st_mode & 0o111)
    finally:
        os.close(parent)


def _hashes(value):
    import re
    if type(value) is not dict or not 1 <= len(value) <= MAX_SOURCE_FILES:
        raise ValueError('bounded nonempty source hash map required')
    validate_paths(value)
    for digest in value.values():
        if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid source digest')
    return value


def prepare(update, base):
    update, base = Path(os.path.abspath(update)), Path(os.path.abspath(base))
    raw, _ = _read(update, 'UPDATE_MANIFEST.json')
    manifest = strict_json(raw)
    if (type(manifest) is not dict or set(manifest) != {'schema', 'base_files', 'changed_files', 'deleted_files'}
            or manifest['schema'] != 'keel.source-update.v1' or manifest['deleted_files'] != []):
        raise ValueError('unsupported source update manifest')
    baseline = _hashes(manifest['base_files'])
    changed = _hashes(manifest['changed_files'])
    validate_paths(sorted(set(baseline) | set(changed)))
    # A source update cannot silently discard local additions. Require a clean
    # extracted base; its generated root manifest is the sole excluded file.
    fd = open_directory(base)
    os.close(fd)
    present = set()
    for directory, dirs, names in os.walk(base, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError('symlink in source base')
        for name in names:
            present.add((Path(directory) / name).relative_to(base).as_posix())
    unexpected = sorted(present - set(baseline) - {'MANIFEST.json'})
    if unexpected:
        raise ValueError('unexpected local files; merge review required: ' + unexpected[0])
    files, total = {}, 0
    for name, expected in sorted(baseline.items()):
        body, executable = _read(base, name)
        if hashlib.sha256(body).hexdigest() != expected:
            raise ValueError('base source differs; merge review required: ' + name)
        total += len(body)
        if total > MAX_SOURCE_BYTES:
            raise ValueError('source update exceeds 100 MiB')
        files[name] = (body, executable)
    for name, expected in sorted(changed.items()):
        if name not in baseline:
            try:
                _read(base, name)
            except FileNotFoundError:
                pass
            else:
                raise ValueError('new file conflicts with existing source: ' + name)
        body, executable = _read(update / 'changes', name)
        if hashlib.sha256(body).hexdigest() != expected:
            raise ValueError('changed source digest mismatch: ' + name)
        total += len(body) - len(files.get(name, (b'', False))[0])
        if total > MAX_SOURCE_BYTES:
            raise ValueError('source update exceeds 100 MiB')
        files[name] = (body, executable)
    if len(files) > MAX_SOURCE_FILES:
        raise ValueError('too many source files')
    # Historical audit manifests cannot describe changed bytes. Replace only in
    # the NEW directory, retaining original audit evidence in the untouched base.
    payload = {name: {'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}
               for name, (body, _) in sorted(files.items()) if name != 'MANIFEST.json'}
    files['MANIFEST.json'] = ((json.dumps({'schema_version': 1, 'version': 'evolution-hardening-20260926',
                                         'files': payload}, sort_keys=True, indent=2) + '\n').encode(), False)
    return files, len(changed)


def apply(update, base, output=None):
    files, changed = prepare(update, base)
    result = {'verified_source_files': len(files), 'changed_files': changed,
              'base_modified': False, 'production_deployed': False, 'execution_authorized': False}
    if output is not None:
        output, base = Path(os.path.abspath(output)), Path(os.path.abspath(base))
        if output == base or base in output.parents:
            raise ValueError('output must be a new directory outside the base')
        extract(files, output, max_files=MAX_SOURCE_FILES)
        result['output'] = str(output)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--update', default=str(HERE))
    parser.add_argument('--base', required=True)
    parser.add_argument('--output', help='NEW directory; omit for verification only')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(apply(args.update, args.base, args.output), indent=2))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print('Source update refused: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
