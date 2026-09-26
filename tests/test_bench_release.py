"""Synthetic 0.11 release evidence, source preservation, and additive installation."""
import ast
import hashlib
import json
import os
from pathlib import Path

import pytest

from tools import build_bench_release as release
from tools import bench_inventory as sources
from tools import install_bench as installer
from tools.transfer_reader import decode_transfer


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save_json(path, value):
    path.write_text(json.dumps(value))


def setup_release(tmp_path, monkeypatch):
    source_root = Path(__file__).resolve().parents[1]
    baseline = {'schema': 'keel.bench.baseline.v1', 'count': 2,
                'files': {'VERSION': sha(b'synthetic-base\n'), 'MANIFEST.json': sha(b'historical\n')}}
    files = {'VERSION': b'synthetic-base\n', 'MANIFEST.json': b'historical\n',
             'tools/transfer_reader.py': (source_root/'tools/transfer_reader.py').read_bytes(),
             'docs/BENCH_HANDOFF.md': b'SYNTHETIC TEST HANDOFF\n',
             'bench-base-manifest.json': json.dumps(baseline).encode()}
    observed = sources.inventory_from_payload(files)
    checks = tmp_path/'checks'
    checks.mkdir()
    maintenance = {'status': 'PASS', 'tests_run': 3, 'failures': 0, 'errors': 0, 'skipped': 0,
                   'unexpected_successes': 0, 'expected_failures': 0}
    summary = {'status': 'PASS', 'production_deployed': False, 'tests_run': 5,
               'runs': [{'suite': 'main', 'exit_code': 0}, {'suite': 'maintenance', 'exit_code': 0}],
               'suites': [{'suite': 'main_flow_assurance_trust_workflow', 'tests': 2, 'failures': 0, 'errors': 0, 'skipped': 0},
                          {'suite': 'maintenance', 'tests': 3, 'failures': 0, 'errors': 0, 'skipped': 0, 'status': 'PASS'}]}
    outputs = {'summary.json': json.dumps(summary).encode(),
               'main/junit.xml': b'<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0"/></testsuites>',
               'main/pytest.log': b'synthetic test log',
               'maintenance/tests.json': json.dumps(maintenance).encode(),
               'maintenance/tests.log': b'synthetic maintenance log'}
    for name, value in outputs.items():
        path = checks/name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(value)
    binding = {'schema': 'keel.bench.test_binding.v1', 'status': 'PASS', 'suite_exit_code': 0,
               'execution_authorized': False, 'inventory': observed,
               'before_sha256': observed['sha256'], 'after_sha256': observed['sha256'],
               'output_sha256': {name: sha(value) for name, value in outputs.items()}}
    save_json(checks/'source-binding.json', binding)
    acceptance = tmp_path/'acceptance.json'
    save_json(acceptance, {'schema': 'keel.bench.acceptance.v1', 'status': 'PASS', 'synthetic': True,
                          'source_unchanged': True, 'release_source_sha256': observed['sha256'],
                          'execution_authorized': False, 'production_deployed': False,
                          'real_local_model_inference': 'NOT_RUN', 'live_repository_integration': 'NOT_VERIFIED',
                          'checks': [{'name': 'synthetic content mutation', 'status': 'PASS'}]})
    out = tmp_path/'delivery'
    arguments = ['--checks', str(checks), '--acceptance', str(acceptance), '--out', str(out)]
    monkeypatch.setattr(release, 'payload', lambda: dict(files))
    monkeypatch.setattr(release, 'inventory', lambda: sources.inventory_from_payload(files))
    return files, checks, acceptance, arguments, out


def decode(path):
    values = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in {'SOURCE_BYTES', 'SOURCE_SHA256', 'SOURCE_FILES', 'PAYLOAD_B64'}:
                values[name] = ast.literal_eval(node.value)
    return decode_transfer(values['PAYLOAD_B64'], values['SOURCE_BYTES'], values['SOURCE_SHA256'], values['SOURCE_FILES'])


