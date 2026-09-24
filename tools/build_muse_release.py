#!/usr/bin/env python3
"""Build the source-bound 0.13 TXT reference and additive integration candidate."""
import argparse
import base64
import hashlib
import io
import json
import lzma
from pathlib import Path
import re
import sys
import textwrap
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_muse import __version__
from tools.muse_inventory import (baseline_manifest, inventory, inventory_from_payload,
                                       payload, read_file, strict_json)
from tools.run_muse_checks import OUTPUTS
from tools.transfer_reader import decode_transfer


def encoded_json(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+'\n').encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def check_test_evidence(binding, outputs, observed):
    if (type(binding) is not dict or binding.get('schema') != 'keel.muse.test_binding.v1'
            or binding.get('status') != 'PASS' or binding.get('suite_exit_code') != 0
            or binding.get('execution_authorized') is not False
            or binding.get('inventory') != observed
            or binding.get('before_sha256') != observed['sha256']
            or binding.get('after_sha256') != observed['sha256']):
        raise ValueError('passing tests bound to the current source are required')
    if set(binding.get('output_sha256', {})) != OUTPUTS:
        raise ValueError('unexpected test evidence file set')
    for name, expected in binding['output_sha256'].items():
        if sha(outputs[name]) != expected:
            raise ValueError('test evidence changed')
    summary = strict_json(outputs['summary.json'])
    maintenance = strict_json(outputs['maintenance/tests.json'])
    root = ET.fromstring(outputs['main/junit.xml'])
    if root.tag != 'testsuites' or any(child.tag != 'testsuite' for child in root):
        raise ValueError('guarded JUnit suite container required')
    suites = list(root)
    for node in suites:
        if any(child.tag == 'testsuite' for child in node):
            raise ValueError('nested JUnit suites are not supported')
        for key in ('tests', 'failures', 'errors', 'skipped'):
            value = node.get(key)
            if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,17})', value):
                raise ValueError('nonnegative canonical JUnit counters required')
    for key in ('tests_run', 'failures', 'errors', 'skipped', 'expected_failures', 'unexpected_successes'):
        if type(maintenance.get(key)) is not int or maintenance[key] < 0:
            raise ValueError('nonnegative integer maintenance counters required')
    counts = {key: sum(int(node.get(key, 0)) for node in suites)
              for key in ('tests', 'failures', 'errors', 'skipped')}
    expected_suites = [dict(suite='main_flow_assurance_trust_workflow', **counts),
                       dict(suite='maintenance', tests=maintenance.get('tests_run'),
                            failures=maintenance.get('failures'), errors=maintenance.get('errors'),
                            skipped=maintenance.get('skipped'), status=maintenance.get('status'))]
    if (summary.get('status') != 'PASS' or summary.get('production_deployed') is not False
            or summary.get('suites') != expected_suites
            or summary.get('runs') != [{'suite': 'main', 'exit_code': 0}, {'suite': 'maintenance', 'exit_code': 0}]
            or any(type(row['tests']) is not int or row['tests'] <= 0
                   or any(row[key] != 0 for key in ('failures', 'errors', 'skipped')) for row in expected_suites)
            or maintenance.get('status') != 'PASS'
            or any(maintenance.get(key, 0) != 0 for key in ('expected_failures', 'unexpected_successes'))
            or type(summary.get('tests_run')) is not int
            or summary.get('tests_run') != sum(row['tests'] for row in expected_suites)):
        raise ValueError('passing nonempty guarded suites with matching counts are required')
    return summary


