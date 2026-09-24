"""Trusted host integration for actual source events and Workbench snapshots.

This in-process API is not an authenticated ingestion endpoint. The embedding
host must authenticate producers before selecting their configured identity.
An allowlisted name, hash, profile assertion or target flag is not proof of
origin, factual accuracy, human consent or permission to execute anything.

Only the source store is mutated. Canonical flow, assurance and trust inputs
are retained byte-for-byte as JSON values. An export never fixes dependencies,
refreshes observations, creates approvals or upgrades an existing hold.
"""
from __future__ import annotations

from copy import deepcopy
import json

from keel_agent.revisions import PREAPPROVAL_COMPONENTS, _canonical, _token
from keel_flow.common import digest
from keel_sources.capture import ingest_attachments
from keel_sources.service import _flow, build_export, register_flow_scopes
from keel_sources.store import SourceStore
from keel_trust.common import clone
from keel_workbench.model import adapt_body, validate


EVENT_SCHEMA = "keel.live_source_event.v1"
_EVENT_FIELDS = {"schema", "producer_id", "scope", "component", "descriptor",
                 "expected_generation", "flow_export_sha256"}


class ConnectorError(ValueError):
    """Fixed diagnostics that do not include applicant values."""


def _require(condition, code):
    if not condition:
        raise ConnectorError(code)


def _binding_issues(component, descriptor, lead):
    """Intersect actual canonical bindings where the host exports those fields."""
    record = descriptor.get("record") if type(descriptor) is dict else None
    if type(record) is not dict:
        return []  # The existing source validator diagnoses missing/invalid data.
    pairs = []
    if component == "target":
        pairs.append((record.get("canonical_posting_url"), lead.get("posting_url"),
                      "target_canonical_posting_mismatch"))
        for field in ("application_url", "destination"):
            if field in lead:
                pairs.append((record.get("application_url"), lead[field], "target_canonical_destination_mismatch"))
    if component == "route":
        pairs.append((record.get("transport"), lead.get("route"), "route_canonical_transport_mismatch"))
        if "account_id" in lead:
            pairs.append((record.get("account_id"), lead["account_id"], "route_canonical_account_mismatch"))
        for field in ("application_url", "destination"):
            if field in lead:
                pairs.append((record.get("destination"), lead[field], "route_canonical_destination_mismatch"))
    return [{"component": component, "code": code} for actual, expected, code in pairs if actual != expected]


