import hashlib
import json
from pathlib import Path

import pytest

from tools.apply_source_update import apply


def bundle(tmp_path):
    base = tmp_path / 'base'; update = tmp_path / 'update'
    base.mkdir(); (update / 'changes').mkdir(parents=True)
    (base / 'old.py').write_bytes(b'old\n')
    (update / 'changes/old.py').write_bytes(b'new\n')
    (update / 'changes/new.py').write_bytes(b'added\n')
    manifest = {'schema': 'keel.source-update.v1',
                'base_files': {'old.py': hashlib.sha256(b'old\n').hexdigest()},
                'changed_files': {name: hashlib.sha256((update/'changes'/name).read_bytes()).hexdigest()
                                  for name in ['old.py', 'new.py']}, 'deleted_files': []}
    (update / 'UPDATE_MANIFEST.json').write_text(json.dumps(manifest))
    return base, update


def test_update_builds_fresh_source_without_touching_base(tmp_path):
    base, update = bundle(tmp_path)
    output = tmp_path / 'new-code'
    result = apply(update, base, output)
    assert (base / 'old.py').read_bytes() == b'old\n'
    assert not (base / 'new.py').exists()
    assert (output / 'old.py').read_bytes() == b'new\n'
    assert (output / 'new.py').read_bytes() == b'added\n'
    assert result['base_modified'] is result['production_deployed'] is False
    manifest = json.loads((output / 'MANIFEST.json').read_text())
    assert set(manifest['files']) == {'old.py', 'new.py'}


@pytest.mark.parametrize('change', ['base', 'patch', 'collision', 'symlink', 'traversal'])
def test_conflicts_and_tampering_refuse_before_output(tmp_path, change):
    base, update = bundle(tmp_path)
    if change == 'base': (base / 'old.py').write_bytes(b'user edits')
    if change == 'patch': (update / 'changes/old.py').write_bytes(b'corrupt')
    if change == 'collision': (base / 'new.py').write_bytes(b'existing')
    if change == 'symlink':
        (base / 'old.py').unlink(); (base / 'old.py').symlink_to(update / 'changes/old.py')
    if change == 'traversal':
        p = update / 'UPDATE_MANIFEST.json'; m = json.loads(p.read_text())
        m['changed_files']['../escape'] = 'a' * 64; p.write_text(json.dumps(m))
    output = tmp_path / 'output'
    with pytest.raises((ValueError, OSError)):
        apply(update, base, output)
    assert not output.exists()


def test_existing_or_nested_destination_refused(tmp_path):
    base, update = bundle(tmp_path)
    existing = tmp_path / 'existing'; existing.mkdir()
    (existing / 'keep').write_text('untouched')
    for output in (existing, base / 'nested', tmp_path / 'sibling/../base/inside'):
        with pytest.raises((ValueError, OSError)): apply(update, base, output)
    assert (existing / 'keep').read_text() == 'untouched'
    assert not (base / 'inside').exists()


def test_local_additions_are_not_silently_dropped(tmp_path):
    base, update = bundle(tmp_path)
    (base / 'custom.py').write_text('my work')
    with pytest.raises(ValueError, match='unexpected local files'):
        apply(update, base, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()
    assert (base / 'custom.py').read_text() == 'my work'


def test_verify_only_creates_no_output(tmp_path):
    base, update = bundle(tmp_path)
    result = apply(update, base)
    assert 'output' not in result
    assert sorted(p.name for p in tmp_path.iterdir()) == ['base', 'update']


def test_explicit_large_source_limit_keeps_default_transfer_limit(tmp_path):
    from tools.transfer_reader import extract
    files = {f'file-{i}.py': (b'', False) for i in range(1001)}
    with pytest.raises(ValueError): extract(files, tmp_path / 'default')
    extract(files, tmp_path / 'source', max_files=10000)
    assert len(list((tmp_path / 'source').iterdir())) == 1001
    for bad in (True, 0, 10001):
        with pytest.raises(ValueError): extract(files, tmp_path / 'bad', max_files=bad)
