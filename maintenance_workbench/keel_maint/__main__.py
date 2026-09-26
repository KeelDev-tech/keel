"""CLI for local review; intentionally contains no live apply/deploy command."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sqlite3
import sys
from . import __version__
from .contracts import canonical,strict_json,ContractError
from .safeio import read_file,write_new_file,MAX_TOTAL
from . import snapshot
from .retrieval import search
from .proposals import stage
from .recipes import execute
from .review import review
from .monitoring import supply_health,conformance
from .demo import run_demo


def read_json(path): return strict_json(read_file(path,8*1024*1024))
def emit(value):print(canonical(value).decode())

def outside(output, protected):
    out=Path(os.path.abspath(output));base=Path(os.path.abspath(protected))
    if out==base or base in out.parents or out in base.parents:
        raise ContractError("output must be outside the input tree")

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("doctor")
    p=sub.add_parser("demo");p.add_argument("--out",required=True)
    p=sub.add_parser("capture");p.add_argument("--source",required=True);p.add_argument("--config",required=True);p.add_argument("--out",required=True)
    p=sub.add_parser("verify");p.add_argument("--snapshot",required=True)
    p=sub.add_parser("search");p.add_argument("--snapshot",required=True);p.add_argument("--workspace",required=True);p.add_argument("--path",action="append",required=True);p.add_argument("--query",required=True);p.add_argument("--mode",choices=["auto","fts5","tokens"],default="auto")
    p=sub.add_parser("stage");p.add_argument("--snapshot",required=True);p.add_argument("--proposal",required=True);p.add_argument("--out",required=True)
    p=sub.add_parser("recipe");p.add_argument("--snapshot",required=True);p.add_argument("--recipe",required=True);p.add_argument("--out",required=True)
    p=sub.add_parser("review");p.add_argument("--base",required=True);p.add_argument("--candidate",required=True);p.add_argument("--contract",action="append",default=[]);p.add_argument("--out",required=True)
    p=sub.add_parser("supply-health");p.add_argument("--observation",required=True);p.add_argument("--evaluated-at",required=True)
    p=sub.add_parser("conformance");p.add_argument("--events",required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=="doctor":
            db=sqlite3.connect(":memory:")
            try:
                try:db.execute("CREATE VIRTUAL TABLE probe USING fts5(body)");fts=True
                except sqlite3.OperationalError:fts=False
            finally:db.close()
            emit({"version":__version__,"python":sys.version.split()[0],"sqlite":sqlite3.sqlite_version,
                  "posix_safe_io":os.name=="posix" and hasattr(os,"O_NOFOLLOW"),"fts5":fts,
                  "fts_fallback":"literal_token_ranking","runtime_test_executor":"NOT_INCLUDED",
                  "external_actions":"NOT_SUPPORTED","paid_services_required":False});return 0
        if args.command=="demo":
            result=run_demo(args.out);emit(result);return 0 if result["status"]=="DEMO_EXPECTATIONS_MET" else 1
        if args.command=="capture":
            outside(args.out,args.source)
            result=snapshot.capture(args.source,read_json(args.config));result.save(args.out)
            emit({"snapshot_id":result.id,"files":len(result.contents),"status":"CAPTURED_REVIEW_COPY","canonical_state_writes":0});return 0
        if args.command=="verify":
            result=snapshot.load(args.snapshot);emit({"snapshot_id":result.id,"status":"CONTENT_HASHES_MATCH","publisher_identity":"NOT_VERIFIED"});return 0
        if args.command=="search":
            emit(search(snapshot.load(args.snapshot),workspace=args.workspace,paths=args.path,query=args.query,mode=args.mode));return 0
        if args.command=="stage":
            outside(args.out,args.snapshot)
            result,report,patch=stage(snapshot.load(args.snapshot),read_json(args.proposal));result.save(args.out)
            write_new_file(Path(args.out)/"staging.json",canonical(report)+b"\n")
            write_new_file(Path(args.out)/"change.patch",patch.encode());emit(report);return 0
        if args.command=="recipe":
            outside(args.out,args.snapshot)
            result=execute(snapshot.load(args.snapshot),read_json(args.recipe));write_new_file(args.out,canonical(result)+b"\n");emit(result)
            return 0 if result["status"]=="LOCAL_CHECKS_PASSED" else 1
        if args.command=="review":
            outside(args.out,args.base);outside(args.out,args.candidate)
            result=review(snapshot.load(args.base),snapshot.load(args.candidate),args.contract)
            write_new_file(args.out,canonical(result)+b"\n");emit(result)
            return 1 if result["syntax"]["status"]=="FAIL" or "INVALID" in result["json_parsing"].values() else 0
        if args.command=="supply-health":
            result=supply_health(read_json(args.observation),evaluated_at=args.evaluated_at);emit(result)
            return 0 if result["status"]=="BUFFER_AND_SUPPLY_PRESENT" else 1
        result=conformance(read_json(args.events));emit(result)
        return 0 if result["status"]=="NO_FINDINGS_IN_EXPORT" else 1
    except (ContractError,OSError,ValueError,TypeError,KeyError) as exc:
        emit({"status":"ERROR","error_type":type(exc).__name__,"message":str(exc)[:400],
              "canonical_state_writes":0});return 2

if __name__=="__main__":raise SystemExit(main())
