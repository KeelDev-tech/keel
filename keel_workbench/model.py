"""Workspace projection over Keel's existing reducers; never execution authority."""
from datetime import datetime, timezone
from keel_flow.board import build as flow_board
from keel_trust.report import build as trust_report
from keel_agent.revisions import COMPONENTS, export_revisions
from keel_trust.common import clone, keys, version, require, text, boolean, unique, digest, clock, ContractError

SCHEMA = "keel.workbench.snapshot.v1"
MAX_ROLES = 2000
LANES = ("Operations", "Sales", "Technology", "General")


def utcnow():
    return datetime.now(timezone.utc)


def adapt_body(body, workspace_id, *, synthetic=False, labels=None, revision_sources=None):
    """Map the public v0.7 snapshot body without inventing observations or approvals."""
    keys(body, {"flow", "assurance", "trust"})
    return {"schema_version": 1, "schema": SCHEMA, "workspace_id": workspace_id, "synthetic": synthetic,
            **clone(body), "labels": clone(labels or []), "revision_sources": clone(revision_sources)}


def validate(document, *, workspace_id, synthetic):
    value = clone(document)
    keys(value, {"schema_version", "schema", "workspace_id", "synthetic", "flow", "assurance", "trust", "labels", "revision_sources"})
    version(value); require(value["schema"] == SCHEMA, "unsupported workspace schema")
    text(value["workspace_id"]); boolean(value["synthetic"])
    require(value["workspace_id"] == workspace_id, "workspace mismatch")
    require(value["synthetic"] is synthetic, "synthetic and operational workspaces must stay separate")
    require(type(value["flow"]) is dict and type(value["flow"].get("leads")) is list, "flow export required")
    require(len(value["flow"]["leads"]) <= MAX_ROLES, "workspace exceeds 2000-role limit")
    unique(value["flow"]["leads"], "role_id")
    roles = {r["role_id"]: r for r in value["flow"]["leads"]}
    unique(value["labels"], "role_id")
    for row in value["labels"]:
        keys(row, {"role_id", "company", "title", "lane"})
        require(row["role_id"] in roles, "label references unknown role")
        for field in ("company", "title"): text(row[field]); require(len(row[field]) <= 200, "label too long")
        require(row["lane"] in LANES, "unsupported career lane")
    if value["trust"] is not None:
        require(type(value["trust"]) is dict and value["trust"].get("workspace_id") == workspace_id, "trust workspace mismatch")
        require(digest(value["trust"].get("flow_export")) == digest(value["flow"]), "trust/flow binding mismatch")
    if value["assurance"] is not None:
        require(type(value["assurance"]) is dict, "assurance must be an object or null")
        require(value["assurance"].get("export_sha256") == digest(value["flow"]), "assurance/flow binding mismatch")
    sources = value["revision_sources"]
    if sources is not None:
        require(type(sources) is dict and sources.get("workspace_id") == workspace_id, "revision-source workspace mismatch")
        require(type(sources.get("roles")) is list and len(sources["roles"]) <= MAX_ROLES, "bounded source roles required")
        unique(sources["roles"], "role_id")
        for row in sources["roles"]:
            require(row["role_id"] in roles and row.get("application_id") == roles[row["role_id"]]["identity"], "revision-source role binding mismatch")
    return value


def source_inventory(document, *, attachment_root, now):
    """Use v0.7's actual source checker; absent exports remain adapter work."""
    normalized = document["revision_sources"]
    if normalized is not None and attachment_root is None:
        for row in normalized["roles"]:
            source = (row.get("sources") or {}).get("attachments")
            record = source.get("record") if isinstance(source, dict) else None
            require(not (isinstance(record, dict) and record.get("files")),
                    "attachment files require a host-configured --attachment-root")
    observed = export_revisions(normalized, attachment_root=attachment_root, now=now) if normalized is not None else None
    by_role = {row["scope"]["role_id"]: row for row in observed["roles"]} if observed else {}
    rows = []; groups = {}
    for lead in document["flow"]["leads"]:
        rid = lead["role_id"]; supplied = by_role.get(rid)
        components = supplied["components"] if supplied else {
            name: {"component": name, "status": "MISSING", "revision": None, "reason": "source_not_exported",
                   "owner": "export_adapter", "responsibility": "system", "source_ref": None} for name in COMPONENTS}
        ready = bool(supplied and supplied["envelope_inputs_ready"])
        mismatches = [name for name, record in components.items() if record["status"] == "READY"
                      and record["revision"] != lead["dependencies"].get(name)]
        bound = bool(ready and not mismatches)
        rows.append({"role_id": rid, "components": components, "inputs_complete": ready,
                     "packet_bound": bound, "binding_mismatches": mismatches})
        for name in mismatches:
            key = (name, "packet_source_revision_mismatch", "system")
            group = groups.setdefault(key, {"component": name, "reason": key[1], "responsibility": "system",
                                           "owner": "export_adapter", "role_ids": []})
            group["role_ids"].append(rid)
        for name, record in components.items():
            if record["status"] == "READY": continue
            group_key = (name, record.get("reason", "unknown"), record.get("responsibility", "system"))
            group = groups.setdefault(group_key, {"component": name, "reason": group_key[1], "responsibility": group_key[2],
                                                   "owner": record.get("owner", "export_adapter"), "role_ids": []})
            group["role_ids"].append(rid)
        if supplied and not ready and all(r["status"] == "READY" for r in components.values()):
            key = ("snapshot", "source_snapshot_invalid", "system")
            group = groups.setdefault(key, {"component": "snapshot", "reason": key[1], "responsibility": "system",
                                           "owner": "export_adapter", "role_ids": []})
            group["role_ids"].append(rid)
    return {"configured": normalized is not None, "rows": rows, "groups": list(groups.values()),
            "complete_roles": sum(r["inputs_complete"] for r in rows), "source_authenticity_verified": False}


