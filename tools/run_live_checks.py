#!/usr/bin/env python3
"""Bind the unchanged guarded regression runners to the entire release source."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.live_inventory import inventory


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--out', required=True)
    args = p.parse_args(argv); out = Path(args.out).absolute()
    if out.exists(): raise ValueError('new result directory required')
    before = inventory()
    run = subprocess.run([sys.executable, '-B', str(ROOT/'tools/run_release_checks.py'), '--out', str(out)],
                         cwd=ROOT, timeout=600, check=False)
    after = inventory()
    outputs = {n: hashlib.sha256((out/n).read_bytes()).hexdigest() for n in
               ('summary.json', 'main/junit.xml', 'main/pytest.log', 'maintenance/tests.json', 'maintenance/tests.log')}
    report = {'schema': 'keel.live.test_binding.v1',
              'status': 'PASS' if run.returncode == 0 and before == after else 'FAIL',
              'before_sha256': before['sha256'], 'after_sha256': after['sha256'],
              'inventory': after, 'suite_exit_code': run.returncode, 'output_sha256': outputs,
              'execution_authorized': False}
    (out/'source-binding.json').write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
    print(json.dumps({k: report[k] for k in ('status','before_sha256','after_sha256')}))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__': raise SystemExit(main())