def test_transfer_preserves_snapshot_and_adds_evidence_without_false_runtime_claims(tmp_path, monkeypatch):
    files, _, _, arguments, out = setup_release(tmp_path, monkeypatch)
    assert release.main(arguments) == 0
    decoded = decode(out/'Keel_0.11.0_Transfer.txt')
    assert all(decoded[name][0] == body for name, body in files.items())
    patch = json.loads(decoded[installer.MANIFEST][0])
    assert patch['base_files'] == {'VERSION': sha(files['VERSION'])}
    assert 'MANIFEST.json' not in patch['files']
    assert not set(patch['base_files']) & set(patch['files'])
    evidence = json.loads((out/'Keel_0.11.0_Release_Evidence.json').read_text())
    assert evidence['baseline_files_preserved'] == 2
    assert evidence['real_local_model_inference'] == 'NOT_RUN'
    assert evidence['live_repository_integration'] == 'NOT_VERIFIED'
    assert evidence['production_deployed'] is False
    assert evidence['execution_authorized'] is False
    assert evidence['source_authenticity_verified'] is False
    assert evidence['truth_independently_verified'] is False
    assert evidence['real_benchmark_trials'] == 'NOT_RUN'
    assert evidence['competitive_superiority_established'] is False
    assert evidence['transfer']['sha256'] == sha((out/'Keel_0.11.0_Transfer.txt').read_bytes())
    assert (out/'Keel_0.11.0_Handoff.txt').read_bytes() == files['docs/BENCH_HANDOFF.md']


def test_snapshot_not_second_read_binds_emitted_payload(tmp_path, monkeypatch):
    files, _, _, arguments, out = setup_release(tmp_path, monkeypatch)
    stale = dict(files)
    stale['docs/BENCH_HANDOFF.md'] = b'untested content'
    monkeypatch.setattr(release, 'payload', lambda: stale)
    with pytest.raises(ValueError, match='bound to the current source'):
        release.main(arguments)
    assert not out.exists()


def test_changed_source_at_end_of_packaging_rejects_output(tmp_path, monkeypatch):
    files, _, _, arguments, out = setup_release(tmp_path, monkeypatch)
    changed = dict(files, VERSION=b'changed')
    monkeypatch.setattr(release, 'inventory', lambda: sources.inventory_from_payload(changed))
    with pytest.raises(ValueError, match='source changed during packaging'):
        release.main(arguments)
    assert not out.exists()


@pytest.mark.parametrize('mutation', ['unexpected_path', 'log', 'suite_failure', 'fake_summary', 'missing_suite'])
def test_tampered_test_evidence_cannot_package(tmp_path, monkeypatch, mutation):
    _, checks, _, arguments, out = setup_release(tmp_path, monkeypatch)
    binding_path = checks/'source-binding.json'
    binding = json.loads(binding_path.read_text())
    if mutation == 'unexpected_path':
        binding['output_sha256']['../outside'] = 'a'*64
    elif mutation == 'log':
        (checks/'main/pytest.log').write_text('tampered')
    else:
        if mutation == 'suite_failure':
            (checks/'main/junit.xml').write_text('<testsuites><testsuite tests="2" failures="1" errors="0" skipped="0"/></testsuites>')
        elif mutation == 'missing_suite':
            (checks/'main/junit.xml').write_text('<testsuites/>')
        else:
            summary = json.loads((checks/'summary.json').read_text())
            summary['tests_run'] = 9999
            save_json(checks/'summary.json', summary)
        binding['output_sha256'] = {name: sha((checks/name).read_bytes()) for name in binding['output_sha256']}
    save_json(binding_path, binding)
    with pytest.raises(ValueError):
        release.main(arguments)
    assert not out.exists()


@pytest.mark.parametrize('mutation', ['negative_junit', 'missing_junit_counter', 'nested_junit',
                                    'boolean_maintenance', 'missing_maintenance_counter'])
