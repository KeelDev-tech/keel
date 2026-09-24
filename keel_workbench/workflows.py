"""Bounded review workflows composed from existing Keel checks."""
from keel_trust.common import keys, require, integer, number, text, digest
from keel_trust.report import build as trust_report
from keel_trust.decisions import operator_brief
from keel_trust.twin import capture, simulate
from keel_trust.replay import run as replay
from tools.make_flow_demo import make_snapshot, NOW

CATALOG = [
    {"id": "source-repair", "name": "Repair the evidence supply", "category": "Infrastructure",
     "description": "Group missing source records into owned adapter tasks. Keep actual human decisions separate.", "scope": "all_or_selected"},
    {"id": "material-review", "name": "Review application materials", "category": "Application quality",
     "description": "Trace changed evidence and packet bindings before materials are reused.", "scope": "all_or_selected"},
    {"id": "daily-brief", "name": "Plan the next review session", "category": "Operator workflow",
     "description": "Build a time-budgeted decision brief with current blockers and exact unanswered questions.", "scope": "whole_workspace"},
    {"id": "incident-replay", "name": "Replay failure cases", "category": "Regression checks",
     "description": "Check nominal readiness, stale exports and changed materials using synthetic incidents.", "scope": "synthetic_fixtures"},
    {"id": "twin-scenario", "name": "Test a pipeline scenario", "category": "What-if analysis",
     "description": "Compare supply delay, consumption speed, rate holds and packet invalidation with a recorded baseline.", "scope": "whole_workspace"},
]


def request(value, role_ids):
    keys(value, {"request_id", "workflow_id", "snapshot_sha256", "role_ids", "options"})
    text(value["request_id"]); require(len(value["request_id"]) <= 128, "request ID too long")
    require(value["workflow_id"] in {r["id"] for r in CATALOG}, "workflow not supported")
    from keel_trust.common import hexdigest, strings
    hexdigest(value["snapshot_sha256"]); strings(value["role_ids"], maximum=2000)
    require(set(value["role_ids"]) <= set(role_ids), "unknown role selection")
    require(type(value["options"]) is dict, "workflow options object required")
    if value["workflow_id"] in {"daily-brief", "incident-replay", "twin-scenario"}:
        require(not value["role_ids"], "this workflow uses whole-workspace or fixture scope")


