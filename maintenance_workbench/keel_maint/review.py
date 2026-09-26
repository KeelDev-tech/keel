"""Review evidence is a local observation, never an approval or provider receipt."""
from __future__ import annotations
from .snapshot import Snapshot,changes
from .analysis import inspect_snapshot,dependency_graph,impact
from .schema import compare
from .contracts import strict_json, require


def review(base: Snapshot,candidate: Snapshot,contract_paths: list[str] | None=None) -> dict:
    diff=changes(base,candidate)
    inspection=inspect_snapshot(candidate)
    graph=dependency_graph(candidate,inspection)
    # Retain old dependency edges as well: removing an import must not hide impact.
    old=dependency_graph(base)
    unique={(e["dependency"],e["consumer"],e["origin"]):e for e in graph["edges"]+old["edges"]}
    graph["edges"]=[unique[k] for k in sorted(unique)]
    json_checks={}
    for row in candidate.manifest["files"]:
        if row["kind"]=="json":
            try:strict_json(candidate.contents[row["path"]]);json_checks[row["path"]]="PARSED"
            except (ValueError,TypeError):json_checks[row["path"]]="INVALID"
    contracts={}
    for path in contract_paths or []:
        require(path in candidate.contents,"contract path outside snapshot")
        contracts[path]=compare(strict_json(base.contents[path]),strict_json(candidate.contents[path]))
    return {"schema_version":1,"kind":"keel-maint.review","workspace":base.workspace,
            **diff,"syntax":inspection,"json_parsing":json_checks,
            "impact":impact(graph,diff["changed_paths"]),"dependency_graph":graph,
            "contract_changes":contracts,"review_decision":"NOT_ISSUED",
            "runtime_tests":"NOT_RUN_TARGET_CODE_EXECUTION_NOT_SUPPORTED",
            "live_integration":"NOT_PERFORMED","canonical_state_writes":0}