def test_rehashed_malformed_counters_cannot_manufacture_a_pass(tmp_path, monkeypatch, mutation):
    _, checks, _, arguments, out = setup_release(tmp_path, monkeypatch)
    if mutation == 'negative_junit':
        raw = ('<testsuites><testsuite tests="2" failures="1" errors="0" skipped="0"/>'
               '<testsuite tests="0" failures="-1" errors="0" skipped="0"/></testsuites>')
        (checks/'main/junit.xml').write_text(raw)
    elif mutation == 'missing_junit_counter':
        (checks/'main/junit.xml').write_text('<testsuites><testsuite tests="2"/></testsuites>')
    elif mutation == 'nested_junit':
        raw = ('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0">'
               '<testsuite tests="0" failures="0" errors="0" skipped="0"/></testsuite></testsuites>')
        (checks/'main/junit.xml').write_text(raw)
    else:
        path = checks/'maintenance/tests.json'
        maintenance = json.loads(path.read_text())
        if mutation == 'boolean_maintenance':
            maintenance['failures'] = False
        else:
            del maintenance['expected_failures']
        save_json(path, maintenance)
    binding_path = checks/'source-binding.json'
    binding = json.loads(binding_path.read_text())
    binding['output_sha256'] = {name: sha((checks/name).read_bytes()) for name in binding['output_sha256']}
    save_json(binding_path, binding)
    with pytest.raises(ValueError):
        release.main(arguments)
    assert not out.exists()


@pytest.mark.parametrize(('key', 'value'), [('status', 'FAIL'), ('synthetic', False), ('source_unchanged', False),
    ('execution_authorized', True), ('production_deployed', True), ('real_local_model_inference', 'PASS'),
    ('release_source_sha256', 'a'*64), ('checks', []), ('checks', [{'name': 'failed', 'status': 'FAIL'}])])
def test_missing_or_misrepresented_acceptance_rejects_release(tmp_path, monkeypatch, key, value):
    _, _, acceptance, arguments, out = setup_release(tmp_path, monkeypatch)
    report = json.loads(acceptance.read_text())
    report[key] = value
    save_json(acceptance, report)
    with pytest.raises(ValueError):
        release.main(arguments)
    assert not out.exists()


def setup_inventory(tmp_path):
    original, code = b'original archive metadata', b'unchanged source'
    baseline = {'schema': 'keel.bench.baseline.v1', 'count': 2,
                'files': {'MANIFEST.json': sha(original), 'source.py': sha(code)}}
    save_json(tmp_path/sources.BASELINE, baseline)
    save_json(tmp_path/sources.ALLOWLIST, [sources.BASELINE, sources.ALLOWLIST])
    (tmp_path/'source.py').write_bytes(code)
    return original


def test_host_inventory_ignores_historical_manifest_only(tmp_path):
    original = setup_inventory(tmp_path)
    expected = sources.inventory(tmp_path)
    assert 'MANIFEST.json' not in expected['files']
    (tmp_path/'MANIFEST.json').write_bytes(b'host own metadata')
    assert sources.inventory(tmp_path) == expected
    with pytest.raises(ValueError, match='preserved baseline changed: MANIFEST.json'):
        sources.payload(tmp_path)
    (tmp_path/'MANIFEST.json').write_bytes(original)
    assert sources.payload(tmp_path)['MANIFEST.json'] == original
    (tmp_path/'source.py').write_bytes(b'modified source')
    with pytest.raises(ValueError, match='preserved baseline changed: source.py'):
        sources.inventory(tmp_path)


@pytest.mark.parametrize('entry', ['../outside', 'docs//bad.md', 'tools\\bad.py', 'audit/bench/stale.json', 'keel_bench/PATCH_MANIFEST.json', 'source.py', 'unknown.py', 'fixtures/other/fixture.json', 'keel_eval/new.py'])
def test_invalid_source_allowlist_is_rejected(tmp_path, entry):
    setup_inventory(tmp_path)
    save_json(tmp_path/sources.ALLOWLIST, [sources.BASELINE, sources.ALLOWLIST, entry])
    with pytest.raises(ValueError):
        sources.inventory(tmp_path)


