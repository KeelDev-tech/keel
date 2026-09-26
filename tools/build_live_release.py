#!/usr/bin/env python3
"""Verified full reference TXT with an additive installer and bound evidence."""
import argparse
import base64
import hashlib
import io
import json
import lzma
from pathlib import Path
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_live import __version__
from tools.live_inventory import payload, inventory, inventory_from_payload
from tools.transfer_reader import decode_transfer


def encoded_json(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+'\n').encode()


def sha(raw): return hashlib.sha256(raw).hexdigest()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('checks', 'rehearsal', 'http', 'browser', 'out'): p.add_argument('--'+name, required=True)
    args = p.parse_args(argv)
    checks, out = Path(args.checks), Path(args.out)
    files = payload(); observed = inventory_from_payload(files)
    binding = json.loads((checks/'source-binding.json').read_text())
    expected_outputs = {'summary.json','main/junit.xml','main/pytest.log','maintenance/tests.json','maintenance/tests.log'}
    if set(binding['output_sha256']) != expected_outputs:
        raise ValueError('unexpected test evidence file set')
    test_outputs = {name: (checks/name).read_bytes() for name in expected_outputs}
    summary = json.loads(test_outputs['summary.json'])
    if (binding['status'] != 'PASS' or summary['status'] != 'PASS'
            or binding['before_sha256'] != observed['sha256'] or binding['after_sha256'] != observed['sha256']):
        raise ValueError('passing tests bound to the current source are required')
    for name, expected in binding['output_sha256'].items():
        if sha(test_outputs[name]) != expected: raise ValueError('test evidence changed')
    rehearsal = json.loads(Path(args.rehearsal).read_text())
    http = json.loads(Path(args.http).read_text())
    browser = json.loads(Path(args.browser).read_text())
    if rehearsal['status'] != 'PASS' or http['status'] != 'PASS':
        raise ValueError('passing rehearsal and HTTP checks required')
    if browser.get('status') not in ('PASS', 'UNVERIFIED'):
        raise ValueError('a rendered browser failure must be resolved before packaging')
    for report in (rehearsal, http, browser):
        if report.get('release_source_sha256') != observed['sha256']:
            raise ValueError('integration evidence must bind the same release source')
    preserved = json.loads(files['live-base-manifest.json'])
    evidence = {'schema': 'keel.live.release_evidence.v1', 'version': __version__,
        'status': 'INTEGRATION_CANDIDATE', 'source_inventory': observed,
        'baseline_files_preserved': preserved['count'], 'guarded_regressions': summary,
        'synthetic_rehearsal': rehearsal, 'loopback_http': http, 'browser': browser,
        'real_source_capture': 'NOT_RUN', 'live_repository_integration': 'NOT_VERIFIED',
        'real_local_model_inference': 'NOT_RUN', 'real_rendered_preparation': 'NOT_RUN',
        'production_deployed': False, 'execution_authorized': False}
    files.update({'audit/live/validation.json': encoded_json(evidence),
                  'audit/live/source-binding.json': encoded_json(binding),
                  'audit/live/main-junit.xml': test_outputs['main/junit.xml'],
                  'audit/live/main-pytest.log': test_outputs['main/pytest.log'],
                  'audit/live/maintenance-tests.json': test_outputs['maintenance/tests.json'],
                  'audit/live/maintenance-tests.log': test_outputs['maintenance/tests.log']})
    # Base target prerequisites are exactly the verified original 0.7 reference.
    original = json.loads(files['workbench-base-manifest.json'])['files']
    base = {n: row['sha256'] for n, row in original.items()}
    # The historical ZIP included MANIFEST.json; its TXT transfer did not.
    # Retain it in this reference but do not require or install it on the host.
    additions = {n: sha(b) for n, b in files.items() if n not in base and n != 'MANIFEST.json'}
    manifest = {'schema': 'keel.additive_patch.v1', 'version': __version__,
                'base_files': base, 'files': additions, 'execution_authorized': False,
                'production_deployed': False}
    files['keel_live/PATCH_MANIFEST.json'] = encoded_json(manifest)
    stream = io.BytesIO(); stream.write(b'KEEL_TEXT_BUNDLE_V1\n')
    stream.write(json.dumps({'files': len(files), 'scope': '0.9 integration plus unchanged reference packages'}).encode()+b'\n')
    for name, body in sorted(files.items()):
        body.decode('utf-8')
        row = {'path': name, 'bytes': len(body), 'sha256': sha(body), 'executable': name.endswith('.sh')}
        stream.write(b'FILE '+json.dumps(row,sort_keys=True).encode()+b'\n'+body+b'\n')
    stream.write(b'END\n'); raw = stream.getvalue(); encoded = base64.b64encode(lzma.compress(raw,preset=6)).decode()
    restored = decode_transfer(encoded,len(raw),sha(raw),len(files))
    if {n: b for n,(b,_) in restored.items()} != files: raise ValueError('transfer roundtrip failed')
    note = '''#!/usr/bin/env python3
# KEEL 0.9 LIVE INTEGRATION — COMPLETE REFERENCE, ADDITIVE INSTALLER
# Save this WHOLE attachment as Keel_0.9.0_Transfer.txt; no ZIP needed.
# Verify: python3 -B Keel_0.9.0_Transfer.txt --verify-only
# Extract: python3 -B Keel_0.9.0_Transfer.txt --out /existing-parent/new-keel-0.9
# Read docs/LIVE_HANDOFF.md before integration. Never extract over a live tree.
# Preview: python3 -B -m keel_live demo --home /new/private/synthetic-fixture
# All demo people, records and decisions are synthetic. No execution authority.
# Real host adapters, identity authentication and browser validation remain host work.
'''
    reader = files['tools/transfer_reader.py'].decode().replace(
        'Read docs/SELF_HOSTED_HANDOFF.md and docs/SELF_HOSTED.md','Read docs/LIVE_HANDOFF.md and docs/LIVE_INTEGRATION.md')
    footer = (f'\nSOURCE_BYTES = {len(raw)}\nSOURCE_SHA256 = {sha(raw)!r}\nSOURCE_FILES = {len(files)}\n'
              f'RELEASE_VERSION = {__version__!r}\nPAYLOAD_B64 = """\n'+ '\n'.join(textwrap.wrap(encoded,100))+
              '\n"""\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n')
    transfer = (note+reader+footer).encode(); compile(transfer,'Keel_0.9.0_Transfer.txt','exec')
    if inventory() != observed:
        raise ValueError('source changed during packaging')
    out.mkdir(parents=True,exist_ok=False)
    (out/'Keel_0.9.0_Transfer.txt').write_bytes(transfer)
    (out/'Keel_0.9.0_Handoff.txt').write_bytes(files['docs/LIVE_HANDOFF.md'])
    evidence['transfer'] = {'file':'Keel_0.9.0_Transfer.txt','files':len(files),'bytes':len(transfer),
                            'sha256':sha(transfer),'source_sha256':sha(raw)}
    (out/'Keel_0.9.0_Release_Evidence.json').write_bytes(encoded_json(evidence))
    print(json.dumps({'version':__version__,'files':len(files),'bytes':len(transfer),
                      'preserved_baseline_files':preserved['count'],'output':str(out.absolute())}))


if __name__ == '__main__': raise SystemExit(main())
