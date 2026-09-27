"""Execute one qualified local preparation route through the shared governor.

This boundary can prepare an evidence assessment. It cannot replace mandatory
blind reviews or authorize any external action. Model quality, source/policy
identity and the low-risk classification remain operator attestations. A model
configuration digest binds configured request limits/endpoint/model name; it is
not an attestation of server model weights.
"""
from collections.abc import Mapping

from keel_agent.models import (
    ModelError, ReviewerConfig, _assessment_from_response, _payload,
    loopback_transport, reviewer_config_digest,
)
from keel_loki.routing import _subject
from keel_workflow.reviews import subject_digest

from .ledger import BudgetExceeded, LedgerError, ResourceLedger
from .policy import QualityPolicy, select_route
from .transport import GovernedTransport


def run_qualified_preparation(subject, routes, configs, *, context_key,
                              budget_units, current_bindings, ledger, scope_id,
                              policy=QualityPolicy(), transport=None, risk="low"):
    """Select then dispatch once; failures and unknown usage hold without retry.

    ``budget_units`` filters operator-defined route cost ceilings. It is not a
    conversion into tokens or credits. The required shared ``ledger`` enforces
    actual call/input/output/time reservations at the dispatch boundary.
    ``current_bindings`` must come from the trusted host, not task/model text.
    All eligible configurations are checked before selecting/dispatching work.
    An injected transport is trusted host code and is identified in the report.
    No held-out labels, contextual bandit training data, cached approvals or
    context-assembly authority are imported into the subject.
    """
    if risk != "low":
        raise ValueError("qualified preparation accepts only low-risk work")
    if not isinstance(ledger, ResourceLedger):
        raise ValueError("a shared ResourceLedger is required")
    if not isinstance(configs, Mapping):
        raise ValueError("route configurations must be a mapping")
    if transport is not None and (not callable(transport) or isinstance(transport, GovernedTransport)):
        raise ValueError("an unwrapped trusted local transport is required")
    ledger.snapshot(scope_id)
    clean_subject = _subject(subject)["cases"][0]["subject"]
    subject_hash = subject_digest(clean_subject)
    proposal = select_route(routes, context_key=context_key, risk="low",
                            budget_units=budget_units, current_bindings=current_bindings,
                            policy=policy)
    report = {
        "schema": "keel.efficiency.preparation.v1", "status": "HOLD", "reason": None,
        "purpose": "preparation", "risk": "low", "subject_sha256": subject_hash,
        "route_proposal": proposal, "selected_route_id": proposal["selected_route_id"],
        "assessment": None, "response_id": None, "resource_receipt": None,
        "usage": None, "model_calls_attempted": 0,
        "transport_mode": "injected" if transport is not None else "local_loopback",
        "resource_budget_enforced": True, "execution_authorized": False,
        "replaces_mandatory_review": False, "source_policy_bindings_authenticated": False,
        "model_weights_attested": False, "model_truth_verified": False,
        "paid_execution_enabled": False,
    }
    if not clean_subject["evidence"]:
        return dict(report, reason="source_evidence_missing")
    if proposal["status"] != "PROPOSAL":
        return dict(report, reason="no_qualified_route")
    by_id = {route.route_id: route for route in routes}
    for row in proposal["routes"]:
        if not row["qualified"]:
            continue
        config = configs.get(row["route_id"])
        if not isinstance(config, ReviewerConfig):
            return dict(report, reason="eligible_route_configuration_missing")
        if reviewer_config_digest(config) != by_id[row["route_id"]].model_hash:
            return dict(report, reason="eligible_model_configuration_mismatch")
    chosen = by_id[proposal["selected_route_id"]]
    config = configs[chosen.route_id]
    governed = GovernedTransport(ledger, scope_id, transport or loopback_transport,
                                binding={"purpose": "preparation", "route_id": chosen.route_id,
                                         "subject_sha256": subject_hash,
                                         "model_config_sha256": chosen.model_hash,
                                         "source_sha256": chosen.source_hash,
                                         "policy_sha256": chosen.policy_hash})
    try:
        payload = _payload(config, "A", {"subject": clean_subject, "subject_sha256": subject_hash})
        response = governed(config, payload, config.timeout_seconds)
        assessment, response_id = _assessment_from_response(config, response, clean_subject["required_claim_ids"])
        report.update(assessment=assessment, response_id=response_id)
        if governed.last_receipt["state"] != "SETTLED":
            report["reason"] = "usage_unresolved"
        elif governed.last_receipt["overages"]:
            report["reason"] = "resource_reservation_overage"
        elif assessment["verdict"] == "ABSTAIN":
            report["reason"] = "model_abstained"
        else:
            report.update(status="PREPARED", reason="validated_preparation_assessment")
    except BudgetExceeded:
        report["reason"] = "resource_budget_exceeded"
    except LedgerError:
        report["reason"] = "resource_ledger_error"
    except ModelError as exc:
        report["reason"] = str(exc)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        report["reason"] = "model_adapter_error"
    finally:
        report["resource_receipt"] = governed.last_receipt
        report["usage"] = governed.last_usage
        if governed.last_receipt is not None and governed.last_receipt["state"] in {"DISPATCHED", "UNKNOWN", "SETTLED"}:
            report["model_calls_attempted"] = 1
    return report