def test_source_hardlinks_and_symlinks_are_rejected(tmp_path):
    setup_inventory(tmp_path)
    source = tmp_path/'source.py'
    moved = tmp_path/'moved.py'
    source.rename(moved)
    source.symlink_to(moved)
    with pytest.raises(OSError):
        sources.inventory(tmp_path)
    source.unlink()
    os.link(moved, source)
    with pytest.raises(ValueError, match='singly linked'):
        sources.inventory(tmp_path)


def setup_patch(tmp_path):
    patch, target = tmp_path/'patch', tmp_path/'target'
    patch.mkdir()
    target.mkdir()
    (target/'VERSION').write_bytes(b'unchanged 0.10 prerequisite')
    (target/'MANIFEST.json').write_bytes(b'host historical metadata remains')
    files = {'keel_bench/added.py': b'synthetic bench addition',
             'tests/added.py': b'synthetic evaluation addition',
             'docs/BENCH_HANDOFF.md': b'synthetic handoff',
             'fixtures/bench/fixture.json': b'{"synthetic":true}'}
    for name, raw in files.items():
        path = patch/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    manifest = {'schema': 'keel.bench.additive_patch.v1', 'version': '0.11.0',
                'base_files': {'VERSION': sha((target/'VERSION').read_bytes())},
                'files': {name: sha(raw) for name, raw in files.items()},
                'execution_authorized': False, 'production_deployed': False}
    save_json(patch/installer.MANIFEST, manifest)
    return patch, target


def file_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def test_additive_install_readonly_default_and_idempotency(tmp_path):
    patch, target = setup_patch(tmp_path)
    before = file_bytes(target)
    verified = installer.install(patch, target)
    assert verified['status'] == 'VERIFIED_FOR_ADDITION'
    assert file_bytes(target) == before
    assert not (target/'keel_bench').exists()
    first = installer.install(patch, target, write=True)
    assert first['new_files'] == 5
    assert first['base_files_verified'] == 1
    assert first['existing_files_modified'] == 0
    installed = file_bytes(target)
    assert all(installed[name] == raw for name, raw in before.items())
    assert all(installed[name] == raw for name, raw in file_bytes(patch).items())
    assert installer.install(patch, target, write=True)['new_files'] == 0
    assert file_bytes(target) == installed


@pytest.mark.parametrize('mutation', ['base', 'source', 'conflict', 'manifest', 'outside'])
def test_install_rejects_conflict_and_tampering_before_any_write(tmp_path, mutation):
    patch, target = setup_patch(tmp_path)
    if mutation == 'base':
        (target/'VERSION').write_bytes(b'host changed')
    elif mutation == 'source':
        (patch/'keel_bench/added.py').write_bytes(b'tampered')
    elif mutation == 'conflict':
        (target/'keel_bench').mkdir()
        (target/'keel_bench/added.py').write_bytes(b'host work')
    else:
        path = patch/installer.MANIFEST
        manifest = json.loads(path.read_text())
        if mutation == 'manifest':
            manifest['base_files']['MANIFEST.json'] = sha((target/'MANIFEST.json').read_bytes())
        else:
            manifest['files']['../outside.py'] = 'a'*64
        save_json(path, manifest)
    before = file_bytes(target)
    with pytest.raises((ValueError, OSError)):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before


def test_manifest_toctou_is_detected_before_install(tmp_path, monkeypatch):
    patch, target = setup_patch(tmp_path)
    before = file_bytes(target)
    original = installer.inspect
    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        path = patch/installer.MANIFEST
        value = json.loads(path.read_text())
        value['version'] = 'TAMPERED'
        save_json(path, value)
        return result
    monkeypatch.setattr(installer, 'inspect', changing)
    with pytest.raises(ValueError, match='manifest changed'):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before


