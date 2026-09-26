#!/usr/bin/env python3
"""Six bounded mutation probes on disposable copies of THIS workbench only.

Not a general mutation score, target-repository runner, or OS sandbox.
No private executor source or production state is loaded.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
PROBES=[
    ("workspace_filter", "keel_maint/retrieval.py", 'require(workspace==snapshot.workspace,"workspace mismatch")', 'require(True,"workspace mismatch")'),
    ("boolean_integer", "keel_maint/contracts.py", 'type(value) is int and 0 <= value <= maximum', 'isinstance(value, int) and 0 <= value <= maximum'),
    ("supply_starvation", "keel_maint/monitoring.py", 'elif observation["actionable"]==0:', 'elif False:'),
    ("protected_candidate", "keel_maint/proposals.py", 'require(not (p==protected or (protected.endswith("/") and p.startswith(protected))),"protected path cannot be changed")', 'require(True,"protected path cannot be changed")'),
    ("snapshot_object_hash", "keel_maint/snapshot.py", 'require(sha(raw)==row["sha256"] and len(raw)==row["bytes"], "snapshot content hash mismatch")', 'require(True, "snapshot content hash mismatch")'),
    ("recipe_false_pass", "keel_maint/recipes.py", '"LOCAL_CHECKS_PASSED" if passed and not failed_other else "LOCAL_CHECKS_NOT_PASSED"', '"LOCAL_CHECKS_PASSED" if True else "LOCAL_CHECKS_NOT_PASSED"'),
]

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True);a=p.parse_args()
    out=Path(a.out);out.mkdir(mode=0o700,exist_ok=False)
    results=[]
    with tempfile.TemporaryDirectory(prefix='keel-maint-probes-') as t:
        for name,file,old,new in PROBES:
            target=Path(t)/name;target.mkdir()
            for folder in ('keel_maint','tests','tools'):
                shutil.copytree(ROOT/folder,target/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            path=target/file;text=path.read_text()
            if text.count(old)!=1:raise RuntimeError('mutation anchor is not unique: '+name)
            path.write_text(text.replace(old,new))
            report_dir=Path(t)/(name+'-results')
            proc=subprocess.run([sys.executable,'-B','tools/run_tests.py','--out',str(report_dir)],
                 cwd=target,env={'PATH':os.defpath,'HOME':str(target),'LC_ALL':'C.UTF-8'},
                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
            report=json.loads((report_dir/'tests.json').read_text())
            detected=(proc.returncode==1 and report['failures']>0 and report['errors']==0 and report['tests_run']>0)
            results.append({'probe':name,'status':'DETECTED' if detected else 'NOT_DETECTED',
                            'failures':report['failures'],'errors':report['errors'],'failed_test_ids':report['failed_test_ids']})
    summary={'schema_version':1,'scope':'six_selected_mutations_not_exhaustive',
             'probes':results,'detected':sum(r['status']=='DETECTED' for r in results),
             'total':len(results),'production_deployed':False}
    (out/'mutation-probes.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary));return 0 if summary['detected']==summary['total'] else 1

if __name__=='__main__':raise SystemExit(main())
