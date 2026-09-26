#!/usr/bin/env python3
"""Build/extract a readable source bundle with per-file lengths and SHA-256.

Extracts only into a new private directory, validates the complete bundle first,
and never executes code. Integrity is not authenticity; compare a trusted digest.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path

try:
    from tools.transfer_reader import MAGIC, MAX_BUNDLE_BYTES, parse_bundle, extract as extract_files
except ModuleNotFoundError:
    from transfer_reader import MAGIC, MAX_BUNDLE_BYTES, parse_bundle, extract as extract_files

LIMIT = MAX_BUNDLE_BYTES


def create(source, output):
    try:
        from tools.package import payload
    except ModuleNotFoundError:
        from package import payload
    files = payload(Path(source))
    bundle = io.BytesIO()
    bundle.write(MAGIC)
    bundle.write((json.dumps({'files': len(files), 'scope': 'source and review artifacts; no private runtime state'}) + '\n').encode())
    for name, body in sorted(files.items()):
        body.decode('utf-8')
        header = {'path': name, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
                  'executable': name.endswith('.sh')}
        bundle.write(b'FILE ' + json.dumps(header, sort_keys=True).encode() + b'\n')
        bundle.write(body)
        bundle.write(b'\n')
    bundle.write(b'END\n')
    raw = bundle.getvalue()
    # Validate before creating an artifact, including total framing overhead.
    parse_bundle(raw)
    with Path(output).open('xb') as stream:
        stream.write(raw)
    return len(files)


def parse(path):
    # Bounded read remains safe if the source grows between stat and read.
    with Path(path).open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    return parse_bundle(raw)


def extract(source, destination):
    files = parse(source)
    extract_files(files, destination, create_parents=True)
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'extract'])
    parser.add_argument('source')
    parser.add_argument('destination')
    args = parser.parse_args()
    count = create(args.source, args.destination) if args.action == 'create' else extract(args.source, args.destination)
    print(json.dumps({'files': count, 'destination': str(Path(args.destination).absolute())}))


if __name__ == '__main__':
    main()