class HostSourceConnector:
    """Bridge a host's normalized producer events into the existing source store.

    ``producer_components`` is trusted configuration mapping each producer ID to
    its permitted source families. It must never be derived from event content.
    ``action`` binds this connector to one exact action (for example PREPARE).
    ``attachment_source_root`` is a trusted host path, never an event field.

    Events use generation compare-and-swap and existing source-version rules.
    Retries are rejected as conflicts/replays, not silently treated as successful
    delivery. Refreshes need actual new observations. There is no exactly-once
    claim. A failed attachment capture can leave verified unreferenced content
    objects; it cannot publish a partial source record or grant any authority.
    """

    def __init__(self, store, *, producer_components, action, attachment_source_root=None):
        _require(isinstance(store, SourceStore), "existing_source_store_required")
        _require(action == "PREPARE", "connector_action_must_be_prepare")
        _require(type(producer_components) is dict and len(producer_components) <= 100,
                 "producer_configuration_invalid")
        configured = {}
        for producer, components in producer_components.items():
            _token(producer)
            _require(type(components) in (tuple, list, set, frozenset) and bool(components)
                     and all(type(c) is str and c in PREAPPROVAL_COMPONENTS for c in components)
                     and len(set(components)) == len(components), "producer_components_invalid")
            configured[producer] = frozenset(components)
        self.store = store
        self.producer_components = configured
        self.action = action
        self.attachment_source_root = attachment_source_root

    def _scope(self, scope, flow):
        _require(type(scope) is dict and set(scope) == {
            "workspace_id", "role_id", "application_id", "action"}, "event_scope_invalid")
        for value in scope.values():
            _token(value)
        _require(scope["workspace_id"] == self.store.workspace_id, "event_workspace_mismatch")
        _require(scope["action"] == self.action, "event_action_mismatch")
        matches = [row for row in flow["leads"] if row["role_id"] == scope["role_id"]
                   and row["identity"] == scope["application_id"]]
        _require(len(matches) == 1, "event_current_identity_mismatch")
        return matches[0]

    def register_flow(self, flow):
        """Explicitly register fresh canonical identities, creating no evidence."""
        return register_flow_scopes(self.store, clone(flow), action=self.action)

    def capture_event(self, event, *, flow, attachment_source_root=None):
        """Capture one event supplied by a trusted in-process producer boundary.

        The optional root override exists for trusted embedding code only. No
        event can set a path outside its host-selected root. The effective root
        should normally be fixed when constructing this connector.
        """
        # The existing 2 MiB/100000-node canonicalizer rejects cyclic, oversized,
        # non-JSON and non-finite input before any database or attachment writes.
        event = json.loads(_canonical(event))
        _require(type(event) is dict and set(event) == _EVENT_FIELDS
                 and event["schema"] == EVENT_SCHEMA, "event_schema_invalid")
        producer, component = event["producer_id"], event["component"]
        _token(producer)
        _require(type(component) is str and component in PREAPPROVAL_COMPONENTS,
                 "event_component_not_capturable")
        _require(producer in self.producer_components
                 and component in self.producer_components[producer], "producer_component_not_allowed")
        _require(type(event["expected_generation"]) is int and event["expected_generation"] >= 0,
                 "event_generation_invalid")
        flow = clone(flow)
        _require(event["flow_export_sha256"] == digest(flow), "event_flow_digest_mismatch")
        # Registering scopes is deliberately separate. Invalid/replayed events do
        # not create scopes, evidence or pending human decisions as a side effect.
        with self.store.transaction() as connection:
            _flow(flow, connection.keel_now)
            lead = self._scope(event["scope"], flow)
            current = self.store.current_generation(connection, event["scope"], component)
            _require(current == event["expected_generation"], "generation_conflict")
            descriptor = event["descriptor"]
            issues = _binding_issues(component, descriptor, lead)
            if issues:
                raise ConnectorError(issues[0]["code"])
            if component == "attachments":
                source_root = (self.attachment_source_root if attachment_source_root is None
                               else attachment_source_root)
                record = descriptor.get("record") if type(descriptor) is dict else None
                files = record.get("files") if type(record) is dict else None
                _require(not files or source_root is not None, "host_attachment_source_root_required")
                descriptor = ingest_attachments(descriptor, source_root=source_root,
                    attachment_root=self.store.attachment_root, scope=event["scope"],
                    now=connection.keel_now)
            # A large file capture may outlive the 90-second canonical freshness
            # window. Re-evaluate before any record/audit history is committed.
            _flow(flow, self.store.clock())
            written = self.store.write_source(connection, event["scope"], component,
                descriptor, event["expected_generation"])
            self.store.audit_event(connection, "HOST_SOURCE_EVENT_CAPTURED", event["scope"],
                component, written["generation"], {"actor_id": producer,
                "source_ref": descriptor["source_ref"], "source_version": descriptor["source_version"],
                "descriptor_sha256": digest(descriptor), "revision": written["revision"]})
        return {"schema": "keel.live_source_capture.v1", "producer_id": producer,
                "component": component, "scope": deepcopy(event["scope"]), **written,
                "flow_export_sha256": event["flow_export_sha256"],
                "producer_identity_authenticated": False, "source_authenticity_verified": False,
                "factual_support_verified": False, "approval_requests_created": 0,
                "canonical_writes": 0, "execution_authorized": False}

    def export_workbench(self, *, flow, assurance, trust, synthetic=False, labels=None, scopes=None,
                         allow_stale_read=False):
        """Return ``source_export`` and ``workbench_snapshot`` from current inputs.

        Existing assurance/trust are required arguments; pass None to represent
        their genuine absence. No valid-looking replacement is synthesized. Exact
        digest bindings are checked by Workbench's validator. Dependencies in the
        flow remain unchanged, so unbound source revisions remain visible.

        Default scope selection includes only this connector's action and exact
        current role/application identities. Explicit scopes must all match.
        ``allow_stale_read`` permits diagnostic rendering of an unchanged stale
        flow. That export has no joined revision columns and no complete inputs;
        it never extends the validity of the original observations.
        Semantic findings remain in source_export and must be intersected with
        Workbench's legacy projection by callers; the snapshot alone is not an
        approval or a v0.8 semantic-validation result.
        """
        _require(type(synthetic) is bool, "synthetic_flag_invalid")
        _require(type(allow_stale_read) is bool, "stale_read_flag_invalid")
        flow = clone(flow)
        def current_flow():
            try:
                _flow(flow, self.store.clock())
            except ValueError as error:
                if allow_stale_read and str(error) == "fresh canonical flow export required":
                    return False
                raise
            return True
        current = current_flow()
        excluded = 0
        if scopes is None:
            existing = self.store.export_snapshot()
            scopes = []
            for row in existing["roles"]:
                scope = {"workspace_id": self.store.workspace_id, **{
                    key: row[key] for key in ("role_id", "application_id", "action")}}
                if scope["action"] != self.action or not any(
                    lead["role_id"] == scope["role_id"] and lead["identity"] == scope["application_id"]
                    for lead in flow["leads"]):
                    excluded += 1
                    continue
                scopes.append(scope)
        _require(type(scopes) in (tuple, list), "export_scopes_invalid")
        scopes = deepcopy(list(scopes))
        for scope in scopes:
            self._scope(scope, flow)
        # Joining is read-only. If freshness expires during this operation, only
        # an explicitly permitted diagnostic read may fall back to an unjoined
        # snapshot; input records and original timestamps are always retained.
        try:
            exported = build_export(self.store, scopes=scopes, flow=flow if current else None)
        except ValueError as error:
            if not (allow_stale_read and str(error) == "fresh canonical flow export required"):
                raise
            current = False
            exported = build_export(self.store, scopes=scopes)
        # Existing records may predate this connector or a refreshed canonical
        # export. Recheck canonical bindings on every view, not just at ingest.
        sources = {(r["role_id"], r["application_id"], r["action"]): r["sources"]
                   for r in exported["snapshot"]["roles"]}
        for row in exported["rows"]:
            scope = row["scope"]
            lead = self._scope(scope, flow)
            records = sources[(scope["role_id"], scope["application_id"], scope["action"])]
            issues = [issue for component in ("target", "route")
                      for issue in _binding_issues(component, records.get(component), lead)]
            row["canonical_binding_verified"] = not issues and all(
                type(records.get(component)) is dict and type(records[component].get("record")) is dict
                for component in ("target", "route"))
            if issues:
                row["semantic_validation"]["issues"].extend(issues)
                row["semantic_validation"]["ready_for_approval"] = False
                row["producer_inputs_complete"] = False
        exported["producer_inputs_complete_count"] = sum(r["producer_inputs_complete"] for r in exported["rows"])
        document = adapt_body({"flow": flow, "assurance": assurance, "trust": trust},
            self.store.workspace_id, synthetic=synthetic, labels=labels,
            revision_sources=exported["snapshot"])
        document = validate(document, workspace_id=self.store.workspace_id, synthetic=synthetic)
        current = current_flow() and current
        if not current:
            for row in exported["rows"]:
                row["columns"] = dict.fromkeys(row["columns"])
                row["current_flow_identity_matched"] = False
                row["canonical_binding_verified"] = False
                row["producer_inputs_complete"] = False
            exported["producer_inputs_complete_count"] = 0
        exported["flow_export_sha256"] = digest(flow)
        exported["current_flow_verified"] = current
        return {"schema": "keel.live_workbench_export.v1", "source_export": exported,
                "workbench_snapshot": document, "excluded_noncurrent_scope_count": excluded,
                "flow_export_sha256": digest(flow), "current_flow_verified": current, "canonical_writes": 0,
                "source_authenticity_verified": False, "factual_support_verified": False,
                "execution_authorized": False}


def export_workbench(store, body, *, action="PREPARE", synthetic=False, labels=None,
                     scopes=None, allow_stale_read=False):
    """Read-only convenience bridge; no producers are granted capture access."""
    _require(type(body) is dict and set(body) == {"flow", "assurance", "trust"},
             "live_body_invalid")
    connector = HostSourceConnector(store, producer_components={}, action=action)
    return connector.export_workbench(**body, synthetic=synthetic, labels=labels,
        scopes=scopes, allow_stale_read=allow_stale_read)