def test_publication_failure_rolls_back_only_this_installs_files(tmp_path, monkeypatch):
    patch, target = setup_patch(tmp_path)
    before = file_bytes(target)
    original = installer._publish
    calls = []
    def failing(parent, temporary, destination):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError('synthetic publication failure')
        original(parent, temporary, destination)
    monkeypatch.setattr(installer, '_publish', failing)
    with pytest.raises(OSError, match='synthetic publication failure'):
        installer.install(patch, target, write=True)
    assert len(calls) == 2
    assert file_bytes(target) == before
    assert not list(target.rglob('.keel-bench-*'))


def test_concurrent_new_destination_is_preserved_without_overwrite(tmp_path, monkeypatch):
    patch, target = setup_patch(tmp_path)
    original = installer._publish
    paths = []
    def racing(parent, temporary, destination):
        path = installer._fd_path(parent, destination)
        path.write_text('CONCURRENT HOST FILE')
        paths.append(path.resolve())
        original(parent, temporary, destination)
    monkeypatch.setattr(installer, '_publish', racing)
    with pytest.raises(OSError):
        installer.install(patch, target, write=True)
    assert paths[0].read_text() == 'CONCURRENT HOST FILE'
    assert (target/'VERSION').read_bytes() == b'unchanged 0.10 prerequisite'
    assert not list(target.rglob('.keel-bench-*'))


def test_target_parent_symlink_cannot_escape(tmp_path):
    patch, target = setup_patch(tmp_path)
    outside = tmp_path/'outside'
    outside.mkdir()
    (target/'keel_bench').symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        installer.install(patch, target, write=True)
    assert list(outside.iterdir()) == []


def test_duplicate_json_manifest_keys_fail_closed(tmp_path):
    patch, target = setup_patch(tmp_path)
    path = patch/installer.MANIFEST
    path.write_text(path.read_text().replace('"version": "0.11.0"', '"version":"0.11.0","version":"0.11.0"'))
    with pytest.raises(ValueError, match='duplicate JSON key'):
        installer.install(patch, target, write=True)


def test_source_change_after_inspection_rolls_back_partial_addition(tmp_path, monkeypatch):
    patch, target = setup_patch(tmp_path)
    before = file_bytes(target)
    original = installer.inspect
    def change_source(*args, **kwargs):
        result = original(*args, **kwargs)
        (patch/'tests/added.py').write_bytes(b'changed after inspection')
        return result
    monkeypatch.setattr(installer, 'inspect', change_source)
    with pytest.raises(ValueError, match='patch changed after verification'):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before
    assert not list(target.rglob('.keel-bench-*'))


def test_manifest_change_during_publication_rolls_back_created_files(tmp_path, monkeypatch):
    patch, target = setup_patch(tmp_path)
    before = file_bytes(target)
    original = installer._publish
    def change_manifest(parent, temporary, destination):
        original(parent, temporary, destination)
        path = patch/installer.MANIFEST
        manifest = json.loads(path.read_text())
        manifest['version'] = 'changed during publication'
        save_json(path, manifest)
    monkeypatch.setattr(installer, '_publish', change_manifest)
    with pytest.raises(ValueError, match='manifest changed'):
        installer.install(patch, target, write=True)
    assert file_bytes(target) == before
    assert not list(target.rglob('.keel-bench-*'))


def test_rollback_preserves_concurrently_replaced_file(tmp_path, monkeypatch):
    patch, target = setup_patch(tmp_path)
    original = installer._publish
    first = []
    def replace_then_fail(parent, temporary, destination):
        if not first:
            original(parent, temporary, destination)
            first.append(installer._fd_path(parent, destination).resolve())
            return
        replacement = first[0].with_name('concurrent-replacement')
        replacement.write_text('CONCURRENT HOST WORK')
        os.replace(replacement, first[0])
        raise OSError('synthetic later publication failure')
    monkeypatch.setattr(installer, '_publish', replace_then_fail)
    with pytest.raises(OSError, match='synthetic later publication failure'):
        installer.install(patch, target, write=True)
    assert first[0].read_text() == 'CONCURRENT HOST WORK'
    assert (target/'VERSION').read_bytes() == b'unchanged 0.10 prerequisite'
    assert not (target/'tests/added.py').exists()
