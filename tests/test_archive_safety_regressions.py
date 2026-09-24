"""Untrusted archive paths, ambiguous evidence, and extraction race regressions."""
import base64
import hashlib
import json
import lzma
import os
from pathlib import Path
import stat
import zipfile

import pytest

from tools import package, text_bundle, transfer_reader


def bundle(names):
    parts = [transfer_reader.MAGIC, (json.dumps({'files': len(names)}) + '\n').encode()]
    for name in names:
        body = b'fixture\n'
        record = {'path': name, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
        parts.extend([b'FILE ' + json.dumps(record).encode() + b'\n', body, b'\n'])
    return b''.join(parts) + b'END\n'


def archive(path, names, *, mode=stat.S_IFREG | 0o644, manifest_transform=lambda value: value):
    body = b'fixture\n'
    manifest = {'schema_version': 1, 'version': 'fixture', 'files': {
        name: {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()} for name in names}}
    with zipfile.ZipFile(path, 'w') as output:
        for name in names:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            output.writestr(info, body)
        output.writestr('MANIFEST.json', manifest_transform(json.dumps(manifest)))


@pytest.mark.parametrize('names', [
    ['a\\b'], ['./a'], ['a//b'], ['a/../b'], ['a:b'], ['a\nfile'], [''],
    ['A', 'a'], ['folder', 'folder/file.py'], ['Folder', 'folder/file.py'],
    ['trailing.'], ['space '], ['e\u0301.py'], ['CON'], ['AUX.txt'], ['a?b'],
    ['Dir/a.py', 'dir/b.py'],
])
@pytest.mark.parametrize('format', ['zip', 'text', 'compressed'])
def test_all_receivers_refuse_ambiguous_paths(tmp_path, names, format):
    if format == 'zip':
        source = tmp_path / 'input.zip'
        archive(source, names)
        check = lambda: package.verify(source)
    else:
        raw = bundle(names)
        if format == 'text':
            source = tmp_path / 'input.txt'
            source.write_bytes(raw)
            check = lambda: text_bundle.parse(source)
        else:
            check = lambda: transfer_reader.decode_transfer(
                base64.b64encode(lzma.compress(raw)).decode(), len(raw),
                hashlib.sha256(raw).hexdigest(), len(names))
    with pytest.raises(ValueError):
        check()


@pytest.mark.parametrize('mode', [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFDIR | 0o755])
def test_zip_receipt_refuses_nonregular_members(tmp_path, mode):
    source = tmp_path / 'input.zip'
    archive(source, ['source.py'], mode=mode)
    with pytest.raises(ValueError):
        package.verify(source)


def test_zip_receipt_refuses_duplicate_manifest_keys(tmp_path):
    source = tmp_path / 'input.zip'
    archive(source, ['source.py'], manifest_transform=lambda value: value.replace('"bytes": 8', '"bytes": 999, "bytes": 8'))
    with pytest.raises(ValueError, match='duplicate JSON'):
        package.verify(source)


@pytest.mark.parametrize('target', ['metadata', 'header'])
def test_text_receivers_refuse_duplicate_json_keys(tmp_path, target):
    raw = bundle(['source.py'])
    raw = raw.replace(b'"files": 1', b'"files": 999, "files": 1') if target == 'metadata' else raw.replace(
        b'"bytes": 8', b'"bytes": 999, "bytes": 8')
    source = tmp_path / 'input.txt'
    source.write_bytes(raw)
    with pytest.raises(ValueError, match='duplicate JSON'):
        text_bundle.parse(source)
    with pytest.raises(ValueError, match='duplicate JSON'):
        transfer_reader.decode_transfer(base64.b64encode(lzma.compress(raw)).decode(), len(raw), hashlib.sha256(raw).hexdigest(), 1)


def test_allowlist_refuses_symlink_directory_within_source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'real').mkdir()
    (source / 'real/file.py').write_text('fixture')
    (source / 'alias').symlink_to(source / 'real', target_is_directory=True)
    (source / 'release-files.json').write_text('["alias/file.py"]')
    with pytest.raises(ValueError, match='symlink'):
        package.payload(source)


@pytest.mark.parametrize('name', ['MANIFEST.json', 'manifest.JSON', 'data/private.json', './VERSION', 'A\\file.py'])
def test_allowlist_refuses_reserved_and_noncanonical_names(tmp_path, name):
    (tmp_path / 'release-files.json').write_text(json.dumps([name]))
    with pytest.raises(ValueError):
        package.payload(tmp_path)


@pytest.mark.parametrize('receiver', ['text', 'compressed'])
def test_extraction_refuses_symlink_ancestor(tmp_path, receiver):
    outside = tmp_path / 'outside'
    outside.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(outside, target_is_directory=True)
    destination = alias / 'destination'
    raw = bundle(['nested/file.py'])
    source = tmp_path / 'bundle.txt'
    source.write_bytes(raw)
    with pytest.raises(OSError):
        if receiver == 'text':
            text_bundle.extract(source, destination)
        else:
            transfer_reader.extract(transfer_reader.parse_bundle(raw), destination)
    assert not (outside / 'destination').exists()


