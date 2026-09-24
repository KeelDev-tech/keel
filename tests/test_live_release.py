"""Release integrity regressions using miniature, explicitly synthetic evidence."""
import ast
import hashlib
import json
from pathlib import Path

import pytest

from tools import build_live_release as release
from tools.live_inventory import inventory_from_payload, inventory, payload
from tools.transfer_reader import decode_transfer


def setup_release(tmp_path, monkeypatch):
    source_root = Path(__file__).resolve().parents[1]
    files = {'VERSION': b'synthetic-base\n',
             'tools/transfer_reader.py': (source_root/'tools/transfer_reader.py').read_bytes(),
             'docs/LIVE_HANDOFF.md': b'SYNTHETIC TEST HANDOFF\n',
             'live-base-manifest.json': b'{"count": 1}',
             'workbench-base-manifest.json': json.dumps({'files': {'VERSION': {
                 'sha256': hashlib.sha256(b'synthetic-base\n').hexdigest()}}}).encode()}
    checks = tmp_path/'checks'; checks.mkdir()
    outputs = {'summary.json': b'{"status":"PASS","synthetic":true}', 'main/junit.xml': b'<synthetic/>',
               'main/pytest.log': b'synthetic', 'maintenance/tests.json': b'{"synthetic":true}',
               'maintenance/tests.log': b'synthetic'}
    for name, value in outputs.items():
        path = checks/name; path.parent.mkdir(exist_ok=True); path.write_bytes(value)
    observed = inventory_from_payload(files)
    binding = {'status':'PASS', 'before_sha256':observed['sha256'], 'after_sha256':observed['sha256'],
               'output_sha256':{name:hashlib.sha256(value).hexdigest() for name,value in outputs.items()}}
    (checks/'source-binding.json').write_text(json.dumps(binding))
    arguments = ['--checks', str(checks)]
    for name in ('rehearsal','http','browser'):
        path = tmp_path/(name+'.json')
        path.write_text(json.dumps({'status':'PASS','synthetic':True,'release_source_sha256':observed['sha256']}))
        arguments += ['--'+name, str(path)]
    out = tmp_path/'delivery'; arguments += ['--out',str(out)]
    monkeypatch.setattr(release, 'payload', lambda: dict(files))
    monkeypatch.setattr(release, 'inventory', lambda: inventory_from_payload(files))
    return files, checks, arguments, out


def test_transfer_contains_the_same_bytes_as_bound_source(tmp_path, monkeypatch):
    files, _, arguments, out = setup_release(tmp_path, monkeypatch)
    release.main(arguments)
    values = {}
    for node in ast.parse((out/'Keel_0.9.0_Transfer.txt').read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in {'SOURCE_BYTES','SOURCE_SHA256','SOURCE_FILES','PAYLOAD_B64'}:
                values[name] = ast.literal_eval(node.value)
    decoded = decode_transfer(values['PAYLOAD_B64'], values['SOURCE_BYTES'],
                              values['SOURCE_SHA256'],values['SOURCE_FILES'])
    assert all(decoded[name][0] == body for name, body in files.items())


def test_source_edit_between_payload_read_and_inventory_cannot_misbind_tests(tmp_path, monkeypatch):
    files, _, arguments, out = setup_release(tmp_path, monkeypatch)
    old_bytes = dict(files)
    old_bytes['docs/LIVE_HANDOFF.md'] = b'UNTESTED DIFFERENT BYTES'
    monkeypatch.setattr(release, 'payload', lambda: old_bytes)
    # inventory() returns tested bytes; the old two-read builder would bundle
    # untested old_bytes while reporting the inventory of tested files.
    with pytest.raises(ValueError, match='bound to the current source'):
        release.main(arguments)
    assert not out.exists()


def test_source_change_during_packaging_refuses_output(tmp_path, monkeypatch):
    files, _, arguments, out = setup_release(tmp_path, monkeypatch)
    changed = dict(files); changed['VERSION'] = b'changed\n'
    monkeypatch.setattr(release, 'inventory', lambda: inventory_from_payload(changed))
    with pytest.raises(ValueError, match='source changed during packaging'):
        release.main(arguments)
    assert not out.exists()


def test_unexpected_evidence_paths_and_modified_logs_are_rejected(tmp_path, monkeypatch):
    _, checks, arguments, out = setup_release(tmp_path, monkeypatch)
    binding_path = checks/'source-binding.json'; binding = json.loads(binding_path.read_text())
    binding['output_sha256']['../unexpected'] = 'a'*64
    binding_path.write_text(json.dumps(binding))
    with pytest.raises(ValueError, match='unexpected test evidence'):
        release.main(arguments)
    binding['output_sha256'].pop('../unexpected'); binding_path.write_text(json.dumps(binding))
    (checks/'main/pytest.log').write_text('modified')
    with pytest.raises(ValueError, match='test evidence changed'):
        release.main(arguments)
    assert not out.exists()


def test_host_inventory_does_not_require_historical_zip_metadata(tmp_path):
    original = b'original archive manifest'
    code = b'# synthetic host source\n'
    manifest = {'files': {'MANIFEST.json': hashlib.sha256(original).hexdigest(),
                           'source.py': hashlib.sha256(code).hexdigest()}}
    (tmp_path/'live-base-manifest.json').write_text(json.dumps(manifest))
    (tmp_path/'live-release-files.json').write_text('[]')
    (tmp_path/'source.py').write_bytes(code)
    expected = inventory(tmp_path)
    assert set(expected['files']) == {'source.py'}
    (tmp_path/'MANIFEST.json').write_bytes(b'host packaging manifest retained')
    assert inventory(tmp_path) == expected
    with pytest.raises(ValueError, match='preserved baseline changed: MANIFEST.json'):
        payload(tmp_path)
    (tmp_path/'source.py').write_bytes(b'changed actual source')
    with pytest.raises(ValueError, match='preserved baseline changed: source.py'):
        inventory(tmp_path)
