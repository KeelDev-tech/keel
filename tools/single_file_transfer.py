#!/usr/bin/env python3
"""Build a complete Python-as-TXT transfer with no separate extractor dependency."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.package import payload
from tools.transfer_reader import decode_transfer


def build(output, root=ROOT):
    files = payload(root); version = files['VERSION'].decode().strip()
    output = Path(output)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', output.name): raise ValueError('use a simple transfer filename')
    raw = io.BytesIO(); raw.write(b'KEEL_TEXT_BUNDLE_V1\n')
    raw.write((json.dumps({'files':len(files),'scope':'source, synthetic examples and review evidence'})+'\n').encode())
    for name,body in sorted(files.items()):
        body.decode('utf-8')
        info={'path':name,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),'executable':name.endswith('.sh')}
        raw.write(b'FILE '+json.dumps(info,sort_keys=True).encode()+b'\n');raw.write(body);raw.write(b'\n')
    raw.write(b'END\n'); body=raw.getvalue(); sha=hashlib.sha256(body).hexdigest()
    encoded=base64.b64encode(lzma.compress(body,preset=6)).decode('ascii')
    # Use the exact bundled decoder to validate all files and path topology before releasing.
    decoded=decode_transfer(encoded,len(body),sha,len(files))
    if {name:entry[0] for name,entry in decoded.items()} != files: raise ValueError('round-trip differs')
    note=f'''KEEL {version} — ONE FILE FOR MUSE

Attach this TXT file to your agent. It contains all {len(files)} source/evidence
files, the extractor, synthetic demos, tests and integration instructions.
No ZIP utility, second attachment, paid API or hosted database is required.

MUSE: save the whole attachment as {output.name}. Inspect this short extractor.
Verify: python3 -B {output.name} --verify-only
Extract: python3 -B {output.name} --out /new/isolated/keel-review
The parent directory must exist. Do not use the live repository as output.
Then read MUSE_HANDOFF.md and docs/WORKFLOW.md and run the release checks.
Do not paste, interpret or manually rewrite the encoded payload.

This is a locally tested review candidate. Receipt, integration and deployment
must be established by the receiving agent. Snapshot twins are not connected
to the live pipeline. No application, browser action or consent is authorized.
'''
    header='#!/usr/bin/env python3\n'+''.join('# '+line+'\n' for line in note.splitlines())+'\n'
    reader=(ROOT/'tools/transfer_reader.py').read_text()
    footer=(f'\nSOURCE_BYTES = {len(body)}\nSOURCE_SHA256 = {sha!r}\nSOURCE_FILES = {len(files)}\n'
            f'RELEASE_VERSION = {version!r}\nPAYLOAD_B64 = """\n'+ '\n'.join(textwrap.wrap(encoded,100))+
            '\n"""\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n')
    data=(header+reader+footer).encode()
    compile(data,str(output),'exec')
    with output.open('xb') as stream:stream.write(data)
    return {'file':str(output.absolute()),'version':version,'files':len(files),'bytes':len(data),
            'sha256':hashlib.sha256(data).hexdigest(),'source_sha256':sha,'original_source_bytes':len(body)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',required=True);args=parser.parse_args()
    print(json.dumps(build(args.out),indent=2))
