"""Compose real source records into Keel's existing seven-revision contract.

Only trusted host code supplies records. No model output is treated as a source,
no authority is inferred, and no canonical pipeline file is written here.
"""
from copy import deepcopy
from datetime import datetime, timezone

from keel_agent.revisions import COMPONENTS, export_revisions
from keel_flow.board import build as flow_board
from keel_flow.common import digest, require
from .capture import validate_semantics


def _flow(flow, now):
    report = flow_board(flow, now=now)
    require(flow.get("complete") is True and flow.get("attempt_history_complete") is True,
            "complete flow and attempt history required")
    observed = datetime.fromisoformat(flow["observed_at"].replace("Z", "+00:00"))
    require(observed.utcoffset() is not None and 0 <= (now-observed).total_seconds() <= 90,
            "fresh canonical flow export required")
    return report


def register_flow_scopes(store, flow, *, action):
    """Register observed canonical identities; no source descriptors are created."""
    now = store.clock()
    _flow(flow, now)
    count = 0
    for row in flow["leads"]:
        store.register_scope({"workspace_id": store.workspace_id, "role_id": row["role_id"],
                              "application_id": row["identity"], "action": action})
        count += 1
    return {"registered_scope_count": count, "flow_export_sha256": digest(flow),
            "source_records_created": 0, "approval_requests_created": 0, "execution_authorized": False}


def build_export(store, *, scopes=None, flow=None):
    """One consistent source snapshot, revisions, semantics and joinable columns.

    A fresh flow is optional for isolated diagnostics, mandatory for emitting
    columns associated with a canonical flow. A supplied flow is joined by exact
    role AND application identity, never by a similar URL or employer name.
    """
    snapshot = store.export_snapshot(scopes=scopes)
    now = store.clock()
    if flow is not None:
        _flow(flow, now)
    revisions = export_revisions(snapshot, attachment_root=store.attachment_root, now=now)
    identities = {(row["role_id"], row["identity"]) for row in flow["leads"]} if flow is not None else None
    rows = []
    coverage = {component: 0 for component in COMPONENTS}
    for source_row, revision_row in zip(snapshot["roles"], revisions["roles"]):
        scope = revision_row["scope"]
        for component, descriptor in source_row["sources"].items():
            if component in coverage and isinstance(descriptor, dict) and "record" in descriptor:
                coverage[component] += 1
        semantics = validate_semantics(source_row["sources"], scope=scope,
                                      attachment_root=store.attachment_root, now=now)
        joined = identities is not None and (scope["role_id"], scope["application_id"]) in identities
        columns = {component+"_revision": revision_row["revisions"].get(component) for component in COMPONENTS}
        # Do not emit valid-looking revision columns for a mismatched current role.
        if identities is not None and not joined:
            columns = {key: None for key in columns}
        rows.append({"scope": scope, "columns": columns,
                     "source_refs": {key: value.get("source_ref") for key, value in source_row["sources"].items()
                                     if isinstance(value, dict)},
                     "semantic_validation": semantics,
                     "current_flow_identity_matched": joined,
                     "producer_inputs_complete": bool(revision_row["envelope_inputs_ready"]
                         and semantics["ready_for_approval"] and (identities is None or joined)),
                     "execution_authorized": False})
    return {"schema": "keel.source_producer_export.v1", "snapshot": snapshot,
            "revision_report": revisions, "rows": rows,
            "source_record_coverage": coverage, "scope_count": len(rows),
            "producer_inputs_complete_count": sum(row["producer_inputs_complete"] for row in rows),
            "flow_export_sha256": digest(flow) if flow is not None else None,
            "canonical_writes": 0, "source_authenticity_verified": False,
            "execution_authorized": False}


def status_summary(export):
    """Value-free operational overview; full export is private host data."""
    return {key: deepcopy(export[key]) for key in ("scope_count", "source_record_coverage",
        "producer_inputs_complete_count", "flow_export_sha256", "canonical_writes",
        "source_authenticity_verified", "execution_authorized")} | {
        "diagnostic_categories": export["revision_report"]["root_causes"],
        "semantic_blocked_scopes": sum(not row["semantic_validation"]["ready_for_approval"] for row in export["rows"])}
