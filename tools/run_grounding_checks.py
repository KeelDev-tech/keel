#!/usr/bin/env python3
"""Run unchanged guarded suites and bind their exact outputs to the 0.10 source."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.grounding_inventory import inventory, read_file

OUTPUTS = frozenset(('summary.json', 'main/junit.xml', 'main/pytest.log',
                     'maintenance/tests.json', 'maintenance/tests.log'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).absolute()
    if out.exists():
        raise ValueError('new result directory required')
    before = inventory()
    run = subprocess.run([sys.executable, '-B', str(ROOT/'tools/run_release_checks.py'), '--out', str(out)],
                         cwd=ROOT, timeout=600, check=False)
    after = inventory()
    outputs = {name: hashlib.sha256(read_file(out, name)).hexdigest() for name in sorted(OUTPUTS)}
    report = {'schema': 'keel.grounding.test_binding.v1',
              'status': 'PASS' if run.returncode == 0 and before == after else 'FAIL',
              'before_sha256': before['sha256'], 'after_sha256': after['sha256'],
              'inventory': after, 'suite_exit_code': run.returncode, 'output_sha256': outputs,
              'execution_authorized': False}
    with (out/'source-binding.json').open('x') as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True)+'\n')
    print(json.dumps({key: report[key] for key in ('status', 'before_sha256', 'after_sha256')}))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