@pytest.mark.parametrize('receiver', ['text', 'compressed'])
def test_concurrent_destination_creation_is_never_replaced(tmp_path, monkeypatch, receiver):
    source = tmp_path / 'bundle.txt'
    source.write_bytes(bundle(['source.py']))
    destination = tmp_path / 'destination'
    original_mkdir = os.mkdir
    contested = []

    def claim_first(path, *args, **kwargs):
        if path == destination.name and kwargs.get('dir_fd') is not None and not contested:
            original_mkdir(path, *args, **kwargs)
            contested.append(destination.stat().st_ino)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, 'mkdir', claim_first)
    with pytest.raises(FileExistsError):
        if receiver == 'text':
            text_bundle.extract(source, destination)
        else:
            transfer_reader.extract({'source.py': (b'fixture', False)}, destination)
    assert contested == [destination.stat().st_ino]
    assert list(destination.iterdir()) == []


def test_extract_revalidates_direct_call_before_creating_output(tmp_path):
    destination = tmp_path / 'destination'
    with pytest.raises(ValueError):
        transfer_reader.extract({'../outside': (b'fixture', False)}, destination)
    assert not destination.exists()
    assert not (tmp_path / 'outside').exists()


def test_text_bundle_reproducible_round_trip_and_no_overwrite(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'VERSION').write_text('fixture\n')
    (source / 'nested').mkdir()
    (source / 'nested/run.sh').write_text('#!/bin/sh\nexit 0\n')
    (source / 'release-files.json').write_text(json.dumps(['VERSION', 'nested/run.sh']))
    first = tmp_path / 'one.txt'
    second = tmp_path / 'two.txt'
    assert text_bundle.create(source, first) == 2
    text_bundle.create(source, second)
    assert first.read_bytes() == second.read_bytes()
    destination = tmp_path / 'restored'
    assert text_bundle.extract(first, destination) == 2
    assert (destination / 'nested/run.sh').read_bytes() == (source / 'nested/run.sh').read_bytes()
    assert (destination / 'nested/run.sh').stat().st_mode & 0o777 == 0o755
    with pytest.raises(FileExistsError):
        text_bundle.extract(first, destination)


def test_broken_destination_link_is_preserved(tmp_path):
    source = tmp_path / 'bundle.txt'
    source.write_bytes(bundle(['source.py']))
    destination = tmp_path / 'destination'
    destination.symlink_to(tmp_path / 'missing')
    with pytest.raises(FileExistsError):
        text_bundle.extract(source, destination)
    assert destination.is_symlink()
    assert not (tmp_path / 'missing').exists()


@pytest.mark.parametrize('names,mode,duplicate', [
    (['a\\b'], stat.S_IFREG | 0o644, False),
    (['A', 'a'], stat.S_IFREG | 0o644, False),
    (['dir', 'dir/file.py'], stat.S_IFREG | 0o644, False),
    (['./source.py'], stat.S_IFREG | 0o644, False),
    (['source.py'], stat.S_IFLNK | 0o777, False),
    (['source.py'], stat.S_IFREG | 0o644, True),
])
def test_shell_verify_has_same_checks_without_tools_package(tmp_path, names, mode, duplicate):
    import subprocess
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    script = Path(__file__).resolve().parents[1] / 'package.sh'
    (fixture / 'package.sh').symlink_to(script)
    source = tmp_path / 'input.zip'
    transform = (lambda value: value.replace('"bytes": 8', '"bytes": 999, "bytes": 8')) if duplicate else (lambda value: value)
    archive(source, names, mode=mode, manifest_transform=transform)
    result = subprocess.run(['bash', str(fixture / 'package.sh'), '--verify', str(source)],
                            cwd=fixture, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert json.loads(result.stdout)['verified_integrity'] is False


# DEFERRED with package.sh staging hunk (b): the live tree has no reviewed
# release-files.json, so the refusal behavior is intentionally not ported.
# This test is preserved in the candidate tree at
# ~/workspace/keel-audit-test/repaired/tests/test_archive_safety_regressions.py
# and should be restored together with hunk (b) once the live team authors
# a reviewed release-files.json.
def test_current_application_requires_reviewed_release_manifest(tmp_path):
    pytest.skip("deferred with package.sh hunk (b): no release-files.json in live tree")


def test_shell_privacy_and_publication_blocks_preserved():
    # Digests bind the exact blocks from the user-supplied audit archive.
    text = (Path(__file__).resolve().parents[1] / 'package.sh').read_text()
    expected = [('# Scrub anything that must never ship', '# --- Reproducible payload', '28c2754a3cd6d6201c58c8d108b61631fee4d91271d06ca49c2aa84422f22c93'), ('# Egress gate (bypass-3 fix)', None, '15f7cc84df71a933b2b1dc0ec0c8fe5132fe33948129a51e2d8f0b71899f53af')]
    for marker, end, digest in expected:
        start = text.index(marker)
        section = text[start:text.index(end, start)] if end else text[start:]
        assert hashlib.sha256(section.encode()).hexdigest() == digest
