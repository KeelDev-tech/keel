"""Transport-neutral, host-authorized read reports. No server or persistence.

The host supplies one complete canonical workspace and live authorization. Client
arguments never select identity, workspace, source paths or evaluation time.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

from keel_agent.revisions import COMPONENTS, SourceError, _metadata, _failure
from keel_flow.common import canonical, clock, digest, strict_json
from keel_live.review import Principal, ReviewError, authorize
from keel_live.surface import qualified_view
from keel_workbench.model import validate

SCHEMA = "keel.connector.readiness.v1"
SUPPORTED_ROLES = 20
MAX_REPORT_ROLES = 30
MAX_REQUEST_BYTES = 256
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_NODES = 100000
MAX_DEPTH = 40
MAX_OUTPUT_BYTES = 256 * 1024
LATENCY_BUDGET_SECONDS = 2.0
OPERATIONS = frozenset({"list_application_blockers", "get_application_readiness"})
HEX = re.compile(r"[0-9a-f]{64}\Z")
STATUSES = frozenset({"READY", "MISSING", "INVALID", "STALE", "REVOKED", "REJECTED", "DEPENDENCY_MISMATCH"})
# Reducer strings are data, even when they look like codes. Unlisted values never
# cross this boundary; adding a new reducer code requires an explicit review here.
REASONS = frozenset("""
observation_stale queue_not_ready posting_url_missing_or_invalid
canonical_identity_missing legacy_history_unreconciled attempt_hold
attempt_state_invalid launch_lock_held_or_unknown policy_not_passed
posting_unverified answers_unresolved approval_unverified packet_missing
provider_route_unverified provider_contract_unvalidated packet_dependencies_changed
approval_expired input_contract_invalid duplicate_identity fit_below_75
action_band_not_apply canonical_hold_present unresolved_attempt_history
attempt_history_incomplete assurance_not_passed assurance_unverified
missing_assurance assurance_host_scope_check_required trust_not_configured
source_records_incomplete source_revision_binding_mismatch refresh_current_export
source_posting_binding_mismatch source_route_binding_mismatch
packet_dependency_hash_mismatch pipeline_inactive synthetic_source_in_operational_snapshot
source_not_exported source_descriptor_missing source_timestamps_missing
human_decision_required source_record_missing source_expired source_revoked
approval_revoked approval_rejected approval_dependency_mismatch approval_scope_mismatch
record_missing_or_invalid source_metadata_invalid source_observation_in_future
source_validity_invalid source_not_yet_observed approval_dependencies_unavailable
approval_not_yet_observed approval_revocation_missing approval_decision_missing
approval_validity_invalid absence_descriptor_invalid identifier_invalid
attachment_manifest_invalid attachment_unavailable_or_symlink attachment_changed_during_read
attachment_path_invalid attachment_not_regular attachment_too_large attachments_total_too_large
attachment_digest_mismatch attachment_size_mismatch attachment_root_unavailable_or_symlink
policy_active_holds human_approval_required_by_capture_contract policy_action_not_allowed
policy_account_not_allowed policy_destination_not_allowed target_not_verified
route_target_destination_mismatch route_target_revision_mismatch
answer_unknown_field answer_required_missing answer_checkbox_type_invalid
answer_required_checkbox_unchecked answer_text_type_invalid answer_required_empty
answer_select_option_invalid answer_human_origin_required generated_draft_not_factual_evidence
required_attachment_missing source_snapshot_invalid packet_source_revision_mismatch
""".split())


class _Limit(ValueError):
    pass


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _error(code):
    return _encode({"schema": SCHEMA, "ok": False, "error": {"code": code},
                    "execution_authorized": False})


def _freeze(value):
    """Bound traversal before allocating a canonical copy of the host export."""
    stack, nodes, characters = [(value, 0)], 0, 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise _Limit()
        if type(item) in (dict, list):
            if len(item) + len(stack) + nodes > MAX_NODES:
                raise _Limit()
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise ValueError()
                characters += sum(len(key) for key in item)
                children = item.values()
            else:
                children = item
            stack.extend((child, depth + 1) for child in children)
        elif type(item) is str:
            characters += len(item)
        elif type(item) is int:
            if item.bit_length() > 256:
                raise _Limit()
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError()
        elif item is not None and type(item) is not bool:
            raise ValueError()
        if characters > MAX_SNAPSHOT_BYTES:
            raise _Limit()
    raw = canonical(value)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise _Limit()
    return strict_json(raw)


def scope_reference(scope):
    """Domain-separated scoped pseudonym, not anonymity or an access credential."""
    return digest({"domain": "keel.connector.scope.v1", "scope": scope})


def _reason(value):
    if value in REASONS:
        return value
    if type(value) is str and value.startswith("source_semantics:"):
        parts = value.split(":")
        if len(parts) == 3 and parts[1] in COMPONENTS and parts[2] in REASONS:
            return value
        return "source_semantics_unclassified"
    if type(value) is str and value.startswith("assurance:"):
        return "assurance_review_required"
    if type(value) is str and value.startswith("material:"):
        return "material_review_required"
    return "unclassified_blocker"


def _stamp(value):
    if type(value) is not str or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _hex(value):
    return value if type(value) is str and HEX.fullmatch(value) else None


class ReadinessAdapter:
    """Trusted in-process binding. All callbacks are host code, never JSON.

    principal_provider must resolve the current authenticated grant on each call
    and raise/return a denied grant after revocation. snapshot_provider must return
    the complete workspace graph, never a client-selected subset. The host must
    serialize revocation/account changes with its response delivery. No callback
    is installed by this package, and their side effects remain the host's duty.
    """
    def __init__(self, *, workspace_id, synthetic, principal_provider,
                 snapshot_provider, attachment_root, host_clock):
        self.workspace_id = workspace_id
        self.synthetic = synthetic
        self.principal_provider = principal_provider
        self.snapshot_provider = snapshot_provider
        self.attachment_root = attachment_root
        self.clock = host_clock

    def _principal(self):
        principal = self.principal_provider()
        # Required even for an empty workspace: authorization cannot be vacuous.
        if (type(principal) is not Principal or principal.workspace_id != self.workspace_id
                or "review:read" not in principal.allowed_actions):
            raise ReviewError("review_access_denied")
        return principal

    def call(self, operation, arguments=b"{}"):
        """Return bounded UTF-8 JSON; errors never contain input or exception text."""
        try:
            if type(operation) is not str or len(operation) > 64 or operation not in OPERATIONS:
                return _error("INVALID_REQUEST")
            if type(arguments) is not bytes or len(arguments) > MAX_REQUEST_BYTES:
                return _error("INVALID_REQUEST")
            request = strict_json(arguments)
            expected = set() if operation == "list_application_blockers" else {"scope_ref"}
            if type(request) is not dict or set(request) != expected:
                return _error("INVALID_REQUEST")
            if expected and _hex(request["scope_ref"]) is None:
                return _error("INVALID_REQUEST")
        except Exception:
            return _error("INVALID_REQUEST")
        try:
            principal = self._principal()
        except Exception:
            return _error("ACCESS_DENIED")
        try:
            supplied = self.snapshot_provider()
            now = clock(self.clock())
        except Exception:
            return _error("HOST_UNAVAILABLE")
        try:
            document = _freeze(supplied)
            leads = document["flow"]["leads"]
            if type(leads) is not list:
                raise ValueError()
            if len(leads) > MAX_REPORT_ROLES:
                raise _Limit()
            sources = document.get("revision_sources")
            if sources is not None and len(sources["roles"]) > MAX_REPORT_ROLES:
                raise _Limit()
            scopes = [{"workspace_id": self.workspace_id, "role_id": row["role_id"],
                       "application_id": row["identity"], "action": "PREPARE"} for row in leads]
            # Match LiveSurface: complete canonical graph authorization BEFORE
            # projection and attachment reads, including get-one requests.
            for scope in scopes:
                authorize(principal, "review:read", scope)
            document = validate(document, workspace_id=self.workspace_id, synthetic=self.synthetic)
            references = {scope["role_id"]: scope_reference(scope) for scope in scopes}
            if expected and request["scope_ref"] not in references.values():
                return _error("NOT_FOUND")
            view = qualified_view(document, attachment_root=self.attachment_root, now=now)
            report = self._report(document, view, references, now)
            if expected:
                report["applications"] = [row for row in report["applications"]
                                          if row["scope_ref"] == request["scope_ref"]]
            report["returned_roles"] = len(report["applications"])
            body = _encode(report)
            if len(body) > MAX_OUTPUT_BYTES:
                return _error("OUTPUT_LIMIT_EXCEEDED")
        except ReviewError:
            return _error("ACCESS_DENIED")
        except _Limit:
            return _error("WORKLOAD_EXCEEDED")
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError):
            return _error("INVALID_SNAPSHOT")
        except Exception:
            return _error("EVALUATION_FAILED")
        try:
            current_principal = self._principal()
            if current_principal != principal:
                return _error("ACCESS_DENIED")
            for scope in scopes:
                authorize(current_principal, "review:read", scope)
        except Exception:
            return _error("ACCESS_DENIED")
        return body

    def _report(self, document, view, references, now):
        normalized = document["revision_sources"]
        source_rows = {row["role_id"]: row["sources"] for row in normalized["roles"]} if normalized else {}
        supplied_wrapper = normalized.get("snapshot") if normalized else None
        wrapper = supplied_wrapper if type(supplied_wrapper) is dict else {}
        wrapper_status, wrapper_reason = "MISSING", "source_not_exported"
        if normalized is not None:
            try:
                # The same metadata validator used by export_revisions; do not
                # invent another freshness gate or renew supplied timestamps.
                _metadata(supplied_wrapper, now)
                wrapper_status, wrapper_reason = "READY", None
            except SourceError as error:
                diagnostic = _failure("snapshot", str(error))
                wrapper_status, wrapper_reason = diagnostic["status"], _reason(str(error))
        current = view["current"] is True and wrapper_status == "READY"
        applications = []
        for row in view["roles"]:
            components = []
            for name in COMPONENTS:
                item = row["sources"][name]
                source = source_rows.get(row["role_id"], {}).get(name)
                source = source if type(source) is dict else {}
                state = item["status"] if item["status"] in STATUSES else "UNKNOWN"
                human = (current and item.get("responsibility") == "human"
                         and item.get("reason") == "human_decision_required")
                components.append({"component": name, "status": state,
                    "reason_code": _reason(item["reason"]) if item.get("reason") else None,
                    "responsibility": "none" if state == "READY" else "human" if human else "system",
                    "revision_sha256": _hex(item.get("revision")),
                    "observed_at": _stamp(source.get("observed_at")),
                    "expires_at": _stamp(source.get("expires_at"))})
            reasons = sorted({_reason(reason) for reason in row["reasons"]})
            passed = bool(current and row["review_checks_passed"] and not reasons
                          and all(item["status"] == "READY" for item in components))
            if not passed and not reasons:
                reasons = ["unclassified_blocker"]
            if view["current"] is not True:
                step, owner = "REFRESH_COMPLETE_WORKSPACE", "system"
            elif wrapper_status != "READY":
                step, owner = "REFRESH_SOURCE_EXPORT", "system"
            elif any(item["status"] != "READY" and item["responsibility"] != "human" for item in components):
                step, owner = "REFRESH_OR_REPAIR_SOURCE_EVIDENCE", "system"
            elif any(reason not in {"source_records_incomplete", "approval_unverified", "approval_expired"}
                     for reason in reasons):
                step, owner = "REVIEW_CANONICAL_BLOCKERS", "system"
            elif any(item["responsibility"] == "human" for item in components):
                step, owner = "REVIEW_EXISTING_APPROVAL_REQUEST", "human"
            elif not passed:
                step, owner = "REVIEW_CANONICAL_BLOCKERS", "system"
            else:
                step, owner = "VERIFY_PREPARATION_AND_READBACK", "human"
            applications.append({"scope_ref": references[row["role_id"]],
                "state": "UNKNOWN" if not current else "CAPTURE_TO_REVIEW_CHECKS_PASSED" if passed else "BLOCKED",
                "capture_review_checks_passed": passed, "reason_codes": reasons,
                "sources": components, "next_step": {"code": step, "responsibility": owner},
                "execution_authorized": False})
        return {"schema": SCHEMA, "ok": True, "synthetic": document["synthetic"],
            "workspace_ref": digest({"domain": "keel.connector.workspace.v1", "workspace": self.workspace_id}),
            "evaluated_at": now.isoformat(), "observed_at": _stamp(document["flow"]["observed_at"]),
            "current": current, "canonical_current": view["current"],
            "source_snapshot": {"status": wrapper_status, "reason_code": wrapper_reason,
                                "observed_at": _stamp(wrapper.get("observed_at")),
                                "expires_at": _stamp(wrapper.get("expires_at"))},
            "pipeline_active": document["flow"]["active"],
            "freshness_basis": "AS_OF_EVALUATED_AT", "workspace_roles": len(applications),
            "pins": {"snapshot_sha256": digest(document), "flow_sha256": digest(document["flow"]),
                     "source_snapshot_sha256": digest(normalized) if normalized is not None else None,
                     "source_revision_sha256": digest(document["flow"]["source_revision"])},
            "applications": applications,
            "uncertainty": {"source_authenticity_verified": False, "operator_authenticity_verified": False,
                            "factual_support_verified": False, "canonical_packet_content_verified": False,
                            "rendered_preparation": "NOT_RUN", "form_readback": "NOT_RUN",
                            "question_impact": "NOT_ASSESSED"},
            "execution_authorized": False}
