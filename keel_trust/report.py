"""Combine current evidence with pipeline gates in a read-only working view."""
from keel_flow.board import build as flow_board
from . import __version__
from .common import envelope, clone, digest, unique, keys, require, hexdigest
from .evidence import evaluate
from .decisions import operator_brief, research_plan
from .twin import capture


def build(document, *, now):
    current = envelope(document, {"flow_export", "evidence_export", "artifact_bindings", "question_costs",
                                  "research_checks", "decision_budget_minutes", "research_budget_minutes"}, now=now)
    require(document["workspace_id"] == document["evidence_export"]["workspace_id"], "evidence workspace mismatch")
    evidence = evaluate(document["evidence_export"], now=now)
    original = flow_board(document["flow_export"], now=now)  # validate original before deriving any view
    working = clone(document["flow_export"])
    artifacts = {r["artifact_id"]: r for r in document["evidence_export"]["artifacts"]}
    invalid = set(evidence["invalidated_artifact_ids"])
    roles = {r["role_id"]: r for r in working["leads"]}
    bindings = {}
    unique(document["artifact_bindings"], "role_id")
    for binding in document["artifact_bindings"]:
        keys(binding, {"role_id", "artifact_id", "artifact_revision", "artifact_sha256", "packet_dependency_hash"})
        require(binding["role_id"] in roles and binding["artifact_id"] in artifacts, "unknown artifact/role binding")
        hexdigest(binding["artifact_sha256"]); hexdigest(binding["packet_dependency_hash"])
        bindings[binding["role_id"]] = binding
    blocked = []
    for rid, role in roles.items():
        binding = bindings.get(rid); reasons = []
        if binding is None:
            reasons.append("MATERIAL_BINDING_MISSING")
        else:
            artifact = artifacts[binding["artifact_id"]]
            if binding["artifact_id"] in invalid: reasons.append("MATERIAL_EVIDENCE_INVALID")
            if (binding["artifact_revision"] != artifact["revision"] or binding["artifact_sha256"] != digest(artifact)
                    or binding["packet_dependency_hash"] != role["packet_dependency_hash"]):
                reasons.append("MATERIAL_BINDING_CHANGED")
        if reasons:
            blocked.append({"role_id": rid, "reasons": reasons})
            role["packet_present"] = False  # in-memory hold; never edit canonical queue
    blocked_ids = {row["role_id"] for row in blocked}
    excluded_releases = [r["release_id"] for r in working["releases"] if blocked_ids.intersection(r["role_ids"])]
    working["releases"] = [r for r in working["releases"] if r["release_id"] not in excluded_releases]
    for row in working["question_dependencies"]:
        if row["role_id"] in blocked_ids: row["other_gates_clear"] = False
    if not current or not evidence["current"]: working["complete"] = False
    flow = flow_board(working, now=now)
    decisions = operator_brief(flow, document["question_costs"], budget_minutes=document["decision_budget_minutes"], now=now)
    research = research_plan(document["research_checks"], budget_minutes=document["research_budget_minutes"], now=now)
    if not flow["readiness"]["current_estimate_verified"]:
        research = {"status": "REFRESH_EXPORT", "selected": [], "minutes": 0,
                    "remaining_minutes": document["research_budget_minutes"], "http_requests": 0, "execution_authorized": False}
    model = capture(working, now=now)
    return {"schema_version": 1, "version": __version__, "workspace_id": document["workspace_id"],
            "evaluated_at": now.isoformat(), "input_sha256": digest(document), "original_flow_sha256": digest(document["flow_export"]),
            "status": "UNVERIFIED" if not flow["readiness"]["current_estimate_verified"] else
                      "NEEDS_REVIEW" if blocked or flow["actions"] else "NO_ALERTS_IN_EXPORT",
            "original_executable_estimate": original["readiness"]["executable_ready"],
            "evidence": evidence, "material_holds": blocked, "excluded_release_ids": excluded_releases,
            "flow": flow, "operator_brief": decisions, "research": research, "twin": model,
            "effects": {"queue_writes": 0, "http_requests": 0, "browser_actions": 0, "messages_sent": 0},
            "execution_authorized": False, "boundary": "derived view only; live host must enforce material holds through canonical gates"}


def markdown(report):
    safe = lambda value: str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    brief, flow = report["operator_brief"], report["flow"]
    lines = ["# Keel evidence and pipeline brief", "", "Status: **" + report["status"] + "**",
             "Observed at evaluation: " + report["evaluated_at"], "",
             "| Measure | Observation |", "|---|---|",
             f"| Original executable estimate | {report['original_executable_estimate']} |",
             f"| After evidence gates | {flow['readiness']['executable_ready']} |",
             f"| Current export | {flow['readiness']['current_estimate_verified']} |",
             f"| Material holds | {len(report['material_holds'])} |",
             f"| Supply forecast | {flow['forecast']['state']} |",
             f"| Decision minutes proposed | {brief['minutes']} |",
             f"| Research minutes proposed | {report['research']['minutes']} |", "", "## Decisions for the owner", ""]
    lines += [f"- {safe(row['owner'])}: {safe(row['question'])} ({row['estimated_minutes']} minutes; reply required)."
              for row in brief["selected"]] or ["No current, budgeted decisions selected."]
    lines += ["", "## Evidence requiring review", ""]
    lines += [f"- {safe(row['role_id'])}: {safe(', '.join(row['reasons']))}." for row in report["material_holds"]] or ["No invalid material bindings in this export."]
    lines += ["", "## Operational checks", ""]
    lines += [f"- {safe(row['action'])}: {safe(row['reason'])}." for row in flow["actions"]]
    lines += ["", "The twin uses supplied snapshots. It has no live connection or demonstrated forecast calibration.",
              "This report proposes reviews; it sends no messages, supplies no personal answers and authorizes no execution.", ""]
    return "\n".join(lines)
