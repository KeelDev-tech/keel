"""Synthetic end-to-end maintenance example. No real personal facts or services."""
from __future__ import annotations
from pathlib import Path
from .contracts import canonical,sha,digest
from .snapshot import make_snapshot
from .proposals import stage
from .recipes import execute
from .review import review
from .safeio import write_tree


def inputs():
    config={"schema_version":1,"workspace":"synthetic-demo","files":[
        {"path":"engine.py","kind":"python"},{"path":"helpers.py","kind":"python"},
        {"path":"contracts/request.json","kind":"json"},{"path":"adapter-map.json","kind":"json"},
        {"path":"fixtures/input.json","kind":"json"},{"path":"docs/adapter.md","kind":"text"},
        {"path":"policies/fixture-policy.json","kind":"json"}],
        "protected_paths":["fixtures/","policies/"],
        "dependencies":[["adapter-map.json","contracts/request.json"],["engine.py","adapter-map.json"]]}
    contract={"type":"object","properties":{"sample_id":{"type":"string","minLength":1}},
              "required":["sample_id"],"additionalProperties":False}
    raw={"engine.py":b"from helpers import normalize\n\ndef prepare(value):\n    return normalize(value)\n",
         "helpers.py":b"def normalize(value):\n    return value\n",
         "contracts/request.json":canonical(contract)+b"\n",
         "adapter-map.json":canonical({"sample_id":"input_id"})+b"\n",
         "fixtures/input.json":canonical({"input_id":"synthetic-001","input_revision":2})+b"\n",
         "docs/adapter.md":b"Synthetic adapter mapping. Contract changes require fixture checks. Never an external submission.\n",
         "policies/fixture-policy.json":canonical({"synthetic":True,"external_actions":False})+b"\n"}
    recipe={"schema_version":1,"name":"adapter-compatibility","steps":[
        {"id":"syntax","operation":"syntax","needs":[],"args":{}},
        {"id":"contract","operation":"adapter_contract","needs":["syntax"],"args":{
            "schema_path":"contracts/request.json","mapping_path":"adapter-map.json","input_path":"fixtures/input.json"}},
        {"id":"context","operation":"search","needs":[],"args":{"paths":["docs/adapter.md"],"query":"contract changes"}},
        {"id":"impact","operation":"impact","needs":[],"args":{"paths":["contracts/request.json"]}}],
        "required_checks":["syntax","contract"]}
    return config,raw,recipe


def run_demo(destination: Path | str) -> dict:
    config,raw,recipe=inputs();base=make_snapshot(config,raw)
    changed_raw=dict(raw)
    schema={"type":"object","properties":{"sample_id":{"type":"string","minLength":1},
             "request_revision":{"type":"integer","minimum":1}},
             "required":["sample_id","request_revision"],"additionalProperties":False}
    changed_raw["contracts/request.json"]=canonical(schema)+b"\n"
    upstream=make_snapshot(config,changed_raw)
    proposal={"schema_version":1,"workspace":base.workspace,"base_snapshot":upstream.id,
              "reason":"Map the new synthetic request_revision field using an existing protected input fixture.",
              "changes":[{"path":"adapter-map.json","expected_sha256":sha(upstream.contents["adapter-map.json"]),
                          "replacement":(canonical({"sample_id":"input_id","request_revision":"input_revision"})+b"\n").decode()}]}
    repaired,stage_report,patch=stage(upstream,proposal)
    baseline_result=execute(base,recipe);failure_result=execute(upstream,recipe);repaired_result=execute(repaired,recipe)
    checks={"baseline_fixture_passes":baseline_result["status"]=="LOCAL_CHECKS_PASSED",
            "changed_contract_detected":failure_result["status"]=="LOCAL_CHECKS_NOT_PASSED",
            "repaired_mapping_passes":repaired_result["status"]=="LOCAL_CHECKS_PASSED",
            "protected_input_unchanged":base.contents["fixtures/input.json"]==repaired.contents["fixtures/input.json"],
            "policy_unchanged":base.contents["policies/fixture-policy.json"]==repaired.contents["policies/fixture-policy.json"]}
    report={"schema_version":1,"status":"DEMO_EXPECTATIONS_MET" if all(checks.values()) else "DEMO_FAILED",
            "checks":checks,"external_actions":0,"target_source_executed":False,
            "runtime_tests":"NOT_RUN","production_deployed":False}
    output={"demo-result.json":canonical(report)+b"\n","recipe.json":canonical(recipe)+b"\n",
            "proposal.json":canonical(proposal)+b"\n","change.patch":patch.encode(),
            "baseline-checks.json":canonical(baseline_result)+b"\n","changed-checks.json":canonical(failure_result)+b"\n",
            "repaired-checks.json":canonical(repaired_result)+b"\n","staging.json":canonical(stage_report)+b"\n",
            "upstream-impact.json":canonical(review(base,upstream,["contracts/request.json"]))+b"\n"}
    write_tree(destination,output)
    base.save(Path(destination)/"baseline");upstream.save(Path(destination)/"upstream");repaired.save(Path(destination)/"repaired")
    return report
