"""Static dependency analysis. Parsing is not execution or runtime validation."""
from __future__ import annotations
from collections import deque
from pathlib import Path, PurePosixPath
import os
import subprocess
import sys
import time
from .contracts import strict_json, require, canonical, ContractError
from .safeio import MAX_FILE
from .snapshot import Snapshot


def inspect_python(raw: bytes) -> dict:
    require(type(raw) is bytes and len(raw)<=MAX_FILE,"bounded source bytes required")
    worker=Path(__file__).with_name("_parser_worker.py").resolve()
    # Only this shipped parser can be launched. There is no command/shell input.
    try:
        p=subprocess.run([sys.executable,"-I","-S","-B",str(worker)], input=raw,
                         stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=6,
                         env={"PATH":os.defpath,"LC_ALL":"C.UTF-8"},cwd="/",close_fds=True)
        require(len(p.stdout)<=1024*1024,"parser output limit exceeded")
        info=strict_json(p.stdout)
        require(type(info) is dict and info.get("status") in ("PARSED","INVALID","UNAVAILABLE"),
                "invalid parser response")
        require(info["status"]!="PARSED" or p.returncode==0,"parser exit disagrees with result")
        return info
    except (subprocess.SubprocessError,OSError,ContractError):
        return {"status":"UNAVAILABLE","reason":"bounded_parser_failed"}


def inspect_snapshot(snapshot: Snapshot, maximum_seconds: float = 30.0) -> dict:
    snapshot.validate()
    require(type(maximum_seconds) in (int,float) and 0<=maximum_seconds<=120, "invalid inspection budget")
    deadline=time.monotonic()+maximum_seconds
    result={}
    for row in snapshot.manifest["files"]:
        if row["kind"]=="python":
            result[row["path"]]=(inspect_python(snapshot.contents[row["path"]]) if time.monotonic()<deadline
                                 else {"status":"NOT_RUN_BUDGET_EXCEEDED"})
    valid=sum(v["status"]=="PARSED" for v in result.values())
    return {"status":"PASS" if result and valid==len(result) else ("NOT_APPLICABLE" if not result else "FAIL"),
            "check":"static_python_parsing_only","files_checked":len(result),"parsed":valid,
            "files":result,"runtime_tests":"NOT_RUN"}


def _module(path: str) -> str:
    parts=path[:-3].split("/")
    if parts[-1]=="__init__": parts.pop()
    return ".".join(parts)


def dependency_graph(snapshot: Snapshot, inspection: dict | None = None) -> dict:
    snapshot.validate()
    inspection=inspection if inspection is not None else inspect_snapshot(snapshot)
    modules={}; ambiguous=set()
    for path in inspection["files"]:
        name=_module(path)
        if name in modules: ambiguous.add(name)
        modules[name]=path
    edges=set(); unresolved=set(); dynamic=[]
    for consumer, record in inspection["files"].items():
        if record["status"]!="PARSED": continue
        if record["dynamic_code"]: dynamic.append(consumer)
        for row in record["imports"]:
            target=row["module"]
            if row["level"]:
                package=_module(consumer).split(".")
                if not consumer.endswith("/__init__.py"): package=package[:-1]
                trim=row["level"]-1
                if trim>len(package):
                    unresolved.add(consumer+":relative_import_outside_scope");continue
                package=package[:len(package)-trim]
                target=".".join(package+([target] if target else []))
            candidates=[target]
            if row["style"]=="from": candidates += [target+"."+n if target else n for n in row["names"] if n!="*"]
            found=False
            for candidate in candidates:
                # Importing a submodule also executes package initialization.
                for end in range(1,len(candidate.split("."))+1):
                    name=".".join(candidate.split(".")[:end])
                    dep=modules.get(name)
                    if dep and name not in ambiguous:
                        found=True
                        if dep!=consumer: edges.add((dep,consumer,"static_import"))
            if not found: unresolved.add(target or "relative_root")
    for consumer,dep in snapshot.config["dependencies"]: edges.add((dep,consumer,"declared"))
    return {"nodes":sorted(snapshot.contents),
            "edges":[{"dependency":a,"consumer":b,"origin":c} for a,b,c in sorted(edges)],
            "coverage":"STATIC_AND_DECLARED_ONLY","dynamic_code_paths":sorted(dynamic),
            "unresolved_or_external_imports":sorted(unresolved),"ambiguous_modules":sorted(ambiguous),
            "parse_status":inspection["status"]}


def impact(graph: dict, changed: list[str]) -> dict:
    require(type(changed) is list and all(type(x) is str for x in changed),"changed paths list required")
    require(set(changed)<=set(graph["nodes"]),"changed path outside graph")
    dependents={n:set() for n in graph["nodes"]}
    for e in graph["edges"]: dependents[e["dependency"]].add(e["consumer"])
    witnesses={n:[n] for n in sorted(set(changed))}; pending=deque(witnesses)
    while pending:
        n=pending.popleft()
        for child in sorted(dependents[n]):
            if child not in witnesses:
                witnesses[child]=witnesses[n]+[child];pending.append(child)
    indegree={n:0 for n in witnesses}
    for n in witnesses:
        for child in dependents[n]:
            if child in indegree: indegree[child]+=1
    queue=deque(sorted(n for n,count in indegree.items() if count==0));order=[]
    while queue:
        n=queue.popleft();order.append(n)
        for child in sorted(dependents[n]):
            if child in indegree:
                indegree[child]-=1
                if indegree[child]==0:queue.append(child)
    return {"changed":sorted(set(changed)),"affected":sorted(witnesses),"witness_paths":witnesses,
            "dependency_order":order,"unorderable_nodes":sorted(set(witnesses)-set(order)),
            "coverage":graph["coverage"],"not_affected_is_not_proven":True}
