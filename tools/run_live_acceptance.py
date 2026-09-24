#!/usr/bin/env python3
"""Run isolated synthetic/loopback checks and bind their reports to source hashes.

Browser checks are optional development checks, never a runtime dependency.
An unavailable browser is explicitly UNVERIFIED and is never upgraded to PASS.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.live_inventory import inventory
from keel_agent.io import write_private


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--out', required=True)
    args = p.parse_args(argv); out = Path(args.out).absolute()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    before = inventory()
    commands = [
        ('rehearsal', [sys.executable, '-B', str(ROOT/'tools/rehearse_live.py'), '--out', str(out/'fixture')], out/'fixture/live-rehearsal.json'),
        ('http', [sys.executable, '-B', str(ROOT/'tools/check_live_http.py'), '--out', str(out/'http-raw.json')], out/'http-raw.json')]
    reports = {}; codes = {}
    for name, command, path in commands:
        with (out/(name+'.log')).open('x') as log:
            run = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=False)
        codes[name] = run.returncode
        reports[name] = json.loads(path.read_text())
        reports[name]['raw_report_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    syntax = []
    node = shutil.which('node')
    if node:
        for name in ('keel_live/static/review.js','keel_live/static/live-nav.js','keel_workbench/static/app.js'):
            run = subprocess.run([node,'--check',str(ROOT/name)],capture_output=True,text=True,timeout=20,check=False)
            syntax.append({'file': name, 'exit_code': run.returncode})
        with (out/'browser.log').open('x') as log:
            run = subprocess.run([node,str(ROOT/'tools/check_live_browser.mjs'),'--out',str(out/'browser-raw.json')],
                                 cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=120,check=False)
        codes['browser'] = run.returncode
        reports['browser'] = json.loads((out/'browser-raw.json').read_text())
    else:
        codes['browser'] = 2
        reports['browser'] = {'status':'UNVERIFIED','reason':'Node.js is unavailable; rendered browser checks did not run.',
                              'synthetic':True,'execution_authorized':False}
    reports['browser']['javascript_syntax'] = syntax
    after = inventory()
    unchanged = before == after
    if not unchanged: raise ValueError('source changed during acceptance checks')
    for name, report in reports.items():
        report['release_source_sha256'] = before['sha256']
        report['runner_exit_code'] = codes[name]
        write_private(out/(name+'.json'), report)
    passed = all(codes[n] == 0 and reports[n]['status'] == 'PASS' for n in ('rehearsal','http'))
    passed = passed and all(row['exit_code'] == 0 for row in syntax)
    # A browser product failure is different from an unavailable test runtime.
    passed = passed and reports['browser']['status'] in ('PASS','UNVERIFIED')
    summary = {'status':'PASS' if passed else 'FAIL','source_unchanged':unchanged,
               'release_source_sha256':before['sha256'], 'browser_status':reports['browser']['status'],
               'execution_authorized':False}
    write_private(out/'summary.json',summary);print(json.dumps(summary))
    return 0 if passed else 1


if __name__ == '__main__': raise SystemExit(main())
