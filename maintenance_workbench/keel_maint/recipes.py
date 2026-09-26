"""Finite declarative workflows. No shell, eval, target imports, or plugins."""
from __future__ import annotations
import sys
import time
from . import __version__
from .contracts import keys, version, identifier, require, digest, strict_json, ContractError
from .analysis import inspect_snapshot, dependency_graph, impact
from .retrieval import search, AnalysisCache
from .schema import validate
from .monitoring import supply_health, conformance
from .snapshot import Snapshot

OPERATIONS={"syntax","impact","search","validate_contract","adapter_contract","supply_health","conformance"}
CHECKS={"syntax","validate_contract","adapter_contract"}


def validate_recipe(recipe: dict) -> list[dict]:
    keys(recipe,{"schema_version","name","steps","required_checks"});version(recipe);identifier(recipe["name"])
    require(type(recipe["steps"]) is list and 0<len(recipe["steps"])<=32,"bounded recipe steps required")
    rows={}
    for step in recipe["steps"]:
        keys(step,{"id","operation","needs","args"});identifier(step["id"])
        require(step["id"] not in rows,"duplicate recipe step")
        require(type(step["operation"]) is str and step["operation"] in OPERATIONS,"operation not allowed")
        require(type(step["needs"]) is list and all(type(n) is str for n in step["needs"]) and len(set(step["needs"]))==len(step["needs"]),"invalid prerequisites")
        require(type(step["args"]) is dict,"step args must be an object")
        rows[step["id"]]=step
    require(type(recipe["required_checks"]) is list and recipe["required_checks"] and all(type(n) is str for n in recipe["required_checks"]),"required checks must be nonempty")
    require(len(set(recipe["required_checks"]))==len(recipe["required_checks"]),"duplicate required check")
    require(set(recipe["required_checks"])<=set(rows),"missing required check")
    require(all(rows[n]["operation"] in CHECKS for n in recipe["required_checks"]),"informational step cannot be a required check")
    for row in rows.values():require(set(row["needs"])<=set(rows),"unknown prerequisite")
    order=[];done=set()
    while len(order)<len(rows):
        available=sorted(n for n,r in rows.items() if n not in done and set(r["needs"])<=done)
        require(bool(available),"recipe dependency cycle")
        for n in available:order.append(rows[n]);done.add(n)
    return order


def execute(snapshot: Snapshot, recipe: dict, cache: AnalysisCache | None = None) -> dict:
    snapshot.validate();order=validate_recipe(recipe)
    results={};parsed=None;graph=None;deadline=time.monotonic()+90
    def document(path):
        require(type(path) is str and path in snapshot.contents,"document outside snapshot")
        return strict_json(snapshot.contents[path])
    for step in order:
        name=step["id"];op=step["operation"];args=step["args"]
        if time.monotonic()>=deadline:
            results[name]={"status":"BLOCKED","reason":"run_budget_exhausted"};continue
        if any(results[n]["status"] not in {"PASS","DONE"} for n in step["needs"]):
            results[name]={"status":"BLOCKED","reason":"prerequisite_not_satisfied"};continue
        try:
            if op=="syntax":
                keys(args,set());parsed=parsed if parsed is not None else inspect_snapshot(snapshot);result=parsed
            elif op=="impact":
                keys(args,{"paths"})
                parsed=parsed if parsed is not None else inspect_snapshot(snapshot)
                graph=graph if graph is not None else dependency_graph(snapshot,parsed)
                result={"status":"DONE","result":impact(graph,args["paths"])}
            elif op=="search":
                keys(args,{"paths","query"})
                key=digest({"workspace":snapshot.workspace,"snapshot":snapshot.id,"args":args,
                            "version":__version__,"python":list(sys.version_info[:3]),"op":"search-v1"})
                found=cache.get(key) if cache else None
                if found is None:
                    found=search(snapshot,workspace=snapshot.workspace,paths=args["paths"],query=args["query"])
                    if cache:cache.put(key,found)
                result={"status":"DONE","result":found}
            elif op=="validate_contract":
                keys(args,{"schema_path","value_path"})
                errors=validate(document(args["value_path"]),document(args["schema_path"]))
                result={"status":"FAIL" if errors else "PASS","errors":errors,
                        "check":"bounded_contract_fixture_not_live_provider"}
            elif op=="adapter_contract":
                keys(args,{"schema_path","mapping_path","input_path"})
                mapping=document(args["mapping_path"]);source=document(args["input_path"])
                require(type(mapping) is dict and 0<len(mapping)<=200 and all(type(k) is str and type(v) is str for k,v in mapping.items()),"flat field mapping required")
                require(type(source) is dict and set(mapping.values())<=set(source),"mapped input field missing")
                output={key:source[field] for key,field in mapping.items()}
                errors=validate(output,document(args["schema_path"]))
                result={"status":"FAIL" if errors else "PASS","errors":errors,
                        "output_sha256":digest(output),"check":"declared_mapping_fixture_not_production_connector"}
            elif op=="supply_health":
                keys(args,{"observation_path","evaluated_at"})
                result={"status":"DONE","result":supply_health(document(args["observation_path"]),evaluated_at=args["evaluated_at"])}
            else:
                keys(args,{"events_path"});result={"status":"DONE","result":conformance(document(args["events_path"]))}
        except (ContractError,ValueError,TypeError,KeyError) as exc:
            result={"status":"FAIL","error_type":type(exc).__name__,"reason":"invalid_or_unsupported_step_input"}
        results[name]=result
    passed=all(results[n]["status"]=="PASS" for n in recipe["required_checks"])
    failed_other=any(r["status"] in {"FAIL","BLOCKED"} for r in results.values())
    return {"schema_version":1,"kind":"keel-maint.recipe-result","workspace":snapshot.workspace,
            "snapshot_id":snapshot.id,"recipe_sha256":digest(recipe),"recipe_name":recipe["name"],
            "workbench_version":__version__,"status":"LOCAL_CHECKS_PASSED" if passed and not failed_other else "LOCAL_CHECKS_NOT_PASSED",
            "steps":results,"runtime_tests":"NOT_RUN_TARGET_CODE_EXECUTION_NOT_SUPPORTED",
            "deployment_authorized":False,"external_actions":0,"canonical_state_writes":0}
