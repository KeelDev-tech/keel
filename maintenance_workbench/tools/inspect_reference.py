#!/usr/bin/env python3
"""Read-only analysis of an explicit KEEL review-copy allowlist; no target imports."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from keel_maint.contracts import strict_json,canonical,digest,sha
from keel_maint.safeio import read_file,write_tree
from keel_maint.snapshot import capture
from keel_maint.analysis import inspect_snapshot,dependency_graph,impact
from keel_maint.retrieval import search
from keel_maint.__main__ import outside

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True);p.add_argument('--config',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();outside(a.out,a.source)
    config=strict_json(read_file(a.config));s=capture(a.source,config)
    inspection=inspect_snapshot(s);graph=dependency_graph(s,inspection)
    search_paths=[x['path'] for x in config['files'] if x['kind']=='text']
    context=search(s,workspace=s.workspace,paths=search_paths,query='approval uncertainty receipt evidence')
    observed_again=capture(a.source,config)
    report={'schema_version':1,'scope':'STATIC_ALLOWLIST_REVIEW_NOT_PRIVATE_EXECUTOR',
            'snapshot_id':s.id,'files_captured':len(s.contents),'python_files':inspection['files_checked'],
            'python_parsed':inspection['parsed'],'parse_status':inspection['status'],
            'static_and_declared_edges':len(graph['edges']),
            'dynamic_code_paths':graph['dynamic_code_paths'],
            'source_allowlist_unchanged':s.id==observed_again.id,
            'runtime_tests':'NOT_RUN','provider_validation':'NOT_RUN',
            'deployment':'NOT_PERFORMED','authorization':'NONE'}
    write_tree(a.out,{'reference-analysis.json':canonical(report)+b'\n',
                     'dependency-graph.json':canonical(graph)+b'\n',
                     'static-parsing.json':canonical(inspection)+b'\n',
                     'scoped-retrieval.json':canonical(context)+b'\n'})
    s.save(Path(a.out)/'snapshot')
    print(canonical(report).decode())
    return 0 if inspection['status']=='PASS' and report['source_allowlist_unchanged'] else 1

if __name__=='__main__':raise SystemExit(main())