def execute(command, document, view, *, now):
    identifier = command["workflow_id"]; options = command["options"]
    wanted = set(command["role_ids"] or [r["role_id"] for r in view["roles"]])
    if identifier == "source-repair":
        keys(options, set()); tasks = []
        for group in view["source_inventory"]["groups"]:
            affected = sorted(wanted.intersection(group["role_ids"]))
            if affected:
                tasks.append({**group, "role_ids": affected, "affected_roles": len(affected),
                              "next_step": "Locate the authentic decision record in the authorized review surface." if group["responsibility"] == "human"
                                           else "Export the actual source record through the canonical adapter; preserve its revision and validity."})
        result = {"tasks": tasks, "system_tasks": sum(t["responsibility"] == "system" for t in tasks),
                  "human_tasks": sum(t["responsibility"] == "human" for t in tasks),
                  "status": "REPAIR_REQUIRED" if tasks else "NO_SOURCE_GAPS_IN_EXPORT"}
    elif identifier == "material-review":
        keys(options, set())
        if document["trust"] is None:
            result = {"status": "TRUST_EXPORT_REQUIRED", "roles": [], "artifacts": [], "claims": []}
        else:
            trust = trust_report(document["trust"], now=now)
            result = {"status": "REVIEW_REQUIRED" if trust["material_holds"] else "MATERIAL_CHECKS_RECORDED",
                      "roles": [r for r in view["roles"] if r["role_id"] in wanted],
                      "material_holds": [r for r in trust["material_holds"] if r["role_id"] in wanted],
                      "artifacts": trust["evidence"]["artifacts"], "claims": trust["evidence"]["claims"],
                      "boundary": "Artifact graph is workspace-wide; role rows are filtered. No material is rewritten or approved."}
    elif identifier == "daily-brief":
        keys(options, {"budget_minutes"}); integer(options["budget_minutes"], maximum=240)
        if document["trust"] is None:
            brief = {"status": "TRUST_EXPORT_REQUIRED", "selected": [], "minutes": 0}
        else:
            trust = trust_report(document["trust"], now=now)
            brief = operator_brief(trust["flow"], document["trust"]["question_costs"], budget_minutes=options["budget_minutes"], now=now)
        result = {"status": ("TRUST_EXPORT_REQUIRED" if document["trust"] is None else
                             "BRIEF_READY" if view["current"] else "REFRESH_EXPORT"), "decisions": brief,
                  "source_tasks": view["source_inventory"]["groups"], "operating_actions": view["actions"],
                  "holds": view["holds"]["rows"], "answers_generated": 0}
    elif identifier == "incident-replay":
        keys(options, set()); cases = []
        for label, change, expected in [
            ("nominal-ready-is-not-executable", lambda x: None, {"executable_ready": 1, "held_applications": 1}),
            ("missing-complete-export", lambda x: x.update(complete=False), {"estimate_current": False, "forecast_state": "UNKNOWN"}),
            ("changed-packet-dependency", lambda x: x["leads"][0]["dependencies"].update(answers="changed"), {"executable_ready": 0})]:
            source = make_snapshot(); change(source)
            cases.append({"schema_version": 1, "case_id": label, "incident_ref": "fixture://"+label,
                          "as_of": NOW.isoformat(), "flow_export": source, "expected": expected})
        result = {**replay(cases), "mode": "SYNTHETIC_REGRESSION", "live_data_tested": False}
    else:
        keys(options, {"delay_minutes", "capacity_multiplier", "rate_hold_source", "invalidate_role"})
        integer(options["delay_minutes"], maximum=1440); number(options["capacity_multiplier"], minimum=.25, maximum=4)
        for field in ("rate_hold_source", "invalidate_role"):
            require(options[field] is None or type(options[field]) is str, "scenario selection must be a string or null")
        # Prefer the evidence-gated working view. Never infer missing assurance or source records.
        model = trust_report(document["trust"], now=now)["twin"] if document["trust"] is not None else capture(document["flow"], now=now)
        changes = []
        if options["capacity_multiplier"] != 1:
            changes.append({"kind": "CAPACITY_MULTIPLIER", "target": "capacity", "value": options["capacity_multiplier"]})
        if options["delay_minutes"]:
            require(bool(model["export"]["releases"]), "no forecast releases available to delay")
            require(len(model["export"]["releases"]) <= 96, "scenario requires a smaller release set")
            changes += [{"kind": "RELEASE_DELAY_SECONDS", "target": r["release_id"], "value": options["delay_minutes"]*60}
                        for r in model["export"]["releases"]]
        if options["rate_hold_source"]:
            changes.append({"kind": "RATE_HOLD", "target": options["rate_hold_source"], "value": True})
        if options["invalidate_role"]:
            changes.append({"kind": "INVALIDATE_PACKET", "target": options["invalidate_role"], "value": "answers"})
        scenario = {"schema_version": 1, "scenario_id": command["request_id"], "base_sha256": model["source_sha256"],
                    "assumption_ref": "operator-scenario:"+command["request_id"], "changes": changes}
        result = {**simulate(model, scenario), "status": "SIMULATION_ONLY",
                  "qualification": "Flow/material model only; assurance and seven-source qualification remain separate and unchanged.",
                  "review_checks_passed_before_scenario": view["counts"]["review_checks_passed"]}
    return {"schema_version": 1, "workflow_id": identifier, "request_id": command["request_id"],
            "workspace_id": document["workspace_id"], "snapshot_sha256": digest(document),
            "evaluated_at": now.isoformat(), "synthetic": document["synthetic"], "current_export": view["current"],
            "result": result, "execution_authorized": False, "effects": view["effects"]}
