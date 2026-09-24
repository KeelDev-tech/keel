#!/usr/bin/env python3
"""Enumerate source files, hashes and mechanical review sites. Not a security verdict."""
import argparse
import ast
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def inventory(root=ROOT):
    root=Path(root);files=[];records=[]
    for directory in ('engines','monitors','worker-charter','keel_local','keel_flow','keel_assurance','keel_trust','keel_workflow','maintenance_workbench','tools','tests'):
        for path in (root/directory).rglob('*'):
            if path.is_file() and path.suffix in {'.py','.mjs','.sh','.json'} and '__pycache__' not in path.parts:
                files.append(path)
    files += [p for p in root.iterdir() if p.is_file() and p.suffix in {'.py','.sh'}]
    files += [root/'roles.json',root/'keel.config.json']
    for path in sorted(set(files)):
        # 2026-09-18 (Keel 0.6.0 live port): the live tree is an integration
        # target, not the candidate layout — optional inventory inputs that
        # are absent here (e.g. roles.json) are skipped, never invented.
        if not path.is_file():
            continue
        body=path.read_bytes();record={'file':str(path.relative_to(root)),'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),
                                     'lines':len(body.splitlines()),'kind':path.suffix,'imports':[],
                                     'broad_exception_handlers':[],'legacy_write_sites':[],'subprocess_sites':[]}
        if path.suffix=='.py':
            tree=ast.parse(body,filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node,ast.Import):record['imports'].extend(a.name for a in node.names)
                if isinstance(node,ast.ImportFrom):record['imports'].append(node.module or '')
                if isinstance(node,ast.ExceptHandler) and (node.type is None or ast.unparse(node.type) in {'Exception','BaseException'}):record['broad_exception_handlers'].append(node.lineno)
                if isinstance(node,ast.Call):
                    call=ast.unparse(node.func)
                    if call in {'json.dump','os.replace','os.rename'} or call.endswith(('.write_text','.write_bytes')):record['legacy_write_sites'].append({'line':node.lineno,'call':call})
                    if call.startswith('subprocess.'):record['subprocess_sites'].append({'line':node.lineno,'call':call})
            record['imports']=sorted(set(record['imports']))
        records.append(record)
    hashes={r['file']:r['sha256'] for r in records}
    return {'schema_version':1,'files':records,'source_files':len(records),'python_files':sum(r['kind']=='.py' for r in records),
            'source_sha256':hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'scope':'Mechanical enumeration, syntax parsing and call-site inventory, plus targeted semantic review documented in the gap register. A listed site is not automatically a bug; absence is not proof of safety.'}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',required=True);args=parser.parse_args()
    result=inventory();Path(args.out).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in {'files','scope'}},indent=2))
