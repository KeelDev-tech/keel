"""Regression tests for ARM 2 (2026-09-19): dependencies-contract schema mismatch.

Blackboard J-20260919-1858-feed-3021: the exporter emitted a 7-key boolean
presence map (packet/questions/materials/identity/consent/policy/capacity)
while keel_local.readiness.dependency_hash requires exactly the DEPENDENCIES
key set {policy, form, answers, attachments, target, approval, route} with
non-empty revision strings -- input_contract_invalid fired on every board
row, masking all real gate signals. A second latent trigger hid behind it:
evaluate_readiness demanded timestamp(approval_expires_at) unconditionally,
so an honestly-absent expiry on a never-approved lead also raised
input_contract_invalid.

All fixtures synthetic; nothing touches queues/telemetry/tray/ledger.
Run from ~/workspace/keel.
"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import export_flow_snapshot as exporter
from keel_local.contracts import ContractError
from keel_local.readiness import DEPENDENCIES, dependency_hash, evaluate_readiness

NOW = datetime.now(timezone.utc)

OLD_BOOLEAN_SHAPE = {"packet": False, "questions": False, "materials": False,
                     "identity": False, "consent": False, "policy": False,
                     "capacity": False}


def queue_entry(**over):
    entry = {"role_id": "TEST-ROLE-1", "company": "TestCo",
             "ats": "greenhouse",
             "application_url": "https://example.org/jobs/1",
             "fit_score": 84, "action_band": "APPLY", "status": "READY",
             "unresolved": []}
    entry.update(over)
    return entry


def test_exporter_emits_contract_dependency_keys():
    row, reason = exporter.lead_row(queue_entry(), NOW.isoformat())
    assert reason is None
    assert set(row["dependencies"]) == set(DEPENDENCIES)
    assert all(type(v) is str and v.strip()
               for v in row["dependencies"].values())
    # the sentinel marks absence; it is never a claimed revision
    assert all(v == "unobserved" for v in row["dependencies"].values())
    assert row["packet_dependency_hash"] is None


def test_emitted_shape_passes_dependency_hash():
    row, _ = exporter.lead_row(queue_entry(), NOW.isoformat())
    assert isinstance(dependency_hash(row["dependencies"]), str)


def test_old_boolean_shape_raises_contract_error():
    with pytest.raises(ContractError):
        dependency_hash(OLD_BOOLEAN_SHAPE)


def test_exporter_row_clears_input_contract_invalid():
    row, _ = exporter.lead_row(queue_entry(), NOW.isoformat())
    result = evaluate_readiness(row, now=NOW)
    assert "input_contract_invalid" not in result.reasons
    # the honest fail-closed reasons remain: no approval, no packet, and the
    # packet's dependency pin is unverifiable against the observed set
    assert "approval_unverified" in result.reasons
    assert "packet_missing" in result.reasons
    assert "packet_dependencies_changed" in result.reasons


def test_unapproved_lead_needs_no_invented_expiry():
    row, _ = exporter.lead_row(queue_entry(), NOW.isoformat())
    assert row["approval_valid"] is False
    assert row["approval_expires_at"] is None
    result = evaluate_readiness(row, now=NOW)
    assert "input_contract_invalid" not in result.reasons
    assert "approval_unverified" in result.reasons


def test_claimed_approval_with_bad_expiry_is_still_invalid_input():
    row, _ = exporter.lead_row(queue_entry(), NOW.isoformat())
    row["approval_valid"] = True
    row["approval_expires_at"] = None  # claimed approval, no evidence
    result = evaluate_readiness(row, now=NOW)
    assert "input_contract_invalid" in result.reasons


def test_claimed_approval_expiry_still_evaluated():
    row, _ = exporter.lead_row(queue_entry(), NOW.isoformat())
    row["approval_valid"] = True
    row["approval_expires_at"] = "2020-01-01T00:00:00Z"
    result = evaluate_readiness(row, now=NOW)
    assert "input_contract_invalid" not in result.reasons
    assert "approval_expired" in result.reasons