def project(document, *, attachment_root=None, now):
    clock(now)
    try:
        flow = flow_board(document["flow"], now=now, assurance=document["assurance"])
    except ContractError as error:
        if str(error) != "assurance lead digest mismatch": raise
        # Keel 0.7 may retain a material review across refreshed observations.
        # Only the canonical host's stable-scope evaluator can qualify that case.
        from keel_assurance.core import evaluate
        evaluate(document["assurance"], now=now)
        flow = flow_board(document["flow"], now=now)
        flow["assurance"]["state"] = "HOST_SCOPE_CHECK_REQUIRED"
        for row in flow["readiness"]["rows"]:
            row["assurance_checks_passed"] = False
            row["executable"] = False
            row["reasons"].append("assurance_host_scope_check_required")
    trust = trust_report(document["trust"], now=now) if document["trust"] is not None else None
    sources = source_inventory(document, attachment_root=attachment_root, now=now)
    source_rows = {r["role_id"]: r for r in sources["rows"]}
    trows = {r["role_id"]: r for r in trust["flow"]["readiness"]["rows"]} if trust else {}
    labels = {r["role_id"]: r for r in document["labels"]}
    leads = {r["role_id"]: r for r in document["flow"]["leads"]}
    current = bool(flow["readiness"]["current_estimate_verified"] and
                   (trust is None or trust["flow"]["readiness"]["current_estimate_verified"]))
    roles = []
    for row in flow["readiness"]["rows"]:
        rid = row["role_id"]; lead = leads[rid]; material = trows.get(rid)
        reasons = list(row["reasons"])
        if row["assurance_checks_passed"] is not True: reasons.append("assurance_not_passed")
        if material is None: reasons.append("trust_not_configured")
        elif not material["executable"]: reasons.extend("material:"+r for r in material["reasons"])
        if not source_rows[rid]["inputs_complete"]: reasons.append("source_records_incomplete")
        if source_rows[rid]["binding_mismatches"]: reasons.append("source_revision_binding_mismatch")
        if not current: reasons.append("refresh_current_export")
        qualified = bool(current and row["executable"] and row["assurance_checks_passed"] is True and
                         material and material["executable"] and source_rows[rid]["packet_bound"])
        label = labels.get(rid, {"company": "Not provided", "title": rid, "lane": "General"})
        roles.append({"role_id": rid, "company": label["company"], "title": label["title"], "lane": label["lane"],
                      "fit_score": lead["fit_score"], "queue_status": lead["status"],
                      "base_checks_passed": row["base_executable"], "review_checks_passed": qualified,
                      "status": "REVIEW_CHECKS_PASSED" if qualified else "REVIEW_REQUIRED",
                      "reasons": sorted(set(reasons)), "sources": source_rows[rid]["components"],
                      "execution_authorized": False})
    questions = [{k: r[k] for k in ("group_id", "question", "options", "classification", "roles_progressed", "urgent", "operator_reply_required")}
                 for r in flow["questions"]["groups"]]
    return {"schema_version": 1, "workspace_id": document["workspace_id"], "synthetic": document["synthetic"],
            "snapshot_sha256": digest(document), "source_revision": document["flow"]["source_revision"],
            "observed_at": document["flow"]["observed_at"], "evaluated_at": now.isoformat(),
            "current": current, "status": "REVIEW_REQUIRED" if current else "UNVERIFIED",
            "counts": {"roles": len(roles), "nominal_ready": flow["readiness"]["nominal_ready"],
                       "base_ready": flow["readiness"]["base_executable_ready"],
                       "review_checks_passed": sum(r["review_checks_passed"] for r in roles),
                       "source_gaps": sum(sum(r["status"] != "READY" for r in row["components"].values())
                                          + len(row["binding_mismatches"]) for row in sources["rows"]),
                       "source_groups": len(sources["groups"]), "human_questions": len(questions),
                       "material_holds": len(trust["material_holds"]) if trust else None},
            "roles": roles, "source_inventory": sources, "questions": questions,
            "scenario_sources": sorted(r["source_id"] for r in document["flow"]["discovery"]["sources"]),
            "actions": flow["actions"], "holds": flow["holds"], "forecast": flow["forecast"],
            "assurance": {"configured": document["assurance"] is not None, "state": flow["assurance"]["state"]},
            "evidence": trust["evidence"] if trust else None,
            "trust_configured": trust is not None, "execution_authorized": False,
            "effects": {"canonical_writes": 0, "model_calls": 0, "outbound_requests": 0, "browser_actions": 0, "messages_sent": 0}}
