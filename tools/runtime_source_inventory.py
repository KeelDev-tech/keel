"""Bounded on-disk consistency for an explicitly selected trusted installation.

This does not authenticate a checkout or attest already imported Python code.
Membership is reviewed in source; source digests are observations, not approval.
There is deliberately no Git discovery, external manifest or fallback profile.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat


PROFILE = 'current-runtime-v1'
SCHEMA = 'keel.runtime-source-files.v1'
MANIFEST_NAME = 'runtime-source-files.json'
EXPECTED_NAMES_SHA256 = '94c401e64f5030a069417c565ec68b0312103fb980f340666253663191d18840'
MAX_MANIFEST_BYTES = 256 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 1024
MAX_ENTRIES = 8192
MAX_DIRECTORIES = 1024
MAX_DEPTH = 16
MAX_PATH_BYTES = 1024

# Exact boundaries from main 78f1549fbecada07b331b4c05fde1bb099ff7b45.
# These are not recursively scanned and are not part of this profile's claim.
EXCLUDED_DIRECTORIES = frozenset({
    '.git', '.github', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.hypothesis',
    'dist', 'docs', 'formal', 'geo-pipeline/history',
    'integrations/mcp-server/docs', 'integrations/mcp-server/scripts',
    'integrations/mcp-server/tests', 'keel_connector/evidence', 'launch',
    'maintenance_workbench/docs', 'maintenance_workbench/evidence',
    'maintenance_workbench/tests', 'privacy/tests', 'program/build',
    'program/intel', 'program/pipeline', 'program/queues', 'program/specs',
    'promotion/tests', 'publish/tests', 'remediation', 'security/tests',
    'shadow/tests', 'site', 'tests', 'transfer-checks-0.5.0',
})
EXCLUDED_SCRIPTS = frozenset({
    'engines/build_dashboard_backup-20260918-k31k32-import-safe.py',
    'engines/build_dashboard_backup-20260918-k33-html-injection.py',
    'engines/live_cache_backup-20260918-k21k22.py',
    'engines/page_capture_backup-20260918-k23.py',
    'engines/queue_io_backup-20260918-k38-k50.py',
    'flow_board_pulse-backup-20260922-arm05-p1.py',
    'flow_board_pulse-backup-20260922-fix4.py',
    'program/prototypes/blocker-inbox/test_blocker_inbox.py',
    'program/prototypes/cost-metering/test_cost_metering.py',
    'program/prototypes/escape-hatch/test_escape_hatch.py',
    'program/prototypes/interview-tracker/test_interview_tracker.py',
    'program/prototypes/run-history/test_run_history.py',
    'program/prototypes/volume-controls/test_volume_controls.py',
    'integrations/mcp-server/scripts/smoke_test.sh', 'package.sh',
    'package_backup-20260918-k36k37-packaging.sh', 'setup.sh', 'start.sh',
})
SOURCE_SUFFIXES = frozenset({'.py', '.pyw', '.js', '.mjs', '.cjs', '.sh',
                             '.bash', '.zsh', '.so', '.pyd', '.dll', '.dylib',
                             '.pyc', '.pyo'})


class RuntimeSourceError(ValueError):
    """A fixed-code consistency or installation boundary failure."""


def _require(condition, code):
    if not condition:
        raise RuntimeSourceError('runtime_source_' + code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def parse_manifest(raw):
    """Validate fixed membership without accessing the filesystem."""
    _require(type(raw) is bytes and len(raw) <= MAX_MANIFEST_BYTES, 'manifest_size')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RuntimeSourceError('runtime_source_manifest_invalid') from exc
    _require(type(value) is dict and set(value) == {'schema', 'profile', 'files'},
             'manifest_fields')
    _require(value['schema'] == SCHEMA and value['profile'] == PROFILE, 'manifest_profile')
    names = value['files']
    _require(type(names) is list and 0 < len(names) <= MAX_MEMBERS, 'manifest_members')
    for name in names:
        _require(type(name) is str, 'member_path')
        parts = name.split('/')
        _require(name.isascii() and len(name.encode()) <= MAX_PATH_BYTES
                 and len(parts) <= MAX_DEPTH and all(part not in {'', '.', '..'} for part in parts)
                 and '\\' not in name and not any(ord(c) < 32 or ord(c) == 127 for c in name),
                 'member_path')
    _require(names == sorted(set(names)), 'manifest_order')
    _require(len(names) == 630 and _sha(_canonical(names)) == EXPECTED_NAMES_SHA256,
             'manifest_membership')
    return tuple(names)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def _stamp(info):
    return _identity(info) + (info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _trusted(info, *, directory=False, ancestor=False):
    _require(info.st_uid in {0, os.getuid()}, 'untrusted_owner')
    _require(not info.st_mode & 0o022 or
             (directory and ancestor and info.st_mode & stat.S_ISVTX), 'writable_mode')
    _require(not info.st_mode & (stat.S_ISUID | stat.S_ISGID), 'special_mode')
    if directory:
        _require(stat.S_ISDIR(info.st_mode), 'directory_type')
    else:
        _require(not info.st_mode & stat.S_ISVTX, 'special_mode')
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'file_type_or_links')


@contextmanager
def _root(root):
    """Anchor every ancestor; never resolve through a symlink."""
    value = os.fspath(root)
    _require(isinstance(value, str) and os.path.isabs(value), 'absolute_root_required')
    _require(value == os.path.normpath(value) and value != '/', 'root_path')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        descriptor = os.open('/', flags)
        stack.callback(os.close, descriptor)
        slash = descriptor
        slash_identity = _identity(os.fstat(slash))
        links = []
        _trusted(os.fstat(descriptor), directory=True, ancestor=True)
        for part in Path(value).parts[1:]:
            parent = descriptor
            descriptor = os.open(part, flags, dir_fd=parent)
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            _trusted(info, directory=True, ancestor=True)
            _require(_identity(os.stat(part, dir_fd=parent, follow_symlinks=False)) ==
                     _identity(info), 'ancestor_changed')
            links.append((parent, part, descriptor, _identity(info)))
        _trusted(os.fstat(descriptor), directory=True)
        yield descriptor
        _trusted(os.fstat(slash), directory=True, ancestor=True)
        _require(_identity(os.fstat(slash)) == slash_identity, 'ancestor_changed')
        for parent, part, child, expected in links:
            info = os.fstat(child)
            _trusted(info, directory=True, ancestor=True)
            _require(_identity(info) == expected ==
                     _identity(os.stat(part, dir_fd=parent, follow_symlinks=False)),
                     'ancestor_changed')


def _read(directory, name, limit):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        _trusted(before)
        _require(before.st_size <= limit, 'file_size')
        chunks, size = [], 0
        while True:
            block = os.read(descriptor, min(65536, limit + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            _require(size <= limit, 'file_size')
        _require(size == before.st_size and _stamp(before) == _stamp(os.fstat(descriptor)) ==
                 _stamp(os.stat(name, dir_fd=directory, follow_symlinks=False)), 'file_changed')
        return b''.join(chunks), _stamp(before)
    finally:
        os.close(descriptor)


def _enumerate(descriptors, names, directories):
    entries, count = {}, 0
    for relative, descriptor in descriptors.items():
        _trusted(os.fstat(descriptor), directory=True)
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                count += 1
                _require(count <= MAX_ENTRIES, 'entry_limit')
                name = relative + '/' + entry.name if relative else entry.name
                _require(len(name.encode('utf-8')) <= MAX_PATH_BYTES, 'entry_path')
                if name in EXCLUDED_DIRECTORIES or name in EXCLUDED_SCRIPTS:
                    continue
                _require(entry.name != '__pycache__', 'bytecode_directory')
                info = entry.stat(follow_symlinks=False)
                entries[name] = _stamp(info)
                if stat.S_ISDIR(info.st_mode):
                    _require(name in directories, 'unexpected_directory')
                    _trusted(info, directory=True)
                else:
                    _trusted(info)
                    suffix = PurePosixPath(name).suffix.lower()
                    if name not in names:
                        _require(suffix not in SOURCE_SUFFIXES and not info.st_mode & 0o111,
                                 'unexpected_source')
    _require(names <= entries.keys() and directories <= entries.keys(), 'missing_member')
    return entries


def _snapshot(root, frozen=None):
    with _root(root) as root_fd:
        root_identity = _identity(os.fstat(root_fd))
        raw, manifest_stamp = _read(root_fd, MANIFEST_NAME, MAX_MANIFEST_BYTES)
        names = parse_manifest(raw)
        if frozen is not None:
            _require((root_identity, raw) == frozen, 'profile_changed')
        directories = {str(parent) for name in names for parent in PurePosixPath(name).parents
                       if str(parent) != '.'}
        _require(len(directories) <= MAX_DIRECTORIES, 'directory_limit')
        with ExitStack() as stack:
            descriptors = {'': root_fd}
            for name in sorted(directories, key=lambda value: (value.count('/'), value)):
                path = PurePosixPath(name)
                parent = str(path.parent) if str(path.parent) != '.' else ''
                descriptor = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=descriptors[parent])
                stack.callback(os.close, descriptor)
                _trusted(os.fstat(descriptor), directory=True)
                descriptors[name] = descriptor
            before = _enumerate(descriptors, set(names), directories)
            files, total = {}, 0
            for name in names:
                path = PurePosixPath(name)
                parent = str(path.parent) if str(path.parent) != '.' else ''
                data, stamp = _read(descriptors[parent], path.name, MAX_FILE_BYTES)
                _require(stamp == before[name], 'file_changed')
                total += len(data)
                _require(total <= MAX_TOTAL_BYTES, 'total_size')
                files[name] = _sha(data)
            _require(before == _enumerate(descriptors, set(names), directories), 'tree_changed')
            _require(manifest_stamp == before[MANIFEST_NAME] and files[MANIFEST_NAME] == _sha(raw),
                     'manifest_changed')
            for name, descriptor in descriptors.items():
                if name:
                    _require(_stamp(os.fstat(descriptor)) == before[name], 'directory_changed')
            report = {'schema': SCHEMA, 'profile': PROFILE, 'allowlist_sha256': _sha(raw),
                      'files': files}
            report['sha256'] = _sha(_canonical(report))
            report['file_count'] = len(files)
    return report, (root_identity, raw)


def current_runtime_provider(root):
    """Freeze one fixed selection; reject any later observed disturbance.

    The CLI supplies its own installation root. This function is not exposed as
    a caller-selected root, manifest, JSON argument or automatic repinning API.
    """
    root = os.fspath(root)
    try:
        initial, frozen = _snapshot(root)
    except OSError as exc:
        raise RuntimeSourceError('runtime_source_filesystem') from exc

    initial_sha = initial['sha256']
    tainted = False

    def inventory():
        nonlocal tainted
        _require(not tainted, 'changed')
        try:
            report = _snapshot(root, frozen)[0]
        except OSError as exc:
            tainted = True
            raise RuntimeSourceError('runtime_source_filesystem') from exc
        except Exception:
            tainted = True
            raise
        # An observed disturbance cannot be erased by restoring bytes before a
        # later callback or exception-path check. A new invocation is required.
        if report['sha256'] != initial_sha:
            tainted = True
            raise RuntimeSourceError('runtime_source_changed')
        return report

    return inventory
