"""Read-only qualification of captured sources against existing Keel reviews.

This is a capture-to-review report, not proof of source truth, a rendered form,
or permission to act. Every assurance/trust record must already be supplied by
the canonical host. No observations, reviews, decisions or packets are created.
"""
from __future__ import annotations

from keel_assurance.core import evaluate as evaluate_assurance
from keel_flow.common import clock, digest, require
from keel_local.readiness import dependency_hash
from keel_sources.capture import validate_semantics
from keel_trust.report import build as trust_report
from keel_workbench.model import project, validate


SCHEMA = "keel.live.capture_review_proof.v1"
SUPPORTED_ACTION = "PREPARE"


def _record(sources, component):
    source = (sources or {}).get("sources", {}).get(component)
    value = source.get("record") if isinstance(source, dict) else None
    return value if isinstance(value, dict) else {}


def build_proof(document, *, workspace_id, synthetic, action, attachment_root=None, now):
    """Recheck one supplied snapshot using existing reducers and actual files.

    ``document`` is a Workbench snapshot with normalized ``revision_sources``.
    An operational/synthetic label is a trusted host assertion, not independently
    authenticated origin. Unknown or inconsistent contracts raise ValueError;
    missing/stale evidence remains a named blocker. Each invocation rechecks the
    original timestamps and dependencies; a previous report is never authority.
    """
    now = clock(now)
    require(type(synthetic) is bool, "proof requires explicit synthetic flag")
    require(action == SUPPORTED_ACTION, "proof supports PREPARE only")
    checked = validate(document, workspace_id=workspace_id, synthetic=synthetic)
    normalized = checked["revision_sources"]
    if normalized is not None:
        for source_row in normalized["roles"]:
            require(source_row.get("action") == action, "proof source action mismatch")
    view = project(checked, attachment_root=attachment_root, now=now)
    assurance = evaluate_assurance(checked["assurance"], now=now) if checked["assurance"] is not None else None
    trust = trust_report(checked["trust"], now=now) if checked["trust"] is not None else None
    arows = {row["role_id"]: row for row in assurance["rows"]} if assurance else {}
    trows = {row["role_id"]: row for row in trust["flow"]["readiness"]["rows"]} if trust else {}
    source_rows = {row["role_id"]: row for row in normalized["roles"]} if normalized else {}
    inventory = {row["role_id"]: row for row in view["source_inventory"]["rows"]}
    leads = {row["role_id"]: row for row in checked["flow"]["leads"]}
    active = checked["flow"]["active"] is True
    roles = []
    for projected in view["roles"]:
        rid = projected["role_id"]
        lead, sources = leads[rid], source_rows.get(rid)
        scope = {"workspace_id": workspace_id, "role_id": rid,
                 "application_id": lead["identity"], "action": action}
        semantics = validate_semantics(sources["sources"] if sources else {}, scope=scope,
                                      attachment_root=attachment_root, now=now)
        observed = inventory[rid]
        reasons = list(projected["reasons"])
        reasons.extend("source_semantics:" + issue["component"] + ":" + issue["code"]
                       for issue in semantics["issues"])
        # Existing source revisions bind scope and material. Also bind the actual
        # canonical posting and route, which cannot be proved by role ID alone.
        target = _record(sources, "target")
        route = _record(sources, "route")
        posting_bound = target.get("canonical_posting_url") == lead["posting_url"]
        route_bound = route.get("transport") == lead["route"] and lead["route"] in {"browser", "api"}
        if sources and not posting_bound:
            reasons.append("source_posting_binding_mismatch")
        if sources and not route_bound:
            reasons.append("source_route_binding_mismatch")
        try:
            hash_bound = lead["packet_dependency_hash"] == dependency_hash(lead["dependencies"])
        except (ValueError, TypeError, KeyError):
            hash_bound = False
        if not hash_bound:
            reasons.append("packet_dependency_hash_mismatch")
        packet_bound = bool(observed["packet_bound"] and hash_bound and posting_bound and route_bound)
        source_ready = bool(observed["inputs_complete"] and semantics["ready_for_approval"])
        arow, trow = arows.get(rid), trows.get(rid)
        assurance_passed = bool(arow and arow["checks_passed"]
            and assurance["state"] != "UNVERIFIED"
            and view["assurance"]["state"] != "HOST_SCOPE_CHECK_REQUIRED")
        trust_passed = bool(trow and trow["executable"]
            and trust["flow"]["readiness"]["current_estimate_verified"])
        if not active:
            reasons.append("pipeline_inactive")
        marked_synthetic = False
        for component in observed["components"]:
            metadata = _record(sources, component).get("metadata")
            if isinstance(metadata, dict) and (metadata.get("synthetic") is True
                                               or metadata.get("not_live_evidence") is True):
                marked_synthetic = True
        if not synthetic and marked_synthetic:
            reasons.append("synthetic_source_in_operational_snapshot")
        qualified = bool(active and view["current"] and source_ready and packet_bound
                         and assurance_passed and trust_passed and projected["review_checks_passed"]
                         and (synthetic or not marked_synthetic))
        roles.append({"scope": scope,
            "state": "CAPTURE_TO_REVIEW_CHECKS_PASSED" if qualified else "BLOCKED",
            "source_records_complete": observed["inputs_complete"],
            "source_semantics_reviewable": semantics["ready_for_approval"],
            "source_inputs_ready": source_ready,
            "source_scope_matched": sources is not None,
            "packet_dependencies_bound": packet_bound,
            "canonical_posting_bound": posting_bound,
            "canonical_route_bound": route_bound,
            "base_checks_passed": projected["base_checks_passed"],
            "assurance_checks_passed": assurance_passed,
            "trust_checks_passed": trust_passed,
            "capture_review_checks_passed": qualified,
            "components": observed["components"],
            "semantic_issues": semantics["issues"],
            "reasons": sorted(set(reasons)),
            "source_authenticity_verified": False,
            "factual_support_verified": False,
            "canonical_packet_content_verified": False,
            "rendered_preparation": "NOT_RUN", "form_readback": "NOT_RUN",
            "execution_authorized": False})
    counts = {"roles": len(roles)}
    for key in ("source_records_complete", "source_semantics_reviewable", "source_inputs_ready",
                "packet_dependencies_bound", "base_checks_passed", "assurance_checks_passed",
                "trust_checks_passed", "capture_review_checks_passed"):
        counts[key] = sum(row[key] for row in roles)
    qualified_count = counts["capture_review_checks_passed"]
    counts.update(synthetic_capture_review_checks_passed=qualified_count if synthetic else 0,
                  declared_operational_capture_review_checks_passed=0 if synthetic else qualified_count,
                  verified_rendered_preparations=0)
    operational = counts["declared_operational_capture_review_checks_passed"]
    return {"schema": SCHEMA, "schema_version": 1, "workspace_id": workspace_id,
            "synthetic": synthetic, "action": action, "evaluated_at": now.isoformat(),
            "snapshot_sha256": digest(checked), "flow_export_sha256": digest(checked["flow"]),
            "source_snapshot_sha256": digest(normalized) if normalized is not None else None,
            "current": view["current"], "pipeline_active": active,
            "state": "CAPTURE_TO_REVIEW_CHECKS_PASSED" if qualified_count else "BLOCKED",
            "counts": counts, "roles": roles,
            "milestones": [{"target_roles": target, "qualified_declared_operational_roles": operational,
                "status": "TARGET_MET_IN_SUPPLIED_SNAPSHOT" if operational >= target else "NOT_MET",
                "measurement": "capture_to_review_checks_only", "execution_authorized": False}
                for target in (1, 10)],
            "source_authenticity_verified": False, "operator_authenticity_verified": False,
            "factual_support_verified": False, "canonical_packet_content_verified": False,
            "runtime_checks": {"local_model": "NOT_RUN", "rendered_browser": "NOT_RUN",
                               "rendered_preparation": "NOT_RUN", "form_readback": "NOT_RUN"},
            "effects": {"canonical_writes": 0, "source_writes": 0, "model_calls": 0,
                        "outbound_requests": 0, "browser_actions": 0, "messages_sent": 0},
            "execution_authorized": False,
            "limitations": [
                "Host-supplied operational labels and record provenance are not authenticated by this report.",
                "Existing assurance and trust reducers validate supplied evidence; no evidence is generated.",
                "Packet dependency binding does not verify final packet content or rendered form values.",
                "Milestones count current roles once in this supplied snapshot, not historical runs or deployments.",
                "Real model inference, rendered browser preparation and final readback are not exercised."]}
