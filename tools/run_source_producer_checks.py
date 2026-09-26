#!/usr/bin/env python3
"""Run the unchanged guarded suites and bind results to before/after source hashes."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.source_producer_inventory import inventory


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    out=Path(args.out).absolute()
    if out.exists():
        raise ValueError('new checks directory required')
    before=inventory(ROOT)
    completed=subprocess.run([sys.executable,str(ROOT/'tools/run_release_checks.py'),'--out',str(out)],
                             cwd=ROOT,check=False,timeout=600)
    after=inventory(ROOT)
    outputs={}
    for name in ('summary.json','main/junit.xml','main/pytest.log','maintenance/tests.json','maintenance/tests.log'):
        path=out/name
        if path.is_file():
            outputs[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    passed=completed.returncode==0 and before['source_sha256']==after['source_sha256'] and len(outputs)==5
    binding={'schema':'keel.source_test_binding.v1','status':'PASS' if passed else 'FAIL',
             'before_sha256':before['source_sha256'],'after_sha256':after['source_sha256'],
             'suite_exit_code':completed.returncode,'output_sha256':outputs,
             'inventory':after,'execution_authorized':False}
    with (out/'source-inventory.json').open('x') as stream:
        json.dump(binding,stream,indent=2,sort_keys=True);stream.write('\n')
    print(json.dumps({'source_binding':binding['status'],'source_sha256':after['source_sha256']},indent=2))
    return 0 if passed else 1


if __name__=='__main__':
    raise SystemExit(main())