def check_acceptance(report, observed):
    if (type(report) is not dict or report.get('schema') != 'keel.muse.acceptance.v1'
            or report.get('status') != 'PASS' or report.get('synthetic') is not True
            or report.get('source_unchanged') is not True
            or report.get('release_source_sha256') != observed['sha256']
            or report.get('execution_authorized') is not False
            or report.get('production_deployed') is not False
            or report.get('real_local_model_inference') != 'NOT_RUN'
            or report.get('actual_native_browser') != 'NOT_RUN'
            or report.get('account_muse_integration') != 'NOT_VERIFIED'
            or report.get('live_repository_integration') != 'NOT_VERIFIED'):
        raise ValueError('passing synthetic acceptance bound to current source is required')
    checks = report.get('checks')
    if (type(checks) is not list or not checks
            or any(type(row) is not dict or row.get('status') != 'PASS'
                   or not isinstance(row.get('name'), str) or not row['name'] for row in checks)
            or len({row['name'] for row in checks}) != len(checks)):
        raise ValueError('unique passing acceptance checks are required')
    if any(type(report.get(key)) is not int or report[key] != len(checks)
           for key in ('checks_total', 'checks_passed')):
        raise ValueError('acceptance counts must match the complete check list')
    if 'source_sha256' in report and report['source_sha256'] != observed['sha256']:
        raise ValueError('acceptance source pins must agree')
    if 'external_tlc_model_check' in report and report['external_tlc_model_check'] != 'NOT_RUN':
        raise ValueError('external TLC checking was not run for this release')
    if 'rendered_browser' in report and report['rendered_browser'] not in {'NOT_RUN', 'UNAVAILABLE'}:
        raise ValueError('rendered browser execution was not verified for this release')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('checks', 'acceptance', 'out'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args(argv)
    checks, acceptance_path, out = Path(args.checks).absolute(), Path(args.acceptance).absolute(), Path(args.out).absolute()
    # Every emitted source byte and reader byte comes from this one snapshot.
    files = payload()
    observed = inventory_from_payload(files)
    binding_raw = read_file(checks, 'source-binding.json')
    binding = strict_json(binding_raw)
    if type(binding) is not dict or set(binding.get('output_sha256', {})) != OUTPUTS:
        raise ValueError('unexpected test evidence file set')
    test_outputs = {name: read_file(checks, name) for name in sorted(OUTPUTS)}
    summary = check_test_evidence(binding, test_outputs, observed)
    acceptance_raw = read_file(acceptance_path.parent, acceptance_path.name)
    acceptance = strict_json(acceptance_raw)
    check_acceptance(acceptance, observed)
    preserved = baseline_manifest(files['muse-base-manifest.json'])
    evidence = {'schema': 'keel.muse.release_evidence.v1', 'version': __version__,
        'status': 'INTEGRATION_CANDIDATE', 'source_inventory': observed,
        'baseline_files_preserved': preserved['count'],
        'guarded_regressions': summary, 'synthetic_acceptance': acceptance,
        'real_source_capture': 'NOT_RUN', 'live_repository_integration': 'NOT_VERIFIED',
        'account_muse_integration': 'NOT_VERIFIED', 'actual_native_browser': 'NOT_RUN',
        'real_local_model_inference': 'NOT_RUN', 'real_rendered_preparation': 'NOT_RUN',
        'real_benchmark_trials': 'NOT_RUN', 'competitive_superiority_established': False,
        'external_tlc_model_check': 'NOT_RUN',
        'source_authenticity_verified': False, 'truth_independently_verified': False,
        'production_deployed': False, 'execution_authorized': False,
        'evidence_integrity': {'source_binding_sha256': sha(binding_raw),
                               'acceptance_sha256': sha(acceptance_raw)}}
    generated = {'audit/muse/validation.json': encoded_json(evidence),
                 'audit/muse/source-binding.json': binding_raw,
                 'audit/muse/acceptance.json': acceptance_raw,
                 'audit/muse/tests-summary.json': test_outputs['summary.json'],
                 'audit/muse/main-junit.xml': test_outputs['main/junit.xml'],
                 'audit/muse/main-pytest.log': test_outputs['main/pytest.log'],
                 'audit/muse/maintenance-tests.json': test_outputs['maintenance/tests.json'],
                 'audit/muse/maintenance-tests.log': test_outputs['maintenance/tests.log']}
    if set(generated) & set(files) or 'keel_muse/PATCH_MANIFEST.json' in files:
        raise ValueError('generated release members collide with source')
    files.update(generated)
    base = {name: digest for name, digest in preserved['files'].items() if name != 'MANIFEST.json'}
    additions = {name: sha(body) for name, body in files.items() if name not in base and name != 'MANIFEST.json'}
    manifest = {'schema': 'keel.muse.additive_patch.v1', 'version': __version__,
                'base_files': base, 'files': additions, 'execution_authorized': False,
                'production_deployed': False}
    files['keel_muse/PATCH_MANIFEST.json'] = encoded_json(manifest)
    stream = io.BytesIO()
    stream.write(b'KEEL_TEXT_BUNDLE_V1\n')
    stream.write(json.dumps({'files': len(files), 'scope': '0.13 muse plus unchanged 0.12 reference'}).encode()+b'\n')
    for name, body in sorted(files.items()):
        body.decode('utf-8')
        row = {'path': name, 'bytes': len(body), 'sha256': sha(body), 'executable': name.endswith('.sh')}
        stream.write(b'FILE '+json.dumps(row, sort_keys=True).encode()+b'\n'+body+b'\n')
    stream.write(b'END\n')
    raw = stream.getvalue()
    encoded = base64.b64encode(lzma.compress(raw, preset=6)).decode()
    restored = decode_transfer(encoded, len(raw), sha(raw), len(files))
    if {name: body for name, (body, _) in restored.items()} != files:
        raise ValueError('transfer roundtrip failed')
    note = '''#!/usr/bin/env python3
# KEEL 0.13 MUSE ADAPTER AND EVALUATION — COMPLETE REFERENCE, ADDITIVE INSTALLER
# Save this WHOLE attachment as Keel_0.13.0_Transfer.txt; no ZIP needed.
# Verify: python3 -B Keel_0.13.0_Transfer.txt --verify-only
# Extract: python3 -B Keel_0.13.0_Transfer.txt --out /existing-parent/new-keel-0.13
# Read docs/MUSE_HANDOFF.md before integration. Never extract over a live tree.
# Only synthetic acceptance ran here. Native Muse browser, model inference and live integration did not.
# These checks bind measured evidence; they do not establish competitive superiority or execution authority.
'''
    reader = files['tools/transfer_reader.py'].decode().replace(
        'Read docs/SELF_HOSTED_HANDOFF.md and docs/SELF_HOSTED.md',
        'Read docs/MUSE_HANDOFF.md and docs/MUSE.md')
    footer = (f'\nSOURCE_BYTES = {len(raw)}\nSOURCE_SHA256 = {sha(raw)!r}\nSOURCE_FILES = {len(files)}\n'
              f'RELEASE_VERSION = {__version__!r}\nPAYLOAD_B64 = """\n'+'\n'.join(textwrap.wrap(encoded, 100))+
              '\n"""\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n')
    transfer = (note+reader+footer).encode()
    compile(transfer, 'Keel_0.13.0_Transfer.txt', 'exec')
    if inventory() != observed:
        raise ValueError('source changed during packaging')
    out.mkdir(parents=True, exist_ok=False)
    (out/'Keel_0.13.0_Transfer.txt').write_bytes(transfer)
    (out/'Keel_0.13.0_Handoff.txt').write_bytes(files['docs/MUSE_HANDOFF.md'])
    evidence['transfer'] = {'file': 'Keel_0.13.0_Transfer.txt', 'files': len(files), 'bytes': len(transfer),
                            'sha256': sha(transfer), 'source_sha256': sha(raw)}
    (out/'Keel_0.13.0_Release_Evidence.json').write_bytes(encoded_json(evidence))
    print(json.dumps({'version': __version__, 'files': len(files), 'bytes': len(transfer),
                      'preserved_baseline_files': preserved['count'], 'output': str(out)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
